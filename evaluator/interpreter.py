from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd


def parse_session_date(value: str) -> dt.date:
    """
    Parse session date in supported formats:
    - YYYY-MM-DD
    - DDMMYYYY
    """
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return dt.datetime.strptime(value, "%Y-%m-%d").date()

    if len(value) == 8 and value.isdigit():
        return dt.datetime.strptime(value, "%d%m%Y").date()

    raise ValueError(f"Invalid session date format: {value}")


def _session_id_from_timestamp(
    timestamp: pd.Timestamp,
    session_start_hour: int = 13,
    session_start_minute: int = 30,
) -> str:
    """
    Convert UTC timestamp to auction session_id string using boundary offset.
    """
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    ts_utc = ts.tz_convert("UTC")

    offset = pd.Timedelta(hours=session_start_hour, minutes=session_start_minute)
    shifted = ts_utc - offset
    return shifted.date().isoformat()


def load_session_profile(profile_path: Path, session_id: str) -> dict[str, Any]:
    """
    Load one session profile by session_id from JSONL file.
    """
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile JSONL file not found: {profile_path}")

    with profile_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if str(row.get("session_id")) == session_id:
                return row

    raise ValueError(f"No profile row found for session_id='{session_id}' in {profile_path}")


def load_all_session_profiles(profile_path: Path) -> dict[str, dict[str, Any]]:
    """
    Load all session profiles from JSONL into a map keyed by session_id.
    """
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile JSONL file not found: {profile_path}")

    profiles: dict[str, dict[str, Any]] = {}
    with profile_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            session_id = str(row.get("session_id"))
            profiles[session_id] = row

    return profiles


def get_previous_session_profile(
    current_candle_timestamp: pd.Timestamp,
    profile_by_session_id: dict[str, dict[str, Any]],
    session_start_hour: int = 13,
    session_start_minute: int = 30,
) -> dict[str, Any]:
    """
    Resolve current candle session_id, then return the previous session profile.
    """
    current_session_id = _session_id_from_timestamp(
        timestamp=current_candle_timestamp,
        session_start_hour=session_start_hour,
        session_start_minute=session_start_minute,
    )

    current_date = dt.date.fromisoformat(current_session_id)
    previous_session_id = (current_date - dt.timedelta(days=1)).isoformat()

    if previous_session_id not in profile_by_session_id:
        raise ValueError(
            f"Previous session profile not found for session_id='{previous_session_id}' "
            f"(current session_id='{current_session_id}')"
        )

    return profile_by_session_id[previous_session_id]


def classify_balance_state(
    close: float,
    previous_val: float,
    previous_vah: float,
) -> tuple[str, str]:
    """
    Classify candle close location and balance state relative to previous value area.
    """
    if previous_val <= close <= previous_vah:
        return "inside_value", "balance"

    if close > previous_vah:
        return "above_vah", "imbalance"

    return "below_val", "imbalance"


def evaluate_candle_against_previous_value(
    candle: dict[str, Any],
    profile_by_session_id: dict[str, dict[str, Any]],
    session_start_hour: int = 13,
    session_start_minute: int = 30,
) -> dict[str, Any]:
    """
    Evaluate one completed candle against previous session value area only.
    """
    timestamp = pd.Timestamp(candle["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("candle['timestamp'] must be timezone-aware")
    timestamp = timestamp.tz_convert("UTC")

    close = float(candle["close"])

    prev_profile = get_previous_session_profile(
        current_candle_timestamp=timestamp,
        profile_by_session_id=profile_by_session_id,
        session_start_hour=session_start_hour,
        session_start_minute=session_start_minute,
    )

    previous_session_id = str(prev_profile["session_id"])
    previous_val = float(prev_profile["val"])
    previous_vah = float(prev_profile["vah"])
    previous_poc = float(prev_profile["poc_price"])

    location, balance_state = classify_balance_state(
        close=close,
        previous_val=previous_val,
        previous_vah=previous_vah,
    )

    return {
        "timestamp": timestamp.isoformat(),
        "close": close,
        "previous_session_id": previous_session_id,
        "previous_val": previous_val,
        "previous_vah": previous_vah,
        "previous_poc": previous_poc,
        "location": location,
        "balance_state": balance_state,
    }
