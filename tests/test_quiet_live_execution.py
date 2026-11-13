"""Offline verification of the prepared Quiet Breakout live package."""

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from live_trading.burst_runtime import HOUR, MINUTE
from live_trading.live_model.quiet_breakout.ethusdc.strategy import EthFuturesExecutor, Strategy as EthStable
from live_trading.live_model.quiet_breakout.hypeusdt.strategy import (
    QuietAdditionExecutor, QuietFuturesExecutor, Stable, TailDependent,
)
from live_trading.live_model.quiet_breakout.uniusdc.strategy import Strategy as UniStable
from utils.engine.execution.burst_futures import HOLD_MS, BurstFuturesStore
from utils.model_registry import verify_model

from test_burst_live_execution import HypeVenue, NOW, PortfolioVenue
from test_futures_execution import Crash, FakeVenue


class EthVenue(FakeVenue):
    def read(self, path, **kwargs):
        result = super().read(path, **kwargs)
        if path == "/fapi/v1/symbolConfig":
            result[0].update(marginType="ISOLATED", isAutoAddMargin=False)
        return result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0] / 1000)
    stores = []

    def build(model, allocation=100, venue=None):
        cfg = model.execution_config(allocation, True)
        store = BurstFuturesStore(tmp_path / model.DATABASE, cfg)
        stores.append(store)
        is_eth = model is EthStable
        venue = venue or (EthVenue() if is_eth else HypeVenue())
        executor_type = EthFuturesExecutor if is_eth else QuietFuturesExecutor
        return model(store, executor_type(store, venue, cfg)), venue, clock

    yield build
    for store in stores:
        store.close()


def intent(model, now, **changes):
    return dict(dict(
        sample_id=model.ALLOCATION_ID, direction="long", initial_stop=990,
        eligible_timestamp_ms=now, expires_timestamp_ms=now + 59_999,
        confirmation_mechanisms=["initiative_candidate"],
    ), **changes)


def test_combined_source_runs_and_journals(tmp_path):
    from live_trading.live_model.quiet_breakout.hypeusdt import strategy
    package = verify_model(strategy.__file__)
    asset = Path(strategy.__file__).resolve().parent
    root = asset.parents[3]
    assert set(package["model_files"]) == {"stable/model_config.json", "tail_dependent/model_config.json"}
    for model in (Stable, TailDependent):
        source = json.loads((asset / model.VARIANT / "model_config.json").read_text(encoding="utf-8"))
        receipt = root / source["source_run"]
        assert len(source["source_run_sha256"]) == 64
        if not receipt.is_file():
            continue  # Source-only release checkouts exclude research run artifacts.
        raw = receipt.read_bytes()
        assert sha256(raw).hexdigest() == source["source_run_sha256"]
        run = json.loads(raw)
        assert run["status"] == "complete"
        assert source["model_config"] == {
            k: v for k, v in run["requested_model_config"].items() if k not in ("start_date", "end_date")
        }
    path = tmp_path / Stable.DATABASE
    BurstFuturesStore(path, Stable.execution_config(100, False)).close()
    with pytest.raises(ValueError, match="ledger identity"):
        BurstFuturesStore(path, TailDependent.execution_config(100, False))


def test_quiet_live_variant_tags_and_uni_research_receipt():
    from live_trading.live_model.quiet_breakout import ethusdc, hypeusdt, uniusdc
    hype = Path(hypeusdt.strategy.__file__).parent
    eth = Path(ethusdc.strategy.__file__).parent
    uni = Path(uniusdc.strategy.__file__).parent
    variants = (
        (hype / "stable/model_config.json", {"stabil"}, False, Stable),
        (hype / "tail_dependent/model_config.json", {"tail_dependent", "position_addition"}, True, TailDependent),
        (eth / "contract.json", {"stabil"}, False, EthStable),
        (uni / "model_config.json", {"stabil", "position_addition"}, True, UniStable),
    )
    for path, tags, adds, model in variants:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        assert set(descriptor["tags"]) == tags
        assert model.model_config().get("pyramid_add", False) == adds
        model.execution_config(100, False)
        receipt = uni.parents[3] / descriptor["source_run"]
        assert len(descriptor["source_run_sha256"]) == 64
        if not receipt.is_file():
            continue  # Source-only release checkouts exclude research run artifacts.
        raw = receipt.read_bytes()
        assert sha256(raw).hexdigest() == descriptor["source_run_sha256"]
        run = json.loads(raw)
        assert run["status"] == "complete"
        assert model.model_config() == {
            key: value for key, value in run["requested_model_config"].items() if key not in ("start_date", "end_date")
        }


def test_variant_config_tamper_is_rejected(tmp_path):
    from live_trading.live_model.quiet_breakout.hypeusdt import strategy
    asset = tmp_path / "hypeusdt"
    shutil.copytree(Path(strategy.__file__).parent, asset)
    config_file = asset / "tail_dependent/model_config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    config["model_config"]["initiative_risk_multiplier"] = 2
    config_file.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Model configuration changed: tail_dependent/model_config.json"):
        verify_model(asset / "strategy.py")


def test_hourly_breakout_and_closed_flow_evidence(runtime):
    model, venue, _ = runtime(TailDependent)
    closing = 100 * HOUR
    with model.store.transaction():
        for stamp in range(closing - 26 * HOUR, closing, MINUTE):
            price = 102 if stamp == closing - MINUTE else 100
            model.store.db.execute("INSERT INTO burst_minutes VALUES (?,?,?,?,?,?,?)",
                                   (stamp, 100, price, 100, price, 1, 1))
        for hour in range(closing - 26 * HOUR, closing - HOUR, HOUR):
            model.store.db.executemany("INSERT INTO burst_prices VALUES (?,?,?)",
                                       [(hour, 99, 10), (hour, 100, 110)])
        model.store.db.execute("UPDATE burst_minutes SET close=100,high=100 WHERE timestamp_ms=?",
                               (closing - MINUTE,))
        assert model.setup(closing) is None  # A close at the prior high.
        model.store.db.execute("UPDATE burst_minutes SET close=102,high=102 WHERE timestamp_ms=?",
                               (closing - MINUTE,))
        model.store.db.execute("UPDATE burst_minutes SET buy_volume=0 WHERE timestamp_ms=?",
                               (closing - MINUTE,))
        assert model.setup(closing) is None  # Breakout volume below the historical P75.
        model.store.db.execute("UPDATE burst_minutes SET buy_volume=1 WHERE timestamp_ms=?",
                               (closing - MINUTE,))
        model.evaluate(closing, closing, allow_entry=True)
        for offset in range(12):
            stamp = closing + offset * MINUTE
            opening = 102 + offset / 10
            model.store.db.execute("INSERT INTO burst_minutes VALUES (?,?,?,?,?,?,?)",
                                   (stamp, opening, opening + .1, opening, opening + .1, 4, 1))
        due = closing + 12 * MINUTE
        model.evaluate(due, due, allow_entry=True)
    history = model.get("quiet_confirmation_history")
    assert len(history) == 1 and history[0]["efficiency"] == pytest.approx(1)
    session = model.store.active()
    assert session["state"] == "PENDING"
    payload = json.loads(session["intention"])
    assert payload["eligible_timestamp_ms"] == due
    assert payload["entry_risk_fraction"] == .015
    assert "initiative_candidate" in payload["confirmation_mechanisms"]
    assert model.get("active_setup") is None
    assert not venue.posts


@pytest.mark.parametrize("model_type", [TailDependent, EthStable])
def test_p5_and_p85_use_the_same_prior_confirmations(runtime, model_type):
    model, _, clock = runtime(model_type)
    now = clock[0]
    path = pd.DataFrame(dict(timestamp_ms=range(now - 12 * MINUTE, now, MINUTE),
                             open=[100] * 12, close=list(range(101, 113))))
    signal = dict(sample_id="risk-rejected", direction="long", stop=100,
                  entry_reference=112, entry_eligible_timestamp_ms=now,
                  confirmation_mechanisms=["initiative_candidate", "exhaustion_candidate"])
    model.put("quiet_confirmation_history", [dict(efficiency=.8, distance=.05)] * 100)
    model._confirmation(signal, path, now, allow_entry=True)
    assert model.store.active() is None
    history = model.get("quiet_confirmation_history")
    assert len(history) == 100 and history[-1]["distance"] == pytest.approx(12 / 112)
    model._confirmation(signal, path, now, allow_entry=True)
    assert model.get("quiet_confirmation_history") == history
    now += MINUTE
    path["timestamp_ms"] += MINUTE
    signal.update(sample_id="path-rejected", stop=110, entry_eligible_timestamp_ms=now)
    path["close"] = [101, 99] * 6
    model._confirmation(signal, path, now, allow_entry=True)
    assert model.store.active() is None
    assert model.get("quiet_confirmation_history")[-1]["efficiency"] == pytest.approx(1 / 23)
    now += MINUTE
    path["timestamp_ms"] += MINUTE
    signal.update(sample_id="accepted", entry_eligible_timestamp_ms=now)
    path["close"] = list(range(101, 113))
    model._confirmation(signal, path, now, allow_entry=True)
    payload = json.loads(model.store.active()["intention"])
    assert payload["max_entry_stop_fraction"] == pytest.approx(.05)
    assert payload["entry_risk_fraction"] == .01


@pytest.mark.parametrize(("model_type", "direction", "at_one_r", "posts"), [
    (Stable, "long", -1, ["ENTRY", "STOP", "TP1"]),
    (Stable, "short", -1, ["ENTRY", "STOP", "TP1"]),
    (TailDependent, "long", 0, ["ENTRY", "STOP"]),
])
def test_sizing_and_exit_lifecycle(runtime, model_type, direction, at_one_r, posts):
    model, venue, clock = runtime(model_type)
    executor, store = model.executor, model.store
    sign = 1 if direction == "long" else -1
    executor.offer(intent(model, clock[0], direction=direction, initial_stop=1000 - 10 * sign), clock[0])
    executor.cycle(clock[0])
    plan = json.loads(store.active()["plan"])
    assert Decimal(plan["risk_budget"]) == Decimal("1.5")
    assert Decimal(plan["notional_cap"]) == Decimal("500")
    assert [role for role, _ in venue.posts] == posts
    if model_type is Stable:
        partial = venue.posts[-1][1]
        assert Decimal(partial["quantity"]) == Decimal(plan["quantity"]) / 10
        assert Decimal(partial["price"]) == 1000 + 10 * sign
    for move, expected in ((10, at_one_r), (20, 1), (40, 2)):
        clock[0] += 1000
        model.put("live_excursion", dict(setup_id=model.ALLOCATION_ID, timestamp_ms=clock[0],
                                       price=1000 + sign * move))
        executor.cycle(clock[0])
        plan = json.loads(store.active()["plan"])
        assert plan.get("risk_lock_r", -1) == expected
    assert plan["force_exit_ms"] == NOW + HOLD_MS
    venue.clock = clock[0] = NOW + HOLD_MS
    executor.cycle(clock[0], new_entries_ok=False)
    executor.cycle(clock[0], new_entries_ok=False)
    assert store.active() is None
    assert venue.stop_at_close == [True]


@pytest.mark.parametrize("quote_passes", [False, True])
def test_p85_rechecked_at_executable_price_and_actual_fill(runtime, quote_passes):
    class SlippingVenue(HypeVenue):
        def fill(self, cid, quantity=None, price=None, **kwargs):
            if self.orders[cid]["side"] == "BUY":
                price = "1000.4"
            return super().fill(cid, quantity, price, **kwargs)

    model, venue, clock = runtime(TailDependent, venue=SlippingVenue())
    model.executor.offer(intent(model, clock[0], max_entry_stop_fraction=.0101 if quote_passes else .009),
                         clock[0])
    model.executor.cycle(clock[0])
    if quote_passes:
        assert [role for role, _ in venue.posts] == ["ENTRY", "STOP", "CLOSE"]
        assert venue.stop_at_close == [True]
        assert model.store.active()["reason"] == "actual_fill_exceeds_stop_distance_p85"
    else:
        assert not venue.posts
        assert model.store.active() is None
        row = model.store.db.execute("SELECT reason FROM sessions").fetchone()
        assert row["reason"] == "executable_quote_exceeds_stop_distance_p85"

def test_eth_package_matches_selected_receipt():
    from live_trading.live_model.quiet_breakout.ethusdc import strategy
    path = Path(strategy.__file__).resolve()
    contract = verify_model(path)
    receipt = path.parents[4] / contract["source_run"]
    assert len(contract["source_run_sha256"]) == 64
    if receipt.is_file():
        raw = receipt.read_bytes()
        assert sha256(raw).hexdigest() == contract["source_run_sha256"]
        run = json.loads(raw)
        assert run["status"] == "complete"
        assert EthStable.model_config() == {
            key: value for key, value in run["requested_model_config"].items()
            if key not in ("start_date", "end_date")
        }
    config = EthStable.execution_config(100, False)
    assert (config["symbol"], config["margin_asset"], config["accept_new_entries"]) == ("ETHUSDC", "USDC", False)


@pytest.mark.parametrize(("mechanisms", "budget"), [
    (["initiative_candidate", "absorption_candidate"], "1.5"),
    (["initiative_candidate", "exhaustion_candidate"], "1"),
    (["absorption_candidate"], "1"),
])
def test_eth_risk_is_conditional_and_restart_uses_24h(runtime, mechanisms, budget):
    model, venue, clock = runtime(EthStable)
    executor, store = model.executor, model.store
    executor.offer(intent(model, NOW, confirmation_mechanisms=mechanisms), NOW)
    venue.clock = clock[0] = NOW + 450
    executor.cycle(clock[0])
    session = store.active()
    plan = json.loads(session["plan"])
    deadline = NOW + 450 + 24 * HOUR
    assert plan["first_fill_ms"] == NOW + 450
    assert plan["force_exit_ms"] == deadline
    assert Decimal(plan["risk_budget"]) == Decimal(budget)
    assert Decimal(plan["notional_cap"]) == Decimal("500")
    assert [role for role, _ in venue.posts] == ["ENTRY", "STOP", "TP1"]
    partial = venue.posts[-1][1]
    assert Decimal(partial["quantity"]) == Decimal(plan["quantity"]) / 10
    assert Decimal(partial["price"]) == 1010
    # A crash can leave the original parent's deadline on the just-opened plan.
    plan["force_exit_ms"] = plan["first_fill_ms"] + HOLD_MS
    with store.transaction():
        store.db.execute("UPDATE sessions SET plan=? WHERE session=?", (json.dumps(plan), session["session"]))
    recovered = EthFuturesExecutor(store, venue, executor.c)
    venue.clock = clock[0] = deadline - 1
    recovered.cycle(clock[0], new_entries_ok=False)
    assert json.loads(store.active()["plan"])["force_exit_ms"] == deadline
    assert not any(role == "CLOSE" for role, _ in venue.posts)
    venue.clock = clock[0] = deadline
    recovered.cycle(clock[0], new_entries_ok=False)
    recovered.cycle(clock[0], new_entries_ok=False)
    assert store.active() is None
    assert venue.stop_at_close == [True]
    row = store.db.execute("SELECT reason FROM sessions").fetchone()
    assert row["reason"] == "time_exit_24h"


def test_eth_late_recovery_of_first_fill_exits_without_new_target(runtime):
    model, venue, clock = runtime(EthStable)
    executor, store = model.executor, model.store
    venue.fail["ENTRY"] = "after_crash"
    executor.offer(intent(model, NOW), NOW)
    with pytest.raises(Crash):
        executor.cycle(NOW)
    assert store.active()["state"] == "ENTRY_PENDING"

    venue.clock = clock[0] = NOW + 25 * HOUR
    recovered = EthFuturesExecutor(store, venue, executor.c)
    recovered.cycle(clock[0], new_entries_ok=False)
    recovered.cycle(clock[0], new_entries_ok=False)
    assert store.active() is None
    assert [role for role, _ in venue.posts] == ["ENTRY", "STOP", "CLOSE"]
    assert venue.stop_at_close == [True]
    row = store.db.execute("SELECT reason FROM sessions").fetchone()
    assert row["reason"] == "time_exit_24h"


class AddLane(FakeVenue):
    quote_price = Decimal("1000")
    add_slippage = Decimal(0)
    stop_race = False

    def get(self, path, **kwargs):
        result = super().get(path, **kwargs)
        if path == "/fapi/v1/ticker/bookTicker":
            result.update(bidPrice=str(self.quote_price), askPrice=str(self.quote_price))
        return result

    def fill(self, cid, quantity=None, price=None, **kwargs):
        if cid.endswith("-add"):
            sign = 1 if self.orders[cid]["side"] == "BUY" else -1
            price = self.quote_price + sign * self.add_slippage
        return super().fill(cid, quantity, price, **kwargs)

    def submit(self, role, params):
        if role != "ADD":
            return super().submit(role, params)
        if self.stop_race:
            stop = next(row for row in self.orders.values() if row.get("algoStatus") == "NEW")
            self.stop_out(price=stop["triggerPrice"])
        if "ADD" in self.fail:
            self.fail["ENTRY"] = self.fail.pop("ADD")
        index = len(self.posts)
        try:
            return super().submit("ENTRY", params)
        finally:
            self.posts[index] = ("ADD", params)


def open_addition_lane(tmp_path, monkeypatch, model_type, sign=1):
    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0] / 1000)
    config = model_type.execution_config(100, True)
    store = BurstFuturesStore(tmp_path / model_type.DATABASE, config)
    lane = AddLane()
    venue = PortfolioVenue()
    venue.lanes[model_type.SYMBOL] = lane
    executor = QuietAdditionExecutor(store, venue, config)
    executor.offer(intent(model_type, NOW, direction="long" if sign == 1 else "short",
                          initial_stop=1000 - 10 * sign), NOW)
    executor.cycle(NOW)
    assert store.active()["state"] == "OPEN"
    return executor, store, venue, lane, clock


def advance_addition(executor, store, lane, clock, model_type, *, sign=1):
    if model_type is UniStable:
        tp = next(row for row in lane.orders.values() if row.get("type") == "LIMIT")
        lane.fill(tp["clientOrderId"], maker=True)
    lane.clock = clock[0] = NOW + 5000
    mark = dict(setup_id=model_type.ALLOCATION_ID, timestamp_ms=clock[0], agg_trade_id=10,
                price=1000 + sign * 20)
    store.set_meta("live_excursion", json.dumps(mark))
    store.set_meta("public_cursor", json.dumps(mark))
    executor.cycle(clock[0])
    assert json.loads(store.active()["plan"])["risk_lock_r"] == 1
    assert not any(role == "ADD" for role, _ in lane.posts)
    lane.clock = clock[0] = NOW + 5001
    lane.quote_price = Decimal(mark["price"])
    store.set_meta("public_cursor", json.dumps(dict(mark, agg_trade_id=11, timestamp_ms=clock[0])))


@pytest.mark.parametrize("model_type", [TailDependent, UniStable])
@pytest.mark.parametrize("sign", [1, -1])
def test_one_add_uses_confirmed_native_stop_and_common_deadline(tmp_path, monkeypatch, model_type, sign):
    executor, store, _, lane, clock = open_addition_lane(tmp_path, monkeypatch, model_type, sign)
    try:
        advance_addition(executor, store, lane, clock, model_type, sign=sign)
        executor.cycle(clock[0])
        plan = json.loads(store.active()["plan"])
        addon, = [params for role, params in lane.posts if role == "ADD"]
        quantity = Decimal(addon["quantity"])
        assert 0 < quantity <= Decimal(".8") * Decimal(plan["actual_entry_quantity"])
        assert quantity * 10 <= Decimal(".8") * Decimal(plan["actual_initial_price_risk"])
        native_stop, = [row for row in lane.orders.values() if row.get("algoStatus") == "NEW"]
        assert native_stop["closePosition"] is True
        assert Decimal(native_stop["triggerPrice"]) == 1000 + sign * 10
        executor.cycle(clock[0])
        assert len([role for role, _ in lane.posts if role == "ADD"]) == 1
        lane.clock = clock[0] = NOW + HOLD_MS
        executor.cycle(clock[0], new_entries_ok=False)
        executor.cycle(clock[0], new_entries_ok=False)
        assert lane.position == 0 and store.active() is None
        assert lane.stop_at_close == [True]
        assert store.db.execute("SELECT reason FROM sessions").fetchone()[0] == "time_exit_48h"
        assert store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == (4 if model_type is UniStable else 3)
    finally:
        store.close()


def test_pending_add_keeps_close_all_protection_until_final_quantity(tmp_path, monkeypatch):
    executor, store, _, lane, clock = open_addition_lane(tmp_path, monkeypatch, TailDependent)
    try:
        advance_addition(executor, store, lane, clock, TailDependent)
        lane.initial_fill = Decimal("0.001")
        executor.cycle(clock[0])
        add = store.db.execute("SELECT * FROM orders WHERE role='ADD'").fetchone()
        assert add["state"] == "PARTIALLY_FILLED"
        lane.clock = clock[0] = clock[0] + 1000
        store.set_meta("live_excursion", json.dumps(dict(setup_id=TailDependent.ALLOCATION_ID,
                       timestamp_ms=clock[0], agg_trade_id=12, price=1040)))
        executor.cycle(clock[0])
        assert json.loads(store.active()["plan"])["risk_lock_r"] == 1
        assert sum(role == "STOP" for role, _ in lane.posts) == 3
        lane.fill(add["client_id"], price="1020")
        executor.cycle(clock[0])
        assert json.loads(store.active()["plan"])["risk_lock_r"] == 2
        assert sum(role == "STOP" for role, _ in lane.posts) == 5
    finally:
        store.close()


@pytest.mark.parametrize("outcome", ["lost_ack", "stop_race", "slippage"])
def test_add_reconciliation_and_exit_guards(tmp_path, monkeypatch, outcome):
    executor, store, venue, lane, clock = open_addition_lane(tmp_path, monkeypatch, TailDependent)
    try:
        advance_addition(executor, store, lane, clock, TailDependent)
        if outcome == "lost_ack":
            lane.fail["ADD"] = "after_crash"
            with pytest.raises(Crash):
                executor.cycle(clock[0])
            assert executor._addition_order(store.active())["state"] == "SENDING"
            executor = QuietAdditionExecutor(store, venue, executor.c)
            executor.cycle(clock[0], new_entries_ok=False)
            assert executor._addition_order(store.active())["state"] == "FILLED"
            assert lane.position > 0
        else:
            if outcome == "stop_race":
                lane.stop_race = True
            else:
                lane.add_slippage = Decimal("8")
            executor.cycle(clock[0])
            executor.cycle(clock[0], new_entries_ok=False)
            assert lane.position == 0 and store.active() is None
            reason = store.db.execute("SELECT reason FROM sessions").fetchone()[0]
            assert reason == ("stop_left_unclosed_quantity" if outcome == "stop_race"
                              else "actual_add_fill_exceeds_risk_or_notional_cap")
        assert len([role for role, _ in lane.posts if role == "ADD"]) == 1
    finally:
        store.close()


def test_unreadable_stop_skips_add(tmp_path, monkeypatch):
    executor, store, _, lane, clock = open_addition_lane(tmp_path, monkeypatch, TailDependent)
    try:
        advance_addition(executor, store, lane, clock, TailDependent)
        stop, = [row for row in lane.orders.values() if row.get("algoStatus") == "NEW"]
        lane.query_failures.add(stop["clientAlgoId"])
        executor.cycle(clock[0])
        assert executor._addition_order(store.active()) is None
        assert not any(role == "ADD" for role, _ in lane.posts)
    finally:
        store.close()


def test_expired_add_trigger_does_not_submit(tmp_path, monkeypatch):
    executor, store, _, lane, clock = open_addition_lane(tmp_path, monkeypatch, TailDependent)
    try:
        advance_addition(executor, store, lane, clock, TailDependent)
        lane.clock = clock[0] = NOW + 65_000
        cursor = json.loads(store.meta("public_cursor"))
        store.set_meta("public_cursor", json.dumps(dict(cursor, timestamp_ms=clock[0])))
        executor.cycle(clock[0])
        assert executor._addition_order(store.active()) is None
        assert not any(role == "ADD" for role, _ in lane.posts)
    finally:
        store.close()


def test_selected_burst_rm_e_add_reconciles_before_peer_entry(tmp_path, monkeypatch):
    from live_trading.live_model.burst_breakout.hypeusdt.strategy import Strategy as BurstHype
    from live_trading.live_model.burst_breakout.injusdt.strategy import Strategy as BurstInj
    from utils.engine.execution.burst_futures import CoordinatedBurstAccount

    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0] / 1000)
    venue = PortfolioVenue()
    lane = venue.lanes["HYPEUSDT"] = AddLane()
    inj_lane = venue.lanes["INJUSDT"]
    journals = {model.SYMBOL: (tmp_path / model.DATABASE, model.execution_config(100, True))
                for model in (BurstHype, BurstInj)}
    from contextlib import ExitStack
    with ExitStack() as stores:
        def worker(model):
            path, config = journals[model.SYMBOL]
            store = BurstFuturesStore(path, config)
            stores.callback(store.close)
            executor = model.EXECUTOR(store, venue, config)
            CoordinatedBurstAccount(executor, journals)
            return executor

        hype = worker(BurstHype)
        with hype.shared_account.admission():
            hype.entry_guard(NOW)
        inj = worker(BurstInj)
        with inj.shared_account.admission():
            inj.entry_guard(NOW)

        hype.offer(intent(BurstHype, NOW), NOW)
        hype.cycle(NOW)
        plan = json.loads(hype.s.active()["plan"])
        assert Decimal(plan["risk_budget"]) == Decimal("1.5")
        tp, = [row for row in lane.orders.values() if row.get("type") == "LIMIT"]
        assert Decimal(tp["origQty"]) == Decimal(plan["actual_entry_quantity"]) / 10

        lane.clock = clock[0] = NOW + 4000
        lane.fill(tp["clientOrderId"])
        mark = dict(setup_id=BurstHype.ALLOCATION_ID, timestamp_ms=clock[0], agg_trade_id=9, price=1010)
        hype.s.set_meta("live_excursion", json.dumps(mark))
        hype.s.set_meta("public_cursor", json.dumps(mark))
        hype.cycle(clock[0])
        initial_stop, = [row for row in lane.orders.values() if row.get("algoStatus") == "NEW"]
        assert Decimal(initial_stop["triggerPrice"]) == Decimal(990)
        assert hype.s.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2

        advance_addition(hype, hype.s, lane, clock, BurstHype)
        lane.fail["ADD"] = "after_crash"
        with pytest.raises(Crash):
            hype.cycle(clock[0])
        assert hype._addition_order(hype.s.active())["state"] == "SENDING"
        add, = [params for role, params in lane.posts if role == "ADD"]
        assert Decimal(add["quantity"]) <= Decimal(plan["actual_entry_quantity"]) * Decimal(".8")
        stop, = [row for row in lane.orders.values() if row.get("algoStatus") == "NEW"]
        assert stop["closePosition"] and Decimal(stop["triggerPrice"]) == Decimal(1010)

        inj.offer(intent(BurstInj, NOW, confirmation_mechanisms=["initiative_candidate", "exhaustion_candidate"]),
                  clock[0])
        inj_lane.clock = clock[0]
        inj.cycle(clock[0])
        assert inj.s.active()["state"] == "PENDING"
        assert not inj_lane.posts
        assert "peer_add_awaiting_reconciliation" in inj.s.db.execute(
            "SELECT payload FROM audit WHERE kind='entry_deferred'"
        ).fetchone()[0]

        resumed = worker(BurstHype)
        resumed.cycle(clock[0], new_entries_ok=False)
        assert resumed._addition_order(resumed.s.active())["state"] == "FILLED"
        assert resumed.s.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 3
        assert resumed.s.equity(0) == lane.wallet - Decimal(200)
        assert [role for role, _ in lane.posts].count("ADD") == 1
        inj.cycle(clock[0])
        assert inj.s.active()["state"] == "OPEN"
        peer_plan = json.loads(inj.s.active()["plan"])
        assert Decimal(peer_plan["risk_budget"]) == (100 + resumed.s.equity(0)) * Decimal(".009")
        assert Decimal(peer_plan["notional_cap"]) < Decimal(400)


@pytest.mark.parametrize("mechanisms,multiplier", [
    (["initiative_candidate"], Decimal("1.5")),
    (["initiative_candidate", "exhaustion_candidate"], Decimal(1)),
    (["exhaustion_candidate"], Decimal(1)),
])
def test_selected_burst_sizing_uses_pinned_base_risk(mechanisms, multiplier):
    from live_trading.live_model.burst_breakout.hypeusdt.strategy import Strategy as BurstHype
    from live_trading.live_model.burst_breakout.injusdt.strategy import Strategy as BurstInj
    from live_trading.live_model.burst_breakout.uniusdc.strategy import Strategy as BurstUni
    for model, base_risk in ((BurstHype, ".01"), (BurstInj, ".009"), (BurstUni, ".01")):
        config = model.execution_config(100, True)
        executor = model.EXECUTOR(None, None, config)
        assert executor.entry_risk_fraction(dict(confirmation_mechanisms=mechanisms, entry_risk_fraction=.99)) == (
            Decimal(base_risk) * multiplier
        )
