from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd


def parse_utc_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tz is None:
        raise ValueError(f"Timestamp is timezone-naive (UTC offset required): {value}")
    return ts.tz_convert("UTC")


def parse_iso8601_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, format="ISO8601")


def parse_session_date(value: str) -> dt.date:
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return dt.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid --session-date '{value}'. Expected YYYY-MM-DD.") from exc

    if len(value) == 8 and value.isdigit():
        try:
            return dt.datetime.strptime(value, "%d%m%Y").date()
        except ValueError as exc:
            raise ValueError(f"Invalid --session-date '{value}'. Expected DDMMYYYY.") from exc

    raise ValueError(f"Invalid --session-date '{value}'. Supported formats: YYYY-MM-DD or DDMMYYYY.")


def previous_session_date(session_date: dt.date) -> dt.date:
    return session_date - dt.timedelta(days=1)


def session_window_from_date(
    session_date: dt.date,
    session_start_hour: int,
    session_start_minute: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(
        dt.datetime(
            year=session_date.year,
            month=session_date.month,
            day=session_date.day,
            hour=session_start_hour,
            minute=session_start_minute,
            tzinfo=dt.timezone.utc,
        )
    )
    end = start + pd.Timedelta(days=1)
    return start, end


def compute_profile_overlay_window(
    profile_start: pd.Timestamp,
    profile_end: pd.Timestamp,
    width_ratio: float,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not (0 < width_ratio <= 1):
        raise ValueError(f"profile width ratio must be in (0, 1], got {width_ratio}")
    duration = profile_end - profile_start
    overlay_end = profile_start + (duration * width_ratio)
    return profile_start, overlay_end
