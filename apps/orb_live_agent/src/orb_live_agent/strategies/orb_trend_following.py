from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo


def decide_algorithm(snapshot: dict[str, Any], trigger_observation: dict[str, Any]) -> dict[str, Any]:
    snapshot = {**snapshot, "orderflow_features": trigger_observation.get("orderflow_features")}
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
    strong_body = body / range_ >= float(snapshot.get("orb_min_candidate_body_ratio", 0.35))
    close_pos = (close - low) / range_
    short_max_close_position = float(snapshot.get("orb_short_max_close_position", 0.45))
    long_min_close_position = float(snapshot.get("orb_long_min_close_position", 0.55))
    require_directional_delta = bool(snapshot.get("orb_require_directional_delta", True))
    min_preentry_delta_ratio = float(snapshot.get("orb_min_preentry_delta_ratio", 0.05))
    reasons = set(trigger_observation.get("reasons") or [])
    orb_low, orb_high = _orb_extremes(snapshot)
    if orb_low is None or orb_high is None:
        return _wait(snapshot, "missing_ny_first_15m_profile")

    context = _pre_entry_context(snapshot, orb_low, orb_high)
    first_inefficiency_direction = _first_price_inefficiency_direction(snapshot, orb_low, orb_high)

    if (
        close < orb_low
        and high >= orb_low
        and _directional_delta_passes(delta, "short", require_directional_delta)
        and strong_body
        and close_pos <= short_max_close_position
    ):
        if first_inefficiency_direction is None:
            return _wait(snapshot, "wait_for_first_price_inefficiency")
        if first_inefficiency_direction != "short":
            return _wait(snapshot, "reject_short_not_fastest_price_inefficiency")
        direct_displacement = _is_direct_displacement(snapshot, "short")
        if _reject_opposite_touch(snapshot, bool(context["opposite_touched_for_short"]), direct_displacement):
            return _wait(snapshot, "reject_opposite_orb_touched_before_short")
        if context["short_delta_ratio"] < min_preentry_delta_ratio:
            return _wait(snapshot, "reject_weak_pre_entry_short_delta")
        if not _has_breakout_retest(snapshot, "short", orb_low, orb_high) and not direct_displacement:
            return _wait(snapshot, "wait_for_short_breakout_retest_continuation")
        if not _passes_volume_expansion_filter(snapshot):
            return _wait(snapshot, "reject_low_volume_expansion")
        if not _passes_supportive_bubble_filter(snapshot, "short"):
            return _wait(snapshot, "reject_weak_supportive_bubble_ratio")
        stop = _stop_level(snapshot, "short", orb_low, orb_high)
        if stop is None:
            return _wait(snapshot, "missing_or_invalid_orb_stop_level")
        return _take(snapshot, "short", close, stop[0], "algorithm_short_orb_continuation_ny_first_15m_session_low", stop[1])

    if (
        close > orb_high
        and low <= orb_high
        and _directional_delta_passes(delta, "long", require_directional_delta)
        and strong_body
        and close_pos >= long_min_close_position
    ):
        if first_inefficiency_direction is None:
            return _wait(snapshot, "wait_for_first_price_inefficiency")
        if first_inefficiency_direction != "long":
            return _wait(snapshot, "reject_long_not_fastest_price_inefficiency")
        direct_displacement = _is_direct_displacement(snapshot, "long")
        if _reject_opposite_touch(snapshot, bool(context["opposite_touched_for_long"]), direct_displacement):
            return _wait(snapshot, "reject_opposite_orb_touched_before_long")
        if context["long_delta_ratio"] < min_preentry_delta_ratio:
            return _wait(snapshot, "reject_weak_pre_entry_long_delta")
        if not _has_breakout_retest(snapshot, "long", orb_low, orb_high) and not direct_displacement:
            return _wait(snapshot, "wait_for_long_breakout_retest_continuation")
        if not _passes_volume_expansion_filter(snapshot):
            return _wait(snapshot, "reject_low_volume_expansion")
        if not _passes_supportive_bubble_filter(snapshot, "long"):
            return _wait(snapshot, "reject_weak_supportive_bubble_ratio")
        stop = _stop_level(snapshot, "long", orb_low, orb_high)
        if stop is None:
            return _wait(snapshot, "missing_or_invalid_orb_stop_level")
        return _take(snapshot, "long", close, stop[0], "algorithm_long_orb_continuation_ny_first_15m_session_high", stop[1])

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
    current_dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc).astimezone(tz)
    start = _orb_entry_start(current_dt, snapshot, tz)
    window_minutes = int(snapshot.get("orb_entry_window_minutes") or 30)
    return start <= current_dt < start + timedelta(minutes=window_minutes)


def _orb_extremes(snapshot: dict[str, Any]) -> tuple[float | None, float | None]:
    profile = snapshot.get("ny_first_15m_profile") or {}
    low = profile.get("session_low")
    high = profile.get("session_high")
    return (float(low) if low is not None else None, float(high) if high is not None else None)


def _passes_volume_expansion_filter(snapshot: dict[str, Any]) -> bool:
    min_ratio = snapshot.get("orb_min_volume_expansion_ratio")
    if min_ratio is None:
        return True
    features = snapshot.get("orderflow_features") or {}
    ratio = features.get("volume_expansion_ratio")
    return ratio is not None and float(ratio) >= float(min_ratio)


def _passes_supportive_bubble_filter(snapshot: dict[str, Any], direction: str) -> bool:
    min_ratio = snapshot.get("orb_min_supportive_bubble_qty_ratio")
    if min_ratio is None:
        return True
    features = snapshot.get("orderflow_features") or {}
    buy_qty = float(features.get("buy_bubble_qty", 0.0) or 0.0)
    sell_qty = float(features.get("sell_bubble_qty", 0.0) or 0.0)
    supportive_qty = buy_qty if direction == "long" else sell_qty
    opposing_qty = sell_qty if direction == "long" else buy_qty
    if supportive_qty <= 0 and opposing_qty <= 0:
        return True
    if opposing_qty <= 0:
        return supportive_qty > 0
    return supportive_qty / opposing_qty >= float(min_ratio)


def _pre_entry_context(snapshot: dict[str, Any], orb_low: float, orb_high: float) -> dict[str, float | bool]:
    current_ms = int(snapshot.get("snapshot_timestamp_ms") or 0)
    tz = ZoneInfo(str(snapshot.get("session_timezone") or "America/New_York"))
    current_dt = datetime.fromtimestamp(current_ms / 1000.0, tz=timezone.utc).astimezone(tz)
    orb_entry_start = _orb_entry_start(current_dt, snapshot, tz)
    orb_entry_start_ms = int(orb_entry_start.astimezone(timezone.utc).timestamp() * 1000)
    recent = snapshot.get("recent_candles") or []
    prior = [c for c in recent if orb_entry_start_ms <= int(c.get("timestamp_ms", 0)) < current_ms]
    lookback_minutes = max(1, int(snapshot.get("orb_preentry_delta_lookback_minutes", 15)))
    delta_window = [c for c in recent if current_ms - lookback_minutes * 60_000 <= int(c.get("timestamp_ms", 0)) < current_ms]
    buy_delta = sum(float(c.get("delta", 0.0)) for c in delta_window)
    volume = sum(float(c.get("volume", 0.0)) for c in delta_window)
    return {
        "long_delta_ratio": buy_delta / volume if volume > 0 else 0.0,
        "short_delta_ratio": -buy_delta / volume if volume > 0 else 0.0,
        "opposite_touched_for_long": any(float(c.get("low", 0.0)) <= orb_low for c in prior),
        "opposite_touched_for_short": any(float(c.get("high", 0.0)) >= orb_high for c in prior),
    }


def _has_breakout_retest(snapshot: dict[str, Any], direction: str, orb_low: float, orb_high: float) -> bool:
    current_ms = int(snapshot.get("snapshot_timestamp_ms") or 0)
    tz = ZoneInfo(str(snapshot.get("session_timezone") or "America/New_York"))
    current_dt = datetime.fromtimestamp(current_ms / 1000.0, tz=timezone.utc).astimezone(tz)
    start_ms = int(_orb_entry_start(current_dt, snapshot, tz).astimezone(timezone.utc).timestamp() * 1000)
    prior = sorted(
        (c for c in snapshot.get("recent_candles") or [] if start_ms <= int(c.get("timestamp_ms", 0)) < current_ms),
        key=lambda c: int(c.get("timestamp_ms", 0)),
    )
    seen_breakout = False
    for candle in prior:
        close = float(candle.get("close", 0.0))
        if direction == "short":
            if not seen_breakout:
                seen_breakout = close < orb_low
            elif float(candle.get("high", close)) >= orb_low and close <= orb_low:
                return True
        else:
            if not seen_breakout:
                seen_breakout = close > orb_high
            elif float(candle.get("low", close)) <= orb_high and close >= orb_high:
                return True
    return False


def _is_direct_displacement(snapshot: dict[str, Any], direction: str) -> bool:
    current_ms = int(snapshot.get("snapshot_timestamp_ms") or 0)
    prior = [c for c in snapshot.get("recent_candles") or [] if int(c.get("timestamp_ms", 0)) < current_ms]
    return _is_direct_displacement_candle(snapshot.get("last_candle") or {}, direction, prior, snapshot)


def _first_price_inefficiency_direction(snapshot: dict[str, Any], orb_low: float, orb_high: float) -> str | None:
    current_ms = int(snapshot.get("snapshot_timestamp_ms") or 0)
    tz = ZoneInfo(str(snapshot.get("session_timezone") or "America/New_York"))
    current_dt = datetime.fromtimestamp(current_ms / 1000.0, tz=timezone.utc).astimezone(tz)
    start_ms = int(_orb_entry_start(current_dt, snapshot, tz).astimezone(timezone.utc).timestamp() * 1000)
    candles = sorted(
        (c for c in snapshot.get("recent_candles") or [] if start_ms <= int(c.get("timestamp_ms", 0)) <= current_ms),
        key=lambda c: int(c.get("timestamp_ms", 0)),
    )
    last_candle = dict(snapshot.get("last_candle") or {})
    if current_ms and any(int(c.get("timestamp_ms", 0)) == current_ms for c in candles):
        candles = [
            {**c, **last_candle, "timestamp_ms": current_ms} if int(c.get("timestamp_ms", 0)) == current_ms else c
            for c in candles
        ]
    elif current_ms:
        current = dict(snapshot.get("last_candle") or {})
        current["timestamp_ms"] = current_ms
        candles.append(current)

    prior: list[dict[str, Any]] = []
    for candle in candles:
        close = float(candle.get("close", 0.0))
        high = float(candle.get("high", close))
        low = float(candle.get("low", close))
        delta = float(candle.get("delta", 0.0))
        require_directional_delta = bool(snapshot.get("orb_require_directional_delta", True))
        if (
            close < orb_low
            and high >= orb_low
            and _directional_delta_passes(delta, "short", require_directional_delta)
            and _is_direct_displacement_candle(candle, "short", prior, snapshot)
        ):
            return "short"
        if (
            close > orb_high
            and low <= orb_high
            and _directional_delta_passes(delta, "long", require_directional_delta)
            and _is_direct_displacement_candle(candle, "long", prior, snapshot)
        ):
            return "long"
        prior.append(candle)
    return None


def _is_direct_displacement_candle(
    candle: dict[str, Any],
    direction: str,
    prior: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> bool:
    range_ = _candle_range(candle)
    body_ratio = _candle_body(candle, direction) / range_
    close_pos = (float(candle.get("close", 0.0)) - float(candle.get("low", 0.0))) / range_
    if body_ratio < float(snapshot.get("orb_direct_min_body_ratio", 0.65)):
        return False
    if direction == "short" and close_pos > float(snapshot.get("orb_direct_short_max_close_position", 0.30)):
        return False
    if direction == "long" and close_pos < float(snapshot.get("orb_direct_long_min_close_position", 0.70)):
        return False

    ranges = [_candle_range(c) for c in prior[-30:] if _candle_range(c) > 0]
    range_ratio = range_ / median(ranges) if ranges else 0.0
    volume = float(candle.get("volume", 0.0))
    delta_ratio = abs(float(candle.get("delta", 0.0))) / volume if volume > 0 else 0.0
    return (
        range_ratio >= float(snapshot.get("orb_direct_min_range_ratio", 1.5))
        or delta_ratio >= float(snapshot.get("orb_direct_min_delta_ratio", 0.85))
    )


def _directional_delta_passes(delta: float, direction: str, required: bool) -> bool:
    return not required or (delta > 0 if direction == "long" else delta < 0)


def _orb_entry_start(current_dt: datetime, snapshot: dict[str, Any], tz: ZoneInfo) -> datetime:
    value = str(snapshot.get("orb_entry_start_time") or "09:45")
    hour, minute = value.split(":", maxsplit=1)
    return datetime.combine(current_dt.date(), time(int(hour), int(minute)), tzinfo=tz)


def _reject_opposite_touch(snapshot: dict[str, Any], touched: bool, direct_displacement: bool) -> bool:
    if not touched:
        return False
    policy = str(snapshot.get("orb_opposite_touch_policy", "strict")).lower()
    if policy == "ignore":
        return False
    if policy == "displacement_override":
        return not direct_displacement
    return True


def _stop_level(
    snapshot: dict[str, Any],
    direction: str,
    orb_low: float,
    orb_high: float,
) -> tuple[float, str] | None:
    model = str(snapshot.get("orb_stop_model", "opposite_extreme")).lower()
    profile = snapshot.get("ny_first_15m_profile") or {}
    if model == "opposite_extreme":
        price = orb_low if direction == "long" else orb_high
        label = "low" if direction == "long" else "high"
        return price, f"opposite ORB {label} {price:.6f}"
    if model == "poc":
        price = profile.get("poc_price")
        return (float(price), f"ORB POC {float(price):.6f}") if price is not None else None
    if model == "opposite_value_area":
        field = "val" if direction == "long" else "vah"
        price = profile.get(field)
        return (float(price), f"ORB {field.upper()} {float(price):.6f}") if price is not None else None
    return None


def _candle_range(candle: dict[str, Any]) -> float:
    explicit_range = float(candle.get("range", 0.0))
    if explicit_range > 0:
        return explicit_range
    high = float(candle.get("high", candle.get("close", 0.0)))
    low = float(candle.get("low", candle.get("close", 0.0)))
    return max(high - low, 1e-12)


def _candle_body(candle: dict[str, Any], direction: str) -> float:
    if candle.get("body") is not None:
        return abs(float(candle.get("body", 0.0)))
    close = float(candle.get("close", 0.0))
    if candle.get("open") is not None:
        return abs(close - float(candle.get("open", close)))
    reference = float(candle.get("high" if direction == "short" else "low", close))
    return abs(close - reference)


def _wait(snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
    decision = {
        "decision": "WAIT",
        "reason": reason,
        "provider": "algorithm",
        "strategy": "orb_trend_following",
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
    }
    if snapshot.get("orderflow_features") is not None:
        decision["orderflow_features"] = snapshot["orderflow_features"]
    return decision


def _take(snapshot: dict[str, Any], direction: str, entry: float, stop: float, reason: str, invalidation: str) -> dict[str, Any]:
    decision = {
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
    if snapshot.get("orderflow_features") is not None:
        decision["orderflow_features"] = snapshot["orderflow_features"]
    return decision
