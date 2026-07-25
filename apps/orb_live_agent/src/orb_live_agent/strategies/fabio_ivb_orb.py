from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class FabioIvbConfig:
    orb_start: time = time(8, 30)
    orb_minutes: int = 30
    trade_end: time = time(14, 0)
    delta_threshold: float = 200.0
    use_cumulative_delta: bool = False
    cumulative_delta_threshold: float = 500.0
    tp_rr: float = 1.0
    qty: float = 1.0


@dataclass
class FabioIvbState:
    day: str = ""
    orb_high: float | None = None
    orb_low: float | None = None
    orb_done: bool = False
    long_done: bool = False
    cum_delta: float = 0.0


def decide(candle: dict[str, Any], state: FabioIvbState, config: FabioIvbConfig, tz: ZoneInfo) -> dict[str, Any]:
    close_dt = _bar_close_dt(candle, tz)
    day = close_dt.date().isoformat()
    if state.day != day:
        state.day = day
        state.orb_high = None
        state.orb_low = None
        state.orb_done = False
        state.long_done = False
        state.cum_delta = 0.0

    orb_start = datetime.combine(close_dt.date(), config.orb_start, tzinfo=tz)
    orb_end = orb_start + timedelta(minutes=config.orb_minutes)
    trade_end = datetime.combine(close_dt.date(), config.trade_end, tzinfo=tz)
    bar_delta = float(candle.get("delta", 0.0))
    if orb_start < close_dt <= trade_end:
        state.cum_delta += bar_delta

    if orb_start < close_dt <= orb_end:
        high = float(candle["high"])
        low = float(candle["low"])
        state.orb_high = high if state.orb_high is None else max(state.orb_high, high)
        state.orb_low = low if state.orb_low is None else min(state.orb_low, low)
        return _wait(candle, state, "building_orb")

    if not state.orb_done and state.orb_high is not None and close_dt > orb_end:
        state.orb_done = True

    if not state.orb_done or close_dt <= orb_end or close_dt >= trade_end:
        return _wait(candle, state, "outside_trade_window")
    if state.long_done:
        return _wait(candle, state, "long_already_done_today")

    entry = float(candle["close"])
    stop = state.orb_low
    if state.orb_high is None or stop is None or entry <= state.orb_high or stop >= entry:
        return _wait(candle, state, "no_long_breakout")

    delta_ok = bar_delta >= config.delta_threshold
    if config.use_cumulative_delta:
        delta_ok = delta_ok and state.cum_delta >= config.cumulative_delta_threshold
    if not delta_ok:
        return _wait(candle, state, "delta_filter_failed")

    state.long_done = True
    risk = entry - stop
    return {
        "decision": "TAKE",
        "provider": "algorithm",
        "strategy": "fabio_ivb_orb",
        "entry_model": "fabio_ivb_long_only_orb",
        "direction": "long",
        "entry": entry,
        "stop_loss": stop,
        "take_profit": entry + config.tp_rr * risk,
        "tp_rr": config.tp_rr,
        "qty": config.qty,
        "orb_high": state.orb_high,
        "orb_low": stop,
        "bar_delta": bar_delta,
        "cumulative_delta": state.cum_delta,
        "snapshot_timestamp_ms": int(candle["timestamp_ms"]),
        "bar_close_time": close_dt.isoformat(),
        "reason": "close_above_orb_high_with_delta",
    }


def _bar_close_dt(candle: dict[str, Any], tz: ZoneInfo) -> datetime:
    timestamp_ms = int(candle["timestamp_ms"])
    timeframe = str(candle.get("timeframe", "5m"))
    minutes = int(timeframe[:-1]) if timeframe.endswith("m") else 5
    return datetime.fromtimestamp((timestamp_ms + minutes * 60_000) / 1000.0, timezone.utc).astimezone(tz)


def _wait(candle: dict[str, Any], state: FabioIvbState, reason: str) -> dict[str, Any]:
    return {
        "decision": "WAIT",
        "provider": "algorithm",
        "strategy": "fabio_ivb_orb",
        "reason": reason,
        "snapshot_timestamp_ms": int(candle["timestamp_ms"]),
        "orb_high": state.orb_high,
        "orb_low": state.orb_low,
    }
