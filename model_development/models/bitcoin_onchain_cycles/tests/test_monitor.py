from __future__ import annotations

import pytest

from model_development.models.bitcoin_onchain_cycles.run_daily_monitor import (
    classify_accumulation_signal,
    describe_zone,
)


@pytest.mark.parametrize(
    ("zscore", "previous", "expected"),
    [
        (5.0, 4.0, "PAUSE"),
        (7.2, 6.9, "PAUSE"),
        (1.9, 2.1, "TIER_1"),
        (0.4, 0.6, "TIER_2"),
        (-0.3, 0.1, "TIER_3"),
        (1.4, 1.2, "ZONE"),
        (0.3, 0.1, "ZONE"),
        (3.0, 3.2, "DCA"),
    ],
)
def test_accumulation_signal_never_sells(
    zscore: float, previous: float, expected: str
) -> None:
    assert classify_accumulation_signal(zscore, previous) == expected


@pytest.mark.parametrize(
    ("zscore", "expected"),
    [
        (-0.5, "tier_3"),
        (0.0, "tier_2"),
        (1.0, "tier_1"),
        (4.9, "neutral"),
        (5.0, "euphoria"),
    ],
)
def test_zone_labels_track_deepest_tier(zscore: float, expected: str) -> None:
    assert describe_zone(zscore) == expected


def test_signal_ignores_a_rising_score_inside_a_tier() -> None:
    assert classify_accumulation_signal(0.4, -0.5) == "ZONE"
    assert classify_accumulation_signal(0.4, 0.3) == "ZONE"
