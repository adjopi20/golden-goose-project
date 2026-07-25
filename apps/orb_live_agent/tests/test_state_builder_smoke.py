from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.config import AgentConfig
from orb_live_agent.ai_decision import AiDecisionService
from orb_live_agent.fast_orb_backtest import run as run_fast_backtest, run_with_features
from orb_live_agent.fast_orb_sweep import combinations, parse_grid
from orb_live_agent.feature_cache import FeatureSet
from orb_live_agent.main import _effective_config, _execution_decision, _pre_ai_wait_decision
from orb_live_agent.paper_broker import PaperBroker
from orb_live_agent.regime import build_or_load_regime_cache, frozen_regime_for_session
from orb_live_agent.risk_gate import RiskGate
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
        ai_max_tokens=384000,
        ai_timeout_seconds=300,
        rules_file=ROOT / "apps" / "orb_live_agent" / "rules" / "trend_following_orb.md",
        max_ai_calls_per_day=150,
        session_timezone="America/New_York",
        ny_open_time="09:30",
        setup_cutoff_time="17:30",
        overnight_start_time="17:30",
        pre_ny_start_time="01:30",
        orb_session_start_time="09:30",
        orb_entry_start_time="09:45",
        volume_profile_bins=10,
        bubble_lookback_min_trades=1,
        bubble_percentile=0.95,
        bubble_min_qty=10.0,
        bubble_min_notional=None,
        orb_entry_window_minutes=30,
        orb_min_volume_expansion_ratio=2.0,
        orb_min_supportive_bubble_qty_ratio=None,
        orb_min_candidate_body_ratio=0.35,
        orb_short_max_close_position=0.45,
        orb_long_min_close_position=0.55,
        orb_require_directional_delta=True,
        orb_min_preentry_delta_ratio=0.05,
        orb_preentry_delta_lookback_minutes=15,
        orb_opposite_touch_policy="strict",
        orb_direct_min_body_ratio=0.65,
        orb_direct_short_max_close_position=0.30,
        orb_direct_long_min_close_position=0.70,
        orb_direct_min_range_ratio=1.5,
        orb_direct_min_delta_ratio=0.85,
        orb_stop_model="opposite_extreme",
        paper_initial_equity=1000.0,
        paper_risk_fraction=0.05,
        paper_fee_bps=4.0,
        paper_slippage_bps=5.0,
        paper_min_stop_risk_pct=0.0015,
        paper_max_stop_risk_pct=0.025,
        paper_tp1_r=4.0,
        paper_tp1_fraction=0.5,
        paper_runner_trail_tp1_fraction=0.5,
        paper_exit_mode="tp1_trail",
        paper_trail_activation_r=4.0,
        paper_trail_distance_r=2.0,
        paper_protection_enabled=True,
        paper_protection_activation_r=1.0,
        paper_protection_stop_r=0.0,
        paper_protection_fraction=0.0,
        paper_max_hold_exit_time="01:30",
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


def test_dynamic_bubble_threshold_is_computed_once_for_closed_minute(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        bubble_min_qty=None,
        bubble_min_notional=None,
        bubble_lookback_min_trades=2,
        bubble_percentile=0.5,
    )
    state = LiveStateBuilder(config)
    trades = [
        {"timestamp": _ms_ny(2024, 7, 1, 1, 0) + 10_000, "price": 10.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 0) + 20_000, "price": 10.0, "qty": 10.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 1) + 5_000, "price": 10.0, "qty": 6.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 1, 2), "price": 10.0, "qty": 1.0, "is_buyer_maker": False},
    ]

    state.push_trade(trades[0])
    state.push_trade(trades[1])
    assert state.push_trade(trades[2]).bubbles == []
    closed = state.push_trade(trades[3])

    assert closed is not None
    assert len(closed.bubbles) == 1
    assert closed.bubbles[0]["qty"] == 6.0
    assert closed.bubbles[0]["min_qty"] == 5.5


def test_previous_24h_profile_is_frozen_at_ny_open(tmp_path: Path) -> None:
    state = LiveStateBuilder(_config(tmp_path))
    trades = [
        {"timestamp": _ms_ny(2024, 6, 30, 9, 29), "price": 15.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 6, 30, 9, 31), "price": 10.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 29), "price": 20.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 31), "price": 100.0, "qty": 50.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 32), "price": 101.0, "qty": 50.0, "is_buyer_maker": False},
    ]

    for trade in trades[:4]:
        state.push_trade(trade)
    closed = state.push_trade(trades[4])

    assert closed is not None
    profile = closed.snapshot["previous_24h_profile_for_session"]
    assert profile["profile_type"] == "previous_24h_profile_for_session"
    assert profile["frozen_at_session_open"] is True
    assert profile["session_low"] == 10.0
    assert profile["session_high"] == 20.0
    assert profile["total_volume"] == 2.0


def test_ny_first_15m_profile_is_frozen_after_window_end(tmp_path: Path) -> None:
    state = LiveStateBuilder(_config(tmp_path))
    trades = [
        {"timestamp": _ms_ny(2024, 7, 1, 9, 30), "price": 10.001, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 31), "price": 11.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 44), "price": 12.003, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 45), "price": 100.0, "qty": 50.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 46), "price": 101.0, "qty": 50.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 47), "price": 200.0, "qty": 50.0, "is_buyer_maker": False},
    ]

    assert state.push_trade(trades[0]) is None
    assert state.push_trade(trades[1]).snapshot["ny_first_15m_profile"] is None
    assert state.push_trade(trades[2]).snapshot["ny_first_15m_profile"] is None
    assert state.push_trade(trades[3]).snapshot["ny_first_15m_profile"] is None
    profile = state.push_trade(trades[4]).snapshot["ny_first_15m_profile"]
    assert profile["profile_type"] == "ny_first_15m_profile"
    assert profile["frozen_at_window_end"] is True
    assert profile["session_low"] == 10.001
    assert profile["session_high"] == 12.003
    assert state.push_trade(trades[5]).snapshot["ny_first_15m_profile"]["session_high"] == 12.003


def test_live_execution_uses_current_trade_price_and_logs_config(tmp_path: Path) -> None:
    signal = {"decision": "TAKE", "entry": 6.60, "stop_loss": 6.55}
    execution = _execution_decision(signal, 6.61)

    assert signal["entry"] == 6.60
    assert execution["signal_entry"] == 6.60
    assert execution["entry"] == 6.61
    assert _effective_config(_config(tmp_path))["log_dir"] == str(tmp_path)


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
    assert observation["orderflow_features"]["bubble_count"] == 2
    assert observation["orderflow_features"]["buy_bubble_count"] == 2
    assert observation["orderflow_features"]["sell_bubble_count"] == 0
    assert observation["orderflow_features"]["buy_bubble_qty"] == 23.0
    assert observation["orderflow_features"]["max_bubble_qty"] == 12.0
    assert observation["orderflow_features"]["max_bubble_side"] == "buy"


def test_ai_provider_key_can_be_present_without_live_calls(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = AiDecisionService(config)
    decision = service.decide({"snapshot_timestamp_ms": 123}, {"triggered": True})
    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "stub_ai_provider"


def test_algorithm_provider_takes_short_retest_continuation_without_api(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 99.0, "high": 101.0, "low": 98.5, "body": 1.5, "range": 2.5, "delta": -100.0},
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "open": 101.0, "close": 99.0, "high": 101.2, "low": 98.8, "body": 2.0, "range": 2.4, "delta": -100.0, "volume": 100.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "close": 99.8, "high": 100.2, "low": 99.2, "delta": -40.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 0), "high": 101.0, "low": 98.5, "delta": -100.0, "volume": 100.0},
        ],
        "session_extremes": {},
        "previous_24h_profile_for_session": None,
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "TAKE"
    assert decision["provider"] == "algorithm"
    assert decision["entry_model"] == "trend"
    assert decision["direction"] == "short"
    assert decision["stop_loss"] == 105.0
    assert decision["invalidation"] == "opposite ORB high 105.000000"
    assert service.last_request_body is None


def test_algorithm_provider_waits_for_retest_after_first_breakout(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 99.0, "high": 101.0, "low": 98.5, "body": 1.5, "range": 2.5, "delta": -100.0},
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "close": 100.5, "high": 101.0, "low": 100.1, "delta": -100.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "close": 99.0, "high": 101.0, "low": 98.5, "delta": -100.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "wait_for_first_price_inefficiency"


def test_algorithm_provider_takes_direct_displacement_breakout(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 99.0,
            "high": 101.0,
            "low": 98.5,
            "body": 1.8,
            "range": 2.5,
            "delta": -90.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "close": 100.5, "high": 101.0, "low": 100.0, "range": 1.0, "delta": -100.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "close": 99.0, "high": 101.0, "low": 98.5, "range": 2.5, "delta": -90.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    trigger = {
        "triggered": True,
        "reasons": ["ny_first_15m_profile_low_closed_below"],
        "orderflow_features": {"buy_bubble_count": 0, "sell_bubble_count": 2, "sell_bubble_qty": 42.0},
    }
    decision = service.decide(snapshot, trigger)

    assert decision["decision"] == "TAKE"
    assert decision["reason"] == "algorithm_short_orb_continuation_ny_first_15m_session_low"
    assert decision["orderflow_features"]["sell_bubble_qty"] == 42.0


def test_algorithm_provider_rejects_low_volume_expansion(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 99.0,
            "high": 101.0,
            "low": 98.5,
            "body": 1.8,
            "range": 2.5,
            "delta": -90.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "close": 100.5, "high": 101.0, "low": 100.0, "range": 1.0, "delta": -100.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "close": 99.0, "high": 101.0, "low": 98.5, "range": 2.5, "delta": -90.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": 2.0,
    }

    decision = service.decide(
        snapshot,
        {
            "triggered": True,
            "reasons": ["ny_first_15m_profile_low_closed_below"],
            "orderflow_features": {"volume_expansion_ratio": 1.5},
        },
    )

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "reject_low_volume_expansion"


def test_algorithm_provider_rejects_later_direction_against_fastest_inefficiency(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 99.0,
            "high": 101.0,
            "low": 98.5,
            "body": 1.8,
            "range": 2.5,
            "delta": -90.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "open": 104.0, "close": 106.0, "high": 106.5, "low": 104.0, "body": 2.0, "range": 2.5, "delta": 100.0, "volume": 100.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 0), "close": 99.0, "high": 101.0, "low": 98.5, "range": 2.5, "delta": -90.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "reject_short_not_fastest_price_inefficiency"


def test_algorithm_provider_blocks_orb_after_bias_window(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 15),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 99.0, "high": 101.0, "low": 98.5, "body": 1.8, "range": 2.5, "delta": -100.0, "volume": 100.0},
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "outside_orb_bias_window"


def test_algorithm_provider_uses_configured_orb_entry_window(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 20),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 99.0, "high": 101.0, "low": 98.5, "body": 1.8, "range": 2.5, "delta": -100.0, "volume": 100.0},
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "close": 99.0, "high": 101.0, "low": 98.5, "body": 1.8, "range": 2.5, "delta": -90.0, "volume": 100.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 10), "close": 99.8, "high": 100.2, "low": 99.2, "delta": -40.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 20), "close": 99.0, "high": 101.0, "low": 98.5, "delta": -100.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_entry_window_minutes": 45,
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "TAKE"
    assert decision["direction"] == "short"


def test_algorithm_provider_rejects_weak_supportive_bubble_ratio(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 99.0,
            "high": 101.0,
            "low": 98.5,
            "body": 1.8,
            "range": 2.5,
            "delta": -90.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "close": 100.5, "high": 101.0, "low": 100.0, "range": 1.0, "delta": -100.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "close": 99.0, "high": 101.0, "low": 98.5, "range": 2.5, "delta": -90.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": 2.0,
        "orb_min_supportive_bubble_qty_ratio": 1.0,
    }

    decision = service.decide(
        snapshot,
        {
            "triggered": True,
            "reasons": ["ny_first_15m_profile_low_closed_below"],
            "orderflow_features": {
                "volume_expansion_ratio": 2.5,
                "buy_bubble_qty": 100.0,
                "sell_bubble_qty": 50.0,
            },
        },
    )

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "reject_weak_supportive_bubble_ratio"


def test_algorithm_provider_rejects_weak_pre_entry_delta(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 99.0, "high": 101.0, "low": 98.5, "body": 1.8, "range": 2.5, "delta": -100.0, "volume": 100.0},
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 45), "high": 99.0, "low": 98.0, "delta": -1.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 0), "high": 101.0, "low": 98.5, "delta": -100.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]})

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "reject_weak_pre_entry_short_delta"


def test_algorithm_provider_rejects_prior_opposite_orb_touch(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
        "session_timezone": "America/New_York",
        "last_candle": {"close": 106.0, "high": 106.5, "low": 104.0, "body": 2.0, "range": 2.5, "delta": 100.0},
        "recent_candles": [
            {"timestamp_ms": _ms_ny(2024, 7, 1, 9, 50), "high": 104.0, "low": 99.0, "delta": 100.0, "volume": 200.0},
            {"timestamp_ms": _ms_ny(2024, 7, 1, 10, 0), "high": 106.5, "low": 104.0, "delta": 100.0, "volume": 100.0},
        ],
        "ny_first_15m_profile": {"session_low": 100.0, "session_high": 105.0},
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_high_closed_above"]})

    assert decision["decision"] == "WAIT"
    assert decision["reason"] == "reject_opposite_orb_touched_before_long"


def test_algorithm_provider_allows_displacement_after_opposite_orb_touch(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    snapshot = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 106.0,
            "high": 106.5,
            "low": 104.0,
            "body": 2.0,
            "range": 2.5,
            "delta": 100.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {
                "timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
                "close": 103.0,
                "high": 104.0,
                "low": 99.0,
                "body": 1.0,
                "range": 5.0,
                "delta": 100.0,
                "volume": 200.0,
            },
            {
                "timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
                "close": 106.0,
                "high": 106.5,
                "low": 104.0,
                "body": 2.0,
                "range": 2.5,
                "delta": 100.0,
                "volume": 100.0,
            },
        ],
        "ny_first_15m_profile": {
            "session_low": 100.0,
            "session_high": 105.0,
            "poc_price": 102.0,
            "val": 101.0,
            "vah": 104.0,
        },
        "orb_opposite_touch_policy": "displacement_override",
        "orb_min_volume_expansion_ratio": None,
    }

    decision = service.decide(snapshot, {"triggered": True, "reasons": ["ny_first_15m_profile_high_closed_above"]})

    assert decision["decision"] == "TAKE"
    assert decision["direction"] == "long"


def test_algorithm_provider_uses_configured_orb_stop_model(tmp_path: Path) -> None:
    service = AiDecisionService(replace(_config(tmp_path), ai_provider="algorithm"))
    base = {
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
        "session_timezone": "America/New_York",
        "last_candle": {
            "close": 99.0,
            "high": 101.0,
            "low": 98.5,
            "body": 1.8,
            "range": 2.5,
            "delta": -90.0,
            "volume": 100.0,
        },
        "recent_candles": [
            {
                "timestamp_ms": _ms_ny(2024, 7, 1, 9, 45),
                "close": 100.5,
                "high": 101.0,
                "low": 100.0,
                "range": 1.0,
                "delta": -100.0,
                "volume": 200.0,
            },
            {
                "timestamp_ms": _ms_ny(2024, 7, 1, 9, 50),
                "close": 99.0,
                "high": 101.0,
                "low": 98.5,
                "range": 2.5,
                "delta": -90.0,
                "volume": 100.0,
            },
        ],
        "ny_first_15m_profile": {
            "session_low": 100.0,
            "session_high": 105.0,
            "poc_price": 102.0,
            "val": 101.0,
            "vah": 104.0,
        },
        "orb_min_volume_expansion_ratio": None,
    }
    trigger = {"triggered": True, "reasons": ["ny_first_15m_profile_low_closed_below"]}

    poc = service.decide({**base, "orb_stop_model": "poc"}, trigger)
    value_area = service.decide({**base, "orb_stop_model": "opposite_value_area"}, trigger)

    assert poc["decision"] == "TAKE"
    assert poc["stop_loss"] == 102.0
    assert value_area["decision"] == "TAKE"
    assert value_area["stop_loss"] == 104.0


def test_pre_ai_wait_blocks_incomplete_required_profiles() -> None:
    base = {
        "setup_observation_active": True,
        "snapshot_timestamp_ms": 123,
        "previous_24h_profile_for_session": None,
        "ny_first_15m_profile": {"session_low": 1.0, "session_high": 2.0},
    }
    assert _pre_ai_wait_decision(base)["reason"] == "missing_previous_24h_profile"
    base["previous_24h_profile_for_session"] = {"poc_price": 1.5}
    base["ny_first_15m_profile"] = None
    assert _pre_ai_wait_decision(base)["reason"] == "missing_ny_first_15m_profile"
    base["ny_first_15m_profile"] = {"session_low": 1.0, "session_high": 2.0}
    assert _pre_ai_wait_decision(base) is None


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


def test_paper_broker_uses_benchmark_tp1_and_runner_trailing(tmp_path: Path) -> None:
    broker = PaperBroker(_config(tmp_path))
    decision = {
        "decision": "TAKE",
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 99.0,
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
    }
    opened = broker.on_decision(decision, {"accepted": True})
    assert opened["entry_fill"] > 100.0
    assert opened["entry_fee"] > 0.0
    assert broker.position is not None
    assert broker.position.tp1_hit is False

    quiet_events = broker.on_candle({"high": 101.0, "low": 100.5})
    assert quiet_events == []
    assert broker.position is not None
    assert broker.position.tp1_hit is False

    protection_events = broker.on_candle({"high": broker.position.entry + broker.position.initial_risk, "low": 100.5})
    assert protection_events[0]["event"] == "paper_protection_update"
    assert protection_events[0]["reason"] == "one_r_entry_protection"
    assert broker.position.protection_hit is True
    assert broker.position.stop_loss == broker.position.entry

    tp1_events = broker.on_candle({"high": 105.0, "low": 100.5})
    assert tp1_events[0]["event"] == "paper_tp1"
    assert tp1_events[0]["reason"] == "tp1"
    assert broker.position is not None
    assert broker.position.tp1_hit is True
    assert broker.position.qty_open == broker.position.qty_total * 0.5
    assert broker.position.runner_stop is not None

    runner_stop = float(broker.position.runner_stop)
    close_events = broker.on_candle({"high": 105.5, "low": runner_stop - 0.01})
    assert close_events[-1]["event"] == "paper_close"
    assert close_events[-1]["reason"] == "runner_trailing_stop"
    assert close_events[-1]["exit_fill"] < close_events[-1]["exit_requested"]
    assert close_events[-1]["exit_fee"] > 0.0
    assert broker.has_open_position() is False


def test_paper_broker_closes_at_entry_after_one_r_protection(tmp_path: Path) -> None:
    broker = PaperBroker(_config(tmp_path))
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    broker.on_candle({"high": broker.position.entry + broker.position.initial_risk, "low": 100.5})
    closed = broker.on_candle({"high": broker.position.entry + 0.1, "low": broker.position.entry - 0.01})

    assert closed[0]["event"] == "paper_close"
    assert closed[0]["reason"] == "protected_stop"
    assert closed[0]["exit_requested"] == closed[0]["position"]["entry"]
    assert broker.has_open_position() is False


def test_paper_broker_supports_trailing_only(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        paper_exit_mode="trail_only",
        paper_trail_activation_r=2.0,
        paper_trail_distance_r=1.0,
        paper_protection_enabled=False,
    )
    broker = PaperBroker(config)
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    activation = broker.position.trail_activation_price
    activated = broker.on_candle({"high": activation, "low": broker.position.entry})

    assert activated[0]["event"] == "paper_trail_activation"
    assert broker.position is not None
    assert broker.position.tp1_hit is False
    assert broker.position.qty_open == broker.position.qty_total

    runner_stop = float(broker.position.runner_stop)
    closed = broker.on_candle({"high": activation, "low": runner_stop})
    assert closed[-1]["event"] == "paper_close"
    assert closed[-1]["reason"] == "runner_trailing_stop"


def test_paper_broker_supports_configurable_protection(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        paper_protection_activation_r=2.0,
        paper_protection_stop_r=0.5,
    )
    broker = PaperBroker(config)
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    entry = broker.position.entry
    risk = broker.position.initial_risk
    assert broker.on_candle({"high": entry + risk, "low": entry}) == []

    protected = broker.on_candle({"high": entry + 2.0 * risk, "low": entry})
    assert protected[0]["event"] == "paper_protection_update"
    assert broker.position is not None
    assert broker.position.stop_loss == entry + 0.5 * risk


def test_paper_broker_can_scale_out_at_protection_r(tmp_path: Path) -> None:
    broker = PaperBroker(replace(_config(tmp_path), paper_protection_fraction=0.25))
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    qty_total = broker.position.qty_total
    initial_stop = broker.position.stop_loss
    protection_price = broker.position.entry + broker.position.initial_risk

    protected = broker.on_candle({"high": protection_price, "low": broker.position.entry})

    assert protected[0]["event"] == "paper_tp1"
    assert protected[0]["reason"] == "protection_scale_out"
    assert protected[0]["qty_closed"] == qty_total * 0.25
    assert broker.position is not None
    assert broker.position.qty_open == qty_total * 0.75
    assert broker.position.protection_hit is True
    assert broker.position.protection_scaled_out is True
    assert broker.position.tp1_hit is False
    assert broker.position.trail_active is False
    assert broker.position.stop_loss == initial_stop

    tp1 = broker.on_candle({"high": broker.position.tp1_price, "low": broker.position.entry})

    assert tp1[0]["event"] == "paper_tp1"
    assert tp1[0]["reason"] == "tp1"
    assert tp1[0]["qty_closed"] == qty_total * 0.5
    assert broker.position is not None
    assert abs(broker.position.qty_open - qty_total * 0.25) < 1e-9
    assert broker.position.tp1_hit is True
    assert broker.position.trail_active is True


def test_scale_out_then_original_stop_keeps_initial_stop_reason(tmp_path: Path) -> None:
    broker = PaperBroker(replace(_config(tmp_path), paper_protection_fraction=0.25))
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    protection_price = broker.position.entry + broker.position.initial_risk
    broker.on_candle({"high": protection_price, "low": broker.position.entry})

    stopped = broker.on_candle({"high": broker.position.entry, "low": broker.position.stop_loss})

    assert stopped[0]["event"] == "paper_close"
    assert stopped[0]["reason"] == "initial_stop"
    assert broker.has_open_position() is False


def test_paper_broker_applies_tp1_and_protection_atomically_at_same_price(tmp_path: Path) -> None:
    broker = PaperBroker(replace(_config(tmp_path), paper_tp1_r=1.0))
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    tp1 = broker.position.tp1_price
    events = broker.on_candle({"high": tp1, "low": broker.position.entry})

    assert events[0]["event"] == "paper_tp1"
    assert events[0]["protection_applied"] is True
    assert broker.position is not None
    assert broker.position.protection_hit is True
    assert broker.position.tp1_hit is True
    assert broker.position.trail_active is True
    assert broker.position.stop_loss == broker.position.entry


def test_paper_broker_can_disable_protection(tmp_path: Path) -> None:
    broker = PaperBroker(replace(_config(tmp_path), paper_protection_enabled=False))
    broker.on_decision(
        {
            "decision": "TAKE",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 9, 46),
        },
        {"accepted": True},
    )
    assert broker.position is not None
    initial_stop = broker.position.stop_loss
    events = broker.on_candle(
        {"high": broker.position.entry + broker.position.initial_risk, "low": broker.position.entry}
    )

    assert events == []
    assert broker.position.stop_loss == initial_stop


def test_paper_broker_force_exits_at_next_overnight_end(tmp_path: Path) -> None:
    broker = PaperBroker(_config(tmp_path))
    decision = {
        "decision": "TAKE",
        "direction": "short",
        "entry": 100.0,
        "stop_loss": 110.0,
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 10, 0),
    }
    broker.on_decision(decision, {"accepted": True})

    assert broker.on_trade({"timestamp": _ms_ny(2024, 7, 2, 1, 29), "price": 95.0}) == []
    closed = broker.on_trade({"timestamp": _ms_ny(2024, 7, 2, 1, 30), "price": 95.0})

    assert closed[0]["event"] == "paper_close"
    assert closed[0]["reason"] == "overnight_time_invalidation"
    assert broker.has_open_position() is False


def test_paper_broker_uses_profile_targets_for_mean_reversion(tmp_path: Path) -> None:
    broker = PaperBroker(_config(tmp_path))
    decision = {
        "decision": "TAKE",
        "entry_model": "mean_reversion",
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 99.0,
        "snapshot_timestamp_ms": _ms_ny(2024, 7, 1, 14, 23),
    }
    snapshot = {"previous_24h_profile_for_session": {"poc_price": 102.0, "vah": 103.0, "val": 97.0}}
    opened = broker.on_decision(decision, {"accepted": True}, snapshot)

    assert opened["position"]["entry_model"] == "mean_reversion"
    assert opened["position"]["tp1_price"] == 102.0
    assert opened["position"]["tp2_price"] == 103.0

    tp1 = broker.on_candle({"high": 102.1, "low": 100.0})
    assert tp1[0]["event"] == "paper_tp1"
    assert broker.position is not None
    assert broker.position.runner_stop == broker.position.entry

    closed = broker.on_candle({"high": 103.1, "low": 101.0})
    assert closed[0]["event"] == "paper_close"
    assert closed[0]["reason"] == "tp2"
    assert broker.has_open_position() is False


def test_paper_broker_rejects_take_without_snapshot_timestamp(tmp_path: Path) -> None:
    broker = PaperBroker(_config(tmp_path))
    rejected = broker.on_decision(
        {"decision": "TAKE", "direction": "long", "entry": 100.0, "stop_loss": 99.0},
        {"accepted": True},
    )
    assert rejected["reason"] == "missing_snapshot_timestamp_ms"


def test_risk_gate_rejects_non_trend_entry_model() -> None:
    rejected = RiskGate().validate(
        {
            "decision": "TAKE",
            "entry_model": "mean_reversion",
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
        },
        has_open_position=False,
    )

    assert rejected["reason"] == "unsupported_entry_model"


def test_risk_gate_enforces_stop_risk_bounds() -> None:
    gate = RiskGate(min_stop_risk_pct=0.0015, max_stop_risk_pct=0.025)

    tight = gate.validate({"decision": "TAKE", "direction": "long", "entry": 100.0, "stop_loss": 99.9}, False)
    wide = gate.validate({"decision": "TAKE", "direction": "short", "entry": 100.0, "stop_loss": 103.0}, False)
    accepted = gate.validate({"decision": "TAKE", "direction": "long", "entry": 100.0, "stop_loss": 98.0}, False)

    assert tight["reason"] == "stop_risk_below_min"
    assert wide["reason"] == "stop_risk_above_max"
    assert accepted["reason"] == "accepted"


def test_fast_backtest_cache_matches_uncached(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SYMBOL", "AVAXUSDC")
    monkeypatch.setenv("SESSION_TIMEZONE", "America/New_York")
    monkeypatch.setenv("NY_OPEN_TIME", "09:30")
    monkeypatch.setenv("SETUP_CUTOFF_TIME", "17:30")
    monkeypatch.setenv("OVERNIGHT_START_TIME", "17:30")
    monkeypatch.setenv("PRE_NY_START_TIME", "01:30")
    monkeypatch.setenv("VOLUME_PROFILE_BINS", "10")
    monkeypatch.setenv("BUBBLE_PERCENTILE", "0.95")
    monkeypatch.setenv("BUBBLE_LOOKBACK_MIN_TRADES", "1")
    monkeypatch.setenv("ORB_ENTRY_WINDOW_MINUTES", "30")
    monkeypatch.setenv("ORB_MIN_VOLUME_EXPANSION_RATIO", "")
    monkeypatch.setenv("ORB_MIN_SUPPORTIVE_BUBBLE_QTY_RATIO", "")

    rows = [
        {"timestamp": _ms_ny(2024, 6, 30, 9, 31), "price": 10.0, "qty": 5.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 29), "price": 11.0, "qty": 5.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 30), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 44), "price": 105.0, "qty": 100.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 45), "price": 100.5, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 45) + 30_000, "price": 98.5, "qty": 100.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 2, 1, 30), "price": 97.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 8, 9, 0), "price": 98.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 8, 9, 29), "price": 99.0, "qty": 1.0, "is_buyer_maker": False},
    ]
    input_path = tmp_path / "trades.parquet"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    uncached_dir = tmp_path / "uncached"
    cached_dir = tmp_path / "cached"
    cache_dir = tmp_path / "feature_cache"
    common = {
        "input": str(input_path),
        "start_date": "2024-07-01",
        "end_date": "2024-07-08",
    }
    uncached = run_fast_backtest(argparse.Namespace(**common, output_dir=str(uncached_dir), cache_dir=None, use_cache=False, refresh_cache=False))
    cached = run_fast_backtest(argparse.Namespace(**common, output_dir=str(cached_dir), cache_dir=str(cache_dir), use_cache=True, refresh_cache=True))

    assert {k: uncached[k] for k in uncached if k != "output_dir"} == {k: cached[k] for k in cached if k != "output_dir"}
    assert (uncached_dir / "trades.jsonl").read_text(encoding="utf-8") == (cached_dir / "trades.jsonl").read_text(encoding="utf-8")
    assert (cache_dir / "candles_1m.parquet").exists()
    assert (cache_dir / "session_contexts.json").exists()
    assert (cache_dir / "minute_orderflow.parquet").exists()
    cached_candle_times = set(pd.read_parquet(cache_dir / "candles_1m.parquet", columns=["timestamp_ms"])["timestamp_ms"])
    assert _ms_ny(2024, 7, 8, 9, 0) in cached_candle_times
    assert _ms_ny(2024, 7, 8, 9, 29) in cached_candle_times


def test_fast_backtest_supports_overnight_orb_anchor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SYMBOL", "AVAXUSDC")
    monkeypatch.setenv("SESSION_TIMEZONE", "America/New_York")
    monkeypatch.setenv("NY_OPEN_TIME", "09:30")
    monkeypatch.setenv("SETUP_CUTOFF_TIME", "17:30")
    monkeypatch.setenv("OVERNIGHT_START_TIME", "17:30")
    monkeypatch.setenv("PRE_NY_START_TIME", "01:30")
    monkeypatch.setenv("ORB_SESSION_START_TIME", "17:30")
    monkeypatch.setenv("ORB_ENTRY_START_TIME", "17:45")
    monkeypatch.setenv("ORB_ENTRY_WINDOW_MINUTES", "30")
    monkeypatch.setenv("ORB_MIN_VOLUME_EXPANSION_RATIO", "0")
    monkeypatch.setenv("ORB_MIN_SUPPORTIVE_BUBBLE_QTY_RATIO", "")
    monkeypatch.setenv("PAPER_MAX_STOP_RISK_PCT", "0.10")
    monkeypatch.setenv("PAPER_MAX_HOLD_EXIT_TIME", "09:29")
    monkeypatch.setenv("VOLUME_PROFILE_BINS", "10")
    monkeypatch.setenv("BUBBLE_PERCENTILE", "0.95")
    monkeypatch.setenv("BUBBLE_LOOKBACK_MIN_TRADES", "1")

    rows = [
        {"timestamp": _ms_ny(2024, 6, 30, 17, 31), "price": 10.0, "qty": 5.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 17, 30), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 17, 44), "price": 105.0, "qty": 100.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 17, 45), "price": 100.5, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 17, 45) + 30_000, "price": 98.5, "qty": 100.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 2, 9, 29), "price": 97.0, "qty": 1.0, "is_buyer_maker": True},
    ]
    input_path = tmp_path / "overnight.parquet"
    pd.DataFrame(rows).to_parquet(input_path, index=False)
    output_dir = tmp_path / "overnight_run"

    summary = run_fast_backtest(
        argparse.Namespace(
            input=str(input_path),
            start_date="2024-07-01",
            end_date="2024-07-01",
            output_dir=str(output_dir),
            cache_dir=None,
            use_cache=False,
            refresh_cache=False,
        )
    )
    trades = [json.loads(line) for line in (output_dir / "trades.jsonl").read_text(encoding="utf-8").splitlines() if line]

    assert summary["trades_taken"] == 1
    assert trades[0]["entry_time"].startswith("2024-07-01T17:45")
    assert trades[0]["exit_time"].startswith("2024-07-02T09:29")
    assert trades[0]["close_reason"] == "overnight_time_invalidation"


def test_regime_cache_builds_and_freezes_before_ny_open(tmp_path: Path) -> None:
    cache_dir = tmp_path / "feature_cache"
    cache_dir.mkdir()
    rows = []
    for hour in range(12):
        price = 100.0 + hour
        rows.append(
            {
                "timestamp_ms": _ms_ny(2024, 7, 1, hour, 0),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.25,
                "volume": 100.0,
            }
        )
    pd.DataFrame(rows).to_parquet(cache_dir / "candles_1m.parquet", index=False)

    regimes = build_or_load_regime_cache(
        cache_dir,
        refresh=True,
        short_vol_hours=1,
        long_vol_days=1,
        momentum_hours=1,
        normalization_days=1,
    )
    loaded = build_or_load_regime_cache(
        cache_dir,
        short_vol_hours=1,
        long_vol_days=1,
        momentum_hours=1,
        normalization_days=1,
    )
    frozen = frozen_regime_for_session(regimes, date(2024, 7, 1), "America/New_York")

    assert len(loaded) == len(regimes)
    assert (cache_dir / "regime_1h.parquet").exists()
    assert (cache_dir / "regime_manifest.json").exists()
    assert frozen is not None
    assert datetime.fromtimestamp(frozen["timestamp_ms"] / 1000, timezone.utc).year == 2024
    assert frozen["timestamp_ms"] < _ms_ny(2024, 7, 1, 9, 30)
    assert frozen["frozen_at_session_open"] is True


def test_parameter_sweep_builds_cartesian_product() -> None:
    grid = parse_grid(
        [
            "ORB_FOLLOW_FAILURE_FILTER=false,true",
            "PAPER_EXIT_MODE=tp1_trail,trail_only",
            "PAPER_PROTECTION_ENABLED=true,false",
            "PAPER_PROTECTION_FRACTION=0,0.25",
            "PAPER_TRAIL_DISTANCE_R=1.5",
        ]
    )
    variants = combinations(grid)

    assert len(variants) == 16
    assert {variant[0]["ORB_FOLLOW_FAILURE_FILTER"] for variant in variants} == {True, False}
    assert {variant[0]["PAPER_EXIT_MODE"] for variant in variants} == {"tp1_trail", "trail_only"}
    assert {variant[0]["PAPER_PROTECTION_ENABLED"] for variant in variants} == {True, False}
    assert {variant[0]["PAPER_PROTECTION_FRACTION"] for variant in variants} == {0.0, 0.25}


def test_parameter_sweep_accepts_setup_stop_and_risk_parameters() -> None:
    grid = parse_grid(
        [
            "ORB_MIN_CANDIDATE_BODY_RATIO=0.2,0.35",
            "ORB_OPPOSITE_TOUCH_POLICY=strict,displacement_override,ignore",
            "ORB_STOP_MODEL=opposite_extreme,poc,opposite_value_area",
            "ORB_SESSION_START_TIME=17:30",
            "ORB_ENTRY_START_TIME=17:45",
            "PAPER_MIN_STOP_RISK_PCT=0.001",
            "PAPER_MAX_STOP_RISK_PCT=0.03",
            "PAPER_MAX_HOLD_EXIT_TIME=09:29",
        ]
    )
    variants = combinations(grid)

    assert len(variants) == 18
    assert {variant[0]["ORB_OPPOSITE_TOUCH_POLICY"] for variant in variants} == {
        "strict",
        "displacement_override",
        "ignore",
    }
    assert {variant[0]["ORB_STOP_MODEL"] for variant in variants} == {
        "opposite_extreme",
        "poc",
        "opposite_value_area",
    }


def test_follow_candle_mode_can_toggle_three_condition_failure_filter(tmp_path: Path) -> None:
    day = date(2024, 7, 1)
    times = [_ms_ny(2024, 7, 1, 9, minute) for minute in (45, 46, 47)]
    candles = [
        {"timestamp_ms": times[0], "open": 100.0, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 100.0, "delta": 20.0},
        {"timestamp_ms": times[1], "open": 101.5, "high": 101.6, "low": 100.4, "close": 100.5, "volume": 100.0, "delta": -20.0},
        {"timestamp_ms": times[2], "open": 100.6, "high": 101.0, "low": 98.5, "close": 99.0, "volume": 100.0, "delta": -20.0},
    ]
    features = FeatureSet(
        rows_loaded=3,
        candles=candles,
        contexts={day: {"ny_first_15m_profile": {"session_low": 99.0, "session_high": 101.0, "poc_price": 100.0}}},
        triggers={},
        force_exit_trades={},
    )
    config = replace(_config(tmp_path), orb_stop_model="poc", paper_protection_enabled=False)

    rejected = run_with_features(config, features, day, day, tmp_path / "rejected", entry_mode="follow_candle", failure_filter=True)
    accepted = run_with_features(config, features, day, day, tmp_path / "accepted", entry_mode="follow_candle", failure_filter=False)

    assert rejected["positions_opened"] == 0
    assert accepted["positions_opened"] == 1
    assert accepted["trades_taken"] == 1
    trade = json.loads((tmp_path / "accepted" / "trades.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert trade["stop_loss"] == 100.0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_closed_candle_delta_profile_extremes_and_bubble(tmp_path)
        test_dynamic_bubble_threshold_is_computed_once_for_closed_minute(tmp_path)
        test_previous_24h_profile_is_frozen_at_ny_open(tmp_path)
        test_ny_first_15m_profile_is_frozen_after_window_end(tmp_path)
        test_trigger_observer_reports_without_gating_ai(tmp_path)
        test_ai_provider_key_can_be_present_without_live_calls(tmp_path)
        test_algorithm_provider_takes_short_retest_continuation_without_api(tmp_path)
        test_algorithm_provider_waits_for_retest_after_first_breakout(tmp_path)
        test_algorithm_provider_takes_direct_displacement_breakout(tmp_path)
        test_algorithm_provider_blocks_orb_after_bias_window(tmp_path)
        test_algorithm_provider_uses_configured_orb_entry_window(tmp_path)
        test_algorithm_provider_rejects_weak_supportive_bubble_ratio(tmp_path)
        test_algorithm_provider_rejects_weak_pre_entry_delta(tmp_path)
        test_algorithm_provider_rejects_prior_opposite_orb_touch(tmp_path)
        test_algorithm_provider_allows_displacement_after_opposite_orb_touch(tmp_path)
        test_algorithm_provider_uses_configured_orb_stop_model(tmp_path)
        test_pre_ai_wait_blocks_incomplete_required_profiles()
        test_storage_bootstrap_reads_recent_raw_trades(tmp_path)
        test_paper_broker_uses_benchmark_tp1_and_runner_trailing(tmp_path)
        test_paper_broker_closes_at_entry_after_one_r_protection(tmp_path)
        test_paper_broker_can_scale_out_at_protection_r(tmp_path)
        test_scale_out_then_original_stop_keeps_initial_stop_reason(tmp_path)
        test_paper_broker_force_exits_at_next_overnight_end(tmp_path)
        test_paper_broker_uses_profile_targets_for_mean_reversion(tmp_path)
        test_paper_broker_rejects_take_without_snapshot_timestamp(tmp_path)
        test_risk_gate_rejects_non_trend_entry_model()
        test_risk_gate_enforces_stop_risk_bounds()
        test_parameter_sweep_accepts_setup_stop_and_risk_parameters()
    print("orb_live_agent smoke check passed")
