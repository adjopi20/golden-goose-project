"""Offline paper plumbing: synthetic public prints, no network or exchange IO."""
import ast
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from paper_trading.models.burst_breakout import strategy as paper
from model_development.models.burst_breakout import strategy as research
from utils.engine.state.base import StateStore
from utils.model_registry import verify_model

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "paper_trading/config/burst_hype_16h_b_3m12m_rm_e_48h.json"


def config():
    return json.loads(CONFIG_PATH.read_text())


def open_manager(path):
    return paper.create_paper_manager(path, config())


def signal(direction="long", timestamp=1000):
    return dict(sample_id=f"test-{timestamp}", session_day="1970-01-01", direction=direction,
        stop=90. if direction == "long" else 110., entry_reference=100.,
        feature_as_of_ms=timestamp, feature_available_at_ms=timestamp,
        entry_eligible_timestamp_ms=timestamp, entry_deadline_timestamp_ms=timestamp+60_000,
        force_exit_timestamp_ms=timestamp+48*paper.HOUR)


def trade(seq, timestamp, price, buy=True, qty=1.):
    return dict(symbol="HYPEUSDT", agg_trade_id=seq, timestamp_ms=timestamp,
                price=float(price), quantity=float(qty), buy=buy)


def queue(m, direction="long", timestamp=1000):
    with m.store.transaction():
        assert m._queue(signal(direction,timestamp), timestamp)


def records(m, kind):
    return [json.loads(r[0]) for r in m.store.db.execute(
        "SELECT payload FROM journal WHERE kind=? ORDER BY id", (kind,))]


@pytest.mark.parametrize("direction", ["long", "short"])
def test_rm_e_partial_trailing_restart_and_48h_exit(tmp_path, direction):
    path = tmp_path / "account.sqlite"
    m = open_manager(path)
    side = 1 if direction == "long" else -1
    try:
        queue(m,direction)
        m.on_trade(trade(1,1000,100.),1000)
        assert m.status()["account"]["position"] is None
        m.on_trade(trade(2,1001,100.),1001)
        p = m.status()["account"]["position"]
        assert p["quantity"] == 1.
        assert p["force_exit_timestamp_ms"] == 1001 + 48*paper.HOUR
        m.on_trade(trade(3,1002,100+side*10),1002)
        assert m.status()["account"]["position"]["stop"] == 100-side*10
        assert m.status()["account"]["partial_trigger"] is not None
        m.store.close()
        m = open_manager(path)
        assert m.on_trade(trade(3,1002,100+side*10),1002) == "duplicate"
        m.on_trade(trade(4,1003,100+side*11),1003)
        p = m.status()["account"]["position"]
        assert p["tp1_filled"] and p["remaining_quantity"] == .9
        assert p["stop"] == 100-side*10
        m.on_trade(trade(5,1004,100+side*20),1004)
        assert m.status()["account"]["position"]["stop"] == 100+side*10
        m.on_trade(trade(6,1005,100+side*40),1005)
        assert m.status()["account"]["position"]["stop"] == 100+side*20
        m.store.close()
        m = open_manager(path)
        final_ms = 1001+48*paper.HOUR
        m.on_trade(trade(7,final_ms,100+side*30),final_ms)
        assert m.status()["account"]["position"] is None
        result = records(m,"trade")[0]
        expected_gross = .1*11+.9*30
        expected_fees = (.04 + (100+side*11)*.1*.0004 + (100+side*30)*.9*.0004)
        assert result["gross_pnl_before_costs"] == pytest.approx(expected_gross)
        assert result["fees"] == pytest.approx(expected_fees)
        assert result["pnl"] == pytest.approx(expected_gross-expected_fees)
        assert result["exit_reason"] == "time_exit_next_print"
        assert len(records(m,"fill")) == 3
    finally:
        m.store.close()


def test_stop_remains_poc_after_1r_and_trigger_survives_restart(tmp_path):
    path=tmp_path/"stop.sqlite";m=open_manager(path)
    try:
        queue(m)
        for seq,price in enumerate([100,110,111,100,89],1):
            m.on_trade(trade(seq,1000+seq,price),1000+seq)
        assert m.status()["account"]["stop_trigger"] is not None
        m.store.close();m=open_manager(path)
        m.on_trade(trade(6,1006,88),1006)
        result=records(m,"trade")[0]
        assert result["exit_reason"] == "initial_stop_next_print"
        assert result["remaining_exit_quantity"] == .9
        assert result["gross_pnl_before_costs"] == pytest.approx(1.1-10.8)
    finally:m.store.close()


def test_filled_daily_quota_no_overlap_and_geometric_sizing(tmp_path):
    m=open_manager(tmp_path/"quota.sqlite")
    try:
        queue(m);m.on_trade(trade(1,1001,100),1001)
        with m.store.transaction():assert not m._queue(signal(timestamp=1002),1002)
        m.on_trade(trade(2,1002,89),1002);m.on_trade(trade(3,1003,88),1003)
        with m.store.transaction():assert not m._queue(signal(timestamp=1004),1004)
        nextday=paper.DAY+1000
        with m.store.transaction():assert m._queue(signal(timestamp=nextday),nextday)
        equity=m.status()["account"]["closed_equity"]
        m.on_trade(trade(4,nextday+1,100),nextday+1)
        assert m.status()["account"]["position"]["quantity"] == pytest.approx(equity*.01/10)
    finally:m.store.close()


def test_invalid_prints_and_missing_ids_rollback(tmp_path):
    m=open_manager(tmp_path/"invalid.sqlite")
    try:
        original=trade(1,1000,100);m.on_trade(original,1000)
        state=m.status();cursor=m.get("native_cursor")
        invalid=[(dict(original,quantity=2),1000), (trade(3,1001,101),1001),
                 (trade(2,999,101),1001), (trade(2,1001,101),12000),
                 (dict(trade(2,1001,101),symbol="BTCUSDC"),1001)]
        for row,now in invalid:
            with pytest.raises(ValueError):m.on_trade(row,now)
            assert m.status() == state and m.get("native_cursor") == cursor
        assert m.on_trade(original,1000)=="duplicate"
    finally:m.store.close()


def test_other_account_and_changed_configuration_rejected(tmp_path):
    path=tmp_path/"legacy.sqlite"
    other=StateStore(path,"binance");other.close()
    before=path.read_bytes()
    with pytest.raises(ValueError,match="Not this paper"):
        paper.create_paper_manager(path,config())
    assert path.read_bytes()==before
    altered=config();altered["execution"]["risk_fraction"] = .02
    with pytest.raises(ValueError,match="Configuration differs"):
        paper.create_paper_manager(tmp_path/"new.sqlite",altered)
    assert not (tmp_path/"new.sqlite").exists()


def test_source_and_all_existing_engine_pins_unchanged():
    verify_model(paper.__file__)
    for folder in ["paper_trading/models/pre_ny_hype", "paper_trading/models/pre_ny_bnb",
                   "paper_trading/models/volume_profile_reversion",
                   "live_trading/live_model/btc_mvrv"]:
        verify_model(ROOT/folder/"strategy.py")
    source=ast.parse(Path(paper.__file__).read_text())
    reference=ast.parse(Path(research.__file__).read_text())
    names={"_burst_signals_from_features","assess_breakout_flow","confirm_flow_entries","rm_a_stop","rm_e_stop"}
    extract=lambda t:{n.name:ast.dump(n,include_attributes=False) for n in t.body if isinstance(n,ast.FunctionDef) and n.name in names}
    assert extract(source)==extract(reference)


def test_native_warmup_burst_flow_decision_next_print_and_sliding_profile(tmp_path):
    m=open_manager(tmp_path/"flow.sqlite")
    seq=0
    try:
        # One print per minute is synthetic data, not an OHLC/profile substitution.
        with m.store.transaction():
            for minute in range(26*60+13):
                stamp=minute*paper.MINUTE
                hour=minute//60;within=minute%60
                if hour<25:price=100.+.1*(within%2);qty=1.
                elif hour==25:price=100.+10*within/59;qty=20.
                else:price=110.+.1*within;qty=30.
                seq+=1;m.on_trade(trade(seq,stamp,price,qty=qty),stamp)
        setups=records(m,"setup");assert setups
        candidate=records(m,"candidate")[-1]
        assert candidate["direction"] == "long"
        assert candidate["feature_as_of_ms"] == (26*60+12)*paper.MINUTE
        assert candidate["confirmation_mechanisms"] == ["initiative_candidate"]
        assert m.status()["account"]["position"] is None
        stamp=(26*60+12)*paper.MINUTE+1
        seq+=1;m.on_trade(trade(seq,stamp,111.2,qty=30),stamp)
        position=m.status()["account"]["position"]
        assert position is not None and position["entry_timestamp_ms"] == stamp
        assert position["initial_stop"] == setups[-1]["profile"]["poc"]
        # At hour 27 the profile advances even with that position still open.
        with m.store.transaction():
            for minute in range(26*60+13,27*60+1):
                stamp=minute*paper.MINUTE;seq+=1
                m.on_trade(trade(seq,stamp,111.2,qty=1),stamp)
        assert m.get("latest_profile")["closing"] == 27*paper.HOUR
        assert m.status()["account"]["position"] is not None
        assert len(records(m,"candidate")) == 1
    finally:m.store.close()


def test_prefix_flow_assessment_does_not_require_future_bars():
    stamps=np.arange(26*60)*paper.MINUTE
    prices=100.+np.arange(len(stamps))*.001
    minutes=pd.DataFrame(dict(timestamp_ms=stamps,open=prices,high=prices+.001,
        low=prices,close=prices+.001,buy_volume=np.ones(len(stamps))*2,sell_volume=np.ones(len(stamps))))
    start=24*paper.HOUR
    s=signal(timestamp=start);s.update(entry_reference=float(prices[24*60-1]),stop=90.)
    prefix=minutes[minutes.timestamp_ms<start+12*paper.MINUTE]
    past=paper.assess_breakout_flow(prefix,[s],step_minutes=3,window_minutes=12)
    full=research.assess_breakout_flow(minutes,[s],step_minutes=3,window_minutes=12)
    pd.testing.assert_frame_equal(past[past.assessment_timestamp_ms<=start+12*paper.MINUTE].reset_index(drop=True),
        full[full.assessment_timestamp_ms<=start+12*paper.MINUTE].reset_index(drop=True))


def test_failed_commit_path_rolls_back_fill_and_replays_once(tmp_path, monkeypatch):
    m=open_manager(tmp_path/"rollback.sqlite")
    try:
        queue(m)
        original=m._record
        def fail(kind,payload):
            if kind=="position_open":raise RuntimeError("simulated journal failure")
            return original(kind,payload)
        monkeypatch.setattr(m,"_record",fail)
        t=trade(1,1001,100)
        with pytest.raises(RuntimeError,match="journal failure"):m.on_trade(t,1001)
        assert not records(m,"fill")
        assert m.get("native_cursor") is None
        assert m.status()["account"]["position"] is None
        assert m.status()["account"]["pending_candidates"] == 1
        monkeypatch.setattr(m,"_record",original)
        assert m.on_trade(t,1001)=="processed"
        assert m.on_trade(t,1001)=="duplicate"
        assert len(records(m,"fill")) == 1
    finally:m.store.close()


def test_single_writer_and_pending_candidate_restart(tmp_path):
    path=tmp_path/"writer.sqlite";m=open_manager(path)
    try:
        queue(m)
        with pytest.raises((OSError,PermissionError)):
            open_manager(path)
        m.store.close();m=open_manager(path)
        assert m.status()["account"]["pending_candidates"] == 1
        m.on_trade(trade(1,1001,100),1001)
        assert len(records(m,"fill")) == 1
    finally:m.store.close()


def test_burst_during_position_is_observed_then_blocked_only_at_entry_time(tmp_path):
    m=open_manager(tmp_path/"observe_while_open.sqlite")
    try:
        queue(m);m.on_trade(trade(1,1001,100),1001)
        closing=41*paper.HOUR
        with m.store.transaction():
            for stamp in range(closing-17*paper.HOUR,closing,paper.MINUTE):
                inside=stamp-(closing-paper.HOUR)
                price=100. if inside<0 else 100.+10.*inside/(59*paper.MINUTE)
                qty=1. if inside<0 else 20.
                m.store.db.execute("INSERT INTO burst_minutes VALUES (?,?,?,?,?,?,?)",
                                   (stamp,price,price,price,price,qty,0.))
            for hour in range(closing-17*paper.HOUR,closing-paper.HOUR,paper.HOUR):
                m.store.db.execute("INSERT INTO burst_prices VALUES (?,?,?)",(hour,100.,30.))
                m.store.db.execute("INSERT INTO burst_prices VALUES (?,?,?)",(hour,100.1,30.))
            setup=m._hour_setup(closing,closing)
            assert setup is not None and setup["direction"]=="long"
            assert not m._queue(setup,closing)
        assert m.status()["account"]["position"] is not None
        assert records(m,"blocked_setup")[-1]["reason"]=="position_or_entry_pending"
    finally:m.store.close()


def test_trailing_exit_classification_and_risk_lock_restart(tmp_path):
    path=tmp_path/"locked.sqlite";m=open_manager(path)
    try:
        queue(m)
        m.on_trade(trade(1,1001,100),1001)
        m.on_trade(trade(2,1002,120),1002)
        m.on_trade(trade(3,1003,121),1003)
        m.on_trade(trade(4,1004,109),1004)
        m.store.close();m=open_manager(path)
        m.on_trade(trade(5,1005,108),1005)
        close=records(m,"position_closed")[-1]
        assert close["exit_reason"]=="risk_lock_stop_next_print"
        assert close["initial_stop"]==90. and close["final_stop"]==110.
        assert close["risk_stop_updates"]==1
    finally:m.store.close()
