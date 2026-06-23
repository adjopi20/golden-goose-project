from __future__ import annotations

from .inside_value_percentage import StopDecision


def decide_nearby_high_low_stop(*args, **kwargs) -> StopDecision:
    return StopDecision("NO_VALID_STRUCTURE", "NEARBY_HIGH_LOW", None, None, None, "NO_VALID_STRUCTURE")