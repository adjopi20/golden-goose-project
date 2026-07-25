from __future__ import annotations

import pandas as pd


def vwap(price: pd.Series, volume: pd.Series, group: pd.Series | None = None) -> pd.Series:
    pv = price.astype(float) * volume.astype(float)
    vol = volume.astype(float)
    if group is None:
        return pv.cumsum() / vol.cumsum().replace(0, pd.NA)
    return pv.groupby(group).cumsum() / vol.groupby(group).cumsum().replace(0, pd.NA)


def vwap_std(price: pd.Series, volume: pd.Series, anchor_vwap: pd.Series, group: pd.Series | None = None) -> pd.Series:
    weighted_var = (price.astype(float) - anchor_vwap.astype(float)).pow(2) * volume.astype(float)
    vol = volume.astype(float)
    if group is None:
        return (weighted_var.cumsum() / vol.cumsum().replace(0, pd.NA)).clip(lower=0).pow(0.5)
    return (weighted_var.groupby(group).cumsum() / vol.groupby(group).cumsum().replace(0, pd.NA)).clip(lower=0).pow(0.5)


def vwap_bands(anchor_vwap: pd.Series, std: pd.Series, mult: float) -> pd.DataFrame:
    width = std.astype(float) * float(mult)
    return pd.DataFrame({"vwap_upper": anchor_vwap + width, "vwap_lower": anchor_vwap - width})
