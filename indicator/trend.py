from __future__ import annotations

import pandas as pd


GMMA_SHORT = (3, 5, 8, 10, 12, 15)
GMMA_LONG = (30, 35, 40, 45, 50, 60)


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.astype(float).rolling(window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False).mean()


def gmma(close: pd.Series, short: tuple[int, ...] = GMMA_SHORT, long: tuple[int, ...] = GMMA_LONG) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    for period in short:
        out[f"ema_{period}"] = ema(close, period)
    for period in long:
        out[f"ema_{period}"] = ema(close, period)
    out["gmma_short_mean"] = out[[f"ema_{p}" for p in short]].mean(axis=1)
    out["gmma_long_mean"] = out[[f"ema_{p}" for p in long]].mean(axis=1)
    out["gmma_spread"] = (out["gmma_short_mean"] / out["gmma_long_mean"]) - 1.0
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.astype(float).shift(1)
    return pd.concat(
        [
            high.astype(float) - low.astype(float),
            (high.astype(float) - prev_close).abs(),
            (low.astype(float) - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / window, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    high = high.astype(float)
    low = low.astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(high, low, close)
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False).mean() / tr.ewm(alpha=1 / window, adjust=False).mean()
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False).mean() / tr.ewm(alpha=1 / window, adjust=False).mean()
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / window, adjust=False).mean()


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_window: int = 20,
    atr_window: int = 14,
    atr_mult: float = 2.0,
) -> pd.DataFrame:
    middle = ema(close, ema_window)
    width = atr(high, low, close, atr_window) * float(atr_mult)
    return pd.DataFrame({"keltner_mid": middle, "keltner_upper": middle + width, "keltner_lower": middle - width})


def z_score(series: pd.Series, window: int) -> pd.Series:
    series = series.astype(float)
    return (series - series.rolling(window).mean()) / series.rolling(window).std()
