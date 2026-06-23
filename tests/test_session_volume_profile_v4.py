import hashlib
import pathlib

import numpy as np
import pandas as pd
import pytest

from indicator import volume_profile as vp
from indicator.volume_profile import build_basic_volume_profile
from types import SimpleNamespace

from scripts.session_interaction_research_v4 import (
    apply_prior_rolling_ranks,
    build_daily_sessions,
    build_session_windows,
    classify_context_direction,
    classify_price_vs_value,
    classify_us_high_reach_state,
    classify_us_low_reach_state,
    classify_us_relative_structure,
    export_workbook,
    timestamp_series_to_ns_array,
    validate_sheet_names,
)


def _trades(prices, qtys):
    return pd.DataFrame({"price": prices, "qty": qtys})


def test_basic_normal_profile_multiple_bins():
    result = build_basic_volume_profile(_trades([100, 101, 102, 103], [1, 2, 3, 4]), n_bins=4)
    assert result["bins"] == 4
    assert result["session_low"] == 100.0
    assert result["session_high"] == 103.0
    assert result["total_volume"] == 10.0


def test_poc_identifies_highest_volume_bin():
    result = build_basic_volume_profile(_trades([100.1, 100.2, 101.2], [5, 4, 1]), n_bins=2)
    assert result["poc_bin_index"] == 0
    assert result["poc_volume"] == 9.0


def test_poc_tie_selects_lowest_bin():
    result = build_basic_volume_profile(_trades([100.1, 101.1], [5, 5]), n_bins=2)
    assert result["poc_bin_index"] == 0


def test_equal_adjacent_value_area_includes_both_sides():
    result = build_basic_volume_profile(_trades([100.1, 101.1, 102.1], [5, 10, 5]), n_bins=3, value_area_pct=0.70)
    assert result["value_area_volume"] == 20.0
    assert result["val"] == 100.1 or result["val"] <= 100.1
    assert result["vah"] >= 102.1


def test_value_area_reaches_or_exceeds_target_with_custom_pct():
    result = build_basic_volume_profile(_trades([100.1, 100.2, 101.1, 102.1], [4, 4, 1, 1]), n_bins=3, value_area_pct=0.5)
    assert result["value_area_volume_pct"] >= 0.5


def test_zero_range_session():
    result = build_basic_volume_profile(_trades([100, 100, 100], [1, 2, 3]), n_bins=30)
    assert result["bin_width"] == 0.0
    assert result["poc_price"] == 100.0
    assert result["val"] == 100.0
    assert result["vah"] == 100.0
    assert result["value_area_volume_pct"] == 1.0


def test_missing_required_column():
    with pytest.raises(ValueError, match="Missing required columns"):
        build_basic_volume_profile(pd.DataFrame({"price": [1, 2]}))


def test_invalid_n_bins():
    with pytest.raises(ValueError, match="n_bins must be > 0"):
        build_basic_volume_profile(_trades([1], [1]), n_bins=0)


def test_invalid_value_area_pct():
    with pytest.raises(ValueError, match="value_area_pct"):
        build_basic_volume_profile(_trades([1], [1]), value_area_pct=0)


def test_negative_quantity_rejected():
    with pytest.raises(ValueError, match="Negative quantity"):
        build_basic_volume_profile(_trades([100, 101], [1, -1]))


def test_basic_function_does_not_call_hvn_lvn_helpers(monkeypatch):
    monkeypatch.setattr(vp, "_segment_profile_regimes", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")))
    result = build_basic_volume_profile(_trades([100, 101], [1, 2]), n_bins=2)
    assert result["total_volume"] == 3.0


def test_classify_price_vs_value_boundaries():
    assert classify_price_vs_value(10, 10, 9) == "INSIDE_VALUE"
    assert classify_price_vs_value(9, 10, 9) == "INSIDE_VALUE"
    assert classify_price_vs_value(10.1, 10, 9) == "ABOVE_VALUE"
    assert classify_price_vs_value(8.9, 10, 9) == "BELOW_VALUE"


def test_legacy_us_v3_names_absent_from_v4():
    source = pathlib.Path("scripts/session_interaction_research_v4.py").read_text(encoding="utf-8")
    forbidden = [
        "classify_us_high_state",
        "classify_us_low_state",
        "classify_us_directional_outcome_v3",
        "classify_us_structure_3x3_label",
        "classify_close_location_v3",
        "classify_position_vs_structure",
        "add_us_outcomes_v3",
        "us_high_state",
        "us_low_state",
        "us_directional_outcome_v3",
        "us_structure_3x3_label",
        "us_high_position_vs_structure",
        "us_low_position_vs_structure",
        "us_close_location_v3",
        "us_extension_above_outer_high",
        "us_extension_below_outer_low",
        "us_extension_above_outer_high_R",
        "us_extension_below_outer_low_R",
        "inner_high_buffer",
        "inner_low_buffer",
        "common_overlap_range",
        "upper_internal_range",
        "lower_internal_range",
    ]
    for name in forbidden:
        assert name not in source
    assert "asia_session_context" in source


def test_v3_file_hash_unchanged_reference():
    expected_hash = "34a48cedf18baad5b06c9e70d52f921ca3c4eb4edefcc369da4b49287f1bb017"
    actual_hash = hashlib.sha256(pathlib.Path("scripts/session_interaction_research_v3.py").read_bytes()).hexdigest()
    assert actual_hash == expected_hash


@pytest.mark.parametrize(
    ("us_high", "inner_high", "outer_high_buffer", "expected"),
    [
        (100.0, 100.0, 101.0, "HIGH_NOT_REACHED"),
        (101.0, 100.0, 101.0, "HIGH_INTERNAL_REACH"),
        (101.0001, 100.0, 101.0, "HIGH_OUTER_EXPANSION"),
    ],
)
def test_us_high_reach_state_boundaries(us_high, inner_high, outer_high_buffer, expected):
    assert classify_us_high_reach_state(us_high, inner_high, outer_high_buffer) == expected


@pytest.mark.parametrize(
    ("us_low", "inner_low", "outer_low_buffer", "expected"),
    [
        (100.0, 100.0, 99.0, "LOW_NOT_REACHED"),
        (99.0, 100.0, 99.0, "LOW_INTERNAL_REACH"),
        (98.9999, 100.0, 99.0, "LOW_OUTER_EXPANSION"),
    ],
)
def test_us_low_reach_state_boundaries(us_low, inner_low, outer_low_buffer, expected):
    assert classify_us_low_reach_state(us_low, inner_low, outer_low_buffer) == expected


@pytest.mark.parametrize(
    ("high_state", "low_state", "expected"),
    [
        ("HIGH_OUTER_EXPANSION", "LOW_OUTER_EXPANSION", "BOTH_OUTER_EXPANSION"),
        ("HIGH_OUTER_EXPANSION", "LOW_INTERNAL_REACH", "UP_OUTER_WITH_LOW_INTERNAL"),
        ("HIGH_OUTER_EXPANSION", "LOW_NOT_REACHED", "UP_OUTER_ONLY"),
        ("HIGH_INTERNAL_REACH", "LOW_OUTER_EXPANSION", "DOWN_OUTER_WITH_HIGH_INTERNAL"),
        ("HIGH_INTERNAL_REACH", "LOW_INTERNAL_REACH", "BOTH_INTERNAL_REACH"),
        ("HIGH_INTERNAL_REACH", "LOW_NOT_REACHED", "UPPER_INTERNAL_ONLY"),
        ("HIGH_NOT_REACHED", "LOW_OUTER_EXPANSION", "DOWN_OUTER_ONLY"),
        ("HIGH_NOT_REACHED", "LOW_INTERNAL_REACH", "LOWER_INTERNAL_ONLY"),
        ("HIGH_NOT_REACHED", "LOW_NOT_REACHED", "NO_INNER_REACH"),
    ],
)
def test_us_relative_structure_all_nine_combinations(high_state, low_state, expected):
    assert classify_us_relative_structure(high_state, low_state) == expected


def test_direction_boundaries_exact_tolerance_are_no_direction():
    assert classify_context_direction(0.10, 0.10) == "NO_DIRECTION"
    assert classify_context_direction(-0.10, 0.10) == "NO_DIRECTION"


def _base_rank_args():
    return SimpleNamespace(
        quality_rank_lookback=10,
        quality_rank_min_history=2,
    )


def test_us_prior_ranks_exclude_current_observation_and_respect_separate_pools():
    daily = pd.DataFrame(
        [
            {
                "anchor_utc_date": pd.Timestamp("2026-01-01", tz="UTC"),
                "asia_range_pct": 0.01,
                "europe_range_pct": 0.01,
                "asia_efficiency": 0.5,
                "europe_efficiency": 0.5,
                "asia_context_direction": "UP",
                "europe_context_direction": "UP",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.02,
                "us_efficiency": 0.20,
                "us_direction": "UP",
                "us_high_reach_state": "HIGH_INTERNAL_REACH",
                "us_high_reach_value": 0.10,
                "us_low_reach_state": "LOW_NOT_REACHED",
                "us_low_reach_value": 0.00,
            },
            {
                "anchor_utc_date": pd.Timestamp("2026-01-02", tz="UTC"),
                "asia_range_pct": 0.02,
                "europe_range_pct": 0.02,
                "asia_efficiency": 0.6,
                "europe_efficiency": 0.6,
                "asia_context_direction": "UP",
                "europe_context_direction": "UP",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.03,
                "us_efficiency": 0.30,
                "us_direction": "UP",
                "us_high_reach_state": "HIGH_OUTER_EXPANSION",
                "us_high_reach_value": 0.40,
                "us_low_reach_state": "LOW_INTERNAL_REACH",
                "us_low_reach_value": 0.12,
            },
            {
                "anchor_utc_date": pd.Timestamp("2026-01-03", tz="UTC"),
                "asia_range_pct": 0.03,
                "europe_range_pct": 0.03,
                "asia_efficiency": 0.7,
                "europe_efficiency": 0.7,
                "asia_context_direction": "UP",
                "europe_context_direction": "UP",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.50,
                "us_efficiency": 0.90,
                "us_direction": "UP",
                "us_high_reach_state": "HIGH_INTERNAL_REACH",
                "us_high_reach_value": 0.20,
                "us_low_reach_state": "LOW_OUTER_EXPANSION",
                "us_low_reach_value": 0.50,
            },
        ]
    )

    ranked, excluded = apply_prior_rolling_ranks(daily, _base_rank_args())

    assert ranked.loc[0, "us_range_rank"] == "INSUFFICIENT_HISTORY"
    assert ranked.loc[1, "us_range_rank"] == "INSUFFICIENT_HISTORY"
    assert ranked.loc[2, "us_range_rank"] == "HIGH_RANGE"

    assert ranked.loc[2, "us_direction_strength_rank"] == "HIGH_DIRECTION_STRENGTH"
    assert ranked.loc[2, "us_high_reach_strength_rank"] == "INSUFFICIENT_HISTORY"
    assert ranked.loc[2, "us_low_reach_strength_rank"] == "INSUFFICIENT_HISTORY"
    assert excluded["us_range_rank_insufficient_history_count"] == 2


def test_us_not_applicable_ranks_for_no_direction_and_no_reach():
    daily = pd.DataFrame(
        [
            {
                "anchor_utc_date": pd.Timestamp("2026-01-01", tz="UTC"),
                "asia_range_pct": 0.01,
                "europe_range_pct": 0.01,
                "asia_efficiency": 0.0,
                "europe_efficiency": 0.0,
                "asia_context_direction": "NO_DIRECTION",
                "europe_context_direction": "NO_DIRECTION",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.02,
                "us_efficiency": 0.01,
                "us_direction": "NO_DIRECTION",
                "us_high_reach_state": "HIGH_NOT_REACHED",
                "us_high_reach_value": 0.0,
                "us_low_reach_state": "LOW_NOT_REACHED",
                "us_low_reach_value": 0.0,
            }
        ]
    )
    ranked, excluded = apply_prior_rolling_ranks(daily, _base_rank_args())
    assert ranked.loc[0, "us_direction_strength_rank"] == "NOT_APPLICABLE"
    assert ranked.loc[0, "us_high_reach_strength_rank"] == "NOT_APPLICABLE"
    assert ranked.loc[0, "us_low_reach_strength_rank"] == "NOT_APPLICABLE"
    assert excluded["us_direction_strength_rank_not_applicable_count"] == 1
    assert excluded["us_high_reach_strength_rank_not_applicable_count"] == 1
    assert excluded["us_low_reach_strength_rank_not_applicable_count"] == 1


def test_unexpected_reach_states_are_reference_unavailable_and_not_added_to_pools():
    daily = pd.DataFrame(
        [
            {
                "anchor_utc_date": pd.Timestamp("2026-01-01", tz="UTC"),
                "asia_range_pct": 0.01,
                "europe_range_pct": 0.01,
                "asia_efficiency": 0.2,
                "europe_efficiency": 0.2,
                "asia_context_direction": "UP",
                "europe_context_direction": "UP",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.02,
                "us_efficiency": 0.20,
                "us_direction": "UP",
                "us_high_reach_state": "HIGH_REACH_UNCLASSIFIED",
                "us_high_reach_value": 0.10,
                "us_low_reach_state": "LOW_REACH_UNCLASSIFIED",
                "us_low_reach_value": 0.10,
            },
            {
                "anchor_utc_date": pd.Timestamp("2026-01-02", tz="UTC"),
                "asia_range_pct": 0.03,
                "europe_range_pct": 0.03,
                "asia_efficiency": 0.4,
                "europe_efficiency": 0.4,
                "asia_context_direction": "UP",
                "europe_context_direction": "UP",
                "asia_reference_available": True,
                "asia_structure_state": "CONTAINED_RANGE",
                "europe_structure_state": "CONTAINED_RANGE",
                "asia_high": 10.0,
                "asia_low": 9.0,
                "asia_reference_us_high": 10.0,
                "asia_reference_us_low": 9.0,
                "europe_high": 10.0,
                "europe_low": 9.0,
                "europe_reference_asia_high": 10.0,
                "europe_reference_asia_low": 9.0,
                "europe_reference_upper_buffer": 10.5,
                "europe_reference_lower_buffer": 8.5,
                "us_range_pct": 0.04,
                "us_efficiency": 0.40,
                "us_direction": "UP",
                "us_high_reach_state": "HIGH_INTERNAL_REACH",
                "us_high_reach_value": 0.20,
                "us_low_reach_state": "LOW_INTERNAL_REACH",
                "us_low_reach_value": 0.20,
            },
        ]
    )
    ranked, excluded = apply_prior_rolling_ranks(daily, _base_rank_args())
    assert ranked.loc[0, "us_high_reach_strength_rank"] == "REFERENCE_UNAVAILABLE"
    assert ranked.loc[0, "us_low_reach_strength_rank"] == "REFERENCE_UNAVAILABLE"
    assert ranked.loc[1, "us_high_reach_strength_rank"] == "INSUFFICIENT_HISTORY"
    assert ranked.loc[1, "us_low_reach_strength_rank"] == "INSUFFICIENT_HISTORY"
    assert excluded["us_high_reach_strength_rank_reference_unavailable_count"] == 1
    assert excluded["us_low_reach_strength_rank_reference_unavailable_count"] == 1


def test_validate_sheet_names_rejects_duplicates_and_oversized_names():
    validate_sheet_names(["a", "b", "c"])
    with pytest.raises(ValueError, match="Duplicate Excel worksheet names"):
        validate_sheet_names(["a", "a"])
    with pytest.raises(ValueError, match="exceed 31 characters"):
        validate_sheet_names(["x" * 32])


def test_workbook_export_succeeds_and_sheet_names_are_valid(tmp_path):
    output = tmp_path / "smoke.xlsx"
    base = pd.DataFrame(
        [{
            "sample_n": 1,
            "sample_pct": 1.0,
            "us_direction_up_pct": 1.0,
            "us_direction_down_pct": 0.0,
            "us_direction_no_direction_pct": 0.0,
            "both_outer_expansion_pct": 0.0,
            "up_outer_with_low_internal_pct": 0.0,
            "up_outer_only_pct": 1.0,
            "down_outer_with_high_internal_pct": 0.0,
            "both_internal_reach_pct": 0.0,
            "upper_internal_only_pct": 0.0,
            "down_outer_only_pct": 0.0,
            "lower_internal_only_pct": 0.0,
            "no_inner_reach_pct": 0.0,
            "most_common_us_direction": "UP",
            "most_common_us_direction_pct": 1.0,
            "most_common_us_relative_structure": "UP_OUTER_ONLY",
            "most_common_us_relative_structure_pct": 1.0,
            "median_us_range_pct": 0.02,
            "median_us_efficiency": 0.3,
            "median_us_close_position": 0.7,
        }]
    )
    daily_export = pd.DataFrame([
        {
            "session_day_wib": "2026-01-01",
            "anchor_utc_date": pd.Timestamp("2026-01-01", tz="UTC"),
            "us_direction": "UP",
            "us_relative_structure": "UP_OUTER_ONLY",
            "us_range_rank": "HIGH_RANGE",
            "us_direction_strength_rank": "HIGH_DIRECTION_STRENGTH",
            "us_high_reach_strength_rank": "INSUFFICIENT_HISTORY",
            "us_low_reach_strength_rank": "NOT_APPLICABLE",
            "us_open_position": 0.2,
            "us_close_position": 0.7,
            "us_high_reach_state": "HIGH_OUTER_EXPANSION",
            "us_low_reach_state": "LOW_NOT_REACHED",
            "us_high_reach_value": 0.3,
            "us_low_reach_value": 0.0,
        }
    ])
    context = {
        "asia_context_summary": base.copy(), "europe_context_summary": base.copy(),
        "asia_europe_context_combo_summary": base.copy(), "asia_structure_summary": base.copy(),
        "europe_structure_summary": base.copy(), "asia_direction_summary": base.copy(),
        "europe_direction_summary": base.copy(), "asia_range_rank_summary": base.copy(),
        "europe_range_rank_summary": base.copy(), "asia_direction_strength_rank_summary": base.copy(),
        "europe_direction_strength_rank_summary": base.copy(), "asia_expansion_strength_rank_summary": base.copy(),
        "europe_expansion_strength_rank_summary": base.copy(), "asia_reference_overlap_rank_summary": base.copy(),
        "europe_reference_overlap_rank_summary": base.copy(), "us_direction_summary": base.copy(),
        "us_relative_structure_summary": base.copy(), "us_range_rank_summary": base.copy(),
        "us_direction_strength_rank_summary": base.copy(), "us_high_reach_strength_rank_summary": base.copy(),
        "us_low_reach_strength_rank_summary": base.copy(), "us_relative_structure_by_direction_summary": base.copy(),
        "asia_close_vs_own_value_summary": base.copy(), "europe_close_vs_own_value_summary": base.copy(),
        "europe_close_vs_asia_value_summary": base.copy(), "us_open_vs_asia_value_summary": base.copy(),
        "us_open_vs_europe_value_summary": base.copy(), "pre_us_value_combo_summary": base.copy(),
        "asia_buffer_tag_tables": [("A", base.copy())], "europe_buffer_tag_tables": [("A", base.copy())],
        "us_buffer_tag_tables": [("A", base.copy())],
    }
    config_df = pd.DataFrame({"parameter": ["smoke_test"], "value": ["ok"]})
    export_workbook(output, config_df, daily_export, base.copy(), base.copy(), context, daily_export.copy())
    assert output.exists()


def test_searchsorted_session_slicing_matches_boolean_slicing():
    timestamps = pd.date_range("2026-01-01 20:00:00", periods=40, freq="h", tz="UTC")
    df = pd.DataFrame({
        "timestamp": timestamps,
        "price": np.arange(len(timestamps), dtype=float) + 100.0,
        "qty": np.ones(len(timestamps), dtype=float),
        "notional": np.arange(len(timestamps), dtype=float) + 100.0,
    })
    timestamp_ns = timestamp_series_to_ns_array(df["timestamp"])
    start = pd.Timestamp("2026-01-02 07:00:00", tz="UTC")
    end = pd.Timestamp("2026-01-02 15:00:00", tz="UTC")
    start_idx = int(np.searchsorted(timestamp_ns, start.value, side="left"))
    end_idx = int(np.searchsorted(timestamp_ns, end.value, side="left"))
    sliced = df.iloc[start_idx:end_idx]
    filtered = df.loc[(df["timestamp"] >= start) & (df["timestamp"] < end)]
    pd.testing.assert_frame_equal(sliced.reset_index(drop=True), filtered.reset_index(drop=True))


def test_build_daily_sessions_searchsorted_path_smoke():
    timestamps = pd.date_range("2025-12-31 22:00:00", "2026-01-03 23:00:00", freq="5min", tz="UTC")
    prices = np.linspace(100.0, 120.0, len(timestamps))
    qty = np.ones(len(timestamps), dtype=float)
    df = pd.DataFrame({"timestamp": timestamps, "price": prices, "qty": qty, "notional": prices * qty, "is_buyer_maker": False, "aggressor_side": 1})
    args = SimpleNamespace(asia_start="23:00", asia_end="07:00", europe_start="07:00", europe_end="15:00", us_start="15:00", us_end="23:00")
    windows = build_session_windows(args)
    daily, stats = build_daily_sessions(df, windows, min_trades_per_session=10, volume_profile_bins=10, value_area_pct=0.70)
    assert not daily.empty
    assert stats["complete_days"] >= 1