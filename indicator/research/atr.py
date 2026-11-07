"""Independent true-range and Wilder ATR indicators for research."""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicator.research._common import _period, _seeded_average, _standard_hlc


def candle_true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(H-L, abs(H-previous C), abs(L-previous C)); first value undefined."""
    high, low, close = _standard_hlc(high, low, close)
    previous = close.shift(1)
    with np.errstate(over="ignore", invalid="ignore"):
        output = pd.concat([high-low, (high-previous).abs(), (low-previous).abs()], axis=1).max(axis=1)
    if len(output):
        output.iloc[0] = np.nan
    if np.isinf(output.to_numpy()).any():
        raise ValueError("True range overflowed float64")
    return output.rename("true_range")


def average_true_range(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder ATR with SMA seed over N true ranges that have a previous close.

    First valid output is zero-based index N (N+1 price candles).
    """
    window = _period(window)
    ranges = candle_true_range(high, low, close)
    return _seeded_average(ranges, window, 1/window, start=1).rename(f"atr_{window}")

