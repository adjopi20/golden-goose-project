from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from statistics import median
from typing import Any


@dataclass(frozen=True)
class BTCOpeningRangeBreakoutConfig:
    session_start: str = "13:30"
    range_minutes: int = 15
    entry_end: str = "17:00"
    volume_lookback: int = 20
    volume_multiplier: float = 1.5
    atr_period: int = 14
    atr_multiplier: float = 0.75
    ema_period: int = 9
    assumed_spread_bps: float = 2.0
    max_spread_bps: float = 5.0
    risk_fraction: float = 0.0025
    max_daily_loss_fraction: float = 0.01
    max_consecutive_losses: int = 3
    tp1_r: float = 1.0
    tp1_fraction: float = 0.5

    def __post_init__(self) -> None:
        if _parse_time(self.entry_end) <= _parse_time(self.session_start):
            raise ValueError("entry_end must be after session_start")
        if self.range_minutes < 1 or self.volume_lookback < 1 or self.atr_period < 1 or self.ema_period < 1:
            raise ValueError("range and indicator periods must be positive")
        if self.volume_multiplier < 1:
            raise ValueError("volume_multiplier must be at least 1")
        if not 0.6 <= self.atr_multiplier <= 0.9:
            raise ValueError("atr_multiplier must be in [0.6, 0.9]")
        if self.assumed_spread_bps < 0 or self.max_spread_bps < 0:
            raise ValueError("spread assumptions cannot be negative")
        if not 0.0025 <= self.risk_fraction <= 0.005:
            raise ValueError("risk_fraction must be in [0.0025, 0.005]")
        if not 0.01 <= self.max_daily_loss_fraction <= 0.015:
            raise ValueError("max_daily_loss_fraction must be in [0.01, 0.015]")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be positive")
        if self.tp1_r <= 0 or not 0 < self.tp1_fraction <= 1:
            raise ValueError("take-profit settings must be positive")


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def _utc(candle: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(candle["timestamp_ms"]) / 1000.0, timezone.utc)


def _session_candles(candles: list[dict[str, Any]], config: BTCOpeningRangeBreakoutConfig) -> list[dict[str, Any]]:
    day = _utc(candles[-1]).date()
    start = _parse_time(config.session_start)
    return [candle for candle in candles if _utc(candle).date() == day and _utc(candle).time() >= start]


def _completed_five_minute_bars(candles: list[dict[str, Any]], asof_ms: int) -> list[dict[str, float]]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for candle in candles:
        timestamp_ms = int(candle["timestamp_ms"])
        bucket = timestamp_ms // 300_000 * 300_000
        if bucket + 300_000 <= asof_ms:
            buckets.setdefault(bucket, []).append(candle)
    bars: list[dict[str, float]] = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda row: int(row["timestamp_ms"]))
        if len(rows) != 5:
            continue
        bars.append(
            {
                "open": float(rows[0]["open"]),
                "high": max(float(row["high"]) for row in rows),
                "low": min(float(row["low"]) for row in rows),
                "close": float(rows[-1]["close"]),
            }
        )
    return bars


def five_minute_atr(
    candles: list[dict[str, Any]],
    period: int = 14,
    asof_ms: int | None = None,
) -> float | None:
    if not candles or period < 1:
        return None
    cutoff = asof_ms if asof_ms is not None else int(candles[-1]["timestamp_ms"]) + 60_000
    bars = _completed_five_minute_bars(candles, cutoff)
    if len(bars) < period + 1:
        return None
    true_ranges = [
        max(
            bar["high"] - bar["low"],
            abs(bar["high"] - previous["close"]),
            abs(bar["low"] - previous["close"]),
        )
        for previous, bar in zip(bars, bars[1:])
    ]
    return sum(true_ranges[-period:]) / period


def five_minute_ema(
    candles: list[dict[str, Any]],
    period: int = 9,
    asof_ms: int | None = None,
) -> float | None:
    if not candles or period < 1:
        return None
    cutoff = asof_ms if asof_ms is not None else int(candles[-1]["timestamp_ms"]) + 60_000
    closes = [bar["close"] for bar in _completed_five_minute_bars(candles, cutoff)]
    if len(closes) < period:
        return None
    alpha = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    for close in closes[period:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def opening_range_breakout_decision(
    candles: list[dict[str, Any]],
    config: BTCOpeningRangeBreakoutConfig,
) -> dict[str, Any]:
    if len(candles) < max(config.volume_lookback + 1, 2):
        return {"decision": "WAIT", "reason": "insufficient_history"}
    latest = candles[-1]
    latest_dt = _utc(latest)
    start = _parse_time(config.session_start)
    range_end_minutes = start.hour * 60 + start.minute + config.range_minutes
    range_end = time(range_end_minutes // 60 % 24, range_end_minutes % 60)
    if not (range_end <= latest_dt.time() < _parse_time(config.entry_end)):
        return {"decision": "WAIT", "reason": "outside_entry_window"}
    if config.assumed_spread_bps > config.max_spread_bps:
        return {"decision": "WAIT", "reason": "assumed_spread_too_wide"}

    session = _session_candles(candles, config)
    opening = [candle for candle in session if start <= _utc(candle).time() < range_end]
    if len(opening) < config.range_minutes:
        return {"decision": "WAIT", "reason": "incomplete_opening_range"}
    opening_high = max(float(candle["high"]) for candle in opening)
    opening_low = min(float(candle["low"]) for candle in opening)
    close = float(latest["close"])
    if close <= opening_high or float(candles[-2]["close"]) > opening_high:
        return {"decision": "WAIT", "reason": "no_new_long_breakout"}

    vwap_rows = [candle for candle in session if int(candle["timestamp_ms"]) <= int(latest["timestamp_ms"])]
    total_volume = sum(float(candle["volume"]) for candle in vwap_rows)
    if total_volume <= 0:
        return {"decision": "WAIT", "reason": "missing_session_volume"}
    session_vwap = sum(
        ((float(candle["high"]) + float(candle["low"]) + float(candle["close"])) / 3.0) * float(candle["volume"])
        for candle in vwap_rows
    ) / total_volume
    if close <= session_vwap:
        return {"decision": "WAIT", "reason": "below_session_vwap"}

    baseline = median(float(candle["volume"]) for candle in candles[-config.volume_lookback - 1:-1])
    volume_ratio = float(latest["volume"]) / baseline if baseline > 0 else 0.0
    if volume_ratio < config.volume_multiplier:
        return {"decision": "WAIT", "reason": "insufficient_volume_expansion"}
    atr = five_minute_atr(candles, config.atr_period)
    if atr is None or atr <= 0:
        return {"decision": "WAIT", "reason": "insufficient_five_minute_atr"}

    return {
        "decision": "TAKE",
        "entry_model": "trend",
        "strategy": "btc_opening_range_breakout",
        "direction": "long",
        "entry": close,
        "stop_loss": close - config.atr_multiplier * atr,
        "snapshot_timestamp_ms": int(latest["timestamp_ms"]),
        "reason": "opening_range_high_breakout_above_vwap_on_volume",
        "opening_range_high": opening_high,
        "opening_range_low": opening_low,
        "session_vwap": session_vwap,
        "volume_ratio": volume_ratio,
        "five_minute_atr": atr,
        "assumed_spread_bps": config.assumed_spread_bps,
    }


def ema_runner_stop(candles: list[dict[str, Any]], config: BTCOpeningRangeBreakoutConfig) -> float | None:
    return five_minute_ema(candles, config.ema_period)
