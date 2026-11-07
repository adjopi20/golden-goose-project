"""Shared validation and seeded smoothing primitives; no indicator bundle."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

TECHNICAL_CONVENTION = "wilder_talib_seed_v1"


def _period(window: int, minimum: int = 1) -> int:
    if type(window) is not int or not minimum <= window <= 100_000:
        raise ValueError(f"window must be an integer from {minimum} to 100000")
    return window


def _standard_series(series: pd.Series) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise ValueError("Indicator input must be a Series")
    if series.index.hasnans or not series.index.is_unique or not series.index.is_monotonic_increasing:
        raise ValueError("Indicator indexes must be valid, unique and ascending")
    values = series.astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("Indicator input must be finite; repair missing history before calculating")
    return values


def _standard_hlc(high: pd.Series, low: pd.Series, close: pd.Series):
    high, low, close = map(_standard_series, (high, low, close))
    if not high.index.equals(low.index) or not high.index.equals(close.index):
        raise ValueError("High, low and close must have identical ordered indexes")
    if ((high < low) | (close < low) | (close > high)).any():
        raise ValueError("Invalid high/low/close geometry")
    return high, low, close


def _seeded_average(values: pd.Series, window: int, alpha: float, *, start: int = 0) -> pd.Series:
    output = np.full(len(values), np.nan)
    seed = start + window - 1
    if seed < len(values):
        # Divide first to avoid overflowing a finite average's unscaled sum.
        state = math.fsum(float(x) / window for x in values.iloc[start:seed+1])
        output[seed] = state
        for i in range(seed+1, len(values)):
            state = state * (1 - alpha) + float(values.iloc[i]) * alpha
            if not math.isfinite(state):
                raise ValueError("Indicator smoothing overflowed float64")
            output[i] = state
    return pd.Series(output, index=values.index)

