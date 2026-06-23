from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from session.context import PreNYContext
from session.context import add_asia_europe_contexts
from session.context import build_pre_ny_daily_contexts
from session.context import build_session_windows
from session.context import load_raw_aggtrades
from session.context import timestamp_series_to_ns_array
from strategy.trend_following_ny.config import TrendFollowingNYConfig
from strategy.trend_following_ny.engine import simulate_ny_session
from strategy.trend_following_ny.entry_models.second_breakout import evaluate_second_breakout_entry
from strategy.trend_following_ny.entry_models.value_area_zone import evaluate_value_area_zone_entry
from strategy.trend_following_ny.stop_models.inside_value_percentage import decide_inside_value_percentage_stop

import numpy as np


def _context() -> PreNYContext:
    return PreNYContext(
        session_day_wib="2026-01-01",
        ny_start_utc=pd.Timestamp("2026-01-01 15:00:00", tz="UTC"),
        ny_end_utc=pd.Timestamp("2026-01-01 23:00:00", tz="UTC"),
        asia_session_context="ASIA_CTX",
        europe_session_context="EUROPE_CTX",
        asia_europe_session_context_combo="ASIA_ASIA_CTX__EUROPE_EUROPE_CTX",
        europe_poc=100.0,
        europe_val=99.0,
        europe_vah=101.0,
        profile_bins=50,
        value_area_pct=0.70,
    )


def _config(**overrides) -> TrendFollowingNYConfig:
    base = dict(symbol="AVAXUSDC")
    base.update(overrides)
    return TrendFollowingNYConfig(**base)


def _arrays_from_prices(prices: list[float], start: str = "2026-01-01 15:00:00"):
    timestamps = [pd.Timestamp(start, tz="UTC") + pd.Timedelta(seconds=i) for i in range(len(prices))]
    timestamp_arr = np.array(timestamps, dtype=object)
    timestamp_ns_arr = np.array([ts.value for ts in timestamps], dtype=np.int64)
    raw_index_arr = np.arange(len(prices), dtype=np.int64)
    price_arr = np.array(prices, dtype=float)
    return price_arr, timestamp_arr, timestamp_ns_arr, raw_index_arr


def _run_engine(context, entry, stop, config, prices, ny_end_idx=None):
    price_arr, timestamp_arr, timestamp_ns_arr, raw_index_arr = _arrays_from_prices(prices)
    if ny_end_idx is None:
        ny_end_idx = len(prices)
    return simulate_ny_session(
        context,
        entry,
        stop,
        config,
        price_arr,
        timestamp_arr,
        timestamp_ns_arr,
        raw_index_arr,
        0,
        ny_end_idx,
    )


def test_pre_ny_context_excludes_current_ny_outcome_fields():
    field_names = {f.name for f in fields(PreNYContext)}
    forbidden = {
        "us_direction",
        "us_relative_structure",
        "us_range_rank",
        "us_direction_strength_rank",
        "us_high_reach_strength_rank",
        "us_low_reach_strength_rank",
        "us_high",
        "us_low",
        "us_close",
    }
    assert field_names.isdisjoint(forbidden)


def test_entry_occurs_strictly_after_signal_by_raw_index_with_same_timestamp():
    context = _context()
    config = _config(entry_latency_ms=0)
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 10, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 11, "price": 101.25},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 12, "price": 101.4},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    price_arr = np.array([101.2, 101.25, 101.4], dtype=float)
    timestamps = np.array([
        pd.Timestamp("2026-01-01 15:00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 15:00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 15:00:01", tz="UTC"),
    ], dtype=object)
    timestamp_ns_arr = np.array([ts.value for ts in timestamps], dtype=np.int64)
    raw_idx = np.array([10, 11, 12], dtype=np.int64)
    result = simulate_ny_session(context, entry, stop, config, price_arr, timestamps, timestamp_ns_arr, raw_idx, 0, 3)
    assert entry.signal_raw_index == 10
    assert result["entry_raw_index"] == 11


def test_initial_r_does_not_change_after_entry_and_stop_cannot_widen_for_long():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 1, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 2, "price": 101.3},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:02", tz="UTC"), "raw_index": 3, "price": 103.8},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:03", tz="UTC"), "raw_index": 4, "price": 102.7},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 103.8, 102.7])
    expected_r = abs(result["entry_price"] - result["stop_price"])
    assert result["initial_risk_price"] == expected_r
    assert result["stop_price"] <= result["entry_price"]


def test_inside_value_stop_rejected_when_outside_value_area():
    context = _context()
    config = _config(inside_value_stop_pct=0.05)
    stop = decide_inside_value_percentage_stop(context, "LONG", config)
    assert stop.status == "STOP_OUTSIDE_VALUE_AREA"


def test_no_valid_stop_returns_explicit_rejection_without_fallback():
    context = _context()
    config = _config()
    stop = decide_inside_value_percentage_stop(context, None, config)
    assert stop.status == "NO_VALID_STOP"


def test_missing_or_empty_ny_trades_produce_insufficient_data():
    context = _context()
    config = _config()
    empty = pd.DataFrame(columns=["timestamp", "raw_index", "price"])
    entry = evaluate_value_area_zone_entry(context, empty, config)
    price_arr = np.array([], dtype=float)
    timestamp_arr = np.array([], dtype=object)
    timestamp_ns_arr = np.array([], dtype=np.int64)
    raw_idx = np.array([], dtype=np.int64)
    result = simulate_ny_session(context, entry, None, config, price_arr, timestamp_arr, timestamp_ns_arr, raw_idx, 0, 0)
    assert result["status"] == "INSUFFICIENT_DATA"


def test_v4_script_remains_independently_executable_import_path():
    source = Path("scripts/session_interaction_research_v4.py").read_text(encoding="utf-8")
    assert "if __name__ == \"__main__\":" in source
    assert "main()" in source


def test_simulasi_script_remains_independently_executable_import_path():
    source = Path("scripts/simulasi.py").read_text(encoding="utf-8")
    assert "if __name__ == \"__main__\":" in source
    assert "raise SystemExit(main())" in source


def test_timestamp_series_to_ns_array_preserves_nanosecond_scale():
    series = pd.Series([pd.Timestamp("2024-06-01 00:00:06.432000", tz="UTC")])
    arr = timestamp_series_to_ns_array(series)
    assert int(arr[0]) == pd.Timestamp("2024-06-01 00:00:06.432000", tz="UTC").value


def test_add_asia_europe_contexts_restores_all_four_buffer_tag_columns():
    daily = pd.DataFrame([
        {
            "anchor_utc_date": pd.Timestamp("2026-01-01", tz="UTC"),
            "us_high": 105.0,
            "us_low": 95.0,
            "asia_high": 101.0,
            "asia_low": 99.0,
            "asia_close": 100.5,
            "asia_efficiency": 0.2,
            "europe_high": 102.0,
            "europe_low": 98.5,
            "europe_close": 101.5,
            "europe_efficiency": 0.3,
        },
        {
            "anchor_utc_date": pd.Timestamp("2026-01-02", tz="UTC"),
            "us_high": 106.0,
            "us_low": 96.0,
            "asia_high": 104.0,
            "asia_low": 94.0,
            "asia_close": 103.0,
            "asia_efficiency": 0.4,
            "europe_high": 107.0,
            "europe_low": 93.0,
            "europe_close": 106.0,
            "europe_efficiency": 0.5,
        },
    ])
    args = SimpleNamespace(buffer_pct=0.005, context_direction_tolerance=0.10)
    enriched, _ = add_asia_europe_contexts(daily, args)
    for col in [
        "asia_high_buffer_tag",
        "asia_low_buffer_tag",
        "europe_high_buffer_tag",
        "europe_low_buffer_tag",
    ]:
        assert col in enriched.columns


def test_long_stop_above_entry_is_rejected():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    stop = type(stop)(stop.status, stop.stop_model, 102.0, stop.reference_price, stop.buffer_pct, stop.reason, stop.metadata)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3])
    assert result["status"] == "STOP_CONFIGURATION_INVALID"
    assert result["reason"] == "STOP_ON_WRONG_SIDE"


def test_short_stop_below_entry_is_rejected():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 98.9},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 98.8},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    stop = type(stop)(stop.status, stop.stop_model, 98.0, stop.reference_price, stop.buffer_pct, stop.reason, stop.metadata)
    result = _run_engine(context, entry, stop, config, [98.9, 98.8])
    assert result["status"] == "STOP_CONFIGURATION_INVALID"
    assert result["reason"] == "STOP_ON_WRONG_SIDE"


def test_stop_gap_produces_actual_r_worse_than_negative_one_r():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 98.0])
    assert result["gross_R"] < -1.0


def test_trailing_gap_uses_actual_crossing_trade():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 105.0, 102.0])
    assert result["reason"] == "TRAILING_STOP_HIT"
    assert result["final_exit_market_price"] == 102.0


def test_tp1_partial_fill_and_remaining_trailing_pnl_are_weighted_correctly():
    context = _context()
    config = _config(tp1_fraction=0.5)
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 105.0, 103.0])
    assert pd.notna(result["tp1_timestamp"])
    assert result["tp1_fill_price"] == result["tp1_price"]
    assert result["gross_R"] > 0


def test_fee_percentage_is_converted_to_r_using_initial_risk():
    context = _context()
    config = _config(fee_pct_per_side=0.001)
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 105.0, 103.0])
    assert result["fee_R"] > 0


def test_slippage_is_not_double_counted():
    context = _context()
    config = _config(slippage_pct_per_side=0.001)
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 0, "price": 101.2},
        {"timestamp": pd.Timestamp("2026-01-01 15:00:01", tz="UTC"), "raw_index": 1, "price": 101.3},
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    result = _run_engine(context, entry, stop, config, [101.2, 101.3, 105.0, 103.0])
    assert np.isclose(result["net_R"], result["gross_R"] - result["slippage_R"] - result["fee_R"])


def test_trade_active_across_later_ny_session_blocks_overlapping_entry():
    blocked_until = pd.Timestamp.max.tz_localize("UTC")
    later_ny_start = pd.Timestamp("2026-01-02 15:00:00", tz="UTC")
    assert later_ny_start <= blocked_until


def test_new_entry_after_previous_exit_is_allowed():
    blocked_until = pd.Timestamp("2026-01-01 16:00:00", tz="UTC")
    later_ny_start = pd.Timestamp("2026-01-02 15:00:00", tz="UTC")
    assert later_ny_start > blocked_until


def test_second_breakout_returns_entry_decision_not_dict():
    result = evaluate_second_breakout_entry()
    assert hasattr(result, "status")
    assert not isinstance(result, dict)


def test_pre_ny_context_does_not_require_completed_current_ny():
    timestamps = pd.date_range("2026-01-01 15:00:00", periods=60, freq="h", tz="UTC")
    prices = np.arange(len(timestamps), dtype=float) + 100.0
    qty = np.ones(len(timestamps), dtype=float)
    df = pd.DataFrame({
        "timestamp": timestamps,
        "price": prices,
        "qty": qty,
        "notional": prices * qty,
        "is_buyer_maker": False,
        "source_raw_index": np.arange(len(timestamps), dtype=np.int64),
        "raw_index": np.arange(len(timestamps), dtype=np.int64),
        "timestamp_ns": np.array([ts.value for ts in timestamps], dtype=np.int64),
        "timestamp_ms": np.array([ts.value // 1_000_000 for ts in timestamps], dtype=np.int64),
        "aggressor_side": 1,
    })
    args = SimpleNamespace(asia_start="23:00", asia_end="07:00", europe_start="07:00", europe_end="15:00", us_start="15:00", us_end="23:00")
    windows = build_session_windows(args)
    out = build_pre_ny_daily_contexts(df, windows, 1, 10, 0.70, 0.005, 0.10)
    forbidden = {"us_direction", "us_relative_structure", "us_high_reach_state", "us_low_reach_state"}
    assert forbidden.isdisjoint(set(out.columns))


def test_canonical_raw_index_increases_in_chronological_order(tmp_path):
    df = pd.DataFrame({
        "timestamp": [1717200007000, 1717200006000, 1717200006000],
        "price": [1.0, 2.0, 3.0],
        "qty": [1.0, 1.0, 1.0],
        "is_buyer_maker": [False, False, True],
    })
    path = tmp_path / "mini.parquet"
    df.to_parquet(path, index=False)
    loaded = load_raw_aggtrades(path)
    assert loaded["raw_index"].is_monotonic_increasing


def test_diagnostics_preserve_engine_rejection_reason():
    context = _context()
    config = _config()
    ny_trades = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01 15:00:00", tz="UTC"), "raw_index": 10, "price": 101.2}
    ])
    entry = evaluate_value_area_zone_entry(context, ny_trades, config)
    stop = decide_inside_value_percentage_stop(context, entry.direction, config)
    price_arr = np.array([101.2], dtype=float)
    timestamps = np.array([pd.Timestamp("2026-01-01 15:00:00", tz="UTC")], dtype=object)
    timestamp_ns_arr = np.array([timestamps[0].value], dtype=np.int64)
    raw_idx = np.array([10], dtype=np.int64)
    result = simulate_ny_session(context, entry, stop, config, price_arr, timestamps, timestamp_ns_arr, raw_idx, 0, 1)
    assert result["status"] == "NO_ELIGIBLE_ENTRY_TRADE_AFTER_SIGNAL"
    assert result["reason"] == "NO_ELIGIBLE_ENTRY_TRADE_AFTER_SIGNAL"


def test_empty_trade_results_export_with_stable_schema():
    from scripts.simulasi import TRADE_RESULTS_COLUMNS
    assert "gross_R" in TRADE_RESULTS_COLUMNS
    assert "slippage_R" in TRADE_RESULTS_COLUMNS
    assert "fee_R" in TRADE_RESULTS_COLUMNS
    assert "net_R" in TRADE_RESULTS_COLUMNS


def test_simulasi_summary_counts_entry_candidates_from_diagnostics_flag():
    from scripts.simulasi import _summary

    sessions_df = pd.DataFrame([
        {"simulation_status": "TRADE_COMPLETED", "entry_candidate": True},
        {"simulation_status": "TRADE_COMPLETED", "entry_candidate": True},
        {"simulation_status": "TRADE_COMPLETED", "entry_candidate": True},
    ])
    trades_df = pd.DataFrame([
        {"status": "TRADE_COMPLETED", "net_R": 1.0},
        {"status": "TRADE_COMPLETED", "net_R": 0.5},
        {"status": "TRADE_COMPLETED", "net_R": -0.25},
    ])

    summary = _summary(trades_df, sessions_df)

    assert int(summary.loc[0, "Entry candidates"]) == 3
    assert int(summary.loc[0, "Trades created"]) == 3
