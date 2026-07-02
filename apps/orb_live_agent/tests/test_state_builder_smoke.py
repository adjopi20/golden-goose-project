from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.config import AgentConfig
from orb_live_agent.ai_decision import AiDecisionService
from orb_live_agent.state_builder import LiveStateBuilder
from orb_live_agent.storage import JsonlStorage
from orb_live_agent.trigger_observer import observe_triggers

def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        symbol="AVAXUSDC",
        stream_base="wss://example.invalid/stream",
        log_dir=tmp_path,
        ai_provider="stub",
        ai_live_calls_enabled=False,
        ai_base_url="https://api.deepseek.com",
        ai_model="deepseek-v4-pro",
        rules_file=ROOT / "models" / "orb" / "model" / "checkpoint_ai_assisted_main_benchmark.md",
        max_ai_calls_per_day=150,
        session_timezone="America/New_York",
        ny_open_time="09:30",
        setup_cutoff_time="17:30",
        overnight_start_time="17:30",
        pre_ny_start_time="01:30",
        volume_profile_bins=10,
        bubble_lookback_min_trades=1,
        bubble_percentile=0.95,
        bubble_min_qty=10.0,
        bubble_min_notional=None,
        paper_initial_equity=1000.0,
        paper_risk_fraction=0.05,
        audit_kline_1m=False,
    )


def _ms_ny(year: int, month: int, day: int, hour: int, minute: int) -> int:
    dt = datetime(year, month, day, hour, minute, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def test_closed_candle_delta_profile_extremes_and_bubble(tmp_path: Path) -> None:
    state = LiveStateBuilder(_config(tmp_path))
    trades = [
        {"timestamp": 1_719_813_000_000, "price": 25.0, "qty": 12.0, "is_buyer_maker": False},
        {"timestamp": 1_719_813_030_000, "price": 26.0, "qty": 5.0, "is_buyer_maker": True},
        {"timestamp": 1_719_813_060_000, "price": 24.5, "qty": 11.0, "is_buyer_maker": False},
    ]

    assert state.push_trade(trades[0]) is None
    assert state.push_trade(trades[1]) is None
    closed = state.push_trade(trades[2])

    assert closed is not None
    assert closed.candle["open"] == 25.0
    assert closed.candle["high"] == 26.0
    assert closed.candle["low"] == 25.0
    assert closed.candle["close"] == 26.0
    assert closed.candle["delta"] == 7.0
    assert closed.bubbles[0]["qty"] == 12.0
    assert closed.snapshot["previous_24h_profile_for_session"] is None
    assert closed.snapshot["session_extremes"]["pre_ny"]["high"] == 26.0
    assert closed.snapshot["setup_observation_active"] is False
    assert state.is_setup_observation_active(_ms_ny(2024, 7, 1, 9, 31)) is True
    assert state.is_setup_observation_active(_ms_ny(2024, 7, 1, 17, 29)) is True
    assert state.is_setup_observation_active(_ms_ny(2024, 7, 1, 17, 30)) is False


def test_previous_24h_profile_is_frozen_at_ny_open(tmp_path: Path) -> None:
    state = LiveStateBuilder(_config(tmp_path))
    trades = [
        {"timestamp": _ms_ny(2024, 6, 30, 9, 31), "price": 10.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 29), "price": 20.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 31), "price": 100.0, "qty": 50.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 32), "price": 101.0, "qty": 50.0, "is_buyer_maker": False},
    ]

    for trade in trades[:3]:
        state.push_trade(trade)
    closed = state.push_trade(trades[3])

    assert closed is not None
    profile = closed.snapshot["previous_24h_profile_for_session"]
    assert profile["profile_type"] == "previous_24h_profile_for_session"
    assert profile["frozen_at_session_open"] is True
    assert profile["session_low"] == 10.0
    assert profile["session_high"] == 20.0
    assert profile["total_volume"] == 2.0


def test_trigger_observer_reports_without_gating_ai(tmp_path: Path) -> None:
    state = LiveStateBuilder(_config(tmp_path))
    trades = [
        {"timestamp": _ms_ny(2024, 7, 1, 1, 31), "price": 25.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 31), "price": 26.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 32), "price": 25.0, "qty": 12.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 32), "price": 27.0, "qty": 11.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 33), "price": 27.0, "qty": 1.0, "is_buyer_maker": False},
    ]

    for trade in trades[:4]:
        state.push_trade(trade)
    closed = state.push_trade(trades[4])

    assert closed is not None
    observation = observe_triggers(closed.snapshot, closed.bubbles, closed.trigger_reference_levels)
    assert observation["mode"] == "observe_only"
    assert observation["triggered"] is True
    assert "pre_ny_high_touched" in observation["reasons"]
    assert "pre_ny_high_closed_above" in observation["reasons"]
    assert "order_bubble_in_closed_minute" in observation["reasons"]


def test_ai_provider_key_can_be_present_without_live_calls(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = AiDecisionService(config)
    decision = service.decide({"snapshot_timestamp_ms": 123}, {"triggered": True})
    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "stub_ai_provider"


def test_storage_bootstrap_reads_recent_raw_trades(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    old_trade = {"timestamp": _ms_ny(2024, 6, 30, 7, 59), "agg_trade_id": 1, "price": 1.0, "qty": 1.0, "is_buyer_maker": False}
    recent_trade = {"timestamp": _ms_ny(2024, 7, 1, 8, 0), "agg_trade_id": 2, "price": 2.0, "qty": 1.0, "is_buyer_maker": False}
    latest_trade = {"timestamp": _ms_ny(2024, 7, 1, 9, 0), "agg_trade_id": 3, "price": 3.0, "qty": 1.0, "is_buyer_maker": False}
    storage.write("raw_aggtrade", old_trade)
    storage.write("raw_aggtrade", latest_trade)
    storage.write("raw_aggtrade", recent_trade)

    rows = storage.read_recent_raw_aggtrades(lookback_hours=25)

    assert [row["agg_trade_id"] for row in rows] == [2, 3]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_closed_candle_delta_profile_extremes_and_bubble(tmp_path)
        test_previous_24h_profile_is_frozen_at_ny_open(tmp_path)
        test_trigger_observer_reports_without_gating_ai(tmp_path)
        test_ai_provider_key_can_be_present_without_live_calls(tmp_path)
        test_storage_bootstrap_reads_recent_raw_trades(tmp_path)
    print("orb_live_agent smoke check passed")
