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

    if any(b.get("is_primary", True) for b in bubbles):
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
        "orderflow_features": _orderflow_features(snapshot, bubbles),
        "mode": "observe_only",
    }


def _bubble_side(bubble: dict[str, Any]) -> str:
    if bubble.get("aggressive_side") in {"buy", "sell"}:
        return str(bubble["aggressive_side"])
    return "sell" if bool(bubble.get("is_buyer_maker")) else "buy"


def _orderflow_features(snapshot: dict[str, Any], bubbles: list[dict[str, Any]]) -> dict[str, Any]:
    candle = snapshot["last_candle"]
    recent = snapshot.get("recent_candles") or []
    prior = [c for c in recent if int(c.get("timestamp_ms", 0)) < int(candle.get("timestamp_ms", 0))]
    prior_volumes = [float(c.get("volume", 0.0)) for c in prior[-30:] if float(c.get("volume", 0.0)) > 0]
    prior_ranges = [float(c.get("range", 0.0)) for c in prior[-30:] if float(c.get("range", 0.0)) > 0]
    prior_bodies = [float(c.get("body", 0.0)) for c in prior[-30:] if float(c.get("body", 0.0)) > 0]
    p99_bubbles = [b for b in bubbles if bool(b.get("is_p99"))]
    bubbles = [b for b in bubbles if b.get("is_primary", True)]
    buy_bubbles = [b for b in bubbles if _bubble_side(b) == "buy"]
    sell_bubbles = [b for b in bubbles if _bubble_side(b) == "sell"]
    p99_buy_bubbles = [b for b in p99_bubbles if _bubble_side(b) == "buy"]
    p99_sell_bubbles = [b for b in p99_bubbles if _bubble_side(b) == "sell"]
    largest = max(bubbles, key=lambda b: float(b.get("qty", 0.0)), default=None)
    volume = float(candle.get("volume", 0.0))
    delta = float(candle.get("delta", 0.0))
    candle_range = float(candle.get("range", 0.0))
    body = float(candle.get("body", 0.0))
    open_price = float(candle.get("open", 0.0))
    close_price = float(candle.get("close", 0.0))
    low = float(candle.get("low", 0.0))
    upper_wick = float(candle.get("upper_wick", 0.0))
    lower_wick = float(candle.get("lower_wick", 0.0))

    def _cvd(window: int) -> tuple[float, float]:
        rows = recent[-window:]
        cvd = sum(float(c.get("delta", 0.0)) for c in rows)
        total = sum(float(c.get("volume", 0.0)) for c in rows)
        return cvd, cvd / total if total > 0 else 0.0

    def _bubble_vwap(rows: list[dict[str, Any]]) -> float | None:
        qty = sum(float(row.get("qty", 0.0)) for row in rows)
        return sum(float(row.get("price", 0.0)) * float(row.get("qty", 0.0)) for row in rows) / qty if qty > 0 else None

    cvd5, cvd5_ratio = _cvd(5)
    cvd15, cvd15_ratio = _cvd(15)
    cvd30, cvd30_ratio = _cvd(30)
    bubble_qty = sum(float(b.get("qty", 0.0)) for b in bubbles)
    bubble_delta = sum(float(b.get("qty", 0.0)) * (1 if _bubble_side(b) == "buy" else -1) for b in bubbles)
    p99_qty = sum(float(b.get("qty", 0.0)) for b in p99_bubbles)
    p99_delta = sum(float(b.get("qty", 0.0)) * (1 if _bubble_side(b) == "buy" else -1) for b in p99_bubbles)
    volume_expansion = volume / median(prior_volumes) if prior_volumes else 0.0
    range_expansion = candle_range / median(prior_ranges) if prior_ranges else 0.0
    body_expansion = body / median(prior_bodies) if prior_bodies else 0.0
    direction_sign = 1 if close_price > open_price else (-1 if close_price < open_price else 0)
    p99_buy_vwap = _bubble_vwap(p99_buy_bubbles)
    p99_sell_vwap = _bubble_vwap(p99_sell_bubbles)
    return {
        "minutes_from_ny_open": snapshot.get("minutes_from_ny_open"),
        "minutes_from_entry_start": snapshot.get("minutes_from_entry_start"),
        "candle_open": open_price,
        "candle_high": float(candle.get("high", 0.0)),
        "candle_low": low,
        "candle_close": close_price,
        "candle_range": candle_range,
        "candle_body": body,
        "candle_body_ratio": body / candle_range if candle_range > 0 else 0.0,
        "candle_upper_wick": upper_wick,
        "candle_lower_wick": lower_wick,
        "candle_upper_wick_ratio": upper_wick / candle_range if candle_range > 0 else 0.0,
        "candle_lower_wick_ratio": lower_wick / candle_range if candle_range > 0 else 0.0,
        "candle_close_position": (close_price - low) / candle_range if candle_range > 0 else 0.5,
        "candle_direction": "buy" if close_price > open_price else ("sell" if close_price < open_price else "flat"),
        "candle_trade_count": int(candle.get("trade_count", 0)),
        "candle_buy_volume": float(candle.get("buy_volume", 0.0)),
        "candle_sell_volume": float(candle.get("sell_volume", 0.0)),
        "candle_delta": delta,
        "candle_volume": volume,
        "candle_delta_ratio": delta / volume if volume > 0 else 0.0,
        "directional_delta_ratio": direction_sign * delta / volume if volume > 0 else 0.0,
        "cvd_recent_5": cvd5,
        "cvd_ratio_recent_5": cvd5_ratio,
        "cvd_recent_15": cvd15,
        "cvd_ratio_recent_15": cvd15_ratio,
        "cvd_recent_30": cvd30,
        "cvd_ratio_recent_30": cvd30_ratio,
        "volume_expansion_ratio": volume_expansion,
        "range_expansion_ratio": range_expansion,
        "body_expansion_ratio": body_expansion,
        "range_result_per_volume_effort": range_expansion / volume_expansion if volume_expansion > 0 else None,
        "body_result_per_volume_effort": body_expansion / volume_expansion if volume_expansion > 0 else None,
        "p95_qty_threshold": snapshot.get("p95_qty"),
        "p99_qty_threshold": snapshot.get("p99_qty"),
        "bubble_count": len(bubbles),
        "buy_bubble_count": len(buy_bubbles),
        "sell_bubble_count": len(sell_bubbles),
        "buy_bubble_qty": sum(float(b.get("qty", 0.0)) for b in buy_bubbles),
        "sell_bubble_qty": sum(float(b.get("qty", 0.0)) for b in sell_bubbles),
        "bubble_delta": bubble_delta,
        "bubble_delta_ratio": bubble_delta / bubble_qty if bubble_qty > 0 else 0.0,
        "p99_bubble_count": len(p99_bubbles),
        "p99_buy_bubble_count": len(p99_buy_bubbles),
        "p99_sell_bubble_count": len(p99_sell_bubbles),
        "p99_buy_bubble_qty": sum(float(b.get("qty", 0.0)) for b in p99_buy_bubbles),
        "p99_sell_bubble_qty": sum(float(b.get("qty", 0.0)) for b in p99_sell_bubbles),
        "p99_bubble_delta": p99_delta,
        "p99_bubble_delta_ratio": p99_delta / p99_qty if p99_qty > 0 else 0.0,
        "p99_buy_vwap": p99_buy_vwap,
        "p99_sell_vwap": p99_sell_vwap,
        "p99_buy_price_position": (p99_buy_vwap - low) / candle_range if p99_buy_vwap is not None and candle_range > 0 else None,
        "p99_sell_price_position": (p99_sell_vwap - low) / candle_range if p99_sell_vwap is not None and candle_range > 0 else None,
        "max_bubble_qty": float(largest.get("qty", 0.0)) if largest else 0.0,
        "max_bubble_side": _bubble_side(largest) if largest else None,
        "max_bubble_price": float(largest["price"]) if largest else None,
        "max_bubble_timestamp_ms": int(largest["timestamp_ms"]) if largest and largest.get("timestamp_ms") is not None else None,
        "max_bubble_price_position": (
            (float(largest["price"]) - low) / candle_range if largest and candle_range > 0 else None
        ),
    }
