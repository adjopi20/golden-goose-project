from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from indicator.volume_profile import build_basic_volume_profile

EXPANSION_LEVELS = (1.0, 2.0, 4.0, 8.0)
BUBBLE_COLUMNS = (
    "bubble_count",
    "buy_bubble_count",
    "sell_bubble_count",
    "buy_bubble_qty",
    "sell_bubble_qty",
    "max_bubble_qty",
    "max_bubble_side",
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def _ms(day: date, value: time, tz: ZoneInfo, next_day_if_before: time | None = None) -> int:
    target_day = day + timedelta(days=1) if next_day_if_before is not None and value <= next_day_if_before else day
    return int(datetime.combine(target_day, value, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)


def _iso(timestamp_ms: int | None, tz: ZoneInfo) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()


def _slice(ts: np.ndarray, start_ms: int, end_ms: int) -> slice:
    return slice(int(np.searchsorted(ts, start_ms, side="left")), int(np.searchsorted(ts, end_ms, side="left")))


def _read(path: Path, start_ms: int, end_ms: int) -> pd.DataFrame:
    df = pd.read_parquet(path, filters=[("timestamp", ">=", start_ms), ("timestamp", "<", end_ms)])
    missing = {"timestamp", "price", "qty", "is_buyer_maker"} - set(df.columns)
    if missing:
        raise ValueError(f"Input parquet missing columns: {sorted(missing)}")
    return df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)


def _load_orderflow_features(cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / "candles_1m.parquet"
    candles = pd.read_parquet(path, columns=["timestamp_ms", "open", "high", "low", "close", "volume", "delta"])
    candles = candles.sort_values("timestamp_ms", kind="mergesort").drop_duplicates("timestamp_ms", keep="last")
    volume = candles["volume"].astype(float)
    delta = candles["delta"].astype(float)
    timestamps = candles["timestamp_ms"].astype(np.int64)
    rolling_volume = volume.rolling(30, min_periods=30).sum()
    rolling_delta = delta.rolling(30, min_periods=30).sum()
    prior_volume_median = volume.shift(1).rolling(30, min_periods=30).median()
    continuous_recent = timestamps.sub(timestamps.shift(29)).eq(29 * 60_000)
    continuous_prior = timestamps.sub(timestamps.shift(30)).eq(30 * 60_000)
    candles["candle_delta_ratio"] = delta.div(volume.where(volume > 0))
    candles["cvd_recent_30"] = rolling_delta.where(continuous_recent)
    candles["rolling_volume_30"] = rolling_volume.where(continuous_recent)
    candles["cvd_ratio_30"] = candles["cvd_recent_30"].div(candles["rolling_volume_30"].where(candles["rolling_volume_30"] > 0))
    candles["volume_expansion_ratio"] = volume.div(prior_volume_median.where(prior_volume_median > 0)).where(continuous_prior)
    bubbles_path = cache_dir / "minute_orderflow.parquet"
    if bubbles_path.exists():
        bubbles = pd.read_parquet(bubbles_path, columns=["snapshot_timestamp_ms", *BUBBLE_COLUMNS]).rename(
            columns={"snapshot_timestamp_ms": "timestamp_ms"}
        )
        candles = candles.merge(bubbles, on="timestamp_ms", how="left", validate="one_to_one")
    return candles.set_index("timestamp_ms")


def _orderflow_values(features: pd.DataFrame, candle_start_ms: int, direction: str | None, tz: ZoneInfo) -> dict[str, Any]:
    base = {
        "orderflow_candle_start_time": _iso(candle_start_ms, tz),
        "orderflow_candle_complete_time": _iso(candle_start_ms + 60_000, tz),
    }
    if candle_start_ms not in features.index:
        return {**base, "orderflow_available": False, "p95_bubbles_available": False}
    row = features.loc[candle_start_ms]
    required = ("candle_delta_ratio", "cvd_ratio_30", "volume_expansion_ratio")
    flow_available = all(pd.notna(row[name]) for name in required)
    bubbles_available = "bubble_count" in row.index and pd.notna(row["bubble_count"])
    direction_sign = 1.0 if direction == "long" else -1.0 if direction == "short" else None
    buy_count = int(row["buy_bubble_count"]) if bubbles_available else None
    sell_count = int(row["sell_bubble_count"]) if bubbles_available else None
    buy_qty = float(row["buy_bubble_qty"]) if bubbles_available else None
    sell_qty = float(row["sell_bubble_qty"]) if bubbles_available else None
    supportive_count = buy_count if direction == "long" else sell_count if direction == "short" else None
    opposing_count = sell_count if direction == "long" else buy_count if direction == "short" else None
    supportive_qty = buy_qty if direction == "long" else sell_qty if direction == "short" else None
    opposing_qty = sell_qty if direction == "long" else buy_qty if direction == "short" else None
    total_bubble_qty = (supportive_qty or 0.0) + (opposing_qty or 0.0)
    bubble_imbalance = ((supportive_qty or 0.0) - (opposing_qty or 0.0)) / total_bubble_qty if total_bubble_qty > 0 else 0.0
    return {
        **base,
        "orderflow_available": flow_available,
        "p95_bubbles_available": bubbles_available,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "candle_delta": float(row["delta"]),
        "candle_volume": float(row["volume"]),
        "candle_delta_ratio": float(row["candle_delta_ratio"]),
        "cvd_recent_30": float(row["cvd_recent_30"]) if pd.notna(row["cvd_recent_30"]) else None,
        "rolling_volume_30": float(row["rolling_volume_30"]) if pd.notna(row["rolling_volume_30"]) else None,
        "cvd_ratio_30": float(row["cvd_ratio_30"]) if pd.notna(row["cvd_ratio_30"]) else None,
        "volume_expansion_ratio": float(row["volume_expansion_ratio"]) if pd.notna(row["volume_expansion_ratio"]) else None,
        "directional_delta_ratio": direction_sign * float(row["candle_delta_ratio"]) if direction_sign is not None else None,
        "directional_cvd_ratio_30": direction_sign * float(row["cvd_ratio_30"]) if direction_sign is not None and pd.notna(row["cvd_ratio_30"]) else None,
        "p95_bubble_present": bool(row["bubble_count"] > 0) if bubbles_available else None,
        "bubble_count": int(row["bubble_count"]) if bubbles_available else None,
        "buy_bubble_count": buy_count,
        "sell_bubble_count": sell_count,
        "buy_bubble_qty": buy_qty,
        "sell_bubble_qty": sell_qty,
        "max_bubble_qty": float(row["max_bubble_qty"]) if bubbles_available else None,
        "max_bubble_side": str(row["max_bubble_side"]) if bubbles_available and pd.notna(row["max_bubble_side"]) else None,
        "supportive_bubble_count": supportive_count,
        "opposing_bubble_count": opposing_count,
        "supportive_bubble_qty": supportive_qty,
        "opposing_bubble_qty": opposing_qty,
        "directional_bubble_qty_imbalance": bubble_imbalance if bubbles_available and direction_sign is not None else None,
    }


def _orderflow_at_breakout(features: pd.DataFrame | None, breakout_ts: int, direction: str, tz: ZoneInfo) -> dict[str, Any]:
    if features is None:
        return {}
    candle_start_ms = breakout_ts // 60_000 * 60_000
    return _orderflow_values(features, candle_start_ms, direction, tz)


def _orderflow_path(
    features: pd.DataFrame | None,
    breakout_ts: int,
    observation_end_ms: int,
    direction: str,
    tz: ZoneInfo,
) -> list[dict[str, Any]]:
    if features is None:
        return []
    start_ms = breakout_ts // 60_000 * 60_000
    rows = []
    for minute, candle_start_ms in enumerate(range(start_ms, observation_end_ms, 60_000)):
        rows.append(
            {
                "minutes_from_breakout_candle": minute,
                "is_breakout_candle": minute == 0,
                "diagnostic_only_after_raw_breakout": True,
                **_orderflow_values(features, candle_start_ms, direction, tz),
            }
        )
    return rows


def _close_zone(close: float, profile_low: float, profile_high: float) -> str:
    if close > profile_high:
        return "above"
    if close < profile_low:
        return "below"
    return "inside"


def _advance_bias(bias: str, waiting: bool, close_zone: str) -> tuple[str, bool, str]:
    if close_zone == "inside":
        return bias, True, "closed_inside_wait"
    close_direction = "long" if close_zone == "above" else "short"
    if close_direction != bias:
        return close_direction, False, "opposite_direction_breakout_bias_flip"
    if waiting:
        return bias, False, "same_direction_rebreak"
    return bias, False, "bias_held_outside"


def _window_candle_anatomy(values: dict[str, Any], bias: str | None, profile: dict[str, Any]) -> dict[str, Any]:
    if "open" not in values:
        return {}
    open_price = float(values["open"])
    high = float(values["high"])
    low = float(values["low"])
    close = float(values["close"])
    candle_range = high - low
    body = close - open_price
    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    orb_width = float(profile["profile_width"])
    sign = 1.0 if bias == "long" else -1.0 if bias == "short" else None
    return {
        "close_zone": _close_zone(close, float(profile["profile_low"]), float(profile["profile_high"])),
        "candle_range": candle_range,
        "candle_body": body,
        "body_ratio": abs(body) / candle_range if candle_range > 0 else 0.0,
        "upper_wick_ratio": upper_wick / candle_range if candle_range > 0 else 0.0,
        "lower_wick_ratio": lower_wick / candle_range if candle_range > 0 else 0.0,
        "range_orb_width_ratio": candle_range / orb_width if orb_width > 0 else None,
        "body_orb_width_ratio": abs(body) / orb_width if orb_width > 0 else None,
        "directional_body_ratio": sign * body / candle_range if sign is not None and candle_range > 0 else None,
        "rejection_wick_ratio": (upper_wick if bias == "long" else lower_wick) / candle_range if bias and candle_range > 0 else None,
        "adverse_wick_ratio": (lower_wick if bias == "long" else upper_wick) / candle_range if bias and candle_range > 0 else None,
    }


def _orb_window_path(
    features: pd.DataFrame,
    orb_end_ms: int,
    observation_end_ms: int,
    profile: dict[str, Any],
    breakout_ts: int | None,
    initial_direction: str | None,
    tz: ZoneInfo,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    breakout_candle_ms = breakout_ts // 60_000 * 60_000 if breakout_ts is not None else None
    bias: str | None = None
    waiting = False
    rows: list[dict[str, Any]] = []
    counts = Counter()

    for sequence, candle_start_ms in enumerate(range(orb_end_ms, observation_end_ms, 60_000)):
        bias_before = bias
        neutral_values = _orderflow_values(features, candle_start_ms, None, tz)
        close_zone = None
        event = "waiting_for_initial_breakout"

        if neutral_values.get("orderflow_available"):
            close_zone = _close_zone(
                float(neutral_values["close"]),
                float(profile["profile_low"]),
                float(profile["profile_high"]),
            )

        if breakout_candle_ms is not None and candle_start_ms >= breakout_candle_ms and close_zone is not None:
            if bias is None:
                bias = str(initial_direction)
                expected_zone = "above" if bias == "long" else "below"
                if close_zone == "inside":
                    waiting = True
                    event = "initial_breakout_closed_inside_wait"
                elif close_zone != expected_zone:
                    bias = "long" if close_zone == "above" else "short"
                    waiting = False
                    event = "initial_breakout_opposite_close_bias_flip"
                else:
                    waiting = False
                    event = "initial_breakout_accepted"
            else:
                bias, waiting, event = _advance_bias(bias, waiting, close_zone)

        counts[event] += 1
        values = _orderflow_values(features, candle_start_ms, bias, tz)
        rows.append(
            {
                "minutes_from_orb_end": sequence,
                "minutes_from_initial_breakout_candle": (
                    (candle_start_ms - breakout_candle_ms) // 60_000 if breakout_candle_ms is not None else None
                ),
                "is_initial_breakout_candle": candle_start_ms == breakout_candle_ms,
                "initial_breakout_seen": breakout_candle_ms is not None and candle_start_ms >= breakout_candle_ms,
                "bias_before": bias_before,
                "bias_after": bias,
                "waiting_after_inside_close": waiting,
                "state_event": event,
                **values,
                **_window_candle_anatomy(values, bias, profile),
            }
        )

    return rows, {
        "orb_no_breakout": breakout_ts is None,
        "initial_breakout_direction": initial_direction,
        "final_observed_bias": bias,
        "failed_acceptance_closes": counts["initial_breakout_closed_inside_wait"] + counts["closed_inside_wait"],
        "same_direction_rebreaks": counts["same_direction_rebreak"],
        "opposite_direction_bias_flips": (
            counts["initial_breakout_opposite_close_bias_flip"] + counts["opposite_direction_breakout_bias_flip"]
        ),
    }


def _self_check_rebreak_state() -> None:
    assert _advance_bias("long", False, "inside") == ("long", True, "closed_inside_wait")
    assert _advance_bias("long", True, "above") == ("long", False, "same_direction_rebreak")
    assert _advance_bias("long", True, "below") == ("short", False, "opposite_direction_breakout_bias_flip")


def _profile(orb_df: pd.DataFrame, bins: int) -> dict[str, Any] | None:
    if len(orb_df) < 2:
        return None
    profile = build_basic_volume_profile(orb_df[["price", "qty"]], n_bins=bins)
    return {
        "profile_low": float(profile["session_low"]),
        "profile_high": float(profile["session_high"]),
        "profile_width": float(profile["session_high"] - profile["session_low"]),
        "poc_price": float(profile["poc_price"]),
        "val": float(profile["val"]),
        "vah": float(profile["vah"]),
        "total_volume": float(profile["total_volume"]),
    }


def _first_breakout(prices: np.ndarray, high: float, low: float) -> tuple[int, str] | None:
    for index, price in enumerate(prices):
        if price > high:
            return index, "long"
        if price < low:
            return index, "short"
    return None


def _stop_level(profile: dict[str, Any], direction: str, risk_model: str) -> float:
    if risk_model == "opposite_extreme":
        return float(profile["profile_low"] if direction == "long" else profile["profile_high"])
    if risk_model == "poc":
        return float(profile["poc_price"])
    if risk_model == "opposite_value_area":
        return float(profile["val"] if direction == "long" else profile["vah"])
    raise ValueError(f"Unsupported risk model: {risk_model}")


def _path_stats(
    prices: np.ndarray,
    timestamps: np.ndarray,
    direction: str,
    breakout_level: float,
    risk: float,
    stop: float,
) -> dict[str, Any]:
    max_favorable_r = 0.0
    first_1r_ts: int | None = None
    first_1r_price: float | None = None
    for price, timestamp in zip(prices, timestamps):
        if direction == "long":
            favorable_r = (float(price) - breakout_level) / risk
            max_favorable_r = max(max_favorable_r, favorable_r)
            if first_1r_ts is None and favorable_r >= 1.0:
                first_1r_ts = int(timestamp)
                first_1r_price = float(price)
            if price <= stop:
                return _path_result(max_favorable_r, first_1r_ts, first_1r_price, "loser_sl", int(timestamp), float(price))
        else:
            favorable_r = (breakout_level - float(price)) / risk
            max_favorable_r = max(max_favorable_r, favorable_r)
            if first_1r_ts is None and favorable_r >= 1.0:
                first_1r_ts = int(timestamp)
                first_1r_price = float(price)
            if price >= stop:
                return _path_result(max_favorable_r, first_1r_ts, first_1r_price, "loser_sl", int(timestamp), float(price))
    return _path_result(max_favorable_r, first_1r_ts, first_1r_price, "time_invalidation", None, None)


def _path_result(
    max_favorable_r: float,
    first_1r_ts: int | None,
    first_1r_price: float | None,
    end_reason: str,
    end_ts: int | None,
    end_price: float | None,
) -> dict[str, Any]:
    outcome_reason = "winner_1r" if first_1r_ts is not None else end_reason
    return {
        "outcome_reason": outcome_reason,
        "outcome_ts": first_1r_ts if first_1r_ts is not None else end_ts,
        "outcome_price": first_1r_price if first_1r_ts is not None else end_price,
        "path_end_reason": end_reason,
        "path_end_ts": end_ts,
        "path_end_price": end_price,
        "max_favorable_r_before_invalidation": max(0.0, float(max_favorable_r)),
    }


def _sample_day(
    *,
    day: date,
    df: pd.DataFrame,
    ts: np.ndarray,
    tz: ZoneInfo,
    orb_start: time,
    orb_minutes: int,
    breakout_window_minutes: int,
    outcome_end: time,
    bins: int,
    risk_model: str = "opposite_extreme",
    orderflow_features: pd.DataFrame | None = None,
    collect_orderflow_path: bool = False,
) -> dict[str, Any]:
    orb_start_ms = _ms(day, orb_start, tz)
    orb_end_ms = orb_start_ms + orb_minutes * 60_000
    breakout_end_ms = orb_end_ms + breakout_window_minutes * 60_000
    outcome_end_ms = _ms(day, outcome_end, tz, next_day_if_before=orb_start)

    orb_df = df.iloc[_slice(ts, orb_start_ms, orb_end_ms)]
    profile = _profile(orb_df, bins)
    if profile is None or profile["profile_width"] <= 0:
        return {"session_day": day.isoformat(), "sample": False, "reason": "missing_or_flat_orb_profile"}

    obs_slice = _slice(ts, orb_end_ms, breakout_end_ms)
    obs_df = df.iloc[obs_slice]
    if obs_df.empty:
        return {"session_day": day.isoformat(), "sample": False, "reason": "empty_breakout_window", **profile}

    prices = obs_df["price"].to_numpy(dtype=float)
    breakout = _first_breakout(prices, profile["profile_high"], profile["profile_low"])
    if breakout is None:
        return {"session_day": day.isoformat(), "sample": False, "reason": "no_breakout", **profile}

    local_breakout_idx, direction = breakout
    global_breakout_idx = obs_slice.start + local_breakout_idx
    breakout_ts = int(ts[global_breakout_idx])
    breakout_price = float(df.iloc[global_breakout_idx]["price"])
    width = float(profile["profile_width"])
    if direction == "long":
        breakout_level = float(profile["profile_high"])
        stop = _stop_level(profile, direction, risk_model)
        risk = breakout_level - stop
        target = breakout_level + risk
    else:
        breakout_level = float(profile["profile_low"])
        stop = _stop_level(profile, direction, risk_model)
        risk = stop - breakout_level
        target = breakout_level - risk
    if risk <= 0:
        return {"session_day": day.isoformat(), "sample": False, "reason": "invalid_risk_model", "risk_model": risk_model, **profile}

    end_idx = int(np.searchsorted(ts, outcome_end_ms, side="left"))
    path = _path_stats(
        df["price"].to_numpy(dtype=float)[global_breakout_idx:end_idx],
        ts[global_breakout_idx:end_idx],
        direction,
        breakout_level,
        risk,
        stop,
    )
    outcome_reason = path["outcome_reason"]
    max_favorable_r = float(path["max_favorable_r_before_invalidation"])
    sample = {
        "session_day": day.isoformat(),
        "sample": True,
        "direction": direction,
        "result": "win" if outcome_reason == "winner_1r" else "loss" if outcome_reason == "loser_sl" else "unresolved",
        "outcome_reason": outcome_reason,
        "orb_start_time": _iso(orb_start_ms, tz),
        "orb_end_time": _iso(orb_end_ms, tz),
        "breakout_window_end_time": _iso(breakout_end_ms, tz),
        "outcome_end_time": _iso(outcome_end_ms, tz),
        "breakout_time": _iso(breakout_ts, tz),
        "breakout_price": breakout_price,
        "breakout_level": breakout_level,
        "risk_model": risk_model,
        "stop_loss": stop,
        "target_1r": target,
        "risk_abs": risk,
        "outcome_time": _iso(path["outcome_ts"], tz),
        "outcome_price": path["outcome_price"],
        "path_end_reason": path["path_end_reason"],
        "path_end_time": _iso(path["path_end_ts"], tz),
        "path_end_price": path["path_end_price"],
        "max_favorable_r_before_invalidation": max_favorable_r,
        "reached_1r": max_favorable_r >= 1.0,
        "reached_2r": max_favorable_r >= 2.0,
        "reached_4r": max_favorable_r >= 4.0,
        "reached_8r": max_favorable_r >= 8.0,
        "reached_gt_8r": max_favorable_r > 8.0,
        "expansion_bucket": _expansion_bucket(max_favorable_r),
        "r_result": 1.0 if outcome_reason == "winner_1r" else -1.0 if outcome_reason == "loser_sl" else 0.0,
        **_orderflow_at_breakout(orderflow_features, breakout_ts, direction, tz),
        **profile,
    }
    if collect_orderflow_path:
        sample["_orderflow_path"] = _orderflow_path(orderflow_features, breakout_ts, breakout_end_ms, direction, tz)
    return sample


def _expansion_bucket(max_favorable_r: float) -> str:
    if max_favorable_r > 8.0:
        return ">8R"
    if max_favorable_r >= 8.0:
        return "8R"
    if max_favorable_r >= 4.0:
        return "4R_to_8R"
    if max_favorable_r >= 2.0:
        return "2R_to_4R"
    if max_favorable_r >= 1.0:
        return "1R_to_2R"
    return "<1R"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [row for row in rows if row.get("sample")]
    resolved = [row for row in samples if row["result"] != "unresolved"]
    by_direction: dict[str, Any] = {}
    for direction in ("long", "short"):
        subset = [row for row in samples if row.get("direction") == direction]
        resolved_subset = [row for row in subset if row["result"] != "unresolved"]
        by_direction[direction] = _bucket_metrics(subset, resolved_subset)
    return {
        "sessions": len(rows),
        "samples": len(samples),
        "skipped": len(rows) - len(samples),
        "skip_reasons": dict(Counter(row.get("reason", "") for row in rows if not row.get("sample"))),
        "orderflow_available": sum(bool(row.get("orderflow_available")) for row in samples),
        "orderflow_missing": sum(row.get("orderflow_available") is False for row in samples),
        **_bucket_metrics(samples, resolved),
        "by_direction": by_direction,
    }


def _bucket_metrics(samples: list[dict[str, Any]], resolved: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(row["result"] == "win" for row in samples)
    losses = sum(row["result"] == "loss" for row in samples)
    unresolved = sum(row["result"] == "unresolved" for row in samples)
    return {
        "samples": len(samples),
        "wins": wins,
        "losses": losses,
        "unresolved": unresolved,
        "win_rate_resolved": wins / len(resolved) if resolved else None,
        "win_rate_all": wins / len(samples) if samples else None,
        "expectancy_r_resolved": sum(row["r_result"] for row in resolved) / len(resolved) if resolved else None,
        "expectancy_r_all_unresolved_0r": sum(row["r_result"] for row in samples) / len(samples) if samples else None,
        **_expansion_metrics(samples),
    }


def _expansion_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "reach_1r_rate": None,
            "reach_2r_rate": None,
            "reach_4r_rate": None,
            "reach_8r_rate": None,
            "reach_gt_8r_rate": None,
            "avg_max_favorable_r_before_invalidation": None,
        }
    return {
        "reach_1r_rate": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) >= 1.0 for row in samples) / len(samples),
        "reach_2r_rate": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) >= 2.0 for row in samples) / len(samples),
        "reach_4r_rate": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) >= 4.0 for row in samples) / len(samples),
        "reach_8r_rate": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) >= 8.0 for row in samples) / len(samples),
        "reach_gt_8r_rate": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) > 8.0 for row in samples) / len(samples),
        "avg_max_favorable_r_before_invalidation": sum(float(row.get("max_favorable_r_before_invalidation", 0.0)) for row in samples) / len(samples),
    }


def _markdown(summary: dict[str, Any], args: argparse.Namespace) -> str:
    lines = [
        "# Basic ORB 1R Observation",
        "",
        "Pure event study. No fees, no slippage, no compounding, no broker.",
        "",
        f"- Input: `{args.input}`",
        f"- ORB window: `{args.orb_start}` + `{args.orb_minutes}` minutes",
        f"- Breakout window: `{args.breakout_window_minutes}` minutes after ORB end",
        f"- Outcome horizon: `{args.outcome_end}` NY time",
        f"- Samples: `{summary['samples']}` from `{summary['sessions']}` sessions",
        f"- Wins / losses / unresolved: `{summary['wins']}` / `{summary['losses']}` / `{summary['unresolved']}`",
        f"- Resolved win rate: `{_fmt(summary['win_rate_resolved'])}`",
        f"- Resolved expectancy: `{_fmt(summary['expectancy_r_resolved'])}R`",
        f"- All-sample expectancy, unresolved=0R: `{_fmt(summary['expectancy_r_all_unresolved_0r'])}R`",
        f"- Reach 1R / 2R / 4R / 8R / >8R: `{_fmt(summary['reach_1r_rate'])}` / `{_fmt(summary['reach_2r_rate'])}` / `{_fmt(summary['reach_4r_rate'])}` / `{_fmt(summary['reach_8r_rate'])}` / `{_fmt(summary['reach_gt_8r_rate'])}`",
        f"- Avg max favorable R before invalidation: `{_fmt(summary['avg_max_favorable_r_before_invalidation'])}`",
        f"- Orderflow available / missing: `{summary['orderflow_available']}` / `{summary['orderflow_missing']}`",
        "",
        "| direction | samples | wins | losses | unresolved | resolved WR | exp R | reach 2R | reach 4R | reach 8R | >8R | avg MFE R |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for direction, data in summary["by_direction"].items():
        lines.append(
            f"| {direction} | {data['samples']} | {data['wins']} | {data['losses']} | {data['unresolved']} | "
            f"{_fmt(data['win_rate_resolved'])} | {_fmt(data['expectancy_r_resolved'])} | "
            f"{_fmt(data['reach_2r_rate'])} | {_fmt(data['reach_4r_rate'])} | {_fmt(data['reach_8r_rate'])} | "
            f"{_fmt(data['reach_gt_8r_rate'])} | {_fmt(data['avg_max_favorable_r_before_invalidation'])} |"
        )
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    return "" if value is None else f"{float(value):.4f}"


def run(args: argparse.Namespace) -> dict[str, Any]:
    tz = ZoneInfo(args.timezone)
    start_day = _parse_date(args.start_date)
    end_day = _parse_date(args.end_date)
    orb_start = _parse_time(args.orb_start)
    outcome_end = _parse_time(args.outcome_end)
    feature_cache_dir = getattr(args, "feature_cache_dir", None)
    orderflow_features = _load_orderflow_features(Path(feature_cache_dir)) if feature_cache_dir else None
    rows: list[dict[str, Any]] = []

    day = start_day
    while day <= end_day:
        chunk_end = min(end_day, day + timedelta(days=args.chunk_days - 1))
        read_start = _ms(day, orb_start, tz)
        read_end = _ms(chunk_end, outcome_end, tz, next_day_if_before=orb_start)
        df = _read(Path(args.input), read_start, read_end)
        ts = df["timestamp"].to_numpy(dtype=np.int64)
        current = day
        while current <= chunk_end:
            rows.append(
                _sample_day(
                    day=current,
                    df=df,
                    ts=ts,
                    tz=tz,
                    orb_start=orb_start,
                    orb_minutes=args.orb_minutes,
                    breakout_window_minutes=args.breakout_window_minutes,
                    outcome_end=outcome_end,
                    bins=args.bins,
                    risk_model=getattr(args, "risk_model", "opposite_extreme"),
                    orderflow_features=orderflow_features,
                    collect_orderflow_path=bool(feature_cache_dir),
                )
            )
            current += timedelta(days=1)
        day = chunk_end + timedelta(days=1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for row in rows:
        if orderflow_features is not None and row.get("profile_width"):
            session_day = _parse_date(row["session_day"])
            orb_end_ms = _ms(session_day, orb_start, tz) + args.orb_minutes * 60_000
            observation_end_ms = orb_end_ms + args.breakout_window_minutes * 60_000
            breakout_ts = (
                int(datetime.fromisoformat(row["breakout_time"]).timestamp() * 1000)
                if row.get("breakout_time")
                else None
            )
            window, state_summary = _orb_window_path(
                orderflow_features,
                orb_end_ms,
                observation_end_ms,
                row,
                breakout_ts,
                row.get("direction"),
                tz,
            )
            row.update(state_summary)
            for minute in window:
                window_rows.append(
                    {
                        "session_day": row["session_day"],
                        "sample": bool(row.get("sample")),
                        "skip_reason": row.get("reason"),
                        "initial_breakout_time": row.get("breakout_time"),
                        "initial_breakout_direction": row.get("direction"),
                        "initial_breakout_result": row.get("result"),
                        "initial_breakout_outcome_reason": row.get("outcome_reason"),
                        "initial_breakout_outcome_time": row.get("outcome_time"),
                        "profile_low": row["profile_low"],
                        "profile_high": row["profile_high"],
                        "profile_width": row["profile_width"],
                        **minute,
                    }
                )
        for minute in row.pop("_orderflow_path", []):
            path_rows.append(
                {
                    "session_day": row["session_day"],
                    "breakout_time": row["breakout_time"],
                    "direction": row["direction"],
                    "result": row["result"],
                    "outcome_reason": row["outcome_reason"],
                    "outcome_time": row["outcome_time"],
                    "path_end_time": row["path_end_time"],
                    "expansion_bucket": row["expansion_bucket"],
                    "max_favorable_r_before_invalidation": row["max_favorable_r_before_invalidation"],
                    **minute,
                }
            )
    summary = {
        "event": "basic_orb_1r_observation_finished",
        "input": str(args.input),
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "timezone": args.timezone,
        "orb_start": args.orb_start,
        "orb_minutes": args.orb_minutes,
        "breakout_window_minutes": args.breakout_window_minutes,
        "outcome_end": args.outcome_end,
        "risk_model": getattr(args, "risk_model", "opposite_extreme"),
        "feature_cache_dir": feature_cache_dir,
        "orderflow_path_rows": len(path_rows),
        "orb_window_path_rows": len(window_rows),
        "orb_no_breakout_sessions": sum(bool(row.get("orb_no_breakout")) for row in rows),
        "same_direction_rebreaks": sum(int(row.get("same_direction_rebreaks", 0)) for row in rows),
        "opposite_direction_bias_flips": sum(int(row.get("opposite_direction_bias_flips", 0)) for row in rows),
        **_summary(rows),
    }
    _write_jsonl(output_dir / "samples.jsonl", rows)
    if feature_cache_dir:
        pd.DataFrame(path_rows).to_parquet(output_dir / "orderflow_path.parquet", index=False)
        pd.DataFrame(window_rows).to_parquet(output_dir / "orb_window_path.parquet", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "findings.md").write_text(_markdown(summary, args), encoding="utf-8")
    return summary


def main() -> None:
    _self_check_rebreak_state()
    parser = argparse.ArgumentParser(description="Basic ORB first-breakout 1R observation study.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--feature-cache-dir")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--orb-start", default="09:30")
    parser.add_argument("--orb-minutes", type=int, default=15)
    parser.add_argument("--breakout-window-minutes", type=int, default=30)
    parser.add_argument("--outcome-end", default="04:30")
    parser.add_argument("--risk-model", choices=["opposite_extreme", "poc", "opposite_value_area"], default="opposite_extreme")
    parser.add_argument("--bins", type=int, default=50)
    parser.add_argument("--chunk-days", type=int, default=31)
    print(json.dumps(run(parser.parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
