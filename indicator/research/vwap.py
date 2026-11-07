from __future__ import annotations

import pandas as pd
import numpy as np


VWAP_SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4")
VWAP_ANCHOR_PERIODS = ("session", "week", "month", "quarter", "year", "decade")


def anchored_vwap(
    bars: pd.DataFrame,
    source: str = "hlc3",
    anchor_period: str = "month",
    *,
    timezone: str = "UTC",
) -> pd.Series:
    """Cumulative candle-source VWAP, monthly by default, for one instrument.

    Requires unique ascending timezone-aware candle-open timestamps as the index.
    Session resets at local midnight (UTC by default); weeks start Monday and
    decades start in years divisible by ten. Supply complete anchor history for
    a full-period value. Current-bar values are final only after its close.
    Candle-source VWAP approximates trade VWAP; it depends on bar resolution.
    """
    if source not in VWAP_SOURCES:
        raise ValueError(f"source must be one of {VWAP_SOURCES}")
    if anchor_period not in VWAP_ANCHOR_PERIODS:
        raise ValueError(f"anchor_period must be one of {VWAP_ANCHOR_PERIODS}")
    index = bars.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("bars must have a timezone-aware DatetimeIndex")
    if index.hasnans or not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("bars timestamps must be valid, unique and ascending")
    local = index.tz_convert(timezone).tz_localize(None)
    columns = {
        "hl2": ["high", "low"],
        "hlc3": ["high", "low", "close"],
        "ohlc4": ["open", "high", "low", "close"],
    }.get(source, [source])
    required = columns + ["volume"]
    if not bars.columns.is_unique or any(col not in bars for col in required):
        raise ValueError(f"bars must have unique columns including {required}")
    if "symbol" in bars and (bars["symbol"].isna().any() or bars["symbol"].nunique() > 1):
        raise ValueError("bars must contain only one instrument")
    values = bars[required].astype(float)
    if not np.isfinite(values.to_numpy()).all() or (values["volume"] < 0).any():
        raise ValueError("source and volume must be finite; volume must be nonnegative")
    if anchor_period == "decade":
        keys = local.year // 10
    else:
        frequency = {"session": "D", "week": "W-SUN", "month": "M",
                     "quarter": "Q", "year": "Y"}[anchor_period]
        keys = local.to_period(frequency)
    group = pd.Series(keys, index=index)
    price = values[columns].mean(axis=1)
    volume = values["volume"]
    return vwap(price, volume, group).rename("vwap")


def vwap(price: pd.Series, volume: pd.Series, group: pd.Series | None = None) -> pd.Series:
    """Cumulative sum(price * quantity) / sum(quantity), optionally per group.

    With actual trade prices/quantities this computes trade VWAP. With candle
    sources it computes the selected candle approximation. Inputs must align.
    """
    if not price.index.equals(volume.index):
        raise ValueError("price and volume indexes must match in the same order")
    if group is not None and (not price.index.equals(group.index) or group.isna().any()):
        raise ValueError("group must align with price and contain no missing labels")
    price = price.astype(float)
    vol = volume.astype(float)
    if not np.isfinite(price.to_numpy()).all() or not np.isfinite(vol.to_numpy()).all() or (vol < 0).any():
        raise ValueError("price and volume must be finite; volume must be nonnegative")
    with np.errstate(over="ignore", invalid="ignore"):
        pv = price * vol
        if group is None:
            numerator, denominator = pv.cumsum(), vol.cumsum()
        else:
            numerator, denominator = pv.groupby(group).cumsum(), vol.groupby(group).cumsum()
    if not np.isfinite(numerator.to_numpy()).all() or not np.isfinite(denominator.to_numpy()).all():
        raise ValueError("VWAP accumulation overflowed float64")
    return numerator / denominator.where(denominator != 0)


def vwap_std(price: pd.Series, volume: pd.Series, anchor_vwap: pd.Series, group: pd.Series | None = None) -> pd.Series:
    """Volume-weighted population std of prices around the current anchor mean.

    Weighted Welford increments avoid subtracting two large raw second moments.
    This corrects the legacy cumulative residual-to-changing-mean calculation.
    """
    mean = vwap(price, volume, group)
    if not price.index.equals(anchor_vwap.index) or not np.allclose(
        anchor_vwap.astype(float), mean, rtol=1e-12, atol=1e-12, equal_nan=True
    ):
        raise ValueError("anchor_vwap must match VWAP for the same price, volume and group")
    price = price.astype(float)
    vol = volume.astype(float)
    previous = mean.shift() if group is None else mean.groupby(group).shift()
    previous = previous.fillna(price)
    increments = (vol * (price - previous) * (price - mean)).where(vol > 0, 0.0)
    if group is None:
        m2, weight = increments.cumsum(), vol.cumsum()
    else:
        m2, weight = increments.groupby(group).cumsum(), vol.groupby(group).cumsum()
    if not np.isfinite(m2.to_numpy()).all():
        raise ValueError("VWAP variance accumulation overflowed float64")
    return (m2 / weight.where(weight != 0)).clip(lower=0).pow(0.5)


def vwap_bands(anchor_vwap: pd.Series, std: pd.Series, mult: float) -> pd.DataFrame:
    if not anchor_vwap.index.equals(std.index):
        raise ValueError("VWAP and std indexes must match")
    if not np.isfinite(float(mult)) or float(mult) < 0:
        raise ValueError("band multiplier must be finite and nonnegative")
    if (std < 0).any() or np.isinf(std.to_numpy(dtype=float)).any():
        raise ValueError("std must be nonnegative and finite where defined")
    width = std.astype(float) * float(mult)
    return pd.DataFrame({"vwap_upper": anchor_vwap + width, "vwap_lower": anchor_vwap - width})
