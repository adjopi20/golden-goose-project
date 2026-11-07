from dataclasses import replace
import json
import sqlite3

import pytest

from model_development.phase_2.backtest import Config, Portfolio, on_tick
from utils.engine.execution.paper_manager import PaperOrderManager
from utils.engine.state.base import StateStore


CONFIG = Config(tp1_r=1, tp1_fraction=.1)


def signal(day="2026-09-27"):
    return dict(sample_id="sample:"+day, session_day=day, symbol="ETHUSDC", direction="long",
                strategy="test", route="test", stop=90., entry_reference=100.,
                feature_as_of_ms=1000, entry_eligible_timestamp_ms=1000,
                entry_deadline_timestamp_ms=2000, force_exit_timestamp_ms=5000)


def tick(seq, ts, price):
    return dict(agg_trade_id=seq, timestamp_ms=ts, price=price)


def open_manager(path, venue="binance"):
    store = StateStore(path, venue)
    manager = PaperOrderManager(store)
    manager.register("eth", "ETHUSDC", CONFIG, allowed_risks=[.0025, .005], contract="test-v1")
    return store, manager


def submit(manager, **kwargs):
    return manager.submit("eth", signal(), snapshot={"feature_as_of_ms": 1000}, risk_fraction=.005, **kwargs)


def journal(store, kind):
    return [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM journal WHERE kind=? ORDER BY id", (kind,))]


def test_restart_partial_time_exit_matches_shared_engine(tmp_path):
    path = tmp_path/"paper.sqlite"
    store, manager = open_manager(path)
    assert submit(manager) == "pending"
    ticks = [tick(1, 1000, 100.), tick(2, 1500, 110.), tick(3, 1500, 111.), tick(4, 5000, 105.)]
    manager.tick("eth", ticks[0])
    manager.tick("eth", ticks[1])
    assert manager.status()[0]["partial_trigger"] is not None
    store.close()
    store, manager = open_manager(path)
    assert submit(manager) == "duplicate"
    assert manager.tick("eth", ticks[1]) == "duplicate"
    for t in ticks[2:]:
        manager.tick("eth", t)
    reference = Portfolio(label="eth", signals=[signal()], equity=1000.)
    for t in ticks:
        on_tick(reference, t, CONFIG)
    assert journal(store, "trade") == reference.trades
    assert journal(store, "fill") == reference.orders
    assert manager.status()[0]["closed_equity"] == reference.equity
    assert len(journal(store, "fill")) == 3
    store.close()


def test_stop_trigger_survives_restart(tmp_path):
    path = tmp_path/"paper.sqlite"
    store, manager = open_manager(path)
    submit(manager)
    manager.tick("eth", tick(1, 1000, 100.))
    manager.tick("eth", tick(2, 1200, 89.))
    store.close()
    store, manager = open_manager(path)
    manager.tick("eth", tick(3, 1300, 88.))
    assert journal(store, "trade")[0]["exit_reason"] == "initial_stop_next_print"
    assert journal(store, "trade")[0]["exit_price"] == 88.
    store.close()


def test_isolation_and_config_lock(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    manager.register("eth-2", "ETHUSDC", CONFIG)
    submit(manager)
    manager.tick("eth", tick(1, 1000, 100.))
    assert manager.status()[1]["position"] is None
    assert manager.status()[1]["closed_equity"] == 1000
    with pytest.raises(ValueError, match="configuration changed"):
        manager.register("eth", "ETHUSDC", replace(CONFIG, tp1_r=2))
    store.close()
    with pytest.raises(ValueError, match="venue mismatch"):
        StateStore(tmp_path/"paper.sqlite", "lighter")


def test_first_candidate_rejected_still_locks_session(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    assert submit(manager, selected=False) == "selector_rejected"
    with pytest.raises(ValueError, match="different first candidate"):
        submit(manager, selected=True)
    manager.tick("eth", tick(1, 1000, 100.))
    assert not journal(store, "fill")
    store.close()


def test_failed_transaction_does_not_consume_fill_or_cursor(tmp_path, monkeypatch):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    submit(manager)
    original = store.record
    def crash(*args):
        original(*args)
        raise RuntimeError("disk/error simulation")
    monkeypatch.setattr(store, "record", crash)
    with pytest.raises(RuntimeError):
        manager.tick("eth", tick(1, 1000, 100.))
    assert manager.status()[0]["last_trade"] is None
    assert manager.status()[0]["position"] is None
    assert not journal(store, "fill")
    monkeypatch.setattr(store, "record", original)
    manager.tick("eth", tick(1, 1000, 100.))
    assert len(journal(store, "fill")) == 1
    store.close()


def test_invalid_events_and_deadline(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    with pytest.raises(ValueError, match="Snapshot"):
        manager.submit("eth", signal(), snapshot={"feature_as_of_ms": 1100}, risk_fraction=.005)
    with pytest.raises(ValueError, match="Symbol/risk"):
        manager.submit("eth", signal(), snapshot={"feature_as_of_ms": 1000}, risk_fraction=.5)
    submit(manager)
    manager.tick("eth", tick(1, 2100, 100.))
    assert journal(store, "decision")[0]["reason"] == "entry_deadline_passed"
    with pytest.raises(ValueError, match="Conflicting"):
        manager.tick("eth", tick(1, 2100, 101.))
    with pytest.raises(ValueError, match="regressed"):
        manager.tick("eth", tick(2, 2000, 100.))
    assert manager.tick("eth", tick(0, 2000, 100.)) == "old_event_ignored"
    store.close()


def test_backup(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    submit(manager)
    store.backup(tmp_path/"snapshot.sqlite")
    with sqlite3.connect(tmp_path/"snapshot.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.backup(tmp_path/"snapshot.sqlite")
    store.close()


def test_dynamic_risk_is_frozen_until_exit(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    manager.submit("eth", signal(), snapshot={"feature_as_of_ms": 1000}, risk_fraction=.0025)
    manager.tick("eth", tick(1, 1000, 100.))
    assert manager.status()[0]["position"]["actual_initial_risk"] == 2.5
    manager.tick("eth", tick(2, 5000, 105.))
    assert journal(store, "trade")[0]["planned_risk_fraction"] == .0025
    store.close()


def test_next_trade_compounds_closed_equity(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    submit(manager)
    manager.tick("eth", tick(1, 1000, 100.))
    manager.tick("eth", tick(2, 5000, 105.))
    equity = manager.status()[0]["closed_equity"]
    s = signal("2026-09-28")
    for k in ("feature_as_of_ms", "entry_eligible_timestamp_ms", "entry_deadline_timestamp_ms", "force_exit_timestamp_ms"):
        s[k] += 10000
    manager.submit("eth", s, snapshot={"feature_as_of_ms": 11000}, risk_fraction=.005)
    manager.tick("eth", tick(3, 11000, 100.))
    assert manager.status()[0]["position"]["actual_initial_risk"] == pytest.approx(equity*.005)
    store.close()


def test_short_matches_shared_engine(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    s = {**signal(), "direction": "short", "stop": 110.}
    manager.submit("eth", s, snapshot={"feature_as_of_ms": 1000}, risk_fraction=.005)
    ticks = [tick(1, 1000, 100.), tick(2, 1500, 90.), tick(3, 1501, 89.), tick(4, 5000, 95.)]
    ref = Portfolio(label="eth", signals=[s], equity=1000.)
    for t in ticks:
        manager.tick("eth", t)
        on_tick(ref, t, CONFIG)
    assert journal(store, "trade") == ref.trades
    store.close()


def test_late_signal_rejected(tmp_path):
    store, manager = open_manager(tmp_path/"paper.sqlite")
    manager.tick("eth", tick(1, 1000, 100.))
    with pytest.raises(ValueError, match="Late signal"):
        submit(manager)
    assert not journal(store, "candidate")
    store.close()
