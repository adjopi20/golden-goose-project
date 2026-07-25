import pandas as pd

from models.orb.scripts.backtest_orb_follow_candle_entry import (
    _failure_rejects,
    _reject_follow,
    _stop_price,
    _timestamp_ms,
)


def test_rejects_only_when_all_three_follow_conditions_fail() -> None:
    assert _reject_follow("long", 101.0, 100.0, 100.0, -0.1)
    assert not _reject_follow("long", 99.0, 100.5, 100.0, -0.1)
    assert _reject_follow("short", 99.0, 100.0, 100.0, 0.1)
    assert not _reject_follow("short", 101.0, 99.5, 100.0, 0.1)


def test_entry_timestamp_matches_millisecond_cache_keys() -> None:
    values = pd.Series(pd.to_datetime(["2020-01-06T09:49:00-05:00"], utc=True))
    assert _timestamp_ms(values).iloc[0] == 1_578_322_140_000


def test_selects_requested_stop_model() -> None:
    assert _stop_price("long", "poc", 100.0, 95.0, 105.0) == 100.0
    assert _stop_price("long", "opposite_extreme", 100.0, 95.0, 105.0) == 95.0
    assert _stop_price("short", "opposite_extreme", 100.0, 95.0, 105.0) == 105.0


def test_failure_filter_is_pluggable_without_looking_ahead_on_immediate_entries() -> None:
    assert not _failure_rejects("retest_continuation", False, True)
    assert _failure_rejects("retest_continuation", True, True)
    assert _failure_rejects("held_outside_pullback", True, True)
    assert not _failure_rejects("immediate_expansion", True, True)
