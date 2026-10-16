"""Shared, daily, immutable-vintage Coin Metrics source and causal thresholds."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRICS = ["PriceUSD", "CapMrktCurUSD", "CapMVRVCur"]
ORIGIN = "2010-07-18"


class DataError(ValueError):
    pass


def utc(value=None):
    value = datetime.now(timezone.utc) if value is None else pd.Timestamp(value).to_pydatetime()
    if value.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return value.astimezone(timezone.utc)


def fetch(start, end, session=requests):
    params = dict(assets="btc", metrics=",".join(METRICS), frequency="1d",
                  start_time=start, end_time=end, page_size=10000, paging_from="start")
    rows, seen_tokens = [], set()
    for _ in range(100):
        try:
            response = session.get(URL, params=params, timeout=(10, 60))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise DataError(f"Coin Metrics request failed ({type(error).__name__})") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise DataError("Malformed Coin Metrics response")
        rows.extend(payload["data"])
        token = payload.get("next_page_token")
        if not token:
            return normalize(pd.DataFrame(rows))
        if token in seen_tokens:
            raise DataError("Repeated API pagination token")
        seen_tokens.add(token)
        params["next_page_token"] = token
    raise DataError("API pagination exceeded safety limit")


def normalize(frame):
    if not {"time", *METRICS} <= set(frame.columns) or frame.empty:
        raise DataError("Missing required daily metrics")
    frame = frame[["time", *METRICS]].copy()
    dates = pd.to_datetime(frame.time, utc=True, errors="raise")
    if not (dates == dates.dt.normalize()).all():
        raise DataError("Expected daily UTC timestamps")
    frame["time"] = dates.dt.strftime("%Y-%m-%d")
    frame = frame.sort_values("time").reset_index(drop=True)
    if frame.time.duplicated().any():
        raise DataError("Duplicate daily observation")
    frame[METRICS] = frame[METRICS].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(frame[METRICS].to_numpy()).all() or (frame[METRICS] <= 0).any().any():
        raise DataError("Incomplete, nonfinite or nonpositive observation")
    return frame


def merged_history(connection, incoming, fetched_at):
    """Validate before committing. Revisions are archived, never silently used."""
    incoming = normalize(incoming)
    old = pd.read_sql_query("SELECT date AS time, price AS PriceUSD, cap AS CapMrktCurUSD, ratio AS CapMVRVCur FROM raw ORDER BY date", connection)
    if not old.empty:
        overlap = old.merge(incoming, on="time", suffixes=("_old", "_new"))
        for metric in METRICS:
            if not np.isclose(overlap[metric + "_old"], overlap[metric + "_new"], rtol=1e-12, atol=1e-10).all():
                raise DataError("Historical revision detected; archived fetch requires operator review")
        incoming = incoming.loc[~incoming.time.isin(old.time)]
    combined = pd.concat([old, incoming], ignore_index=True).sort_values("time").reset_index(drop=True)
    if combined.empty or combined.time.iloc[0] != ORIGIN:
        raise DataError(f"Full indicator history must start {ORIGIN}")
    if list(combined.time) != list(pd.date_range(ORIGIN, combined.time.iloc[-1]).strftime("%Y-%m-%d")):
        raise DataError("Daily history has a gap")
    for date, price, cap, ratio in incoming.itertuples(index=False, name=None):
        connection.execute("INSERT INTO raw VALUES(?,?,?,?,?)", (date, price, cap, ratio, fetched_at))
    return combined


def prepare(raw, config):
    raw = normalize(raw)
    if raw.time.iloc[0] != config["history_origin"]:
        raise DataError("Indicator origin mismatch")
    if list(raw.time) != list(pd.date_range(raw.time.iloc[0], raw.time.iloc[-1]).strftime("%Y-%m-%d")):
        raise DataError("Indicator history is not contiguous")
    cap = raw.CapMrktCurUSD
    z = (cap - cap / raw.CapMVRVCur) / cap.expanding(min_periods=2).std(ddof=0)
    daily = pd.DataFrame({"date": raw.time, "price": raw.PriceUSD, "z": z}).iloc[1:].reset_index(drop=True)
    if not np.isfinite(daily[["price", "z"]]).all().all():
        raise DataError("Invalid derived Z-score")
    for model in config["models"]:
        if "window" in model:
            past = daily.z.shift(1).rolling(model["window"], min_periods=model["window"])
            daily[model["id"] + "_entry"] = past.quantile(model["low"])
            daily[model["id"] + "_exit"] = past.quantile(model["high"])
        else:
            daily[model["id"] + "_entry"] = model["entry"]
            daily[model["id"] + "_exit"] = model["exit"]
    return daily


def fresh(day, now):
    return str(day) == (utc(now).date() - timedelta(days=1)).isoformat()


def next_close(day, evaluated_at):
    # Row D marks D's UTC close at D+1 midnight. Historical next-row close is
    # D+2 midnight; publication delay may require an even later future close.
    label_due = pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=2)
    actual_due = pd.Timestamp(utc(evaluated_at)).normalize() + pd.Timedelta(days=1)
    return max(label_due, actual_due).isoformat()
