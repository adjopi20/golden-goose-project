"""Independent Wilder ADX/DMI indicators for research."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

from indicator.research._common import _period, _seeded_average, _standard_hlc
from indicator.research.atr import candle_true_range


def directional_movement_index(high: pd.Series, low: pd.Series, close: pd.Series,
                               window: int = 14) -> pd.DataFrame:
    """Wilder +DI/-DI/DX/ADX with TA-Lib 0.6.4's default seed convention.

    DI/DX start at index N; ADX starts at 2*N-1. Equal competing DM moves are
    both zero. A zero TR or DI sum yields zero, without a price-scale epsilon.
    """
    window = _period(window, minimum=2)
    high, low, close = _standard_hlc(high, low, close)
    ranges = candle_true_range(high, low, close)
    up, down = high.diff(), -low.diff()
    plus = up.where((up > down) & (up > 0), 0.)
    minus = down.where((down > up) & (down > 0), 0.)
    plus_di = np.full(len(close), np.nan)
    minus_di = np.full(len(close), np.nan)
    dx = np.full(len(close), np.nan)
    if len(close) > window:
        tr_sum = math.fsum(ranges.iloc[1:window])
        plus_sum = math.fsum(plus.iloc[1:window])
        minus_sum = math.fsum(minus.iloc[1:window])
        for i in range(window, len(close)):
            tr_sum = tr_sum - tr_sum/window + float(ranges.iloc[i])
            plus_sum = plus_sum - plus_sum/window + float(plus.iloc[i])
            minus_sum = minus_sum - minus_sum/window + float(minus.iloc[i])
            if not all(math.isfinite(x) for x in (tr_sum, plus_sum, minus_sum)):
                raise ValueError("Directional movement smoothing overflowed float64")
            plus_di[i] = 100 * (plus_sum/tr_sum) if tr_sum > 0 else 0.
            minus_di[i] = 100 * (minus_sum/tr_sum) if tr_sum > 0 else 0.
            total = plus_di[i] + minus_di[i]
            dx[i] = 100 * abs(plus_di[i]-minus_di[i])/total if total > 0 else 0.
    dx_series = pd.Series(dx, index=close.index)
    adx_values = _seeded_average(dx_series, window, 1/window, start=window)
    return pd.DataFrame(dict(plus_di=plus_di, minus_di=minus_di, dx=dx, adx=adx_values), index=close.index)

