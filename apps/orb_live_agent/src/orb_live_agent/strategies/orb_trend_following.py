from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


def decide_algorithm(snapshot: dict[str, Any], trigger_observation: dict[str, Any]) -> dict[str, Any]:
    if not trigger_observation.get("triggered"):
        return _wait(snapshot, "no_trigger_observed")
    if not _in_orb_bias_window(snapshot):
        return _wait(snapshot, "outside_orb_bias_window")

    candle = snapshot.get("last_candle") or {}
    close = float(candle.get("close", 0.0))
    high = float(candle.get("high", close))
    low = float(candle.get("low", close))
    if close <= 0:
        return _wait(snapshot, "missing_last_candle")

    body = abs(float(candle.get("body", 0.0)))
    range_ = max(float(candle.get("range", 0.0)), 1e-12)
    delta = float(candle.get("delta", 0.0))
    strong_body = body / range_ >= 0.35
    close_pos = (close - low) / range_
    reasons = set(trigger_observation.get("reasons") or [])
    orb_low, orb_high = _orb_extremes(snapshot)
    if orb_low is None or orb_high is None:
        return _wait(snapshot, "missing_ny_first_15m_profile")

    context = _pre_entry_context(snapshot, orb_low, orb_high)

    if close < orb_low and high >= orb_low and delta < 0 and strong_body and close_pos <= 0.45:
        if context["opposite_touched_for_short"]:
            return _wait(snapshot, "reject_opposite_orb_touched_before_short")
        if context["short_delta_ratio"] < 0.05:
            return _wait(snapshot, "reject_weak_pre_entry_short_delta")
        # ponytail: parameter-optimize ORB SL after setup filtering is stable.
        return _take(snapshot, "short", close, orb_high, "algorithm_short_breakdown_ny_first_15m_session_low", f"opposite ORB high {orb_high:.6f}")

    if close > orb_high and low <= orb_high and delta > 0 and strong_body and close_pos >= 0.55:
        if context["opposite_touched_for_long"]:
            return _wait(snapshot, "reject_opposite_orb_touched_before_long")
        if context["long_delta_ratio"] < 0.05:
            return _wait(snapshot, "reject_weak_pre_entry_long_delta")
        # ponytail: parameter-optimize ORB SL after setup filtering is stable.
        return _take(snapshot, "long", close, orb_low, "algorithm_long_breakout_ny_first_15m_session_high", f"opposite ORB low {orb_low:.6f}")

    if any(reason.endswith("_closed_below") for reason in reasons) and delta > 0:
        return _wait(snapshot, "reject_breakdown_absorption_positive_delta")
    if any(reason.endswith("_closed_above") for reason in reasons) and delta < 0:
        return _wait(snapshot, "reject_breakout_absorption_negative_delta")
    return _wait(snapshot, "no_algorithm_trend_setup")


def _in_orb_bias_window(snapshot: dict[str, Any]) -> bool:
    timestamp_ms = snapshot.get("snapshot_timestamp_ms")
    if timestamp_ms is None:
        return False
    tz = ZoneInfo(str(snapshot.get("session_timezone") or "America/New_York"))
    t = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc).astimezone(tz).time()
    return time(9, 45) <= t < time(10, 15)


def _orb_extremes(snapshot: dict[str, Any]) -> tuple[float | None, float | None]:
    profile = snapshot.get("ny_first_15m_profile") or {}
    low = profile.get("session_low")
    high = profile.get("session_high")
    return (float(low) if low is not None else None, float(high) if high is not None else None)


def _pre_entry_context(snapshot: dict[str, Any], orb_low: float, orb_high: float) -> dict[str, float | bool]:
    current_ms = int(snapshot.get("snapshot_timestamp_ms") or 0)
    tz = ZoneInfo(str(snapshot.get("session_timezone") or "America/New_York"))
    current_dt = datetime.fromtimestamp(current_ms / 1000.0, tz=timezone.utc).astimezone(tz)
    orb_entry_start = datetime.combine(current_dt.date(), time(9, 45), tzinfo=tz)
    orb_entry_start_ms = int(orb_entry_start.astimezone(timezone.utc).timestamp() * 1000)
    recent = snapshot.get("recent_candles") or []
    prior = [c for c in recent if orb_entry_start_ms <= int(c.get("timestamp_ms", 0)) < current_ms]
    pre15 = [c for c in recent if current_ms - 15 * 60_000 <= int(c.get("timestamp_ms", 0)) < current_ms]
    buy_delta = sum(float(c.get("delta", 0.0)) for c in pre15)
    volume = sum(float(c.get("volume", 0.0)) for c in pre15)
    return {
        "long_delta_ratio": buy_delta / volume if volume > 0 else 0.0,
        "short_delta_ratio": -buy_delta / volume if volume > 0 else 0.0,
        "opposite_touched_for_long": any(float(c.get("low", 0.0)) <= orb_low for c in prior),
        "opposite_touched_for_short": any(float(c.get("high", 0.0)) >= orb_high for c in prior),
    }


def _wait(snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "decision": "WAIT",
        "reason": reason,
        "provider": "algorithm",
        "strategy": "orb_trend_following",
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
    }


def _take(snapshot: dict[str, Any], direction: str, entry: float, stop: float, reason: str, invalidation: str) -> dict[str, Any]:
    return {
        "decision": "TAKE",
        "entry_model": "trend",
        "strategy": "orb_trend_following",
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "reason": reason,
        "invalidation": invalidation,
        "provider": "algorithm",
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
    }
