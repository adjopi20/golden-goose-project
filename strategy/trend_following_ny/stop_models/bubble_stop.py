from __future__ import annotations

from .inside_value_percentage import StopDecision


def decide_bubble_stop(*args, **kwargs) -> StopDecision:
    return StopDecision("NO_VALID_BUBBLE_BOUNDARY", "BUBBLE_STOP", None, None, None, "NO_VALID_BUBBLE_BOUNDARY")