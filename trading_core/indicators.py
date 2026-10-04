"""Existing indicator formulas shared by research and runtime; no outcomes or IO."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
MINUTE_MS = 60_000
HOUR_MS = 3_600_000


def _finite(value):
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def hourly_indicators(minutes: pd.DataFrame) -> pd.DataFrame:
    """EMA/ATR/ADX from completed, nonempty clock-hour bars (not current hour)."""
    indexed = minutes.set_index(pd.to_datetime(minutes["timestamp_ms"], unit="ms", utc=True))
    hourly = indexed.resample("1h").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        minute_count=("close", "count"))
    hourly = hourly.loc[hourly["minute_count"] > 0].copy()
    close, high, low = hourly["close"], hourly["high"], hourly["low"]
    hourly["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    hourly["ema200"] = close.ewm(span=200, adjust=False, min_periods=200).mean()
    previous_close = close.shift(1)
    true_range = pd.concat((high-low, (high-previous_close).abs(),
                            (low-previous_close).abs()), axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, np.nan)
    hourly["atr14"] = atr
    hourly["adx14"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    hourly["realized_volatility_20h_pct"] = 100*close.pct_change().rolling(20, min_periods=20).std()
    hourly["atr14_to_20h_median"] = atr/atr.rolling(20, min_periods=20).median()
    # DatetimeIndex.asi8 uses the index's own resolution (ms here), not always ns.
    hourly["end_ms"] = hourly.index.as_unit("ms").asi8 + HOUR_MS
    if (hourly["end_ms"].iloc[0] <= int(minutes["timestamp_ms"].iloc[0]) or
            not hourly["end_ms"].is_monotonic_increasing):
        raise ValueError("Completed hourly timestamps are not valid epoch milliseconds")
    return hourly.reset_index(drop=True)


def trend_indicators_15m(minutes: pd.DataFrame) -> pd.DataFrame:
    """Observation only: all-hours EMA200/SMA20/ADX14 on clock-aligned 15m bars.

    Empty bars reset warmup; partial bars retain observed prices and coverage.
    No fabricated prices. EMA and Wilder smoothing start with a full-window SMA.
    """
    indexed = minutes.set_index(pd.to_datetime(minutes["timestamp_ms"], unit="ms", utc=True))
    bars = indexed.resample("15min").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        minute_count=("close", "count"))

    def smooth(values: pd.Series, period: int, alpha: float) -> pd.Series:
        seed = values.rolling(period, min_periods=period).mean().first_valid_index()
        output = pd.Series(np.nan, index=values.index, dtype=float)
        if seed is not None:
            seeded = values.loc[seed:].copy()
            seeded.iloc[0] = values.loc[:seed].iloc[-period:].mean()
            output.loc[seed:] = seeded.ewm(alpha=alpha, adjust=False).mean()
        return output

    chunks = []
    # Every missing full quarter-hour breaks continuity, not every missing minute.
    for _, group in bars.groupby(bars["minute_count"].eq(0).cumsum(), sort=False):
        group = group.loc[group["minute_count"] > 0].copy()
        if group.empty:
            continue
        close, high, low = group["close"], group["high"], group["low"]
        group["ema200"] = smooth(close, 200, 2 / 201)
        group["sma20"] = close.rolling(20, min_periods=20).mean()
        tr = pd.concat((high-low, (high-close.shift()).abs(),
                        (low-close.shift()).abs()), axis=1).max(axis=1)
        atr = smooth(tr, 14, 1 / 14)
        up, down = high.diff(), -low.diff()
        plus = smooth(up.where((up > down) & (up > 0), 0.0), 14, 1 / 14)
        minus = smooth(down.where((down > up) & (down > 0), 0.0), 14, 1 / 14)
        group["plus_di14"] = (100 * plus / atr.replace(0, np.nan)).where(atr.ne(0), 0.0)
        group["minus_di14"] = (100 * minus / atr.replace(0, np.nan)).where(atr.ne(0), 0.0)
        di_sum = group["plus_di14"] + group["minus_di14"]
        dx = (100 * (group["plus_di14"] - group["minus_di14"]).abs()
              / di_sum.replace(0, np.nan)).where(di_sum.ne(0), 0.0)
        group["adx14"] = smooth(dx, 14, 1 / 14)
        for name in ("ema200", "sma20"):
            group[f"{name}_change_4bars_pct"] = 100 * (group[name] / group[name].shift(4) - 1)
            group[f"price_vs_{name}_pct"] = 100 * (close / group[name] - 1)
        group["sma20_vs_ema200_pct"] = 100 * (group["sma20"] / group["ema200"] - 1)
        group["adx14_change_4bars"] = group["adx14"] - group["adx14"].shift(4)
        group["minute_coverage"] = group["minute_count"] / 15
        group["minute_coverage_200bars"] = group["minute_count"].rolling(200).sum() / 3000
        chunks.append(group)
    if not chunks:
        raise ValueError("No nonempty 15-minute bars")
    output = pd.concat(chunks).sort_index()
    output["end_ms"] = output.index.as_unit("ms").asi8 + 15 * MINUTE_MS
    return output.reset_index(drop=True)


def trend_snapshot_15m(frame: pd.DataFrame, as_of_ms: int, direction: str) -> dict:
    """Last completed quarter only; future bars never participate in the snapshot."""
    position = int(np.searchsorted(frame["end_ms"].to_numpy(), as_of_ms, side="right")) - 1
    expected_end = as_of_ms // (15 * MINUTE_MS) * (15 * MINUTE_MS)
    if position < 0 or int(frame.iloc[position]["end_ms"]) != expected_end:
        return {"status": "missing_completed_bar", "ma_structure": "unknown"}
    row = _finite(frame.iloc[position].to_dict())
    row["end_ms"] = int(row["end_ms"])
    ema, sma, close = row["ema200"], row["sma20"], row["close"]
    row["status"] = "ready" if all(row[k] is not None for k in ("ema200", "sma20", "adx14")) else "warmup"
    structure = ("unknown" if ema is None or sma is None else
                 "bullish_stack" if close > sma > ema else
                 "bearish_stack" if close < sma < ema else "mixed")
    row["ma_structure"] = structure
    if structure in ("unknown", "mixed"):
        row["trade_alignment"] = structure
    else:
        row["trade_alignment"] = "aligned" if (structure == "bullish_stack") == (direction == "long") else "opposed"
    return row
