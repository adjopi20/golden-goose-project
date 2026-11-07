from __future__ import annotations

from io import StringIO
from pathlib import Path

import pandas as pd
import numpy as np
import requests


COINMETRICS_URL = (
    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
)
METRICS = ("PriceUSD", "CapMrktCurUSD", "CapMVRVCur")

TREASURY_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
TREASURY_SERIES = "DTB3"


def prepared_window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Bound and validate cached data before it can drive a historical replay."""
    frame = frame.copy()
    required = {"date", "close", "mvrv_zscore"}
    if not required <= set(frame.columns):
        raise ValueError(f"prepared data missing columns: {sorted(required - set(frame.columns))}")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError("prepared dates must be valid and unique")
    frame = frame.loc[frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    frame = frame.sort_values("date").reset_index(drop=True)
    if frame.empty:
        raise ValueError("no prepared data in requested date window")
    expected = pd.date_range(start, frame["date"].iloc[-1], freq="D")
    if not pd.DatetimeIndex(frame["date"]).equals(expected):
        raise ValueError("prepared history must have every calendar day from the requested start")
    numeric = [c for c in ("close", "mvrv_zscore", "nupl", "cvdd") if c in frame]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame[numeric].to_numpy(dtype=float)).all() or frame["close"].le(0).any():
        raise ValueError("prepared prices and indicators must be finite, with positive prices")
    return frame


def download_coinmetrics(destination: Path, requested_end: str) -> pd.Timestamp:
    params = {
        "assets": "btc",
        "metrics": ",".join(METRICS),
        "frequency": "1d",
        "start_time": "2009-01-03",
        "end_time": requested_end,
        "page_size": 10_000,
        "paging_from": "start",
    }
    rows: list[dict[str, str | None]] = []
    while True:
        response = requests.get(COINMETRICS_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        rows.extend(payload["data"])
        token = payload.get("next_page_token")
        if not token:
            break
        params["next_page_token"] = token

    frame = pd.DataFrame(rows)
    if frame.empty or not {"time", *METRICS}.issubset(frame.columns):
        raise ValueError("Coin Metrics did not return the required BTC metrics")
    frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.strftime("%Y-%m-%d")
    for column in METRICS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[["time", *METRICS]].drop_duplicates("time").sort_values("time")
    complete = frame.dropna(subset=list(METRICS))
    if complete.empty:
        raise ValueError("Coin Metrics returned no complete BTC observations")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(destination)
    return pd.Timestamp(complete["time"].iloc[-1])


def latest_cdd_date(path: Path) -> pd.Timestamp:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing")
    frame = pd.read_csv(path, sep="\t")
    if len(frame.columns) != 2:
        raise ValueError("CDD TSV must contain exactly two columns")
    dates = pd.to_datetime(frame.iloc[:, 0], format="%d.%m.%Y", errors="raise")
    return pd.Timestamp(dates.max())


def parse_treasury_yield(text: str) -> pd.Series:
    """Parse a FRED CSV payload into an annualized decimal yield by date.

    FRED omits non-business days and writes missing observations as ``.``; both
    are dropped here. The returned series is in decimal form, so a quoted
    `4.20` becomes `0.042`.
    """
    frame = pd.read_csv(StringIO(text))
    if frame.shape[1] != 2:
        raise ValueError("treasury CSV must contain exactly two columns")
    frame.columns = ["date", "rate"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["rate"] = pd.to_numeric(frame["rate"], errors="coerce")
    frame = frame.dropna().drop_duplicates("date").sort_values("date")
    if frame.empty:
        raise ValueError("treasury CSV contains no usable observations")
    rates = pd.Series(
        frame["rate"].to_numpy(dtype=float) / 100.0,
        index=pd.DatetimeIndex(frame["date"]),
        name=TREASURY_SERIES.lower(),
    )
    return rates


def download_treasury_yield(
    destination: Path,
    start: str,
    end: str,
) -> pd.Series:
    """Fetch the 3-month Treasury bill rate and cache it as a dated CSV."""
    response = requests.get(
        TREASURY_URL,
        params={"id": TREASURY_SERIES, "cosd": start, "coed": end},
        timeout=60,
    )
    response.raise_for_status()
    rates = parse_treasury_yield(response.text)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    rates.rename("annual_yield").to_csv(temporary, index_label="date")
    temporary.replace(destination)
    return rates


def load_treasury_yield(path: Path) -> pd.Series:
    """Read a cached treasury yield CSV, or raise if it is missing."""
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; download it first")
    frame = pd.read_csv(path)
    if not {"date", "annual_yield"}.issubset(frame.columns):
        raise ValueError("treasury cache must contain date and annual_yield")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return pd.Series(
        pd.to_numeric(frame["annual_yield"], errors="coerce").to_numpy(dtype=float),
        index=pd.DatetimeIndex(frame["date"]),
        name="annual_yield",
    ).dropna()
