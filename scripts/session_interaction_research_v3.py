from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd


WIB_TZ = "Asia/Jakarta"
UTC_TZ = "UTC"
QUALITY_RANK_LOW_Q = 1 / 3
QUALITY_RANK_HIGH_Q = 2 / 3


def safe_divide(a, b):
    """Return a / b, with division-by-zero mapped to NaN."""
    if np.isscalar(a) and np.isscalar(b):
        if pd.isna(a) or pd.isna(b) or b == 0:
            return np.nan
        return a / b

    a_arr = np.asarray(a, dtype="float64")
    b_arr = np.asarray(b, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(
            a_arr,
            b_arr,
            out=np.full_like(a_arr, np.nan, dtype="float64"),
            where=(~np.isnan(a_arr)) & (~np.isnan(b_arr)) & (b_arr != 0),
        )
    return out


def pick_col(columns, candidates, required=True):
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    if required:
        raise ValueError(f"Could not find required column. Tried: {candidates}. Available: {list(columns)}")
    return None


def normalize_timestamp(series: pd.Series) -> pd.Series:
    """Normalize various numeric timestamp units to timezone-aware UTC."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, utc=True)

    x = pd.to_numeric(series, errors="coerce")
    if x.notna().sum() == 0:
        return pd.to_datetime(x, utc=True)

    max_abs = float(np.nanmax(np.abs(x.to_numpy(dtype="float64"))))
    if max_abs > 1e17:
        unit = "ns"
    elif max_abs > 1e14:
        unit = "us"
    elif max_abs > 1e11:
        unit = "ms"
    else:
        unit = "s"

    return pd.to_datetime(x, unit=unit, utc=True)


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        h_i = int(h)
        m_i = int(m)
    except Exception as exc:
        raise ValueError(f"Invalid HH:MM value: {value}") from exc
    if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
        raise ValueError(f"Invalid HH:MM value: {value}")
    return h_i, m_i


def format_ts_utc(ts) -> str | None:
    if pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC_TZ)
    else:
        ts = ts.tz_convert(UTC_TZ)
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_ts_wib(ts) -> str | None:
    if pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC_TZ)
    ts = ts.tz_convert(WIB_TZ)
    return ts.strftime("%Y-%m-%d %H:%M:%S WIB")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def make_excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_datetime64tz_dtype(series):
            out[col] = series.dt.tz_localize(None)
    return out


def most_common_with_pct(series: pd.Series) -> tuple[str | None, float]:
    value_counts = series.value_counts(dropna=False)
    if value_counts.empty:
        return None, np.nan
    top_label = value_counts.index[0]
    top_n = int(value_counts.iloc[0])
    return str(top_label), float(safe_divide(top_n, len(series)))


@dataclass(frozen=True)
class SessionWindow:
    name: str
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    start_offset_days: int
    end_offset_days: int

    def start_offset(self) -> pd.Timedelta:
        return pd.Timedelta(days=self.start_offset_days, hours=self.start_hour, minutes=self.start_minute)

    def end_offset(self) -> pd.Timedelta:
        return pd.Timedelta(days=self.end_offset_days, hours=self.end_hour, minutes=self.end_minute)


def validate_session_defaults(args) -> None:
    expected = {
        (args.asia_start, args.asia_end): ("23:00", "07:00"),
        (args.europe_start, args.europe_end): ("07:00", "15:00"),
        (args.us_start, args.us_end): ("15:00", "23:00"),
    }
    for actual, wanted in expected.items():
        parse_hhmm(actual[0])
        parse_hhmm(actual[1])
        parse_hhmm(wanted[0])
        parse_hhmm(wanted[1])


def build_session_windows(args) -> list[SessionWindow]:
    a_sh, a_sm = parse_hhmm(args.asia_start)
    a_eh, a_em = parse_hhmm(args.asia_end)
    e_sh, e_sm = parse_hhmm(args.europe_start)
    e_eh, e_em = parse_hhmm(args.europe_end)
    u_sh, u_sm = parse_hhmm(args.us_start)
    u_eh, u_em = parse_hhmm(args.us_end)

    windows = [
        SessionWindow("asia", a_sh, a_sm, a_eh, a_em, -1, 0),
        SessionWindow("europe", e_sh, e_sm, e_eh, e_em, 0, 0),
        SessionWindow("us", u_sh, u_sm, u_eh, u_em, 0, 0),
    ]

    anchor = pd.Timestamp("2026-01-02 00:00:00", tz=UTC_TZ)
    prev_end = None
    for win in windows:
        start = anchor + win.start_offset()
        end = anchor + win.end_offset()
        if end <= start:
            raise ValueError(f"Session {win.name} must end after start in anchored construction.")
        hours = (end - start).total_seconds() / 3600
        if abs(hours - 8.0) > 1e-9:
            raise ValueError(f"Session {win.name} must be 8 hours. Got {hours}.")
        if prev_end is not None and start != prev_end:
            raise ValueError("Sessions must be contiguous in the 24-hour framework.")
        prev_end = end
    return windows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Asia/Europe/US session interaction research V4.1 on raw Binance aggTrade parquet.")
    parser.add_argument("--input", required=True, help="Input raw aggTrade parquet path")
    parser.add_argument("--symbol", required=True, help="Symbol name, e.g. AVAXUSDC")
    parser.add_argument("--output-xlsx", "--output", dest="output_xlsx", required=True, help="Output Excel workbook path")
    parser.add_argument("--asia-start", default="23:00")
    parser.add_argument("--asia-end", default="07:00")
    parser.add_argument("--europe-start", default="07:00")
    parser.add_argument("--europe-end", default="15:00")
    parser.add_argument("--us-start", default="15:00")
    parser.add_argument("--us-end", default="23:00")
    parser.add_argument("--buffer-pct", type=float, default=0.005)
    parser.add_argument("--context-direction-tolerance", type=float, default=0.10)
    parser.add_argument("--quality-rank-lookback", type=int, default=90)
    parser.add_argument("--quality-rank-min-history", type=int, default=30)
    parser.add_argument("--min-trades-per-session", type=int, default=10)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser


def validate_args(args) -> None:
    if args.input is None or args.symbol is None or args.output_xlsx is None:
        raise ValueError("--input, --symbol, and --output-xlsx are required.")
    if args.min_trades_per_session <= 0:
        raise ValueError("--min-trades-per-session must be > 0")
    if args.buffer_pct < 0:
        raise ValueError("--buffer-pct must be >= 0")
    if args.context_direction_tolerance < 0:
        raise ValueError("--context-direction-tolerance must be >= 0")
    if args.quality_rank_lookback <= 0:
        raise ValueError("--quality-rank-lookback must be > 0")
    if args.quality_rank_min_history <= 0:
        raise ValueError("--quality-rank-min-history must be > 0")
    if args.quality_rank_min_history > args.quality_rank_lookback:
        raise ValueError("--quality-rank-min-history must be <= --quality-rank-lookback")
    validate_session_defaults(args)


def load_raw_aggtrades(path: Path) -> pd.DataFrame:
    print(f"Loading parquet: {path}")
    if not path.exists():
        raise FileNotFoundError(f"Input parquet not found: {path}")

    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    columns = pf.schema.names
    ts_col = pick_col(columns, ["timestamp", "event_timestamp", "T", "time", "transact_time"])
    price_col = pick_col(columns, ["price", "p"])
    qty_col = pick_col(columns, ["qty", "quantity", "q"])
    maker_col = pick_col(columns, ["is_buyer_maker", "m"], required=False)
    read_cols = [ts_col, price_col, qty_col]
    if maker_col:
        read_cols.append(maker_col)

    df = pd.read_parquet(path, columns=read_cols)
    df = df.rename(columns={ts_col: "timestamp", price_col: "price", qty_col: "qty"})
    if maker_col:
        df = df.rename(columns={maker_col: "is_buyer_maker"})
    else:
        df["is_buyer_maker"] = np.nan

    df["timestamp"] = normalize_timestamp(df["timestamp"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("float64")
    df = df.dropna(subset=["timestamp", "price", "qty"]).copy()
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["notional"] = df["price"] * df["qty"]
    if df["is_buyer_maker"].notna().any():
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        df["aggressor_side"] = np.where(df["is_buyer_maker"], -1, 1)
    else:
        df["aggressor_side"] = np.nan
    if df.empty:
        raise ValueError("Input parquet is empty after cleaning required fields.")
    print(f"Loaded rows: {len(df):,}")
    print(f"Timestamp range UTC: {df['timestamp'].min()} -> {df['timestamp'].max()}")
    return df


def add_session_day_wib(df: pd.DataFrame) -> pd.DataFrame:
    ts = df["timestamp"]
    hour_minute = ts.dt.hour * 60 + ts.dt.minute
    utc_date = ts.dt.floor("D")
    anchor_utc_date = utc_date.where(hour_minute >= 7 * 60, utc_date)
    anchor_utc_date = anchor_utc_date.where(hour_minute < 23 * 60, utc_date + pd.Timedelta(days=1))
    df = df.copy()
    df["anchor_utc_date"] = anchor_utc_date
    df["session_day_wib"] = anchor_utc_date.dt.tz_convert(WIB_TZ).dt.strftime("%Y-%m-%d")
    return df


def build_daily_windows(anchor_utc_date: pd.Timestamp, windows: list[SessionWindow]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {win.name: (anchor_utc_date + win.start_offset(), anchor_utc_date + win.end_offset()) for win in windows}


def aggregate_session_metrics(session_df: pd.DataFrame, prefix: str) -> dict:
    session_df = session_df.sort_values("timestamp")
    open_ = float(session_df["price"].iloc[0])
    high_ = float(session_df["price"].max())
    low_ = float(session_df["price"].min())
    close_ = float(session_df["price"].iloc[-1])
    range_ = high_ - low_
    body = close_ - open_
    abs_body = abs(body)
    return {
        f"{prefix}_open": open_,
        f"{prefix}_high": high_,
        f"{prefix}_low": low_,
        f"{prefix}_close": close_,
        f"{prefix}_range": range_,
        f"{prefix}_range_pct": safe_divide(range_, open_),
        f"{prefix}_body": body,
        f"{prefix}_abs_body": abs_body,
        f"{prefix}_body_pct": safe_divide(body, open_),
        f"{prefix}_body_to_range": safe_divide(abs_body, range_),
        f"{prefix}_efficiency": safe_divide(body, range_),
        f"{prefix}_close_position": safe_divide(close_ - low_, range_),
        f"{prefix}_volume": float(session_df["qty"].sum()),
        f"{prefix}_notional": float(session_df["notional"].sum()),
        f"{prefix}_trade_count": int(len(session_df)),
        f"{prefix}_first_trade_timestamp": session_df["timestamp"].iloc[0],
        f"{prefix}_last_trade_timestamp": session_df["timestamp"].iloc[-1],
    }


def first_break_timestamp(session_df: pd.DataFrame, high_level: float, low_level: float, high_cmp: Literal["gt", "gte"], low_cmp: Literal["lt", "lte"]) -> tuple[pd.Timestamp | None, pd.Timestamp | None, str]:
    prices = session_df["price"]
    high_hits = session_df.loc[prices > high_level if high_cmp == "gt" else prices >= high_level, "timestamp"]
    low_hits = session_df.loc[prices < low_level if low_cmp == "lt" else prices <= low_level, "timestamp"]
    high_ts: pd.Timestamp | None = high_hits.iloc[0] if len(high_hits) else None
    low_ts: pd.Timestamp | None = low_hits.iloc[0] if len(low_hits) else None
    if high_ts is None and low_ts is None:
        sequence = "NO_BREAK"
    elif high_ts is not None and low_ts is None:
        sequence = "HIGH_ONLY"
    elif high_ts is None and low_ts is not None:
        sequence = "LOW_ONLY"
    elif high_ts < low_ts:
        sequence = "HIGH_THEN_LOW"
    elif low_ts < high_ts:
        sequence = "LOW_THEN_HIGH"
    else:
        sequence = "BOTH_SAME_TIME"
    return high_ts, low_ts, sequence


def build_daily_sessions(df: pd.DataFrame, windows: list[SessionWindow], min_trades_per_session: int) -> tuple[pd.DataFrame, dict]:
    print("Building complete Asia/Europe/US daily sessions...")
    df = add_session_day_wib(df)
    dataset_min_ts = df["timestamp"].min()
    dataset_max_ts = df["timestamp"].max()
    records: list[dict] = []
    candidate_days = pd.DatetimeIndex(sorted(df["anchor_utc_date"].dropna().unique()))
    dropped_edge_coverage = 0
    dropped_missing_session = 0
    dropped_min_trades = 0
    for anchor_utc_date in candidate_days:
        windows_map = build_daily_windows(anchor_utc_date, windows)
        asia_start, _ = windows_map["asia"]
        _, us_end = windows_map["us"]
        if dataset_min_ts > asia_start or dataset_max_ts < us_end:
            dropped_edge_coverage += 1
            continue
        day_record = {"session_day_wib": anchor_utc_date.tz_convert(WIB_TZ).strftime("%Y-%m-%d"), "anchor_utc_date": anchor_utc_date}
        for name, (start, end) in windows_map.items():
            day_record[f"{name}_start_utc"] = start
            day_record[f"{name}_end_utc"] = end

        missing_session = False
        too_few_trades = False
        for name, (start, end) in windows_map.items():
            session_df = df.loc[(df["timestamp"] >= start) & (df["timestamp"] < end)].copy()
            if session_df.empty:
                missing_session = True
                break
            if len(session_df) < min_trades_per_session:
                too_few_trades = True
                break
            day_record.update(aggregate_session_metrics(session_df, name))
            if name == "europe":
                high_ts, low_ts, seq = first_break_timestamp(session_df, day_record["asia_high"], day_record["asia_low"], "gt", "lt")
                day_record["europe_first_break_asia_high_timestamp"] = high_ts
                day_record["europe_first_break_asia_low_timestamp"] = low_ts
                day_record["europe_first_break_sequence"] = seq
            elif name == "us":
                pre_us_high = max(day_record["asia_high"], day_record["europe_high"])
                pre_us_low = min(day_record["asia_low"], day_record["europe_low"])
                high_ts, low_ts, seq = first_break_timestamp(session_df, pre_us_high, pre_us_low, "gt", "lt")
                day_record["us_first_break_pre_us_high_timestamp"] = high_ts
                day_record["us_first_break_pre_us_low_timestamp"] = low_ts
                day_record["us_first_break_sequence"] = seq
        if too_few_trades:
            dropped_min_trades += 1
            continue
        if missing_session:
            dropped_missing_session += 1
            continue
        records.append(day_record)

    daily = pd.DataFrame(records)
    if daily.empty:
        raise ValueError("No complete session days found after coverage and min-trade filtering.")
    for prefix in ["asia", "europe", "us"]:
        daily[f"{prefix}_start_wib"] = daily[f"{prefix}_start_utc"].map(format_ts_wib)
        daily[f"{prefix}_end_wib"] = daily[f"{prefix}_end_utc"].map(format_ts_wib)
        daily[f"{prefix}_start_utc_str"] = daily[f"{prefix}_start_utc"].map(format_ts_utc)
        daily[f"{prefix}_end_utc_str"] = daily[f"{prefix}_end_utc"].map(format_ts_utc)
    for col in ["europe_first_break_asia_high_timestamp", "europe_first_break_asia_low_timestamp", "us_first_break_pre_us_high_timestamp", "us_first_break_pre_us_low_timestamp"]:
        daily[f"{col}_utc"] = daily[col].map(format_ts_utc)
        daily[f"{col}_wib"] = daily[col].map(format_ts_wib)
    stats = {
        "dataset_min_timestamp_utc": format_ts_utc(dataset_min_ts),
        "dataset_max_timestamp_utc": format_ts_utc(dataset_max_ts),
        "candidate_days": int(len(candidate_days)),
        "complete_days": int(len(daily)),
        "dropped_edge_coverage": int(dropped_edge_coverage),
        "dropped_missing_session": int(dropped_missing_session),
        "dropped_min_trades": int(dropped_min_trades),
    }
    print(f"Complete session days retained: {len(daily):,}")
    return daily, stats


def classify_context_direction(efficiency: float, tolerance: float) -> str:
    if pd.isna(efficiency):
        return "NO_DIRECTION"
    if efficiency > tolerance:
        return "UP"
    if efficiency < -tolerance:
        return "DOWN"
    return "NO_DIRECTION"


def classify_structure_state(session_high: float, session_low: float, upper_buffer: float, lower_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_high, session_low, upper_buffer, lower_buffer]):
        return "REFERENCE_UNAVAILABLE"
    high_expanded = session_high > upper_buffer
    low_expanded = session_low < lower_buffer
    if high_expanded and not low_expanded:
        return "EXPANSION_UP"
    if low_expanded and not high_expanded:
        return "EXPANSION_DOWN"
    if high_expanded and low_expanded:
        return "TWO_SIDED_EXPANSION"
    return "CONTAINED_RANGE"


def combine_session_context(structure_state: str, context_direction: str | float | None) -> str | None:
    if structure_state in [None, "REFERENCE_UNAVAILABLE"] or pd.isna(context_direction):
        return None
    return f"{structure_state}_{context_direction}"


def classify_high_buffer_tag(session_high: float, session_close: float, ref_high: float, upper_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_high, session_close, ref_high, upper_buffer]):
        return "REFERENCE_UNAVAILABLE"
    if session_high <= ref_high:
        return "HIGH_NOT_TOUCHED"
    if session_high > ref_high and session_high <= upper_buffer and session_close <= ref_high:
        return "HIGH_SWEEP_WITHIN_BUFFER_REJECTED"
    if session_high > ref_high and session_high <= upper_buffer and session_close > ref_high:
        return "HIGH_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if session_high > upper_buffer and session_close <= upper_buffer:
        return "HIGH_BREAK_BEYOND_BUFFER_RECOVERED"
    return "HIGH_BREAK_BEYOND_BUFFER_ACCEPTED"


def classify_low_buffer_tag(session_low: float, session_close: float, ref_low: float, lower_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_low, session_close, ref_low, lower_buffer]):
        return "REFERENCE_UNAVAILABLE"
    if session_low >= ref_low:
        return "LOW_NOT_TOUCHED"
    if session_low < ref_low and session_low >= lower_buffer and session_close >= ref_low:
        return "LOW_SWEEP_WITHIN_BUFFER_REJECTED"
    if session_low < ref_low and session_low >= lower_buffer and session_close < ref_low:
        return "LOW_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if session_low < lower_buffer and session_close >= lower_buffer:
        return "LOW_BREAK_BEYOND_BUFFER_RECOVERED"
    return "LOW_BREAK_BEYOND_BUFFER_ACCEPTED"


def add_overlap_diagnostics(daily: pd.DataFrame) -> pd.DataFrame:
    daily = daily.copy()
    overlap = np.maximum(0.0, np.minimum(daily["asia_high"], daily["europe_high"]) - np.maximum(daily["asia_low"], daily["europe_low"]))
    union_range = np.maximum(daily["asia_high"], daily["europe_high"]) - np.minimum(daily["asia_low"], daily["europe_low"])
    daily["overlap"] = overlap
    daily["union_range"] = union_range
    daily["overlap_of_union"] = safe_divide(overlap, union_range)
    daily["overlap_of_asia"] = safe_divide(overlap, daily["asia_range"])
    daily["overlap_of_europe"] = safe_divide(overlap, daily["europe_range"])
    return daily


def add_asia_europe_contexts(daily: pd.DataFrame, args) -> tuple[pd.DataFrame, dict]:
    daily = daily.copy().sort_values("anchor_utc_date").reset_index(drop=True)

    prev_anchor = daily["anchor_utc_date"].shift(1)
    expected_prev_anchor = daily["anchor_utc_date"] - pd.Timedelta(days=1)
    daily["asia_reference_available"] = prev_anchor.eq(expected_prev_anchor)
    daily["asia_reference_us_high"] = daily["us_high"].shift(1).where(daily["asia_reference_available"])
    daily["asia_reference_us_low"] = daily["us_low"].shift(1).where(daily["asia_reference_available"])
    daily["asia_reference_upper_buffer"] = daily["asia_reference_us_high"] * (1 + args.buffer_pct)
    daily["asia_reference_lower_buffer"] = daily["asia_reference_us_low"] * (1 - args.buffer_pct)

    daily["asia_structure_state"] = [
        classify_structure_state(a, b, c, d)
        for a, b, c, d in zip(daily["asia_high"], daily["asia_low"], daily["asia_reference_upper_buffer"], daily["asia_reference_lower_buffer"])
    ]
    daily.loc[~daily["asia_reference_available"], "asia_structure_state"] = "REFERENCE_UNAVAILABLE"
    daily["asia_context_direction"] = daily["asia_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance))
    daily.loc[~daily["asia_reference_available"], "asia_context_direction"] = pd.NA
    daily["asia_session_context"] = [combine_session_context(a, b) for a, b in zip(daily["asia_structure_state"], daily["asia_context_direction"])]
    daily["asia_high_buffer_tag"] = [
        classify_high_buffer_tag(a, b, c, d)
        for a, b, c, d in zip(daily["asia_high"], daily["asia_close"], daily["asia_reference_us_high"], daily["asia_reference_upper_buffer"])
    ]
    daily["asia_low_buffer_tag"] = [
        classify_low_buffer_tag(a, b, c, d)
        for a, b, c, d in zip(daily["asia_low"], daily["asia_close"], daily["asia_reference_us_low"], daily["asia_reference_lower_buffer"])
    ]

    daily["europe_reference_asia_high"] = daily["asia_high"]
    daily["europe_reference_asia_low"] = daily["asia_low"]
    daily["europe_reference_upper_buffer"] = daily["europe_reference_asia_high"] * (1 + args.buffer_pct)
    daily["europe_reference_lower_buffer"] = daily["europe_reference_asia_low"] * (1 - args.buffer_pct)
    daily["europe_structure_state"] = [
        classify_structure_state(a, b, c, d)
        for a, b, c, d in zip(daily["europe_high"], daily["europe_low"], daily["europe_reference_upper_buffer"], daily["europe_reference_lower_buffer"])
    ]
    daily["europe_context_direction"] = daily["europe_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance))
    daily["europe_session_context"] = [combine_session_context(a, b) for a, b in zip(daily["europe_structure_state"], daily["europe_context_direction"])]
    daily["europe_high_buffer_tag"] = [
        classify_high_buffer_tag(a, b, c, d)
        for a, b, c, d in zip(daily["europe_high"], daily["europe_close"], daily["europe_reference_asia_high"], daily["europe_reference_upper_buffer"])
    ]
    daily["europe_low_buffer_tag"] = [
        classify_low_buffer_tag(a, b, c, d)
        for a, b, c, d in zip(daily["europe_low"], daily["europe_close"], daily["europe_reference_asia_low"], daily["europe_reference_lower_buffer"])
    ]

    daily["asia_europe_session_context_combo"] = np.where(
        daily["asia_session_context"].notna() & daily["europe_session_context"].notna(),
        "ASIA_" + daily["asia_session_context"].astype(str) + "__EUROPE_" + daily["europe_session_context"].astype(str),
        pd.NA,
    )

    return daily, {
        "rows_without_previous_us_reference": int((~daily["asia_reference_available"]).sum()),
        "asia_unclassified_count": int((daily["asia_reference_available"] & daily["asia_session_context"].isna()).sum()),
        "europe_unclassified_count": int(daily["europe_session_context"].isna().sum()),
    }


def rank_value_against_history(current_value: float, history: list[float], low_label: str, mid_label: str, high_label: str, min_history: int) -> str:
    if pd.isna(current_value) or len(history) < min_history:
        return "INSUFFICIENT_HISTORY"
    low_cut = float(pd.Series(history, dtype="float64").quantile(QUALITY_RANK_LOW_Q))
    high_cut = float(pd.Series(history, dtype="float64").quantile(QUALITY_RANK_HIGH_Q))
    if current_value <= low_cut:
        return low_label
    if current_value >= high_cut:
        return high_label
    return mid_label


def apply_prior_rolling_ranks(daily: pd.DataFrame, args) -> tuple[pd.DataFrame, dict]:
    daily = daily.copy().sort_values("anchor_utc_date").reset_index(drop=True)
    lookback = args.quality_rank_lookback
    min_history = args.quality_rank_min_history

    daily["asia_range_rank"] = pd.NA
    daily["europe_range_rank"] = pd.NA
    daily["asia_direction_strength_rank"] = pd.NA
    daily["europe_direction_strength_rank"] = pd.NA
    daily["asia_expansion_strength_rank"] = pd.NA
    daily["europe_expansion_strength_rank"] = pd.NA
    daily["asia_reference_overlap_rank"] = pd.NA
    daily["europe_reference_overlap_rank"] = pd.NA

    excluded_counts = {
        "asia_range_rank_insufficient_history_count": 0,
        "europe_range_rank_insufficient_history_count": 0,
        "asia_direction_strength_rank_not_applicable_count": 0,
        "europe_direction_strength_rank_not_applicable_count": 0,
        "asia_direction_strength_rank_insufficient_history_count": 0,
        "europe_direction_strength_rank_insufficient_history_count": 0,
        "asia_expansion_strength_rank_not_applicable_count": 0,
        "europe_expansion_strength_rank_not_applicable_count": 0,
        "asia_expansion_strength_rank_insufficient_history_count": 0,
        "europe_expansion_strength_rank_insufficient_history_count": 0,
        "asia_expansion_strength_rank_reference_unavailable_count": 0,
        "asia_reference_overlap_rank_reference_unavailable_count": 0,
        "asia_reference_overlap_rank_insufficient_history_count": 0,
        "europe_reference_overlap_rank_insufficient_history_count": 0,
    }

    asia_range_hist: list[float] = []
    europe_range_hist: list[float] = []
    asia_dir_hist: list[float] = []
    europe_dir_hist: list[float] = []
    asia_exp_hist: dict[str, list[float]] = {"EXPANSION_UP": [], "EXPANSION_DOWN": [], "TWO_SIDED_EXPANSION": []}
    europe_exp_hist: dict[str, list[float]] = {"EXPANSION_UP": [], "EXPANSION_DOWN": [], "TWO_SIDED_EXPANSION": []}
    asia_overlap_hist: list[float] = []
    europe_overlap_hist: list[float] = []

    for idx, row in daily.iterrows():
        # Range rank
        daily.at[idx, "asia_range_rank"] = rank_value_against_history(
            row["asia_range_pct"], asia_range_hist[-lookback:], "LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE", min_history
        )
        daily.at[idx, "europe_range_rank"] = rank_value_against_history(
            row["europe_range_pct"], europe_range_hist[-lookback:], "LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE", min_history
        )
        if daily.at[idx, "asia_range_rank"] == "INSUFFICIENT_HISTORY":
            excluded_counts["asia_range_rank_insufficient_history_count"] += 1
        if daily.at[idx, "europe_range_rank"] == "INSUFFICIENT_HISTORY":
            excluded_counts["europe_range_rank_insufficient_history_count"] += 1

        # Direction strength rank
        asia_direction_strength = abs(row["asia_efficiency"]) if not pd.isna(row["asia_efficiency"]) else np.nan
        europe_direction_strength = abs(row["europe_efficiency"]) if not pd.isna(row["europe_efficiency"]) else np.nan
        if row["asia_context_direction"] == "NO_DIRECTION" or pd.isna(row["asia_context_direction"]):
            daily.at[idx, "asia_direction_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["asia_direction_strength_rank_not_applicable_count"] += 1
        else:
            daily.at[idx, "asia_direction_strength_rank"] = rank_value_against_history(
                asia_direction_strength,
                asia_dir_hist[-lookback:],
                "LOW_DIRECTION_STRENGTH",
                "MEDIUM_DIRECTION_STRENGTH",
                "HIGH_DIRECTION_STRENGTH",
                min_history,
            )
            if daily.at[idx, "asia_direction_strength_rank"] == "INSUFFICIENT_HISTORY":
                excluded_counts["asia_direction_strength_rank_insufficient_history_count"] += 1
        if row["europe_context_direction"] == "NO_DIRECTION" or pd.isna(row["europe_context_direction"]):
            daily.at[idx, "europe_direction_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["europe_direction_strength_rank_not_applicable_count"] += 1
        else:
            daily.at[idx, "europe_direction_strength_rank"] = rank_value_against_history(
                europe_direction_strength,
                europe_dir_hist[-lookback:],
                "LOW_DIRECTION_STRENGTH",
                "MEDIUM_DIRECTION_STRENGTH",
                "HIGH_DIRECTION_STRENGTH",
                min_history,
            )
            if daily.at[idx, "europe_direction_strength_rank"] == "INSUFFICIENT_HISTORY":
                excluded_counts["europe_direction_strength_rank_insufficient_history_count"] += 1

        # Expansion strength rank
        if not row["asia_reference_available"]:
            daily.at[idx, "asia_expansion_strength_rank"] = "REFERENCE_UNAVAILABLE"
            excluded_counts["asia_expansion_strength_rank_reference_unavailable_count"] += 1
        elif row["asia_structure_state"] == "CONTAINED_RANGE":
            daily.at[idx, "asia_expansion_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["asia_expansion_strength_rank_not_applicable_count"] += 1
        else:
            asia_up = max(0.0, safe_divide(row["asia_high"], row["asia_reference_upper_buffer"]) - 1) if not pd.isna(row["asia_reference_upper_buffer"]) else np.nan
            asia_down = max(0.0, safe_divide(row["asia_reference_lower_buffer"], row["asia_low"]) - 1) if not pd.isna(row["asia_reference_lower_buffer"]) else np.nan
            asia_excess = asia_up if row["asia_structure_state"] == "EXPANSION_UP" else asia_down if row["asia_structure_state"] == "EXPANSION_DOWN" else asia_up + asia_down
            hist = asia_exp_hist[row["asia_structure_state"]][-lookback:]
            daily.at[idx, "asia_expansion_strength_rank"] = rank_value_against_history(
                asia_excess, hist, "LOW_EXPANSION_STRENGTH", "MEDIUM_EXPANSION_STRENGTH", "HIGH_EXPANSION_STRENGTH", min_history
            )
            if daily.at[idx, "asia_expansion_strength_rank"] == "INSUFFICIENT_HISTORY":
                excluded_counts["asia_expansion_strength_rank_insufficient_history_count"] += 1

        if row["europe_structure_state"] == "CONTAINED_RANGE":
            daily.at[idx, "europe_expansion_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["europe_expansion_strength_rank_not_applicable_count"] += 1
        else:
            eu_up = max(0.0, safe_divide(row["europe_high"], row["europe_reference_upper_buffer"]) - 1)
            eu_down = max(0.0, safe_divide(row["europe_reference_lower_buffer"], row["europe_low"]) - 1)
            eu_excess = eu_up if row["europe_structure_state"] == "EXPANSION_UP" else eu_down if row["europe_structure_state"] == "EXPANSION_DOWN" else eu_up + eu_down
            hist = europe_exp_hist[row["europe_structure_state"]][-lookback:]
            daily.at[idx, "europe_expansion_strength_rank"] = rank_value_against_history(
                eu_excess, hist, "LOW_EXPANSION_STRENGTH", "MEDIUM_EXPANSION_STRENGTH", "HIGH_EXPANSION_STRENGTH", min_history
            )
            if daily.at[idx, "europe_expansion_strength_rank"] == "INSUFFICIENT_HISTORY":
                excluded_counts["europe_expansion_strength_rank_insufficient_history_count"] += 1

        # Reference overlap rank
        if not row["asia_reference_available"]:
            daily.at[idx, "asia_reference_overlap_rank"] = "REFERENCE_UNAVAILABLE"
            excluded_counts["asia_reference_overlap_rank_reference_unavailable_count"] += 1
        else:
            asia_overlap = max(0.0, min(row["asia_high"], row["asia_reference_us_high"]) - max(row["asia_low"], row["asia_reference_us_low"]))
            asia_union = max(row["asia_high"], row["asia_reference_us_high"]) - min(row["asia_low"], row["asia_reference_us_low"])
            asia_overlap_value = safe_divide(asia_overlap, asia_union)
            daily.at[idx, "asia_reference_overlap_rank"] = rank_value_against_history(
                asia_overlap_value, asia_overlap_hist[-lookback:], "LOW_OVERLAP", "MEDIUM_OVERLAP", "HIGH_OVERLAP", min_history
            )
            if daily.at[idx, "asia_reference_overlap_rank"] == "INSUFFICIENT_HISTORY":
                excluded_counts["asia_reference_overlap_rank_insufficient_history_count"] += 1

        europe_overlap = max(0.0, min(row["europe_high"], row["europe_reference_asia_high"]) - max(row["europe_low"], row["europe_reference_asia_low"]))
        europe_union = max(row["europe_high"], row["europe_reference_asia_high"]) - min(row["europe_low"], row["europe_reference_asia_low"])
        europe_overlap_value = safe_divide(europe_overlap, europe_union)
        daily.at[idx, "europe_reference_overlap_rank"] = rank_value_against_history(
            europe_overlap_value, europe_overlap_hist[-lookback:], "LOW_OVERLAP", "MEDIUM_OVERLAP", "HIGH_OVERLAP", min_history
        )
        if daily.at[idx, "europe_reference_overlap_rank"] == "INSUFFICIENT_HISTORY":
            excluded_counts["europe_reference_overlap_rank_insufficient_history_count"] += 1

        # Update history pools after ranking current row
        if not pd.isna(row["asia_range_pct"]):
            asia_range_hist.append(float(row["asia_range_pct"]))
        if not pd.isna(row["europe_range_pct"]):
            europe_range_hist.append(float(row["europe_range_pct"]))
        if row["asia_context_direction"] in ["UP", "DOWN"] and not pd.isna(asia_direction_strength):
            asia_dir_hist.append(float(asia_direction_strength))
        if row["europe_context_direction"] in ["UP", "DOWN"] and not pd.isna(europe_direction_strength):
            europe_dir_hist.append(float(europe_direction_strength))
        if row["asia_reference_available"] and row["asia_structure_state"] in asia_exp_hist:
            asia_up = max(0.0, safe_divide(row["asia_high"], row["asia_reference_upper_buffer"]) - 1) if not pd.isna(row["asia_reference_upper_buffer"]) else np.nan
            asia_down = max(0.0, safe_divide(row["asia_reference_lower_buffer"], row["asia_low"]) - 1) if not pd.isna(row["asia_reference_lower_buffer"]) else np.nan
            if row["asia_structure_state"] == "EXPANSION_UP":
                asia_exp_hist[row["asia_structure_state"]].append(float(asia_up))
            elif row["asia_structure_state"] == "EXPANSION_DOWN":
                asia_exp_hist[row["asia_structure_state"]].append(float(asia_down))
            else:
                asia_exp_hist[row["asia_structure_state"]].append(float(asia_up + asia_down))
        if row["europe_structure_state"] in europe_exp_hist:
            eu_up = max(0.0, safe_divide(row["europe_high"], row["europe_reference_upper_buffer"]) - 1)
            eu_down = max(0.0, safe_divide(row["europe_reference_lower_buffer"], row["europe_low"]) - 1)
            if row["europe_structure_state"] == "EXPANSION_UP":
                europe_exp_hist[row["europe_structure_state"]].append(float(eu_up))
            elif row["europe_structure_state"] == "EXPANSION_DOWN":
                europe_exp_hist[row["europe_structure_state"]].append(float(eu_down))
            else:
                europe_exp_hist[row["europe_structure_state"]].append(float(eu_up + eu_down))
        if row["asia_reference_available"]:
            asia_overlap = max(0.0, min(row["asia_high"], row["asia_reference_us_high"]) - max(row["asia_low"], row["asia_reference_us_low"]))
            asia_union = max(row["asia_high"], row["asia_reference_us_high"]) - min(row["asia_low"], row["asia_reference_us_low"])
            asia_overlap_value = safe_divide(asia_overlap, asia_union)
            if not pd.isna(asia_overlap_value):
                asia_overlap_hist.append(float(asia_overlap_value))
        if not pd.isna(europe_overlap_value):
            europe_overlap_hist.append(float(europe_overlap_value))

    return daily, excluded_counts


def classify_us_high_state(us_high: float, outer_high: float, inner_high: float) -> str:
    if pd.isna(us_high) or pd.isna(outer_high) or pd.isna(inner_high):
        return "HIGH_UNCLASSIFIED"
    if us_high > outer_high:
        return "HIGH_OUTER_EXPANSION"
    if us_high > inner_high:
        return "HIGH_INTERNAL_LIQUIDITY_TAKE"
    return "HIGH_CONTAINED"


def classify_us_low_state(us_low: float, outer_low: float, inner_low: float) -> str:
    if pd.isna(us_low) or pd.isna(outer_low) or pd.isna(inner_low):
        return "LOW_UNCLASSIFIED"
    if us_low < outer_low:
        return "LOW_OUTER_EXPANSION"
    if us_low < inner_low:
        return "LOW_INTERNAL_LIQUIDITY_TAKE"
    return "LOW_CONTAINED"


def classify_us_directional_outcome_v3(high_state: str, low_state: str) -> str:
    if high_state == "HIGH_UNCLASSIFIED" or low_state == "LOW_UNCLASSIFIED":
        return "US_UNCLASSIFIED"
    if high_state == "HIGH_OUTER_EXPANSION" and low_state == "LOW_OUTER_EXPANSION":
        return "US_BOTH_SIDE_EXPANSION"
    if high_state == "HIGH_OUTER_EXPANSION":
        return "US_DIRECTIONAL_UP"
    if low_state == "LOW_OUTER_EXPANSION":
        return "US_DIRECTIONAL_DOWN"
    if high_state == "HIGH_CONTAINED" and low_state == "LOW_CONTAINED":
        return "US_TRUE_CONTRACTION"
    if high_state == "HIGH_INTERNAL_LIQUIDITY_TAKE" or low_state == "LOW_INTERNAL_LIQUIDITY_TAKE":
        return "US_INTERNAL_LIQUIDITY_TAKE"
    return "US_UNCLASSIFIED"


def classify_us_structure_3x3_label(high_state: str, low_state: str) -> str:
    mapping = {
        ("HIGH_OUTER_EXPANSION", "LOW_OUTER_EXPANSION"): "US_OUTER_EXPANSION_BOTH",
        ("HIGH_OUTER_EXPANSION", "LOW_INTERNAL_LIQUIDITY_TAKE"): "US_UP_EXPANSION_WITH_LOW_INTERNAL_TAKE",
        ("HIGH_OUTER_EXPANSION", "LOW_CONTAINED"): "US_CLEAN_UPSIDE_REACH",
        ("HIGH_INTERNAL_LIQUIDITY_TAKE", "LOW_OUTER_EXPANSION"): "US_DOWN_EXPANSION_WITH_HIGH_INTERNAL_TAKE",
        ("HIGH_INTERNAL_LIQUIDITY_TAKE", "LOW_INTERNAL_LIQUIDITY_TAKE"): "US_INTERNAL_RANGE_TAKE_BOTH",
        ("HIGH_INTERNAL_LIQUIDITY_TAKE", "LOW_CONTAINED"): "US_UPPER_INTERNAL_TAKE_ONLY",
        ("HIGH_CONTAINED", "LOW_OUTER_EXPANSION"): "US_CLEAN_DOWNSIDE_REACH",
        ("HIGH_CONTAINED", "LOW_INTERNAL_LIQUIDITY_TAKE"): "US_LOWER_INTERNAL_TAKE_ONLY",
        ("HIGH_CONTAINED", "LOW_CONTAINED"): "US_TRUE_CONTRACTION",
    }
    return mapping.get((high_state, low_state), "US_STRUCTURE_UNCLASSIFIED")


def classify_close_location_v3(us_close: float, outer_high: float, inner_high: float, inner_low: float, outer_low: float) -> str:
    if any(pd.isna(x) for x in [us_close, outer_high, inner_high, inner_low, outer_low]):
        return "CLOSE_UNCLASSIFIED"
    if us_close > outer_high:
        return "CLOSE_ABOVE_OUTER_HIGH"
    if us_close > inner_high:
        return "CLOSE_UPPER_INTERNAL_AREA"
    if us_close >= inner_low:
        return "CLOSE_TRUE_OVERLAP_AREA"
    if us_close >= outer_low:
        return "CLOSE_LOWER_INTERNAL_AREA"
    return "CLOSE_BELOW_OUTER_LOW"


def classify_position_vs_structure(price: float, outer_high: float, inner_high: float, inner_low: float, outer_low: float, prefix: str) -> str:
    if any(pd.isna(x) for x in [price, outer_high, inner_high, inner_low, outer_low]):
        return f"{prefix}_UNCLASSIFIED"
    if price > outer_high:
        return f"{prefix}_ABOVE_OUTER_HIGH"
    if price > inner_high:
        return f"{prefix}_UPPER_INTERNAL_AREA"
    if price >= inner_low:
        return f"{prefix}_TRUE_OVERLAP_AREA"
    if price >= outer_low:
        return f"{prefix}_LOWER_INTERNAL_AREA"
    return f"{prefix}_BELOW_OUTER_LOW"


def classify_outer_high_buffer_tag(us_high: float, us_close: float, outer_high: float, outer_high_buffer: float) -> str:
    if any(pd.isna(x) for x in [us_high, us_close, outer_high, outer_high_buffer]):
        return "HIGH_BUFFER_UNCLASSIFIED"
    if us_high <= outer_high:
        return "HIGH_NOT_TOUCHED"
    if us_high <= outer_high_buffer and us_close <= outer_high:
        return "HIGH_TOUCH_WITHIN_BUFFER_REJECTED"
    if us_high <= outer_high_buffer and us_close > outer_high:
        return "HIGH_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if us_high > outer_high_buffer and us_close <= outer_high_buffer:
        return "HIGH_BREAK_BEYOND_BUFFER_RECOVERED"
    return "HIGH_BREAK_BEYOND_BUFFER_ACCEPTED"


def classify_outer_low_buffer_tag(us_low: float, us_close: float, outer_low: float, outer_low_buffer: float) -> str:
    if any(pd.isna(x) for x in [us_low, us_close, outer_low, outer_low_buffer]):
        return "LOW_BUFFER_UNCLASSIFIED"
    if us_low >= outer_low:
        return "LOW_NOT_TOUCHED"
    if us_low >= outer_low_buffer and us_close >= outer_low:
        return "LOW_TOUCH_WITHIN_BUFFER_REJECTED"
    if us_low >= outer_low_buffer and us_close < outer_low:
        return "LOW_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if us_low < outer_low_buffer and us_close >= outer_low_buffer:
        return "LOW_BREAK_BEYOND_BUFFER_RECOVERED"
    return "LOW_BREAK_BEYOND_BUFFER_ACCEPTED"


def add_us_outcomes_v3(daily: pd.DataFrame, args) -> pd.DataFrame:
    daily = daily.copy()
    daily["outer_high"] = np.maximum(daily["asia_high"], daily["europe_high"])
    daily["inner_high"] = np.minimum(daily["asia_high"], daily["europe_high"])
    daily["outer_low"] = np.minimum(daily["asia_low"], daily["europe_low"])
    daily["inner_low"] = np.maximum(daily["asia_low"], daily["europe_low"])
    daily["common_overlap_range"] = daily["inner_high"] - daily["inner_low"]
    daily["outer_range"] = daily["outer_high"] - daily["outer_low"]
    daily["upper_internal_range"] = daily["outer_high"] - daily["inner_high"]
    daily["lower_internal_range"] = daily["inner_low"] - daily["outer_low"]
    daily["us_high_state"] = [classify_us_high_state(a, b, c) for a, b, c in zip(daily["us_high"], daily["outer_high"], daily["inner_high"])]
    daily["us_low_state"] = [classify_us_low_state(a, b, c) for a, b, c in zip(daily["us_low"], daily["outer_low"], daily["inner_low"])]
    daily["us_directional_outcome_v3"] = [classify_us_directional_outcome_v3(a, b) for a, b in zip(daily["us_high_state"], daily["us_low_state"])]
    daily["us_structure_3x3_label"] = [classify_us_structure_3x3_label(a, b) for a, b in zip(daily["us_high_state"], daily["us_low_state"])]
    daily["us_extension_above_outer_high"] = np.maximum(0.0, daily["us_high"] - daily["outer_high"])
    daily["us_extension_below_outer_low"] = np.maximum(0.0, daily["outer_low"] - daily["us_low"])
    daily["us_extension_above_outer_high_R"] = safe_divide(daily["us_extension_above_outer_high"], daily["outer_range"])
    daily["us_extension_below_outer_low_R"] = safe_divide(daily["us_extension_below_outer_low"], daily["outer_range"])
    daily["us_high_position_vs_structure"] = [classify_position_vs_structure(a, b, c, d, e, "HIGH") for a, b, c, d, e in zip(daily["us_high"], daily["outer_high"], daily["inner_high"], daily["inner_low"], daily["outer_low"])]
    daily["us_low_position_vs_structure"] = [classify_position_vs_structure(a, b, c, d, e, "LOW") for a, b, c, d, e in zip(daily["us_low"], daily["outer_high"], daily["inner_high"], daily["inner_low"], daily["outer_low"])]
    daily["us_close_location_v3"] = [classify_close_location_v3(a, b, c, d, e) for a, b, c, d, e in zip(daily["us_close"], daily["outer_high"], daily["inner_high"], daily["inner_low"], daily["outer_low"])]
    daily["outer_high_buffer"] = daily["outer_high"] * (1 + args.buffer_pct)
    daily["inner_high_buffer"] = daily["inner_high"] * (1 + args.buffer_pct)
    daily["inner_low_buffer"] = daily["inner_low"] * (1 - args.buffer_pct)
    daily["outer_low_buffer"] = daily["outer_low"] * (1 - args.buffer_pct)
    daily["us_outer_high_buffer_tag"] = [classify_outer_high_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["us_high"], daily["us_close"], daily["outer_high"], daily["outer_high_buffer"])]
    daily["us_outer_low_buffer_tag"] = [classify_outer_low_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["us_low"], daily["us_close"], daily["outer_low"], daily["outer_low_buffer"])]
    return daily


def build_outcome_metrics(g: pd.DataFrame, total_n: int) -> dict:
    outcome_label, outcome_pct = most_common_with_pct(g["us_directional_outcome_v3"])
    return {
        "sample_n": int(len(g)),
        "sample_pct": float(safe_divide(len(g), total_n)),
        "US_DIRECTIONAL_UP_rate": g["us_directional_outcome_v3"].eq("US_DIRECTIONAL_UP").mean(),
        "US_DIRECTIONAL_DOWN_rate": g["us_directional_outcome_v3"].eq("US_DIRECTIONAL_DOWN").mean(),
        "US_BOTH_SIDE_EXPANSION_rate": g["us_directional_outcome_v3"].eq("US_BOTH_SIDE_EXPANSION").mean(),
        "US_INTERNAL_LIQUIDITY_TAKE_rate": g["us_directional_outcome_v3"].eq("US_INTERNAL_LIQUIDITY_TAKE").mean(),
        "US_TRUE_CONTRACTION_rate": g["us_directional_outcome_v3"].eq("US_TRUE_CONTRACTION").mean(),
        "most_common_us_directional_outcome_v3": outcome_label,
        "most_common_us_directional_outcome_v3_pct": outcome_pct,
        "median_us_range_pct": g["us_range_pct"].median(),
        "median_us_efficiency": g["us_efficiency"].median(),
        "median_us_close_position": g["us_close_position"].median(),
    }


def summarize_outcome_groups(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    grouped = df.groupby(group_cols, dropna=False)
    total_n = len(df)
    rows = []
    for key, g in grouped:
        if not isinstance(key, tuple):
            key = (key,)
        row = {col: val for col, val in zip(group_cols, key)}
        row.update(build_outcome_metrics(g, total_n))
        rows.append(row)
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(["sample_n"] + group_cols, ascending=[False] + [True] * len(group_cols)).reset_index(drop=True)
    return summary


def summarize_rank_groups(df: pd.DataFrame, group_cols: list[str], allowed_values: set[str] | None = None) -> pd.DataFrame:
    filtered = df.copy()
    if allowed_values is not None and len(group_cols) == 1:
        filtered = filtered.loc[filtered[group_cols[0]].isin(allowed_values)].copy()
    elif allowed_values is not None and len(group_cols) == 2:
        filtered = filtered.loc[filtered[group_cols[-1]].isin(allowed_values)].copy()
    return summarize_outcome_groups(filtered, group_cols)


def build_buffer_tag_summary(daily: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return summarize_outcome_groups(daily, cols)


def build_context_summaries(daily: pd.DataFrame) -> dict[str, pd.DataFrame | list[tuple[str, pd.DataFrame]]]:
    asia_eligible = daily.loc[daily["asia_reference_available"] & daily["asia_session_context"].notna()].copy()
    europe_eligible = daily.loc[daily["europe_session_context"].notna()].copy()
    combo_eligible = daily.loc[daily["asia_session_context"].notna() & daily["europe_session_context"].notna()].copy()

    range_values = {"LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE"}
    dir_strength_values = {"LOW_DIRECTION_STRENGTH", "MEDIUM_DIRECTION_STRENGTH", "HIGH_DIRECTION_STRENGTH"}
    exp_strength_values = {"LOW_EXPANSION_STRENGTH", "MEDIUM_EXPANSION_STRENGTH", "HIGH_EXPANSION_STRENGTH"}
    overlap_values = {"LOW_OVERLAP", "MEDIUM_OVERLAP", "HIGH_OVERLAP"}

    combo_summary = summarize_outcome_groups(combo_eligible, ["asia_session_context", "europe_session_context"])
    if not combo_summary.empty:
        combo_summary = combo_summary.loc[combo_summary["sample_n"] >= 10].reset_index(drop=True)

    return {
        "asia_context_summary": summarize_outcome_groups(asia_eligible, ["asia_session_context"]),
        "europe_context_summary": summarize_outcome_groups(europe_eligible, ["europe_session_context"]),
        "asia_europe_context_combo_summary": combo_summary,
        "asia_structure_summary": summarize_outcome_groups(asia_eligible, ["asia_structure_state"]),
        "europe_structure_summary": summarize_outcome_groups(europe_eligible, ["europe_structure_state"]),
        "asia_direction_summary": summarize_outcome_groups(asia_eligible, ["asia_context_direction"]),
        "europe_direction_summary": summarize_outcome_groups(europe_eligible, ["europe_context_direction"]),
        "asia_range_rank_summary": summarize_rank_groups(daily, ["asia_range_rank"], range_values),
        "europe_range_rank_summary": summarize_rank_groups(daily, ["europe_range_rank"], range_values),
        "asia_direction_strength_rank_summary": summarize_rank_groups(daily, ["asia_direction_strength_rank"], dir_strength_values),
        "europe_direction_strength_rank_summary": summarize_rank_groups(daily, ["europe_direction_strength_rank"], dir_strength_values),
        "asia_expansion_strength_rank_summary": summarize_rank_groups(daily, ["asia_structure_state", "asia_expansion_strength_rank"], exp_strength_values),
        "europe_expansion_strength_rank_summary": summarize_rank_groups(daily, ["europe_structure_state", "europe_expansion_strength_rank"], exp_strength_values),
        "asia_reference_overlap_rank_summary": summarize_rank_groups(daily, ["asia_reference_overlap_rank"], overlap_values),
        "europe_reference_overlap_rank_summary": summarize_rank_groups(daily, ["europe_reference_overlap_rank"], overlap_values),
        "asia_buffer_tag_tables": [
            ("A. asia_high_buffer_tag", build_buffer_tag_summary(asia_eligible, ["asia_high_buffer_tag"])),
            ("B. asia_low_buffer_tag", build_buffer_tag_summary(asia_eligible, ["asia_low_buffer_tag"])),
        ],
        "europe_buffer_tag_tables": [
            ("A. europe_high_buffer_tag", build_buffer_tag_summary(europe_eligible, ["europe_high_buffer_tag"])),
            ("B. europe_low_buffer_tag", build_buffer_tag_summary(europe_eligible, ["europe_low_buffer_tag"])),
        ],
        "us_buffer_tag_tables": [
            ("A. us_outer_high_buffer_tag", build_buffer_tag_summary(daily, ["us_outer_high_buffer_tag"])),
            ("B. us_outer_low_buffer_tag", build_buffer_tag_summary(daily, ["us_outer_low_buffer_tag"])),
        ],
    }


def sample_latest_rows_per_group(source: pd.DataFrame, group_cols: list[str], n: int, label: str) -> pd.DataFrame:
    rows = []
    sorted_source = source.sort_values(["anchor_utc_date", "session_day_wib"], ascending=[False, False])
    for key, g in sorted_source.groupby(group_cols, dropna=False):
        sampled = g.head(n).copy()
        sampled["sample_source"] = label
        if not isinstance(key, tuple):
            key = (key,)
        sampled["sample_group"] = " | ".join(f"{col}={val}" for col, val in zip(group_cols, key))
        rows.append(sampled)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def sample_manual_validation_rows(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    asia_eligible = df.loc[df["asia_reference_available"] & df["asia_session_context"].notna()].copy()
    europe_eligible = df.loc[df["europe_session_context"].notna()].copy()
    combo_eligible = df.loc[df["asia_session_context"].notna() & df["europe_session_context"].notna()].copy()
    combo_counts = combo_eligible.groupby(["asia_session_context", "europe_session_context"], dropna=False).size().reset_index(name="sample_n")
    eligible_combos = combo_counts.loc[combo_counts["sample_n"] >= 10, ["asia_session_context", "europe_session_context"]]
    if not eligible_combos.empty:
        eligible_combo_rows = combo_eligible.merge(eligible_combos, on=["asia_session_context", "europe_session_context"], how="inner")
        parts.append(sample_latest_rows_per_group(eligible_combo_rows, ["asia_session_context", "europe_session_context"], 5, "asia_europe_session_context_combo"))
    parts.append(sample_latest_rows_per_group(asia_eligible, ["asia_session_context"], 5, "asia_session_context"))
    parts.append(sample_latest_rows_per_group(europe_eligible, ["europe_session_context"], 5, "europe_session_context"))
    parts.append(sample_latest_rows_per_group(df, ["us_directional_outcome_v3"], 5, "us_directional_outcome_v3"))
    parts.append(sample_latest_rows_per_group(df, ["us_structure_3x3_label"], 5, "us_structure_3x3_label"))
    out = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()
    if out.empty:
        return out
    keep_cols = [
        "sample_source", "sample_group", "session_day_wib",
        "asia_start_utc_str", "asia_end_utc_str", "europe_start_utc_str", "europe_end_utc_str", "us_start_utc_str", "us_end_utc_str",
        "asia_start_wib", "asia_end_wib", "europe_start_wib", "europe_end_wib", "us_start_wib", "us_end_wib",
        "asia_reference_available", "asia_reference_us_high", "asia_reference_us_low", "asia_reference_upper_buffer", "asia_reference_lower_buffer",
        "asia_open", "asia_high", "asia_low", "asia_close", "asia_range_pct", "asia_efficiency", "asia_structure_state", "asia_context_direction", "asia_session_context", "asia_high_buffer_tag", "asia_low_buffer_tag",
        "asia_range_rank", "asia_direction_strength_rank", "asia_expansion_strength_rank", "asia_reference_overlap_rank",
        "europe_reference_asia_high", "europe_reference_asia_low", "europe_reference_upper_buffer", "europe_reference_lower_buffer",
        "europe_open", "europe_high", "europe_low", "europe_close", "europe_range_pct", "europe_efficiency", "europe_structure_state", "europe_context_direction", "europe_session_context", "europe_high_buffer_tag", "europe_low_buffer_tag",
        "europe_range_rank", "europe_direction_strength_rank", "europe_expansion_strength_rank", "europe_reference_overlap_rank",
        "asia_europe_session_context_combo",
        "overlap", "union_range", "overlap_of_union", "overlap_of_asia", "overlap_of_europe",
        "us_open", "us_high", "us_low", "us_close", "outer_high", "inner_high", "inner_low", "outer_low",
        "us_high_state", "us_low_state", "us_directional_outcome_v3", "us_structure_3x3_label", "us_close_location_v3",
        "outer_high_buffer", "outer_low_buffer", "us_outer_high_buffer_tag", "us_outer_low_buffer_tag",
    ]
    keep_cols = [c for c in keep_cols if c in out.columns]
    return out[keep_cols].drop_duplicates().sort_values(["sample_source", "sample_group", "session_day_wib"], ascending=[True, True, False]).reset_index(drop=True)


def build_daily_export(daily: pd.DataFrame) -> pd.DataFrame:
    keep_cols = [
        "session_day_wib", "anchor_utc_date",
        "asia_start_utc_str", "asia_end_utc_str", "europe_start_utc_str", "europe_end_utc_str", "us_start_utc_str", "us_end_utc_str",
        "asia_start_wib", "asia_end_wib", "europe_start_wib", "europe_end_wib", "us_start_wib", "us_end_wib",
        "asia_reference_available", "asia_reference_us_high", "asia_reference_us_low", "asia_reference_upper_buffer", "asia_reference_lower_buffer",
        "europe_reference_asia_high", "europe_reference_asia_low", "europe_reference_upper_buffer", "europe_reference_lower_buffer",
        "asia_open", "asia_high", "asia_low", "asia_close", "asia_range_pct", "asia_efficiency", "asia_structure_state", "asia_context_direction", "asia_session_context", "asia_high_buffer_tag", "asia_low_buffer_tag",
        "europe_open", "europe_high", "europe_low", "europe_close", "europe_range_pct", "europe_efficiency", "europe_structure_state", "europe_context_direction", "europe_session_context", "europe_high_buffer_tag", "europe_low_buffer_tag",
        "asia_range_rank", "asia_direction_strength_rank", "asia_expansion_strength_rank", "asia_reference_overlap_rank",
        "europe_range_rank", "europe_direction_strength_rank", "europe_expansion_strength_rank", "europe_reference_overlap_rank",
        "asia_europe_session_context_combo",
        "overlap", "union_range", "overlap_of_union", "overlap_of_asia", "overlap_of_europe",
        "us_open", "us_high", "us_low", "us_close", "outer_high", "inner_high", "inner_low", "outer_low",
        "us_high_state", "us_low_state", "us_directional_outcome_v3", "us_structure_3x3_label", "us_close_location_v3",
        "outer_high_buffer", "outer_low_buffer", "us_outer_high_buffer_tag", "us_outer_low_buffer_tag",
    ]
    keep_cols = [c for c in keep_cols if c in daily.columns]
    return daily[keep_cols].copy()


PERCENT_KEYWORDS = ["pct", "rate", "share", "position", "_R", "efficiency", "overlap_of_"]
NUMERIC_KEYWORDS = ["open", "high", "low", "close", "range", "volume", "notional", "count", "median", "buffer", "overlap"]


def auto_adjust_and_format(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    ws = writer.book[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for idx, col in enumerate(df.columns, start=1):
        values = [str(col)]
        series = df[col]
        values.extend(["" if pd.isna(v) else str(v) for v in series.head(500)])
        width = min(max(len(v) for v in values) + 2, 50)
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
        col_lower = str(col).lower()
        if any(keyword in col_lower for keyword in PERCENT_KEYWORDS):
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2, max_row=ws.max_row):
                for c in cell:
                    c.number_format = "0.00%"
        elif any(keyword in col_lower for keyword in NUMERIC_KEYWORDS) and pd.api.types.is_numeric_dtype(series):
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2, max_row=ws.max_row):
                for c in cell:
                    c.number_format = "0.000000"


def write_multi_table_sheet(writer: pd.ExcelWriter, sheet_name: str, tables: list[tuple[str, pd.DataFrame]]) -> None:
    start_row = 0
    for title, df in tables:
        pd.DataFrame({title: []}).to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row)
        start_row += 1
        if df.empty:
            pd.DataFrame({"note": ["No rows met the applicable filter."]}).to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row)
            start_row += 4
        else:
            df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row)
            start_row += len(df) + 3


def export_workbook(output_path: Path, config_df: pd.DataFrame, daily_export: pd.DataFrame, us_directional: pd.DataFrame, us_structure: pd.DataFrame, context_summaries: dict[str, pd.DataFrame | list[tuple[str, pd.DataFrame]]], manual_validation: pd.DataFrame) -> None:
    ensure_parent_dir(output_path)
    print(f"Writing Excel workbook: {output_path}")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        sheets = {
            "1_config": make_excel_safe(config_df),
            "2_daily_sessions": make_excel_safe(daily_export),
            "3_us_directional_outcome_summary_v3": make_excel_safe(us_directional),
            "4_us_structure_3x3_summary_v3": make_excel_safe(us_structure),
            "5_asia_context_summary": make_excel_safe(context_summaries["asia_context_summary"]),
            "6_europe_context_summary": make_excel_safe(context_summaries["europe_context_summary"]),
            "7_asia_europe_context_combo_summary": make_excel_safe(context_summaries["asia_europe_context_combo_summary"]),
            "8_asia_structure_summary": make_excel_safe(context_summaries["asia_structure_summary"]),
            "9_europe_structure_summary": make_excel_safe(context_summaries["europe_structure_summary"]),
            "10_asia_direction_summary": make_excel_safe(context_summaries["asia_direction_summary"]),
            "11_europe_direction_summary": make_excel_safe(context_summaries["europe_direction_summary"]),
            "12_asia_range_rank_summary": make_excel_safe(context_summaries["asia_range_rank_summary"]),
            "13_europe_range_rank_summary": make_excel_safe(context_summaries["europe_range_rank_summary"]),
            "14_asia_direction_strength_rank_summary": make_excel_safe(context_summaries["asia_direction_strength_rank_summary"]),
            "15_europe_direction_strength_rank_summary": make_excel_safe(context_summaries["europe_direction_strength_rank_summary"]),
            "16_asia_expansion_strength_rank_summary": make_excel_safe(context_summaries["asia_expansion_strength_rank_summary"]),
            "17_europe_expansion_strength_rank_summary": make_excel_safe(context_summaries["europe_expansion_strength_rank_summary"]),
            "18_asia_reference_overlap_rank_summary": make_excel_safe(context_summaries["asia_reference_overlap_rank_summary"]),
            "19_europe_reference_overlap_rank_summary": make_excel_safe(context_summaries["europe_reference_overlap_rank_summary"]),
            "23_manual_validation_samples": make_excel_safe(manual_validation),
        }
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        write_multi_table_sheet(writer, "20_asia_buffer_tag_summary", [(t, make_excel_safe(df)) for t, df in context_summaries["asia_buffer_tag_tables"]])
        write_multi_table_sheet(writer, "21_europe_buffer_tag_summary", [(t, make_excel_safe(df)) for t, df in context_summaries["europe_buffer_tag_tables"]])
        write_multi_table_sheet(writer, "22_us_buffer_tag_summary", [(t, make_excel_safe(df)) for t, df in context_summaries["us_buffer_tag_tables"]])
        for sheet_name, df in sheets.items():
            auto_adjust_and_format(writer, sheet_name, df)
    print("Workbook export complete.")


def build_config_sheet(args, raw_row_count: int, daily_count: int, stats: dict, reference_stats: dict, rank_stats: dict, us_unclassified_count: int) -> pd.DataFrame:
    items = {
        "run_timestamp_utc": format_ts_utc(pd.Timestamp.now(tz=UTC_TZ)),
        "input_file": args.input,
        "symbol": args.symbol,
        "output_xlsx": args.output_xlsx,
        "raw_row_count": raw_row_count,
        "complete_daily_rows": daily_count,
        "version": "V4.1",
        "title": "Asia/Europe context and quality-rank to US V3 outcome research",
        "session_times": "Asia 23:00-07:00 UTC | Europe 07:00-15:00 UTC | US 15:00-23:00 UTC",
        "buffer_pct": args.buffer_pct,
        "context_direction_tolerance": args.context_direction_tolerance,
        "quality_rank_lookback": args.quality_rank_lookback,
        "quality_rank_min_history": args.quality_rank_min_history,
        "quality_rank_method": "Fixed terciles of prior eligible observations only; current row excluded; no future data; no full-sample thresholds.",
        "note_asia_reference": "Asia reference = immediately preceding completed US session only when exact consecutive session-day continuity exists.",
        "note_europe_reference": "Europe reference = current completed Asia session.",
        "note_us_logic": "US V3 inner/outer outcome logic unchanged.",
        "removed_legacy_logic": "No full-sample range thresholds, no body-type ranks, no direction_combo, no Europe behavior labels, no range_dominance, no overlap_label thresholds, no range_sequence remnants.",
        "us_unclassified_count": us_unclassified_count,
    }
    items.update(reference_stats)
    items.update(rank_stats)
    items.update(stats)
    return pd.DataFrame({"parameter": list(items.keys()), "value": list(items.values())})


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    validate_args(args)
    windows = build_session_windows(args)
    print("Step 1/7: Loading raw trades...")
    df = load_raw_aggtrades(Path(args.input))
    raw_row_count = len(df)
    print("Step 2/7: Building daily Asia/Europe/US sessions...")
    daily, stats = build_daily_sessions(df, windows, args.min_trades_per_session)
    print("Step 3/7: Adding Asia/Europe primary context model...")
    daily = add_overlap_diagnostics(daily)
    daily, reference_stats = add_asia_europe_contexts(daily, args)
    print("Step 4/7: Adding Asia/Europe quality ranks...")
    daily, rank_stats = apply_prior_rolling_ranks(daily, args)
    print("Step 5/7: Adding unchanged US V3 structural outcome labels and buffer tags...")
    daily = add_us_outcomes_v3(daily, args)
    print("Step 6/7: Building summaries and manual validation samples...")
    us_directional = summarize_outcome_groups(daily, ["us_directional_outcome_v3"])
    us_structure = summarize_outcome_groups(daily, ["us_structure_3x3_label"])
    context_summaries = build_context_summaries(daily)
    manual_validation = sample_manual_validation_rows(daily)
    daily_export = build_daily_export(daily)
    us_unclassified_count = int(daily["us_directional_outcome_v3"].eq("US_UNCLASSIFIED").sum())
    config_df = build_config_sheet(args, raw_row_count, len(daily), stats, reference_stats, rank_stats, us_unclassified_count)
    print("Step 7/7: Exporting workbook...")
    export_workbook(Path(args.output_xlsx), config_df, daily_export, us_directional, us_structure, context_summaries, manual_validation)
    print("Done.")


if __name__ == "__main__":
    main()
