from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from live_trading.burst_runtime import NativeBurst, run_stream
from utils.engine.adapters.binance_execution import VenueError
from utils.engine.adapters.binance_futures_readonly import PreflightError
from utils.engine.adapters.burst_market import BurstMarketInput, trade_batch
from utils.engine.contracts import make_event
from utils.engine.execution.burst_futures import (
    BurstFuturesExecutor, BurstFuturesOrders, BurstFuturesStore, CoordinatedBurstAccount, SharedBurstAccount, HOLD_MS,
)
from utils.engine.monitoring.alerts import inspect_target, load_config
from utils.engine.monitoring.engine_alerts import inspect_engine
from test_futures_execution import Crash, FakeVenue, NOW

CONTRACT = "BURST-NATIVE-EXECUTION-TEST"


def translate(value):
    if isinstance(value, list):
        return [translate(item) for item in value]
    if isinstance(value, dict):
        return {k: translate(v) for k, v in value.items()}
    if value == "USDC":
        return "USDT"
    if value == "ETHUSDC":
        return "HYPEUSDT"
    return value


class HypeVenue(FakeVenue):
    def get(self, path, **kw):
        return translate(super().get(path, **kw))

    def read(self, path, **kw):
        result = translate(super().read(path, **kw))
        if path == "/fapi/v1/symbolConfig":
            result[0].update(marginType="ISOLATED", isAutoAddMargin=False)
        return result


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("utils.engine.execution.preny_futures.time.time", lambda: clock[0] / 1000)
    cfg = dict(symbol="HYPEUSDT", contract=CONTRACT, allocation_id="hype_test",
               initial_equity="100", execution_mode="live", execution_enabled=True, accept_new_entries=True)
    db = tmp_path / "hype.sqlite"
    store = BurstFuturesStore(db, cfg)
    venue = HypeVenue()
    engine = BurstFuturesExecutor(store, venue, cfg)
    try:
        yield engine, store, venue, clock
    finally:
        store.close()


def open_position(env, direction="long"):
    e, s, v, now = env
    e.offer(dict(sample_id="burst-1", direction=direction, initial_stop=990 if direction == "long" else 1010,
                 eligible_timestamp_ms=now[0], expires_timestamp_ms=now[0] + 59_999), now[0])
    e.cycle(now[0])
    assert s.active()["state"] == "OPEN"
    assert len(v.posts) == 2
    assert [role for role, _ in v.posts] == ["ENTRY", "STOP"]
    plan = json.loads(s.active()["plan"])
    assert Decimal(plan["actual_initial_price_risk"]) <= Decimal("1")
    assert plan["force_exit_ms"] == NOW + HOLD_MS
    return plan


@pytest.mark.parametrize("direction, sign", [("long", 1), ("short", -1)])
def test_milestones_and_48h_close(env, direction, sign):
    e, s, v, now = env
    open_position(env, direction)
    for move, lock in [(10, 0), (20, 1), (40, 2)]:
        now[0] += 1000
        with s.transaction():
            s.set_meta("live_excursion", json.dumps(dict(setup_id="burst-1", timestamp_ms=now[0], price=1000 + sign * move)))
        e.cycle(now[0])
        assert json.loads(s.active()["plan"])["risk_lock_r"] == lock
        active = [row for row in v.orders.values() if row.get("algoStatus") == "NEW"]
        assert len(active) == 1
        assert Decimal(active[0]["triggerPrice"]) == 1000 + sign * lock * 10
    now[0] = v.clock = NOW + HOLD_MS
    e.cycle(now[0], new_entries_ok=False)
    e.cycle(now[0], new_entries_ok=False)
    assert not s.active()
    assert v.stop_at_close == [True]
    assert len([r for r,p in v.posts if r=="CLOSE"]) == 1
    audit = json.loads(s.db.execute("SELECT payload FROM audit WHERE kind='trade_closed'").fetchone()[0])
    assert audit["reason"] == "time_exit_48h"
    assert Decimal(audit["net_pnl"]) < 0
    assert len(audit["fill_ids"]) == 2


def test_uncertain_replacement_keeps_protection_then_reconciles(env):
    e, s, v, now = env
    open_position(env)
    now[0] += 1000
    v.fail["STOP"] = "timeout"
    with s.transaction():
        s.set_meta("live_excursion", json.dumps(dict(setup_id="burst-1", timestamp_ms=now[0], price=1020)))
    e.cycle(now[0])
    assert not v.cancels
    assert json.loads(s.active()["plan"]).get("risk_lock_r", -1) == -1
    assert len([x for x in v.orders.values() if x.get("algoStatus") == "NEW"]) == 2
    e.cycle(now[0])
    assert [cid.rsplit("-", 1)[-1] for cid in v.cancels] == ["initial", "1"]
    assert len(v.posts) == 4
    assert [params.get("reduceOnly") for role, params in v.posts if role == "STOP"] == [None, "true", None]
    assert json.loads(s.active()["plan"])["risk_lock_r"] == 1



def advance_stop(env, price):
    e, s, v, now = env
    v.clock = now[0] = now[0] + 1000
    s.set_meta("live_excursion", json.dumps(dict(setup_id="burst-1", timestamp_ms=now[0], price=price)))
    e.cycle(now[0])


def test_reconciles_only_recorded_legacy_rejection(env):
    e, s, v, now = env
    open_position(env, "short")
    session = s.active()
    legacy = e._stop_params("ggp-legacy-sl-0", -1, "1000")
    old = e._prepare(session["session"], "STOP", dict(legacy, closePosition="true"))
    s.db.execute("UPDATE orders SET state='UNKNOWN', error='native_order_not_found' WHERE client_id=?", (old,))
    # A missing query by itself is still ambiguous and must prevent another submit.
    advance_stop(env, 990)
    assert s.db.execute("SELECT state FROM orders WHERE client_id=?", (old,)).fetchone()[0] == "UNKNOWN"
    assert len(v.posts) == 2 and not v.cancels
    s.audit("order_submit_error", dict(client_id=old, role="STOP", http_status=400,
                                      venue_code=-4130, uncertain=True))
    advance_stop(env, 990)
    assert tuple(s.db.execute("SELECT state,error,cancel_pending FROM orders WHERE client_id=?", (old,)).fetchone()) == (
        "REJECTED", None, 0)
    assert json.loads(s.active()["plan"])["risk_lock_r"] == 0
    assert all(cid != old for cid in v.cancels)
    assert [order["triggerPrice"] for order in v.orders.values() if order.get("algoStatus") == "NEW"] == ["1000"]


@pytest.mark.parametrize("crash_at", ["guard_ack", "cancel_ack", "replacement_ack", "guard_cancel_ack"])
def test_stop_restart_always_has_a_native_protector(env, monkeypatch, crash_at):
    e, s, v, now = env
    open_position(env)
    submit = v.submit
    cancel = v.cancel

    def submit_with_crash(role, params):
        result = submit(role, params)
        if role == "STOP" and (
            (crash_at == "guard_ack" and "quantity" in params)
            or (crash_at == "replacement_ack" and "-sl-v2-" in params["clientAlgoId"])
        ):
            raise Crash()
        return result

    def cancel_with_crash(cid, **kwargs):
        assert any(key != cid and row.get("algoStatus") == "NEW" for key, row in v.orders.items())
        result = cancel(cid, **kwargs)
        if (crash_at == "cancel_ack" and cid.endswith("-initial")) or (
            crash_at == "guard_cancel_ack" and "-guard-" in cid
        ):
            raise Crash()
        return result

    monkeypatch.setattr(v, "submit", submit_with_crash)
    monkeypatch.setattr(v, "cancel", cancel_with_crash)
    with pytest.raises(Crash):
        advance_stop(env, 1020)
    assert v.position and any(row.get("algoStatus") == "NEW" for row in v.orders.values())
    s.set_meta("live_excursion", "null")
    monkeypatch.setattr(v, "submit", submit)
    monkeypatch.setattr(v, "cancel", cancel)
    with closing(BurstFuturesStore(s.path, e.c)) as reopened:
        resumed = BurstFuturesExecutor(reopened, v, e.c)
        resumed.cycle(now[0], new_entries_ok=False)
        assert json.loads(reopened.active()["plan"])["risk_lock_r"] == 1
        assert not reopened.db.execute("SELECT 1 FROM orders WHERE cancel_pending OR error IS NOT NULL").fetchone()
    assert [role for role, _ in v.posts] == ["ENTRY", "STOP", "STOP", "STOP"]
    assert [row["triggerPrice"] for row in v.orders.values() if row.get("algoStatus") == "NEW"] == ["1010"]


def test_old_stop_can_fill_during_cancel_without_another_submission(env):
    e, s, v, now = env
    open_position(env)
    v.on_cancel = lambda cid: v.stop_out("989")
    advance_stop(env, 1020)
    advance_stop(env, 1020)
    assert not v.position and s.active() is None
    assert [role for role, _ in v.posts] == ["ENTRY", "STOP", "STOP"]
    assert s.db.execute("SELECT reason FROM sessions").fetchone()[0] == "initial_stop"
    assert not any(row.get("algoStatus") == "NEW" for row in v.orders.values())


@pytest.mark.parametrize("reject_guard", [True, False])
def test_stop_rejection_preserves_current_protector(env, monkeypatch, reject_guard):
    e, s, v, now = env
    open_position(env)
    submit = v.submit

    def rejected(role, params):
        if role == "STOP" and ("quantity" in params) == reject_guard:
            raise VenueError(400, -2022)
        return submit(role, params)

    monkeypatch.setattr(v, "submit", rejected)
    advance_stop(env, 1020)
    advance_stop(env, 1020)
    assert v.position and json.loads(s.active()["plan"]).get("risk_lock_r", -1) == -1
    protector, = [row for row in v.orders.values() if row.get("algoStatus") == "NEW"]
    assert protector["closePosition"] == reject_guard
    assert tuple(s.db.execute("SELECT state,error FROM orders WHERE state='REJECTED'").fetchone()) == ("REJECTED", None)
    target = dict(id="test", label="test", path=str(s.path), kind="futures_executor",
                  expected_metadata={"owner": "binance_futures_pilot"},
                  heartbeat_key="worker_heartbeat_ms", max_age_seconds=90)
    s.set_meta("worker_heartbeat_ms", now[0])
    s.set_meta("worker_armed", 1)
    issues, valid = inspect_engine(target, now[0])
    assert valid and [item["code"] for item in issues] == ["STOP_REPLACEMENT_REJECTED"]



def test_burst_adapter_stop_payload_and_rejection_classification(monkeypatch):
    venue = BurstFuturesOrders()
    params = dict(algoType="CONDITIONAL", symbol="UNIUSDC", side="BUY", positionSide="BOTH",
                  type="STOP_MARKET", triggerPrice="5.877", workingType="CONTRACT_PRICE",
                  reduceOnly="true", priceProtect="false", clientAlgoId="ggp-guard-0", quantity="5")

    def rejected(method, path, request):
        assert (method, path, request) == ("POST", "/fapi/v1/algoOrder", params)
        raise VenueError(400, -4130, uncertain=True)

    monkeypatch.setattr(venue, "_request", rejected)
    with pytest.raises(VenueError) as caught:
        venue.submit("STOP", params)
    assert caught.value.code == -4130 and not caught.value.uncertain
    with pytest.raises(PreflightError):
        venue.submit("STOP", dict(params, closePosition="true"))


def test_lost_initial_stop_ack_keeps_position_then_reconciles(env):
    e, s, v, now = env
    v.fail["STOP"] = "timeout"
    e.offer(dict(sample_id="burst-1", direction="long", initial_stop=990,
                 eligible_timestamp_ms=now[0], expires_timestamp_ms=now[0] + 59_999), now[0])
    e.cycle(now[0])
    stop = s.db.execute("SELECT * FROM orders WHERE role='STOP'").fetchone()
    assert stop["state"] == "UNKNOWN"
    assert v.position and [role for role, _ in v.posts] == ["ENTRY", "STOP"]
    with s.transaction():
        for key, value in (("worker_heartbeat_ms", now[0]), ("worker_armed", 1), ("worker_symbol", "HYPEUSDT")):
            s.set_meta(key, value)
    config = Path(__file__).resolve().parents[1] / "live_trading/deploy/burst-alerts.json"
    target = dict(load_config(config)["targets"][0], path=str(s.path))
    s.set_meta("strategy_contract", target["performance_contract"])
    issues, read_ok = inspect_target(target, now[0])
    assert read_ok and {issue["code"] for issue in issues} == {"ORDER_UNRESOLVED", "STOP_UNCONFIRMED"}
    e.cycle(now[0], new_entries_ok=False)
    assert s.active()["state"] == "OPEN"
    assert [role for role, _ in v.posts] == ["ENTRY", "STOP"]
    assert inspect_target(target, now[0]) == ([], True)


def test_restart_resumes_journal_without_sending_an_entry_or_close(env):
    e, s, v, now = env
    plan = open_position(env)
    s.close()
    reopened = BurstFuturesStore(s.path, e.c)
    try:
        reopened.acquire_writer()
        resumed = BurstFuturesExecutor(reopened, v, e.c)
        resumed.cycle(now[0] + 1_000, new_entries_ok=False)
        assert reopened.active()["state"] == "OPEN"
        assert json.loads(reopened.active()["plan"])["force_exit_ms"] == plan["force_exit_ms"]
        assert v.position and [role for role, _ in v.posts] == ["ENTRY", "STOP"]
    finally:
        reopened.close()


def test_known_initial_stop_rejection_still_emergency_closes(env):
    e, s, v, now = env
    v.fail["STOP"] = "rejected"
    e.offer(dict(sample_id="burst-1", direction="long", initial_stop=990,
                 eligible_timestamp_ms=now[0], expires_timestamp_ms=now[0] + 59_999), now[0])
    e.cycle(now[0])
    assert not v.position and [role for role, _ in v.posts] == ["ENTRY", "STOP", "CLOSE"]
    assert s.active()["reason"] == "protective_stop_not_confirmed"


def test_native_stop_fill_reports_actual_reason_and_pnl(env):
    e, s, v, now = env
    open_position(env)
    v.stop_out("989")
    now[0] += 1000
    e.cycle(now[0])
    assert not s.active()
    outcome = json.loads(s.db.execute("SELECT payload FROM audit WHERE kind='trade_closed'").fetchone()[0])
    assert outcome["reason"] == "initial_stop"
    assert Decimal(outcome["realized"]) < 0


def test_missing_history_and_open_position_skip_are_durable(env):
    e, s, _, now = env
    model = NativeBurst(s, e)
    with s.transaction():
        assert model.setup(now[0]) is None
    assert s.db.execute("SELECT json_extract(payload, '$.reason') FROM audit").fetchone()[0] == "profile_warmup"
    open_position(env)
    e.offer(dict(sample_id="burst-2", direction="long", initial_stop=990,
                 eligible_timestamp_ms=now[0], expires_timestamp_ms=now[0] + 59_999), now[0])
    assert s.db.execute("SELECT reason FROM sessions WHERE id='burst-2'").fetchone()[0] == "position_or_entry_pending"


class PortfolioVenue:
    armed, offset, _key = True, 0, "test-api-key"

    def __init__(self):
        self.lanes = {symbol: FakeVenue() for symbol in ("HYPEUSDT", "INJUSDT", "UNIUSDC")}
        self.assets = {"HYPEUSDT": "USDT", "INJUSDT": "USDT", "UNIUSDC": "USDC"}

    def load_credentials(self):
        pass

    def _replace(self, result, symbol):
        if isinstance(result, list):
            return [self._replace(row, symbol) for row in result]
        if isinstance(result, dict):
            return {key: self._replace(value, symbol) for key, value in result.items()}
        return {"ETHUSDC": symbol, "USDC": self.assets[symbol]}.get(result, result)

    def get(self, path, **kw):
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [self._replace(lane.spec, symbol) for symbol, lane in self.lanes.items()]}
        return self._replace(self.lanes[kw["symbol"]].get(path, **kw), kw["symbol"])

    def read(self, path, **kw):
        if path == "/fapi/v3/account":
            assets = []
            for asset in ("USDT", "USDC"):
                wallet = 200 + sum(lane.wallet - 200 for symbol, lane in self.lanes.items()
                                   if self.assets[symbol] == asset)
                assets.append(dict(asset=asset, walletBalance=str(wallet), availableBalance=str(wallet)))
            return {"assets": assets}
        if path in ("/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            return [row for symbol, lane in self.lanes.items() if kw.get("symbol", symbol) == symbol
                    for row in self._replace(lane.read(path), symbol)]
        symbol = kw.get("symbol", "HYPEUSDT")
        result = self._replace(self.lanes[symbol].read(path, **kw), symbol)
        if path == "/fapi/v1/symbolConfig":
            result[0].update(marginType="ISOLATED", isAutoAddMargin=False)
        return result

    def submit(self, role, params):
        return self.lanes[params["symbol"]].submit(role, params)

    def query(self, cid, *, symbol, **kw):
        return self.lanes[symbol].query(cid, **kw)

    def cancel(self, cid, *, symbol, **kw):
        return self.lanes[symbol].cancel(cid, **kw)


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("utils.engine.execution.preny_futures.time.time", lambda: clock[0] / 1000)
    venue = PortfolioVenue()
    models = {}
    try:
        for symbol, asset in venue.assets.items():
            config = dict(symbol=symbol, contract=symbol, allocation_id=symbol, initial_equity="100",
                          execution_mode="live", accept_new_entries=True, margin_asset=asset,
                          risk_management="rm-d" if symbol == "INJUSDT" else "rm-c")
            store = BurstFuturesStore(tmp_path / (symbol + ".sqlite"), config)
            models[symbol] = BurstFuturesExecutor(store, venue, config)
        pools = SharedBurstAccount(list(models.values()), dict(USDT=Decimal("100"), USDC=Decimal("99")))
        yield pools, models, venue, clock
    finally:
        for model in models.values():
            model.s.close()


def test_shared_equity_and_independent_journals(portfolio):
    pools, models, venue, clock = portfolio
    pools.initialize(NOW)
    for symbol, model in models.items():
        model.offer(dict(sample_id=symbol, direction="long", initial_stop=990,
                         eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW+59_999), NOW)
        model.cycle(NOW)
        assert model.s.active()["state"] == "OPEN"
    hype, inj, uni = models.values()
    assert Decimal(json.loads(hype.s.active()["plan"])["risk_budget"]) == Decimal("1")
    assert Decimal(json.loads(inj.s.active()["plan"])["risk_budget"]) == (100 + hype.s.equity(0))/100
    assert Decimal(json.loads(uni.s.active()["plan"])["risk_budget"]) == Decimal("0.99")
    assert all(model.s.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1 for model in models.values())
    assert all(venue.lanes[symbol].position for symbol in models)
    pools.initialize(clock[0])
    venue.lanes["INJUSDT"].wallet += 1
    with pytest.raises(ValueError, match="shared_wallet_fill_funding_mismatch"):
        pools.balance("USDT", NOW)
    assert pools.balance("USDC", NOW)[0] == 99 + uni.s.equity(0)


def test_insufficient_usdc_leaves_account_unbound(portfolio):
    pools, _, venue, _ = portfolio
    venue.lanes["UNIUSDC"].wallet = Decimal("98.99")
    with pytest.raises(ValueError, match="insufficient_USDC"):
        pools.initialize(NOW)
    assert pools.state() is None
    assert not any(lane.posts for lane in venue.lanes.values())


def test_coordinated_entry_reconciles_crash_and_keeps_other_collateral_independent(portfolio):
    _, models, venue, _ = portfolio
    hype, inj, uni = models.values()
    journals = {symbol: (model.s.path, model.c) for symbol, model in models.items()}
    for model in models.values():
        CoordinatedBurstAccount(model, journals)
    # New journals are enrolled by preflight before any strategy is armed.
    key_hash = hashlib.sha256(venue._key.encode()).hexdigest()
    for model in (hype, uni, inj):
        with model.s.transaction():
            model.s.set_meta("key_hash", key_hash)
            model.s.set_meta("worker_symbol", model.symbol)
    for model in models.values():
        with model.shared_account.admission():
            model.entry_guard(NOW)
        model.offer(dict(sample_id=model.symbol, direction="long", initial_stop=990,
                         eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW+59_999), NOW)
    with hype.shared_account.admission():
        inj.cycle(NOW)
        assert inj.s.active()["state"] == "PENDING"
        uni.cycle(NOW)
        assert uni.s.active()["state"] == "OPEN"

    lane = venue.lanes[hype.symbol]
    lane.fail["ENTRY"] = "after_crash"
    with pytest.raises(Crash):
        hype.cycle(NOW)
    assert hype.s.active()["state"] == "ENTRY_PENDING"
    assert hype.s.db.execute("SELECT state FROM orders WHERE role='ENTRY'").fetchone()[0] == "SENDING"
    inj.cycle(NOW)
    assert not venue.lanes[inj.symbol].posts
    reopened = BurstFuturesStore(hype.s.path, hype.c)
    try:
        resumed = BurstFuturesExecutor(reopened, venue, hype.c)
        CoordinatedBurstAccount(resumed, journals)
        resumed.cycle(NOW, new_entries_ok=False)
        assert reopened.active()["state"] == "OPEN"
        assert [role for role, _ in lane.posts] == ["ENTRY", "STOP"]
        inj.cycle(NOW)
        assert inj.s.active()["state"] == "OPEN"
        plan = json.loads(inj.s.active()["plan"])
        assert Decimal(plan["risk_budget"]) == (Decimal("100") + reopened.equity(0)) / 100
        assert len([role for role, _ in venue.lanes[inj.symbol].posts if role == "ENTRY"]) == 1
    finally:
        reopened.close()


def test_bad_symbol_feed_does_not_stop_sibling_or_send_exit(portfolio, monkeypatch):
    pools, executors, venue, wall = portfolio
    pools.initialize(NOW)
    hype = executors["HYPEUSDT"]
    hype.offer(dict(sample_id="burst-1", direction="long", initial_stop=990,
                    eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW + 59_999), NOW)
    hype.cycle(NOW)
    models = {"HYPEUSDT": NativeBurst(hype.s, hype),
              "UNIUSDC": NativeBurst(executors["UNIUSDC"].s, executors["UNIUSDC"])}
    seconds = [0]
    monkeypatch.setattr("live_trading.burst_runtime.time.monotonic", lambda: seconds[0])

    def bad_print(*args, **kwargs):
        raise ValueError("unreconciled_public_stream_sequence")

    monkeypatch.setattr(models["HYPEUSDT"], "ingest", bad_print)

    class Public:
        def stream(self, symbols):
            assert symbols == list(models)
            for age, symbol in ((2, "HYPEUSDT"), (32, "UNIUSDC")):
                seconds[0] = age
                wall[0] = NOW + age * 1000
                yield dict(symbol=symbol, agg_trade_id=1, timestamp_ms=wall[0], price=1000.0,
                           quantity=1.0, buy=True)

    run_stream(models, venue, Public(), duration=31)
    assert hype.s.meta("worker_error") == "feed_ValueError"
    assert models["UNIUSDC"].store.meta("worker_error") == ""
    assert models["UNIUSDC"].get("public_cursor")["agg_trade_id"] == 1
    assert hype.s.active()["state"] == "OPEN"
    assert [role for role, _ in venue.lanes["HYPEUSDT"].posts] == ["ENTRY", "STOP"]


def test_broker_batch_checkpoints_only_after_trade_cursor_commit(env):
    executor, store, _, clock = env
    model = NativeBurst(store, executor)
    broker = BurstMarketInput("nats://test", model)
    ticks = [dict(symbol="HYPEUSDT", agg_trade_id=i, timestamp_ms=clock[0]+i, price=1.0,
                  quantity=2.0, buy=True) for i in (1, 2)]
    event = make_event(venue="binance", product="futures", instrument_id="HYPEUSDT",
                       source="native_aggregate_trades", event_type="trade", event_timestamp_ms=clock[0]+2,
                       available_at_ms=clock[0]+3, received_timestamp_ms=clock[0]+3, stream_sequence=11,
                       source_sequence=2, source_epoch="test-source", feature_version="market-data-v1",
                       quality="complete", payload=dict(trades=ticks, first_source_sequence=1,
                                                        last_source_sequence=2))
    with pytest.raises(ValueError, match="unexpected_burst_market_event"):
        trade_batch(event, "INJUSDT")
    with pytest.raises(OSError, match="not_committed"):
        list(broker.apply("test-broker", 11, event))
    assert model.get("burst_broker_sequence") == 0
    for tick in broker.apply("test-broker", 11, event):
        model.ingest(tick, clock[0]+3, allow_entry=False)
    assert model.get("burst_broker_sequence") == 11
    assert model.get("public_cursor") == ticks[-1]
    assert list(broker.apply("test-broker", 11, event)) == []


def test_partial_recovers_lost_ack_then_stops_at_breakeven(portfolio):
    pools, models, venue, clock = portfolio
    pools.initialize(NOW)
    inj = models["INJUSDT"]
    lane = venue.lanes["INJUSDT"]
    lane.fail["TP1"] = "timeout"
    inj.offer(dict(sample_id="inj-1", direction="long", initial_stop=990,
                   eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW+59_999), NOW)
    inj.cycle(NOW)
    order = inj.s.db.execute("SELECT * FROM orders WHERE role='TP1'").fetchone()
    assert order["state"] == "UNKNOWN"
    tp = json.loads(order["params"])
    plan = json.loads(inj.s.active()["plan"])
    assert Decimal(tp["quantity"]) == Decimal(plan["actual_entry_quantity"]) / 10
    assert tp["reduceOnly"] == "true" and Decimal(tp["price"]) == 1010
    lane.clock = clock[0] = NOW + 1000
    lane.fill(order["client_id"], maker=True)
    inj.s.set_meta("live_excursion", json.dumps(dict(setup_id="inj-1", timestamp_ms=clock[0], price=1010)))
    inj.cycle(clock[0])
    assert len([role for role, _ in lane.posts if role == "TP1"]) == 1
    assert Decimal(lane.position) == Decimal(plan["actual_entry_quantity"]) * Decimal(".9")
    assert json.loads(inj.s.active()["plan"])["risk_lock_r"] == 0
    assert lane.position and any(row.get("algoStatus") == "NEW" for row in lane.orders.values())
    lane.clock = clock[0] = NOW + HOLD_MS
    inj.cycle(clock[0])
    inj.cycle(clock[0])
    assert not inj.s.active()
    close = json.loads(inj.s.db.execute("SELECT payload FROM audit WHERE kind='trade_closed'").fetchone()[0])
    assert close["pnl_asset"] == "USDT" and len(close["fill_ids"]) == 3

def test_restart_with_open_position_and_cleared_excursion_keeps_native_stop(env):
    e, s, v, now = env
    open_position(env)
    NativeBurst(s, e).reset_profile("test_unrecoverable_gap")
    assert s.meta("live_excursion") == "null"
    resumed = BurstFuturesExecutor(s, v, e.c)
    resumed.cycle(now[0] + 1, new_entries_ok=False)
    assert s.active()["state"] == "OPEN"
    assert [role for role, _ in v.posts] == ["ENTRY", "STOP"]


def test_broker_connect_timeout_returns_to_exit_and_heartbeat_loop(env, monkeypatch):
    import asyncio
    from types import ModuleType

    executor, store, _, _ = env
    errors = ModuleType("nats.errors")
    errors.Error = type("NatsError", (Exception,), {})
    monkeypatch.setitem(__import__("sys").modules, "nats.errors", errors)
    monkeypatch.setattr("utils.engine.adapters.burst_market.BROKER_TIMEOUT_SECONDS", .01)

    async def connect_forever(_url):
        await asyncio.Event().wait()

    monkeypatch.setattr("utils.engine.adapters.burst_market.connect", connect_forever)
    market = BurstMarketInput("nats://offline", NativeBurst(store, executor))
    with pytest.raises(TimeoutError):
        next(market.stream([executor.symbol]))


def test_portfolio_uses_local_equity_and_distinct_native_accounts(portfolio, monkeypatch):
    _, models, venue, _ = portfolio
    uid = [1001]
    monkeypatch.setattr(venue, "account_uid", lambda: uid[0], raising=False)
    journals = {name: (model.s.path, model.c) for name, model in models.items()}
    hype, inj, uni = models.values()
    index = hype.s.path.with_name("portfolio-accounts.sqlite")
    from contextlib import closing
    import hashlib
    import sqlite3

    with closing(sqlite3.connect(index)) as db, db:
        db.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL)")
        db.execute("INSERT INTO accounts VALUES ('burst', 'shared-legacy-alias')")
    legacy = CoordinatedBurstAccount(hype, {"HYPEUSDT": journals["HYPEUSDT"]}, group="burst")
    with pytest.raises(ValueError, match="portfolio_uid_migration_requires_bound_journal"):
        legacy.bind_portfolio_account(NOW)
    for model in (hype, inj):
        model.s.set_meta("key_hash", hashlib.sha256(venue._key.encode()).hexdigest())
        model.s.set_meta("portfolio_account_alias", "shared-legacy-alias")
    for model in (hype, inj):
        burst_journals = {symbol: journals[symbol] for symbol in ("HYPEUSDT", "INJUSDT")}
        CoordinatedBurstAccount(model, burst_journals, group="burst").bind_portfolio_account(NOW)
    shared = CoordinatedBurstAccount(uni, {"UNIUSDC": journals["UNIUSDC"]}, group="quiet-core")
    with pytest.raises(ValueError, match="portfolio_account_already_used_or_changed"):
        shared.bind_portfolio_account(NOW)
    uid[0] = 1002
    shared.bind_portfolio_account(NOW)
    uid[0] = 1003
    with pytest.raises(ValueError, match="portfolio_journal_account_changed"):
        shared.bind_portfolio_account(NOW)
    for model in (hype, inj):
        model.offer(dict(sample_id=model.symbol, direction="long", initial_stop=990,
                         eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW+59_999), NOW)
        model.cycle(NOW)
        plan = json.loads(model.s.active()["plan"])
        assert Decimal(plan["equity_before_entry"]) == Decimal(100)
        assert Decimal(plan["risk_budget"]) == Decimal(1)
    assert hype.s.equity(100) < Decimal(100)
    assert inj.s.equity(100) < Decimal(100)
    assert uni.s.equity(100) == Decimal(100)
