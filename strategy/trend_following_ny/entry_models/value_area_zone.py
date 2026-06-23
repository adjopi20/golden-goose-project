from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from session.context import PreNYContext


@dataclass(frozen=True)
class EntryDecision:
    status: str
    direction: str | None
    signal_timestamp: pd.Timestamp | None
    signal_raw_index: int | None
    signal_price: float | None
    reference_level: float | None
    distance_from_reference_pct: float | None
    reason: str
    confirmation_complete: bool
    research_status: str
    metadata: dict = field(default_factory=dict)


def evaluate_value_area_zone_entry(pre_ny_context: PreNYContext, ny_trades: pd.DataFrame, config) -> EntryDecision:
    if ny_trades.empty:
        return EntryDecision("INSUFFICIENT_DATA", None, None, None, None, None, None, "EMPTY_NY_TRADES", False, "ARCHITECTURE_VALIDATION_ONLY")
    for _, row in ny_trades.iterrows():
        price = float(row["price"])
        ts = pd.Timestamp(row["timestamp"])
        raw_index = int(row["raw_index"])
        if pre_ny_context.europe_vah < price <= pre_ny_context.europe_vah * (1 + config.entry_zone_pct):
            return EntryDecision("SIGNAL_CREATED", "LONG", ts, raw_index, price, pre_ny_context.europe_vah, (price - pre_ny_context.europe_vah) / pre_ny_context.europe_vah, "LOCATION_ONLY_DIAGNOSTIC_LONG", False, "ARCHITECTURE_VALIDATION_ONLY", {"confirmation_mode": config.confirmation_mode})
        if pre_ny_context.europe_val * (1 - config.entry_zone_pct) <= price < pre_ny_context.europe_val:
            return EntryDecision("SIGNAL_CREATED", "SHORT", ts, raw_index, price, pre_ny_context.europe_val, (pre_ny_context.europe_val - price) / pre_ny_context.europe_val, "LOCATION_ONLY_DIAGNOSTIC_SHORT", False, "ARCHITECTURE_VALIDATION_ONLY", {"confirmation_mode": config.confirmation_mode})
    return EntryDecision("NO_ENTRY_SIGNAL", None, None, None, None, None, None, "PRICE_NEVER_REACHED_VALID_OUTSIDE_VALUE_ZONE", False, "ARCHITECTURE_VALIDATION_ONLY", {"confirmation_mode": config.confirmation_mode})