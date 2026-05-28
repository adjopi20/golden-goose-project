from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = ["timestamp", "price", "qty", "is_buyer_maker"]


def _to_utc_timestamp(value: pd.Timestamp | str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"Timestamp must be timezone-aware UTC: {value}")
    return ts.tz_convert("UTC")


def _normalize_trade_schema(df: pd.DataFrame) -> pd.DataFrame:
    missing_cols = sorted(set(REQUIRED_COLUMNS) - set(df.columns))
    if missing_cols:
        raise ValueError(f"Missing required trade columns: {missing_cols}")

    out = df[REQUIRED_COLUMNS].copy()
    out["timestamp"] = pd.to_numeric(out["timestamp"], errors="raise").astype("int64")
    out["price"] = pd.to_numeric(out["price"], errors="raise").astype("float64")
    out["qty"] = pd.to_numeric(out["qty"], errors="raise").astype("float64")
    out["is_buyer_maker"] = out["is_buyer_maker"].astype(bool)

    return out.sort_values(["timestamp", "price", "qty"], kind="mergesort").reset_index(drop=True)


def _load_parquet_window(input_path: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    df = pd.read_parquet(
        input_path,
        columns=REQUIRED_COLUMNS,
        filters=[
            ("timestamp", ">=", int(start_ms)),
            ("timestamp", "<", int(end_ms)),
        ],
    )
    return _normalize_trade_schema(df)


def _load_jsonl_window(input_path: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            row = json.loads(line)
            timestamp = int(row["T"])
            if not (start_ms <= timestamp < end_ms):
                continue

            rows.append(
                {
                    "timestamp": timestamp,
                    "price": float(row["p"]),
                    "qty": float(row["q"]),
                    "is_buyer_maker": bool(row["m"]),
                }
            )

    if not rows:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    return _normalize_trade_schema(pd.DataFrame(rows))


def load_trades_window(
    input_path: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """
    Load trades using canonical schema for a half-open UTC time window.

    Returns DataFrame with columns:
    - timestamp (int64, ms)
    - price (float64)
    - qty (float64)
    - is_buyer_maker (bool)
    """
    start_utc = _to_utc_timestamp(start)
    end_utc = _to_utc_timestamp(end)
    if end_utc <= start_utc:
        raise ValueError(f"Invalid window: end ({end_utc}) must be greater than start ({start_utc})")

    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)

    suffix = Path(input_path).suffix.lower()
    if suffix == ".parquet":
        return _load_parquet_window(input_path=input_path, start_ms=start_ms, end_ms=end_ms)
    if suffix == ".jsonl":
        return _load_jsonl_window(input_path=input_path, start_ms=start_ms, end_ms=end_ms)

    raise ValueError(f"Unsupported input extension: {suffix}. Supported: .parquet, .jsonl")


def load_trades(input_path: str) -> pd.DataFrame:
    """
    Load all trades using canonical schema.
    """
    suffix = Path(input_path).suffix.lower()

    if suffix == ".parquet":
        df = pd.read_parquet(input_path, columns=REQUIRED_COLUMNS)
        return _normalize_trade_schema(df)

    if suffix == ".jsonl":
        rows: list[dict[str, object]] = []
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows.append(
                    {
                        "timestamp": int(row["T"]),
                        "price": float(row["p"]),
                        "qty": float(row["q"]),
                        "is_buyer_maker": bool(row["m"]),
                    }
                )
        if not rows:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        return _normalize_trade_schema(pd.DataFrame(rows))

    raise ValueError(f"Unsupported input extension: {suffix}. Supported: .parquet, .jsonl")
