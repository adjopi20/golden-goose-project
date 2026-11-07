"""Independent SMA/EMA/RMA/WMA indicator family for research."""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicator.research._common import _period, _seeded_average, _standard_series


def moving_average(series: pd.Series, window: int = 20, *, method: str = "sma") -> pd.Series:
    """Standard research MA: SMA, SMA-seeded EMA/RMA, or linear WMA.

    EMA alpha=2/(N+1); Wilder RMA alpha=1/N; WMA weights=1..N oldest to newest.
    The first value requires N observations. Period 1 is the identity.
    """
    window = _period(window)
    values = _standard_series(series)
    if method not in ("sma", "ema", "rma", "wma"):
        raise ValueError("MA method must be sma, ema, rma or wma")
    if method == "sma":
        output = values.rolling(window, min_periods=window).mean()
    elif method == "wma":
        weights = np.arange(1, window+1, dtype=float)
        weights /= weights.sum()
        output = values.rolling(window, min_periods=window).apply(lambda x:float(np.dot(x, weights)), raw=True)
    else:
        output = _seeded_average(values, window, 2/(window+1) if method == "ema" else 1/window)
    if np.isinf(output.to_numpy()).any():
        raise ValueError("Moving average overflowed float64")
    return output.rename(f"{method}_{window}")

