from datetime import date

import numpy as np
import pandas as pd

from model_development.models.trend_following_preny_profile_15m.legacy.audit_opportunity import (
    feature_snapshot,
    first_edge_test,
    hourly_indicators,
    session_excursion,
    stop_first_path,
)
from model_development.models.trend_following_preny_profile_15m.legacy.strategy import clock_ms


def _minutes(start, prices, *, highs=None, lows=None):
    highs = highs or prices
    lows = lows or prices
    return pd.DataFrame({
        "timestamp_ms": [start + i*60_000 for i in range(len(prices))],
        "open": prices, "high": highs, "low": lows, "close": prices,
        "volume": [1.0]*len(prices), "delta": [0.5]*len(prices),
    })


def test_fill_snapshot_excludes_unfinished_entry_minute():
    day = date(2026, 8, 19)
    start = clock_ms(day, 9)
    minutes = _minutes(start-60_000, [100.0, 200.0])
    profile = dict(session_day=str(day), profile_window="pre_ny", val=90, poc=100, vah=110)
    as_of = start+1
    row = feature_snapshot(minutes, minutes.timestamp_ms.to_numpy(dtype=np.int64),
                           hourly_indicators(minutes), as_of, profile)
    assert row["price"] == 100.0
    assert row["last_completed_minute_cutoff_ms"] == start
    assert row["vwap_ny_bar_approx"] is None


def test_hourly_features_use_only_completed_hours_not_future_prices():
    day = date(2024, 1, 5)
    start = clock_ms(day, 9)
    # Twenty-five completed hours at 100, followed by two future hours at 200.
    minutes = _minutes(start-25*3_600_000, [100.0]*(25*60) + [200.0]*(2*60))
    hourly = hourly_indicators(minutes)
    profile = dict(session_day=str(day), profile_window="pre_ny", val=90, poc=100, vah=110)
    row = feature_snapshot(minutes, minutes.timestamp_ms.to_numpy(dtype=np.int64),
                           hourly, start, profile)
    assert row["last_completed_hour_end_ms"] == start
    assert row["ema20_gap_pct"] == 0.0
    assert hourly["end_ms"].iloc[0] > minutes["timestamp_ms"].iloc[0]


def test_stop_first_mfe_bounds_avoid_claiming_intraminute_order():
    start = 1_800_000
    minutes = _minutes(start, [100.0, 103.0, 101.0],
                       highs=[102.0, 105.0, 108.0], lows=[99.0, 100.0, 94.0])
    stamps = minutes.timestamp_ms.to_numpy(dtype=np.int64)
    result = stop_first_path(minutes, stamps, entry_ms=start, entry_price=100.0,
                             direction="long", stop=95.0, force_exit_ms=start+180_000,
                             entry_at_minute_open=True)
    assert result["status"] == "stop_touched"
    assert result["mfe_before_stop_lower_r"] == 1.0  # 105 before stop minute
    assert result["mfe_before_stop_upper_r"] == 1.6  # 108 may precede stop
    assert result["intraminute_order_ambiguous"]


def test_no_stop_path_and_invalid_stop_geometry():
    start = 3_600_000
    minutes = _minutes(start, [100.0, 102.0])
    stamps = minutes.timestamp_ms.to_numpy(dtype=np.int64)
    result = stop_first_path(minutes, stamps, entry_ms=start, entry_price=100.0,
                             direction="long", stop=99.0, force_exit_ms=start+120_000,
                             entry_at_minute_open=True)
    assert result["status"] == "time_exit"
    assert result["mfe_before_stop_lower_pct"] == 2.0
    invalid = stop_first_path(minutes, stamps, entry_ms=start, entry_price=100.0,
                              direction="long", stop=101.0, force_exit_ms=start+120_000)
    assert invalid["status"] == "invalid_stop_geometry"


def test_first_edge_close_is_not_delta_filtered():
    day = date(2026, 8, 19)
    start = clock_ms(day, 9)
    profile = dict(session_day=str(day), val=90, poc=100, vah=110)
    bars = [dict(open_timestamp_ms=start, close_timestamp_ms=start+900_000, close=100),
            dict(open_timestamp_ms=start+900_000, close_timestamp_ms=start+1_800_000,
                 close=111, buy_volume=0.0, sell_volume=100.0)]
    edge = first_edge_test(profile, bars)
    assert edge["direction"] == "long"
    assert edge["signal_timestamp_ms"] == start+1_800_000


def test_session_opportunity_has_both_directions_without_trade():
    day = date(2026, 8, 19)
    start = clock_ms(day, 9)
    end = clock_ms(date(2026, 8, 20), 8, 59)
    minutes = _minutes(start, [100.0, 105.0, 96.0],
                       highs=[101.0, 106.0, 100.0], lows=[99.0, 102.0, 95.0])
    # The later candle proves the cache extends beyond the measured horizon.
    later = _minutes(end, [97.0])
    minutes = pd.concat([minutes, later], ignore_index=True)
    result = session_excursion(minutes, minutes.timestamp_ms.to_numpy(dtype=np.int64), day, 5.0)
    assert result["status"] == "complete"
    assert result["max_up_pct"] == 6.0
    assert result["max_down_pct"] == 5.0
    assert result["max_up_atr"] == 1.2
