from __future__ import annotations

from typing import Any

import pandas as pd

from indicator.trend import adx, gmma, keltner_channels, sma, z_score


DEFAULT_PARAMS = {
    "frequency": "W-SUN",
    "persistence_weeks": 8,
    "sma_days": 200,
    "sma_slope_days": 30,
    "adx_days": 14,
    "adx_trend": 25,
    "adx_range": 20,
    "z_days": 90,
    "z_overextended": 3.0,
    "keltner_ema_days": 20,
    "keltner_atr_days": 14,
    "keltner_atr_mult": 2.0,
}


def compute_market_cycle_regimes(daily: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.DataFrame:
    """Observer-only trend regime labels from daily OHLC data."""
    cfg = {**DEFAULT_PARAMS, **(params or {})}
    if daily.empty:
        return pd.DataFrame()

    df = _daily_index(daily)
    high = df.get("high", df["close"]).astype(float)
    low = df.get("low", df["close"]).astype(float)
    close = df["close"].astype(float)
    ma = sma(close, int(cfg["sma_days"]))
    ribbon = gmma(close)
    keltner = keltner_channels(
        high,
        low,
        close,
        ema_window=int(cfg["keltner_ema_days"]),
        atr_window=int(cfg["keltner_atr_days"]),
        atr_mult=float(cfg["keltner_atr_mult"]),
    )
    features = pd.DataFrame(
        {
            "close": close,
            "sma200": ma,
            "sma200_slope": (ma / ma.shift(int(cfg["sma_slope_days"]))) - 1.0,
            "drawdown_1y": (close / close.rolling(365).max()) - 1.0,
            "adx": adx(high, low, close, int(cfg["adx_days"])),
            "z_score": z_score(close, int(cfg["z_days"])),
        }
    )
    features = pd.concat(
        [
            features,
            ribbon[["gmma_short_mean", "gmma_long_mean", "gmma_spread"]],
            keltner,
        ],
        axis=1,
    )
    features["keltner_position"] = (close - features["keltner_mid"]) / (features["keltner_upper"] - features["keltner_mid"])

    weekly = features.resample(str(cfg["frequency"])).last().dropna(subset=["close"]).reset_index(names="date")
    weekly["structure_raw"] = [_structure_label(row, cfg) for _, row in weekly.iterrows()]
    weekly["structure"] = _confirm(weekly["structure_raw"], int(cfg["persistence_weeks"]))
    weekly["market_cycle_raw"] = "not_classified"
    weekly["market_cycle"] = "not_classified"
    weekly["observer_only"] = True
    return weekly.where(pd.notnull(weekly), None)


def _daily_index(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    if "date" in df:
        dt = pd.to_datetime(df["date"], utc=True)
    elif "timestamp_ms" in df:
        dt = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    else:
        dt = pd.to_datetime(df.index, utc=True)
    if "close" not in df:
        raise ValueError("daily data must include a close column")
    return df.assign(dt=dt).set_index("dt").sort_index()


def _structure_label(row: pd.Series, cfg: dict[str, Any]) -> str:
    bullish = _gt(row.get("close"), row.get("sma200")) and _gt(row.get("gmma_spread"), 0.0) and _gt(row.get("sma200_slope"), 0.0)
    bearish = _lt(row.get("close"), row.get("sma200")) and _lt(row.get("gmma_spread"), 0.0) and _lt(row.get("sma200_slope"), 0.0)
    trending = _gt(row.get("adx"), float(cfg["adx_trend"]))
    ranging = _lt(row.get("adx"), float(cfg["adx_range"])) and abs(float(row.get("gmma_spread") or 0.0)) < 0.02
    overextended_up = _gt(row.get("z_score"), float(cfg["z_overextended"])) or _gt(row.get("keltner_position"), 1.5)
    overextended_down = _lt(row.get("z_score"), -float(cfg["z_overextended"])) or _lt(row.get("keltner_position"), -1.5)

    if bullish and trending:
        return "overextended_bull_trend" if overextended_up else "healthy_bull_trend"
    if bearish and trending:
        return "overextended_bear_trend" if overextended_down else "healthy_bear_trend"
    if ranging:
        return "range_chop"
    return "transition"


def _confirm(raw: pd.Series, weeks: int) -> list[str]:
    effective: list[str] = []
    current = "transition"
    for i, value in enumerate(raw.to_list()):
        if i + 1 >= weeks and raw.iloc[i - weeks + 1 : i + 1].eq(value).all():
            current = str(value)
        effective.append(current)
    return effective


def _lt(value: Any, limit: float) -> bool:
    return value is not None and pd.notnull(value) and float(value) < limit


def _gt(value: Any, limit: float) -> bool:
    return value is not None and pd.notnull(value) and float(value) > limit
