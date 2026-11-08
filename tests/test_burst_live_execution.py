from decimal import Decimal
import json

import pytest

from live_trading.live_model.burst_breakout.strategy import CONTRACT, NativeBurst
from utils.engine.execution.burst_futures import BurstFuturesExecutor, BurstFuturesStore, HOLD_MS
from test_futures_execution import FakeVenue, NOW


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
