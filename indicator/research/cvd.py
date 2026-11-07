"""Executed-volume delta and CVD for research; no price-derived trade signing."""
from __future__ import annotations

import numpy as np
import pandas as pd


def volume_delta(bars: pd.DataFrame) -> pd.Series:
    """Buy-aggressor quantity minus sell-aggressor quantity in each candle.

    Requires total volume plus both side volumes, or an explicitly supplied
    signed delta. All quantities must use the same instrument and volume unit.
    Float consistency checks use rtol=1e-9, atol=1e-12; no output rounding.
    """
    if not bars.columns.is_unique or "volume" not in bars:
        raise ValueError("Expected unique columns including total volume")
    if "symbol" in bars and (bars.symbol.isna().any() or bars.symbol.nunique() > 1):
        raise ValueError("Order-flow input must contain one instrument")
    sides = ["buy_volume", "sell_volume"]
    have_sides = all(col in bars for col in sides)
    if any(col in bars for col in sides) and not have_sides:
        raise ValueError("Supply both aggressor-side volumes")
    if not have_sides and "delta" not in bars:
        raise ValueError("CVD needs measured buy/sell volume or signed delta; OHLCV alone is insufficient")
    columns = ["volume"] + (sides if have_sides else []) + (["delta"] if "delta" in bars else [])
    values = bars[columns].astype(float)
    if not np.isfinite(values.to_numpy()).all() or (values[[c for c in columns if c != "delta"]] < 0).any().any():
        raise ValueError("Volume inputs must be finite and unsigned quantities nonnegative")
    if have_sides:
        with np.errstate(over="ignore"):
            total = values.buy_volume + values.sell_volume
        if not np.allclose(total, values.volume, rtol=1e-9, atol=1e-12):
            raise ValueError("Buy plus sell volume must equal total volume")
        delta = values.buy_volume - values.sell_volume
        if "delta" in values and not np.allclose(delta, values.delta, rtol=1e-9, atol=1e-12):
            raise ValueError("Supplied delta conflicts with aggressor-side volumes")
    else:
        delta = values.delta
    excess = delta.abs() > values.volume
    if (excess & ~np.isclose(delta.abs(), values.volume, rtol=1e-9, atol=1e-12)).any():
        raise ValueError("Absolute delta cannot exceed total volume")
    return delta.rename("delta")


def cumulative_volume_delta(bars: pd.DataFrame, *, reset: str = "session") -> pd.Series:
    """Cumulative candle delta, resetting daily at 00:00 UTC by default.

    reset='continuous' accumulates from the first supplied candle. Neither mode
    certifies complete history. CandleContext owns timeframe/availability checks.
    This returns candle-close CVD, not reconstructed intrabar CVD highs/lows.
    """
    index = bars.index
    if (not isinstance(index, pd.DatetimeIndex) or index.tz is None or index.hasnans
            or not index.is_unique or not index.is_monotonic_increasing):
        raise ValueError("CVD requires unique ascending timezone-aware candle timestamps")
    delta = volume_delta(bars)
    if reset == "session":
        group = pd.Series(index.tz_convert("UTC").normalize(), index=index)
    elif reset == "continuous":
        group = pd.Series(0, index=index)
    else:
        raise ValueError("CVD reset must be 'session' (daily 00:00 UTC) or 'continuous'")
    with np.errstate(over="ignore", invalid="ignore"):
        result = delta.groupby(group, sort=False).cumsum()
    if not np.isfinite(result.to_numpy()).all():
        raise ValueError("CVD accumulation overflowed float64")
    return result.rename("cvd")
