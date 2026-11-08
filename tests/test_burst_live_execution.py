from decimal import Decimal
import json
from pathlib import Path

import pytest

from live_trading.burst_runtime import run_stream
from live_trading.live_model.burst_hype.strategy import Strategy as HypeBurst
from live_trading.live_model.burst_uni.strategy import Strategy as UniBurst
from utils.engine.adapters.burst_market import BurstMarketInput, trade_batch
from utils.engine.contracts import make_event
from utils.engine.execution.burst_futures import BurstFuturesExecutor, BurstFuturesStore, SharedBurstAccount, HOLD_MS
from utils.engine.monitoring.alerts import inspect_target, load_config
from test_futures_execution import FakeVenue, NOW

CONTRACT = HypeBurst.CONTRACT


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
    assert len(v.cancels) == 1
    assert len(v.posts) == 3
    assert json.loads(s.active()["plan"])["risk_lock_r"] == 1


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
    model = HypeBurst(s, e)
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
            return [row for symbol, lane in self.lanes.items() for row in self._replace(lane.read(path), symbol)]
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


def test_bad_symbol_feed_does_not_stop_sibling_or_send_exit(portfolio, monkeypatch):
    pools, executors, venue, wall = portfolio
    pools.initialize(NOW)
    hype = executors["HYPEUSDT"]
    hype.offer(dict(sample_id="burst-1", direction="long", initial_stop=990,
                    eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW + 59_999), NOW)
    hype.cycle(NOW)
    models = {"HYPEUSDT": HypeBurst(hype.s, hype),
              "UNIUSDC": UniBurst(executors["UNIUSDC"].s, executors["UNIUSDC"])}
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
    model = HypeBurst(store, executor)
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
