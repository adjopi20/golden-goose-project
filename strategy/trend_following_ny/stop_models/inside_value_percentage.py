from __future__ import annotations

from dataclasses import dataclass, field

from session.context import PreNYContext


@dataclass(frozen=True)
class StopDecision:
    status: str
    stop_model: str
    stop_price: float | None
    reference_price: float | None
    buffer_pct: float | None
    reason: str
    metadata: dict = field(default_factory=dict)


def decide_inside_value_percentage_stop(pre_ny_context: PreNYContext, direction: str, config) -> StopDecision:
    if direction == "SHORT":
        stop_price = pre_ny_context.europe_val * (1 + config.inside_value_stop_pct)
        reference_price = pre_ny_context.europe_val
    elif direction == "LONG":
        stop_price = pre_ny_context.europe_vah * (1 - config.inside_value_stop_pct)
        reference_price = pre_ny_context.europe_vah
    else:
        return StopDecision("NO_VALID_STOP", "INSIDE_VALUE_PERCENTAGE", None, None, config.inside_value_stop_pct, "UNSUPPORTED_DIRECTION")
    if not (pre_ny_context.europe_val <= stop_price <= pre_ny_context.europe_vah):
        return StopDecision("STOP_OUTSIDE_VALUE_AREA", "INSIDE_VALUE_PERCENTAGE", None, reference_price, config.inside_value_stop_pct, "STOP_OUTSIDE_VALUE_AREA")
    return StopDecision("STOP_CREATED", "INSIDE_VALUE_PERCENTAGE", float(stop_price), float(reference_price), config.inside_value_stop_pct, "INSIDE_VALUE_PERCENTAGE_STOP_CREATED")