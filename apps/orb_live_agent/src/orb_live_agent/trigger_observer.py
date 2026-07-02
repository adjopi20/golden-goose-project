from __future__ import annotations

from statistics import median
from typing import Any


def _crosses(candle: dict[str, Any], level: float) -> bool:
    return float(candle["low"]) <= level <= float(candle["high"])


def _closed_beyond(candle: dict[str, Any], level: float) -> str | None:
    close = float(candle["close"])
    if close > level:
        return "above"
    if close < level:
        return "below"
    return None


def _add_level_reasons(reasons: list[str], candle: dict[str, Any], label: str, low: float | None, high: float | None) -> None:
    if high is not None:
        high = float(high)
        if _crosses(candle, high):
            reasons.append(f"{label}_high_touched")
        if _closed_beyond(candle, high) == "above":
            reasons.append(f"{label}_high_closed_above")
    if low is not None:
        low = float(low)
        if _crosses(candle, low):
            reasons.append(f"{label}_low_touched")
        if _closed_beyond(candle, low) == "below":
            reasons.append(f"{label}_low_closed_below")


def observe_triggers(snapshot: dict[str, Any], bubbles: list[dict[str, Any]], reference_levels: dict[str, Any] | None = None) -> dict[str, Any]:
    candle = snapshot["last_candle"]
    reasons: list[str] = []
    levels = reference_levels or snapshot

    session_extremes = levels.get("session_extremes") or {}
    for label, extreme in session_extremes.items():
        if extreme:
            _add_level_reasons(reasons, candle, label, extreme.get("low"), extreme.get("high"))

    previous_profile = levels.get("previous_24h_profile_for_session")
    if previous_profile:
        for field in ("poc_price", "val", "vah"):
            level = previous_profile.get(field)
            if level is not None and _crosses(candle, float(level)):
                reasons.append(f"previous_24h_{field}_touched")

    ny_profile = levels.get("ny_first_15m_profile")
    if ny_profile:
        _add_level_reasons(reasons, candle, "ny_first_15m_profile", ny_profile.get("session_low"), ny_profile.get("session_high"))

    if bubbles:
        reasons.append("order_bubble_in_closed_minute")

    recent = snapshot.get("recent_candles") or []
    prior = recent[:-1]
    if len(prior) >= 10:
        ranges = [float(c["range"]) for c in prior[-30:] if float(c["range"]) > 0]
        bodies = [float(c["body"]) for c in prior[-30:] if float(c["body"]) > 0]
        deltas = [abs(float(c["delta"])) for c in prior[-30:] if abs(float(c["delta"])) > 0]
        if ranges and float(candle["range"]) >= 2.0 * median(ranges):
            reasons.append("abnormal_range_vs_recent")
        if bodies and float(candle["body"]) >= 2.0 * median(bodies):
            reasons.append("abnormal_body_vs_recent")
        if deltas and abs(float(candle["delta"])) >= 2.0 * median(deltas):
            reasons.append("abnormal_delta_vs_recent")

    return {
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
        "setup_observation_active": bool(snapshot.get("setup_observation_active")),
        "triggered": bool(reasons),
        "reasons": sorted(set(reasons)),
        "mode": "observe_only",
    }
