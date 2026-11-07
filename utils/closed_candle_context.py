"""Closed-candle clock for research/backtest/replay only; no feed or orders.

Only fixed UTC timeframes are supported here. Indicator source and anchor
configuration remain with the indicator, never inferred from the model clock.
"""
from dataclasses import dataclass, field
import re
from numbers import Real
from typing import Callable


class CandleNotReady(ValueError):
    """The requested completed candle or its full input coverage is unavailable."""


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("Expected nonnegative integer UTC milliseconds")
    return value


def timeframe_ms(timeframe):
    match = re.fullmatch(r"([1-9][0-9]*)(m|h|d)", timeframe) if isinstance(timeframe, str) else None
    if not match:
        raise ValueError("Expected a fixed timeframe such as 1m, 15m, 1h or 1d")
    duration = int(match[1]) * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[match[2]]
    if duration > 86_400_000 or 86_400_000 % duration:
        raise ValueError("Timeframe must divide a UTC calendar day")
    return duration


def validate_signal_timing(signal):
    """Check known timing evidence; legacy signals may lack availability metadata."""
    eligible = _timestamp(signal["entry_eligible_timestamp_ms"])
    names = ("feature_as_of_ms", "feature_available_at_ms", "signal_timestamp_ms", "decision_timestamp_ms")
    for name in names:
        if name in signal and _timestamp(signal[name]) > eligible:
            raise ValueError(f"Entry eligibility precedes {name}")
    feature = signal.get("feature_as_of_ms")
    available = signal.get("feature_available_at_ms")
    if feature is not None and available is not None and feature > available:
        raise ValueError("Features cannot be available before their closing timestamp")
    for name in names[2:]:
        if name in signal and max((signal[k] for k in names[:2] if k in signal), default=0) > signal[name]:
            raise ValueError("Decision precedes feature availability")


@dataclass(frozen=True)
class IndicatorSpec:
    timeframe: str
    calculate: Callable
    parameters: dict = field(default_factory=dict)


class CandleContext:
    """One simulated decision-time snapshot for research and replay.

    Input is one instrument's OHLCV with a timezone-aware opening DatetimeIndex
    or integer timestamp_ms column. Historical availability defaults to close;
    recorded-event research supplies observed available_at_ms (or uses from_events).
    This API does not establish a deployment protocol or runtime integration.
    Missing input coverage stops evaluation; no prices/volume are fabricated.
    """
    def __init__(self, bars, *, as_of_ms, base_timeframe="1m"):
        import numpy as np
        import pandas as pd

        self.as_of_ms = _timestamp(as_of_ms)
        self.base_ms = timeframe_ms(base_timeframe)
        frame = bars.copy(deep=True)
        if not frame.columns.is_unique:
            raise ValueError("Candle columns must be unique")
        if "timestamp_ms" in frame:
            stamps = frame["timestamp_ms"]
            if stamps.dtype.kind not in "iu" or (stamps < 0).any():
                raise ValueError("timestamp_ms must be nonnegative integer milliseconds")
            frame.index = pd.to_datetime(stamps.to_numpy(), unit="ms", utc=True)
        elif isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
            frame.index = frame.index.tz_convert("UTC")
            if not frame.index.equals(frame.index.floor("ms")):
                raise ValueError("Candle timestamps must have millisecond precision")
            frame["timestamp_ms"] = frame.index.as_unit("ms").asi8
        else:
            raise ValueError("Candle opening timestamps must be explicit and timezone-aware")
        if frame.index.hasnans or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError("Candle timestamps must be valid, unique and ascending")
        if (frame["timestamp_ms"] % self.base_ms).any():
            raise ValueError("Candle opens must align to the base UTC timeframe")
        if "symbol" in frame and (frame.symbol.isna().any() or frame.symbol.nunique() > 1):
            raise ValueError("A candle context must contain one instrument")
        frame["end_ms"] = frame["timestamp_ms"] + self.base_ms
        if "available_at_ms" not in frame:
            frame["available_at_ms"] = frame["end_ms"]
        availability = frame["available_at_ms"]
        if availability.dtype.kind not in "iu" or (availability < frame["end_ms"]).any():
            raise ValueError("Candle availability must be integer milliseconds at/after close")
        closed = (frame.end_ms <= as_of_ms) & (availability <= as_of_ms)
        if "is_closed" in frame:
            if frame.is_closed.dtype.kind != "b":
                raise ValueError("is_closed must contain booleans")
            closed &= frame.is_closed
        frame = frame.loc[closed].copy()
        columns = ["open", "high", "low", "close", "volume"]
        if any(col not in frame for col in columns):
            raise ValueError("Missing OHLCV columns")
        frame[columns] = frame[columns].astype(float)
        if (not np.isfinite(frame[columns].to_numpy()).all() or (frame.volume < 0).any()
                or (frame.low > frame[["open", "close"]].min(axis=1)).any()
                or (frame.high < frame[["open", "close"]].max(axis=1)).any()
                or (frame.low > frame.high).any()):
            raise ValueError("Invalid completed OHLCV candle")
        if any(col in frame for col in ("delta", "buy_volume", "sell_volume")):
            from indicator.research.cvd import volume_delta
            frame["delta"] = volume_delta(frame)
            for col in ("buy_volume", "sell_volume"):
                if col in frame:
                    frame[col] = frame[col].astype(float)
        self._bars = frame

    @classmethod
    def from_events(cls, events, *, as_of_ms, base_timeframe="1m"):
        import pandas as pd
        from utils.engine.contracts import validate_event

        rows, instruments = [], set()
        for raw in events:
            event = validate_event(raw)
            if event["event_type"] != "completed_bar" or event["quality"] != "complete" or event["revision"] != 0:
                raise ValueError("Expected original complete candle events")
            instruments.add((event["venue"], event["product"], event["instrument_id"]))
            payload = event["payload"]
            if payload["close_timestamp_ms"] - payload["open_timestamp_ms"] != timeframe_ms(base_timeframe):
                raise ValueError("Event candle duration differs from base timeframe")
            rows.append({**payload, "timestamp_ms": payload["open_timestamp_ms"],
                         "available_at_ms": max(event["available_at_ms"], event["received_timestamp_ms"])})
        if len(instruments) > 1:
            raise ValueError("Candle events mix instruments or venues")
        if not rows:
            raise CandleNotReady("No completed candle events")
        return cls(pd.DataFrame(rows), as_of_ms=as_of_ms, base_timeframe=base_timeframe)

    def candles(self, timeframe, *, through_ms=None):
        """Closed, fully covered OHLCV buckets, labelled by UTC opening time."""
        duration = timeframe_ms(timeframe)
        through_ms = self.as_of_ms if through_ms is None else _timestamp(through_ms)
        if through_ms > self.as_of_ms:
            raise ValueError("Candle cutoff cannot exceed decision time")
        if duration < self.base_ms or duration % self.base_ms:
            raise ValueError("Calculation timeframe must be a multiple of base timeframe")
        if self._bars.empty:
            raise CandleNotReady("No available completed candles")
        aggregations = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                            close=("close", "last"), volume=("volume", "sum"),
                            bar_count=("close", "count"), available_at_ms=("available_at_ms", "max"))
        for col in ("delta", "buy_volume", "sell_volume"):
            if col in self._bars:
                aggregations[col] = (col, "sum")
        result = self._bars.resample(f"{duration}ms", origin="epoch", label="left", closed="left").agg(**aggregations)
        result["timestamp_ms"] = result.index.as_unit("ms").asi8
        result["end_ms"] = result.timestamp_ms + duration
        result = result.loc[result.end_ms <= through_ms].copy()
        if (result.bar_count != duration // self.base_ms).any():
            raise CandleNotReady("Incomplete candle coverage; repair the input history")
        return result

    def _latest(self, frame, timeframe, *, through_ms=None):
        cutoff = self.as_of_ms if through_ms is None else through_ms
        expected_end = cutoff // timeframe_ms(timeframe) * timeframe_ms(timeframe)
        if frame.empty or int(frame.end_ms.iloc[-1]) != expected_end:
            raise CandleNotReady(f"Latest completed {timeframe} candle is unavailable")
        return frame.iloc[-1].to_dict()

    def evaluate(self, model, *, evaluation_timeframe, indicators):
        """Update all indicators first, then evaluate one model; never submit orders.

        The caller owns one-call-per-boundary deduplication and execution. This
        stateless implementation recalculates supplied history for each snapshot.
        """
        import math
        import pandas as pd

        evaluation_bar = self._latest(self.candles(evaluation_timeframe), evaluation_timeframe)
        boundary = int(evaluation_bar["end_ms"])
        values, timing = {}, {}
        for name, spec in indicators.items():
            frame = self.candles(spec.timeframe, through_ms=boundary)
            latest = self._latest(frame, spec.timeframe, through_ms=boundary)
            output = spec.calculate(frame.copy(deep=True), **spec.parameters)
            if not isinstance(output, (pd.Series, pd.DataFrame)) or not output.index.equals(frame.index):
                raise ValueError("Indicator must preserve its calculation candle index")
            row = output.iloc[-1]
            row_values = row.to_list() if isinstance(row, pd.Series) else [row]
            if any(pd.isna(x) or (isinstance(x, Real) and not math.isfinite(x)) for x in row_values):
                raise CandleNotReady(f"Indicator {name} has not completed warmup")
            values[name] = row.to_dict() if isinstance(row, pd.Series) else row
            timing[name] = dict(timeframe=spec.timeframe, end_ms=int(latest["end_ms"]))
        snapshot = dict(feature_as_of_ms=int(evaluation_bar["end_ms"]),
            feature_available_at_ms=int(self._bars.loc[self._bars.end_ms <= boundary, "available_at_ms"].max()),
            decision_timestamp_ms=self.as_of_ms, entry_eligible_timestamp_ms=self.as_of_ms,
            bar=evaluation_bar, indicators=values, indicator_timing=timing)
        validate_signal_timing(snapshot)
        return {**snapshot, "decision": model(snapshot)}
