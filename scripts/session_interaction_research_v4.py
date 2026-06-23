from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from indicator.volume_profile import build_basic_volume_profile
from session.context import (
    add_asia_europe_contexts as ctx_add_asia_europe_contexts,
    add_overlap_diagnostics as ctx_add_overlap_diagnostics,
    build_daily_sessions as ctx_build_daily_sessions,
    build_session_windows as ctx_build_session_windows,
    load_raw_aggtrades as ctx_load_raw_aggtrades,
)

WIB_TZ = "Asia/Jakarta"
UTC_TZ = "UTC"
QUALITY_RANK_LOW_Q = 1 / 3
QUALITY_RANK_HIGH_Q = 2 / 3
UNCLASSIFIED_VALUE_RELATION = "VALUE_UNAVAILABLE"
PROFILE_SHEET_NAMES = {
    "asia_close_vs_own_value_summary": "24_asia_profile",
    "europe_close_vs_own_value_summary": "25_europe_profile",
    "europe_close_vs_asia_value_summary": "26_europe_vs_asia_va",
    "us_open_vs_asia_value_summary": "27_us_open_vs_asia_va",
    "us_open_vs_europe_value_summary": "28_us_open_vs_europe_va",
    "pre_us_value_combo_summary": "29_pre_us_value_combo",
}


def safe_divide(a, b):
    if np.isscalar(a) and np.isscalar(b):
        if pd.isna(a) or pd.isna(b) or b == 0:
            return np.nan
        return a / b
    a_arr = np.asarray(a, dtype="float64")
    b_arr = np.asarray(b, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(a_arr, b_arr, out=np.full_like(a_arr, np.nan, dtype="float64"), where=(~np.isnan(a_arr)) & (~np.isnan(b_arr)) & (b_arr != 0))
    return out


def classify_price_vs_value(price: float, vah: float, val: float) -> str:
    if any(pd.isna(x) for x in [price, vah, val]):
        return UNCLASSIFIED_VALUE_RELATION
    if price > vah:
        return "ABOVE_VALUE"
    if price < val:
        return "BELOW_VALUE"
    return "INSIDE_VALUE"


def normalized_distance_to_poc(price: float, poc: float) -> float:
    if pd.isna(price) or pd.isna(poc) or poc == 0:
        return np.nan
    return float((price - poc) / poc)


def profile_to_prefixed_fields(profile: dict, prefix: str) -> dict:
    return {
        f"{prefix}_profile_bins": int(profile["bins"]),
        f"{prefix}_profile_bin_width": float(profile["bin_width"]),
        f"{prefix}_profile_total_volume": float(profile["total_volume"]),
        f"{prefix}_poc": float(profile["poc_price"]),
        f"{prefix}_poc_volume": float(profile["poc_volume"]),
        f"{prefix}_poc_volume_pct": float(profile["poc_volume_pct"]),
        f"{prefix}_val": float(profile["val"]),
        f"{prefix}_vah": float(profile["vah"]),
        f"{prefix}_value_area_width": float(profile["value_area_width"]),
        f"{prefix}_value_area_width_pct": profile["value_area_width_pct"],
        f"{prefix}_value_area_volume": float(profile["value_area_volume"]),
        f"{prefix}_value_area_volume_pct": float(profile["value_area_volume_pct"]),
    }


def build_own_value_context(day_record: dict, prefix: str) -> dict:
    return {
        f"{prefix}_open_vs_own_value": classify_price_vs_value(day_record.get(f"{prefix}_open"), day_record.get(f"{prefix}_vah"), day_record.get(f"{prefix}_val")),
        f"{prefix}_close_vs_own_value": classify_price_vs_value(day_record.get(f"{prefix}_close"), day_record.get(f"{prefix}_vah"), day_record.get(f"{prefix}_val")),
        f"{prefix}_open_to_own_poc_pct": normalized_distance_to_poc(day_record.get(f"{prefix}_open"), day_record.get(f"{prefix}_poc")),
        f"{prefix}_close_to_own_poc_pct": normalized_distance_to_poc(day_record.get(f"{prefix}_close"), day_record.get(f"{prefix}_poc")),
    }


def build_cross_value_context(day_record: dict, subject_prefix: str, reference_prefix: str) -> dict:
    return {
        f"{subject_prefix}_open_vs_{reference_prefix}_value": classify_price_vs_value(day_record.get(f"{subject_prefix}_open"), day_record.get(f"{reference_prefix}_vah"), day_record.get(f"{reference_prefix}_val")),
        f"{subject_prefix}_close_vs_{reference_prefix}_value": classify_price_vs_value(day_record.get(f"{subject_prefix}_close"), day_record.get(f"{reference_prefix}_vah"), day_record.get(f"{reference_prefix}_val")),
        f"{subject_prefix}_open_to_{reference_prefix}_poc_pct": normalized_distance_to_poc(day_record.get(f"{subject_prefix}_open"), day_record.get(f"{reference_prefix}_poc")),
        f"{subject_prefix}_close_to_{reference_prefix}_poc_pct": normalized_distance_to_poc(day_record.get(f"{subject_prefix}_close"), day_record.get(f"{reference_prefix}_poc")),
    }


def pick_col(columns, candidates, required=True):
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    if required:
        raise ValueError(f"Could not find required column. Tried: {candidates}. Available: {list(columns)}")
    return None


def normalize_timestamp(series: pd.Series) -> pd.Series:
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


def timestamp_series_to_ns_array(series: pd.Series) -> np.ndarray:
    ts = pd.to_datetime(series, utc=True)
    return ts.dt.tz_localize(None).to_numpy(dtype="datetime64[ns]").astype("int64")


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
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
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
        parse_hhmm(actual[0]); parse_hhmm(actual[1]); parse_hhmm(wanted[0]); parse_hhmm(wanted[1])


def build_session_windows(args) -> list[SessionWindow]:
    a_sh, a_sm = parse_hhmm(args.asia_start); a_eh, a_em = parse_hhmm(args.asia_end)
    e_sh, e_sm = parse_hhmm(args.europe_start); e_eh, e_em = parse_hhmm(args.europe_end)
    u_sh, u_sm = parse_hhmm(args.us_start); u_eh, u_em = parse_hhmm(args.us_end)
    windows = [SessionWindow("asia", a_sh, a_sm, a_eh, a_em, -1, 0), SessionWindow("europe", e_sh, e_sm, e_eh, e_em, 0, 0), SessionWindow("us", u_sh, u_sm, u_eh, u_em, 0, 0)]
    anchor = pd.Timestamp("2026-01-02 00:00:00", tz=UTC_TZ)
    prev_end = None
    for win in windows:
        start = anchor + win.start_offset(); end = anchor + win.end_offset()
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
    parser = argparse.ArgumentParser(description="Asia/Europe/US session interaction research V4 on raw Binance aggTrade parquet.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output-xlsx", "--output", dest="output_xlsx", required=True)
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
    parser.add_argument("--volume-profile-bins", type=int, default=50)
    parser.add_argument("--value-area-pct", type=float, default=0.70)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser


def validate_args(args) -> None:
    if args.min_trades_per_session <= 0: raise ValueError("--min-trades-per-session must be > 0")
    if args.buffer_pct < 0: raise ValueError("--buffer-pct must be >= 0")
    if args.context_direction_tolerance < 0: raise ValueError("--context-direction-tolerance must be >= 0")
    if args.quality_rank_lookback <= 0: raise ValueError("--quality-rank-lookback must be > 0")
    if args.quality_rank_min_history <= 0: raise ValueError("--quality-rank-min-history must be > 0")
    if args.quality_rank_min_history > args.quality_rank_lookback: raise ValueError("--quality-rank-min-history must be <= --quality-rank-lookback")
    if args.volume_profile_bins <= 0: raise ValueError("--volume-profile-bins must be > 0")
    if not (0 < args.value_area_pct <= 1): raise ValueError("--value-area-pct must satisfy 0 < value_area_pct <= 1")
    validate_session_defaults(args)


def load_raw_aggtrades(path: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq
    print(f"Loading parquet: {path}")
    if not path.exists(): raise FileNotFoundError(f"Input parquet not found: {path}")
    pf = pq.ParquetFile(path)
    columns = pf.schema.names
    ts_col = pick_col(columns, ["timestamp", "event_timestamp", "T", "time", "transact_time"])
    price_col = pick_col(columns, ["price", "p"])
    qty_col = pick_col(columns, ["qty", "quantity", "q"])
    maker_col = pick_col(columns, ["is_buyer_maker", "m"], required=False)
    read_cols = [ts_col, price_col, qty_col] + ([maker_col] if maker_col else [])
    df = pd.read_parquet(path, columns=read_cols)
    df = df.rename(columns={ts_col: "timestamp", price_col: "price", qty_col: "qty"})
    if maker_col: df = df.rename(columns={maker_col: "is_buyer_maker"})
    else: df["is_buyer_maker"] = np.nan
    df["timestamp"] = normalize_timestamp(df["timestamp"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("float64")
    df = df.dropna(subset=["timestamp", "price", "qty"]).copy().sort_values("timestamp").reset_index(drop=True)
    df["notional"] = df["price"] * df["qty"]
    if df["is_buyer_maker"].notna().any():
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        df["aggressor_side"] = np.where(df["is_buyer_maker"], -1, 1)
    else:
        df["aggressor_side"] = np.nan
    if df.empty: raise ValueError("Input parquet is empty after cleaning required fields.")
    return df


def build_daily_windows(anchor_utc_date: pd.Timestamp, windows: list[SessionWindow]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {win.name: (anchor_utc_date + win.start_offset(), anchor_utc_date + win.end_offset()) for win in windows}


def aggregate_session_metrics(session_df: pd.DataFrame, prefix: str) -> dict:
    session_df = session_df.sort_values("timestamp")
    open_, high_, low_, close_ = float(session_df["price"].iloc[0]), float(session_df["price"].max()), float(session_df["price"].min()), float(session_df["price"].iloc[-1])
    range_, body, abs_body = high_ - low_, close_ - open_, abs(close_ - open_)
    return {f"{prefix}_open": open_, f"{prefix}_high": high_, f"{prefix}_low": low_, f"{prefix}_close": close_, f"{prefix}_range": range_, f"{prefix}_range_pct": safe_divide(range_, open_), f"{prefix}_body": body, f"{prefix}_abs_body": abs_body, f"{prefix}_body_pct": safe_divide(body, open_), f"{prefix}_body_to_range": safe_divide(abs_body, range_), f"{prefix}_efficiency": safe_divide(body, range_), f"{prefix}_open_position": safe_divide(open_ - low_, range_), f"{prefix}_close_position": safe_divide(close_ - low_, range_), f"{prefix}_volume": float(session_df["qty"].sum()), f"{prefix}_notional": float(session_df["notional"].sum()), f"{prefix}_trade_count": int(len(session_df)), f"{prefix}_first_trade_timestamp": session_df["timestamp"].iloc[0], f"{prefix}_last_trade_timestamp": session_df["timestamp"].iloc[-1]}


def first_break_timestamp(session_df: pd.DataFrame, high_level: float, low_level: float, high_cmp: Literal["gt", "gte"], low_cmp: Literal["lt", "lte"]) -> tuple[pd.Timestamp | None, pd.Timestamp | None, str]:
    prices = session_df["price"]
    high_hits = session_df.loc[prices > high_level if high_cmp == "gt" else prices >= high_level, "timestamp"]
    low_hits = session_df.loc[prices < low_level if low_cmp == "lt" else prices <= low_level, "timestamp"]
    high_ts = high_hits.iloc[0] if len(high_hits) else None
    low_ts = low_hits.iloc[0] if len(low_hits) else None
    if high_ts is None and low_ts is None: sequence = "NO_BREAK"
    elif high_ts is not None and low_ts is None: sequence = "HIGH_ONLY"
    elif high_ts is None and low_ts is not None: sequence = "LOW_ONLY"
    elif high_ts < low_ts: sequence = "HIGH_THEN_LOW"
    elif low_ts < high_ts: sequence = "LOW_THEN_HIGH"
    else: sequence = "BOTH_SAME_TIME"
    return high_ts, low_ts, sequence


def build_daily_sessions(df: pd.DataFrame, windows: list[SessionWindow], min_trades_per_session: int, volume_profile_bins: int, value_area_pct: float) -> tuple[pd.DataFrame, dict]:
    print("Building complete Asia/Europe/US daily sessions...")
    dataset_min_ts, dataset_max_ts = df["timestamp"].min(), df["timestamp"].max()
    candidate_days = pd.date_range(start=dataset_min_ts.floor("D"), end=dataset_max_ts.floor("D"), freq="D", tz=UTC_TZ)
    timestamp_ns = timestamp_series_to_ns_array(df["timestamp"])
    records = []
    dropped_edge_coverage = dropped_missing_session = dropped_min_trades = 0
    for anchor_utc_date in candidate_days:
        windows_map = build_daily_windows(anchor_utc_date, windows)
        asia_start, _ = windows_map["asia"]; _, us_end = windows_map["us"]
        if dataset_min_ts > asia_start or dataset_max_ts < us_end:
            dropped_edge_coverage += 1; continue
        day_record = {"session_day_wib": anchor_utc_date.tz_convert(WIB_TZ).strftime("%Y-%m-%d"), "anchor_utc_date": anchor_utc_date}
        for name, (start, end) in windows_map.items():
            day_record[f"{name}_start_utc"] = start; day_record[f"{name}_end_utc"] = end
        missing_session = too_few_trades = False
        for name, (start, end) in windows_map.items():
            start_idx = int(np.searchsorted(timestamp_ns, pd.Timestamp(start).value, side="left"))
            end_idx = int(np.searchsorted(timestamp_ns, pd.Timestamp(end).value, side="left"))
            session_df = df.iloc[start_idx:end_idx]
            if session_df.empty: missing_session = True; break
            if len(session_df) < min_trades_per_session: too_few_trades = True; break
            day_record.update(aggregate_session_metrics(session_df, name))
            profile = build_basic_volume_profile(session_df, n_bins=volume_profile_bins, value_area_pct=value_area_pct)
            day_record.update(profile_to_prefixed_fields(profile, name))
            day_record.update(build_own_value_context(day_record, name))
            if name == "europe":
                day_record.update(build_cross_value_context(day_record, "europe", "asia"))
                high_ts, low_ts, seq = first_break_timestamp(session_df, day_record["asia_high"], day_record["asia_low"], "gt", "lt")
                day_record["europe_first_break_asia_high_timestamp"] = high_ts; day_record["europe_first_break_asia_low_timestamp"] = low_ts; day_record["europe_first_break_sequence"] = seq
            elif name == "us":
                day_record.update(build_cross_value_context(day_record, "us", "asia")); day_record.update(build_cross_value_context(day_record, "us", "europe"))
                pre_us_high = max(day_record["asia_high"], day_record["europe_high"]); pre_us_low = min(day_record["asia_low"], day_record["europe_low"])
                high_ts, low_ts, seq = first_break_timestamp(session_df, pre_us_high, pre_us_low, "gt", "lt")
                day_record["us_first_break_pre_us_high_timestamp"] = high_ts; day_record["us_first_break_pre_us_low_timestamp"] = low_ts; day_record["us_first_break_sequence"] = seq
        if too_few_trades: dropped_min_trades += 1; continue
        if missing_session: dropped_missing_session += 1; continue
        records.append(day_record)
    daily = pd.DataFrame(records)
    if daily.empty: raise ValueError("No complete session days found after coverage and min-trade filtering.")
    for prefix in ["asia", "europe", "us"]:
        daily[f"{prefix}_start_wib"] = daily[f"{prefix}_start_utc"].map(format_ts_wib); daily[f"{prefix}_end_wib"] = daily[f"{prefix}_end_utc"].map(format_ts_wib); daily[f"{prefix}_start_utc_str"] = daily[f"{prefix}_start_utc"].map(format_ts_utc); daily[f"{prefix}_end_utc_str"] = daily[f"{prefix}_end_utc"].map(format_ts_utc)
    for col in ["europe_first_break_asia_high_timestamp", "europe_first_break_asia_low_timestamp", "us_first_break_pre_us_high_timestamp", "us_first_break_pre_us_low_timestamp"]:
        daily[f"{col}_utc"] = daily[col].map(format_ts_utc); daily[f"{col}_wib"] = daily[col].map(format_ts_wib)
    stats = {"dataset_min_timestamp_utc": format_ts_utc(dataset_min_ts), "dataset_max_timestamp_utc": format_ts_utc(dataset_max_ts), "candidate_days": int(len(candidate_days)), "complete_days": int(len(daily)), "dropped_edge_coverage": int(dropped_edge_coverage), "dropped_missing_session": int(dropped_missing_session), "dropped_min_trades": int(dropped_min_trades)}
    return daily, stats


def classify_context_direction(efficiency: float, tolerance: float) -> str:
    if pd.isna(efficiency): return "NO_DIRECTION"
    if efficiency > tolerance: return "UP"
    if efficiency < -tolerance: return "DOWN"
    return "NO_DIRECTION"


def classify_structure_state(session_high: float, session_low: float, upper_buffer: float, lower_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_high, session_low, upper_buffer, lower_buffer]): return "REFERENCE_UNAVAILABLE"
    high_expanded = session_high > upper_buffer; low_expanded = session_low < lower_buffer
    if high_expanded and not low_expanded: return "EXPANSION_UP"
    if low_expanded and not high_expanded: return "EXPANSION_DOWN"
    if high_expanded and low_expanded: return "TWO_SIDED_EXPANSION"
    return "CONTAINED_RANGE"


def combine_session_context(structure_state: str, context_direction: str | float | None) -> str | None:
    if structure_state in [None, "REFERENCE_UNAVAILABLE"] or pd.isna(context_direction): return None
    return f"{structure_state}_{context_direction}"


def classify_high_buffer_tag(session_high: float, session_close: float, ref_high: float, upper_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_high, session_close, ref_high, upper_buffer]): return "REFERENCE_UNAVAILABLE"
    if session_high <= ref_high: return "HIGH_NOT_TOUCHED"
    if session_high > ref_high and session_high <= upper_buffer and session_close <= ref_high: return "HIGH_SWEEP_WITHIN_BUFFER_REJECTED"
    if session_high > ref_high and session_high <= upper_buffer and session_close > ref_high: return "HIGH_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if session_high > upper_buffer and session_close <= upper_buffer: return "HIGH_BREAK_BEYOND_BUFFER_RECOVERED"
    return "HIGH_BREAK_BEYOND_BUFFER_ACCEPTED"


def classify_low_buffer_tag(session_low: float, session_close: float, ref_low: float, lower_buffer: float) -> str:
    if any(pd.isna(x) for x in [session_low, session_close, ref_low, lower_buffer]): return "REFERENCE_UNAVAILABLE"
    if session_low >= ref_low: return "LOW_NOT_TOUCHED"
    if session_low < ref_low and session_low >= lower_buffer and session_close >= ref_low: return "LOW_SWEEP_WITHIN_BUFFER_REJECTED"
    if session_low < ref_low and session_low >= lower_buffer and session_close < ref_low: return "LOW_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if session_low < lower_buffer and session_close >= lower_buffer: return "LOW_BREAK_BEYOND_BUFFER_RECOVERED"
    return "LOW_BREAK_BEYOND_BUFFER_ACCEPTED"


def add_overlap_diagnostics(daily: pd.DataFrame) -> pd.DataFrame:
    daily = daily.copy(); overlap = np.maximum(0.0, np.minimum(daily["asia_high"], daily["europe_high"]) - np.maximum(daily["asia_low"], daily["europe_low"])); union_range = np.maximum(daily["asia_high"], daily["europe_high"]) - np.minimum(daily["asia_low"], daily["europe_low"])
    daily["overlap"] = overlap; daily["union_range"] = union_range; daily["overlap_of_union"] = safe_divide(overlap, union_range); daily["overlap_of_asia"] = safe_divide(overlap, daily["asia_range"]); daily["overlap_of_europe"] = safe_divide(overlap, daily["europe_range"])
    return daily


def add_asia_europe_contexts(daily: pd.DataFrame, args) -> tuple[pd.DataFrame, dict]:
    daily = daily.copy().sort_values("anchor_utc_date").reset_index(drop=True)
    prev_anchor = daily["anchor_utc_date"].shift(1); expected_prev_anchor = daily["anchor_utc_date"] - pd.Timedelta(days=1)
    daily["asia_reference_available"] = prev_anchor.eq(expected_prev_anchor)
    daily["asia_reference_us_high"] = daily["us_high"].shift(1).where(daily["asia_reference_available"]); daily["asia_reference_us_low"] = daily["us_low"].shift(1).where(daily["asia_reference_available"])
    daily["asia_reference_upper_buffer"] = daily["asia_reference_us_high"] * (1 + args.buffer_pct); daily["asia_reference_lower_buffer"] = daily["asia_reference_us_low"] * (1 - args.buffer_pct)
    daily["asia_structure_state"] = [classify_structure_state(a,b,c,d) for a,b,c,d in zip(daily["asia_high"], daily["asia_low"], daily["asia_reference_upper_buffer"], daily["asia_reference_lower_buffer"])]
    daily.loc[~daily["asia_reference_available"], "asia_structure_state"] = "REFERENCE_UNAVAILABLE"
    daily["asia_context_direction"] = daily["asia_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance)); daily.loc[~daily["asia_reference_available"], "asia_context_direction"] = pd.NA
    daily["asia_session_context"] = [combine_session_context(a,b) for a,b in zip(daily["asia_structure_state"], daily["asia_context_direction"])]
    daily["asia_high_buffer_tag"] = [classify_high_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["asia_high"], daily["asia_close"], daily["asia_reference_us_high"], daily["asia_reference_upper_buffer"])]
    daily["asia_low_buffer_tag"] = [classify_low_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["asia_low"], daily["asia_close"], daily["asia_reference_us_low"], daily["asia_reference_lower_buffer"])]
    daily["europe_reference_asia_high"] = daily["asia_high"]; daily["europe_reference_asia_low"] = daily["asia_low"]; daily["europe_reference_upper_buffer"] = daily["europe_reference_asia_high"] * (1 + args.buffer_pct); daily["europe_reference_lower_buffer"] = daily["europe_reference_asia_low"] * (1 - args.buffer_pct)
    daily["europe_structure_state"] = [classify_structure_state(a,b,c,d) for a,b,c,d in zip(daily["europe_high"], daily["europe_low"], daily["europe_reference_upper_buffer"], daily["europe_reference_lower_buffer"])]
    daily["europe_context_direction"] = daily["europe_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance)); daily["europe_session_context"] = [combine_session_context(a,b) for a,b in zip(daily["europe_structure_state"], daily["europe_context_direction"])]
    daily["europe_high_buffer_tag"] = [classify_high_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["europe_high"], daily["europe_close"], daily["europe_reference_asia_high"], daily["europe_reference_upper_buffer"])]
    daily["europe_low_buffer_tag"] = [classify_low_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["europe_low"], daily["europe_close"], daily["europe_reference_asia_low"], daily["europe_reference_lower_buffer"])]
    daily["asia_europe_session_context_combo"] = np.where(daily["asia_session_context"].notna() & daily["europe_session_context"].notna(), "ASIA_" + daily["asia_session_context"].astype(str) + "__EUROPE_" + daily["europe_session_context"].astype(str), pd.NA)
    return daily, {"rows_without_previous_us_reference": int((~daily["asia_reference_available"]).sum()), "asia_unclassified_count": int((daily["asia_reference_available"] & daily["asia_session_context"].isna()).sum()), "europe_unclassified_count": int(daily["europe_session_context"].isna().sum())}


def rank_value_against_history(current_value: float, history: list[float], low_label: str, mid_label: str, high_label: str, min_history: int) -> str:
    if pd.isna(current_value) or len(history) < min_history: return "INSUFFICIENT_HISTORY"
    low_cut = float(pd.Series(history, dtype="float64").quantile(QUALITY_RANK_LOW_Q)); high_cut = float(pd.Series(history, dtype="float64").quantile(QUALITY_RANK_HIGH_Q))
    if current_value <= low_cut: return low_label
    if current_value >= high_cut: return high_label
    return mid_label


def apply_prior_rolling_ranks(daily: pd.DataFrame, args) -> tuple[pd.DataFrame, dict]:
    daily = daily.copy().sort_values("anchor_utc_date").reset_index(drop=True)
    lookback, min_history = args.quality_rank_lookback, args.quality_rank_min_history
    for col in [
        "asia_range_rank", "europe_range_rank", "asia_direction_strength_rank", "europe_direction_strength_rank",
        "asia_expansion_strength_rank", "europe_expansion_strength_rank", "asia_reference_overlap_rank",
        "europe_reference_overlap_rank", "us_range_rank", "us_direction_strength_rank",
        "us_high_reach_strength_rank", "us_low_reach_strength_rank",
    ]:
        daily[col] = pd.NA
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
        "us_range_rank_insufficient_history_count": 0,
        "us_direction_strength_rank_not_applicable_count": 0,
        "us_direction_strength_rank_insufficient_history_count": 0,
        "us_high_reach_strength_rank_not_applicable_count": 0,
        "us_high_reach_strength_rank_insufficient_history_count": 0,
        "us_high_reach_strength_rank_reference_unavailable_count": 0,
        "us_low_reach_strength_rank_not_applicable_count": 0,
        "us_low_reach_strength_rank_insufficient_history_count": 0,
        "us_low_reach_strength_rank_reference_unavailable_count": 0,
    }
    asia_range_hist=[]; europe_range_hist=[]; asia_dir_hist=[]; europe_dir_hist=[]; asia_exp_hist={"EXPANSION_UP": [], "EXPANSION_DOWN": [], "TWO_SIDED_EXPANSION": []}; europe_exp_hist={"EXPANSION_UP": [], "EXPANSION_DOWN": [], "TWO_SIDED_EXPANSION": []}; asia_overlap_hist=[]; europe_overlap_hist=[]
    us_range_hist=[]; us_direction_strength_hist=[]
    us_high_internal_reach_hist=[]; us_high_outer_expansion_hist=[]
    us_low_internal_reach_hist=[]; us_low_outer_expansion_hist=[]
    for idx,row in daily.iterrows():
        daily.at[idx, "asia_range_rank"] = rank_value_against_history(row["asia_range_pct"], asia_range_hist[-lookback:], "LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE", min_history)
        daily.at[idx, "europe_range_rank"] = rank_value_against_history(row["europe_range_pct"], europe_range_hist[-lookback:], "LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE", min_history)
        if daily.at[idx, "asia_range_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["asia_range_rank_insufficient_history_count"] += 1
        if daily.at[idx, "europe_range_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["europe_range_rank_insufficient_history_count"] += 1
        asia_direction_strength = abs(row["asia_efficiency"]) if not pd.isna(row["asia_efficiency"]) else np.nan; europe_direction_strength = abs(row["europe_efficiency"]) if not pd.isna(row["europe_efficiency"]) else np.nan
        if row["asia_context_direction"] == "NO_DIRECTION" or pd.isna(row["asia_context_direction"]): daily.at[idx, "asia_direction_strength_rank"] = "NOT_APPLICABLE"; excluded_counts["asia_direction_strength_rank_not_applicable_count"] += 1
        else:
            daily.at[idx, "asia_direction_strength_rank"] = rank_value_against_history(asia_direction_strength, asia_dir_hist[-lookback:], "LOW_DIRECTION_STRENGTH", "MEDIUM_DIRECTION_STRENGTH", "HIGH_DIRECTION_STRENGTH", min_history)
            if daily.at[idx, "asia_direction_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["asia_direction_strength_rank_insufficient_history_count"] += 1
        if row["europe_context_direction"] == "NO_DIRECTION" or pd.isna(row["europe_context_direction"]): daily.at[idx, "europe_direction_strength_rank"] = "NOT_APPLICABLE"; excluded_counts["europe_direction_strength_rank_not_applicable_count"] += 1
        else:
            daily.at[idx, "europe_direction_strength_rank"] = rank_value_against_history(europe_direction_strength, europe_dir_hist[-lookback:], "LOW_DIRECTION_STRENGTH", "MEDIUM_DIRECTION_STRENGTH", "HIGH_DIRECTION_STRENGTH", min_history)
            if daily.at[idx, "europe_direction_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["europe_direction_strength_rank_insufficient_history_count"] += 1
        if not row["asia_reference_available"]: daily.at[idx, "asia_expansion_strength_rank"] = "REFERENCE_UNAVAILABLE"; excluded_counts["asia_expansion_strength_rank_reference_unavailable_count"] += 1
        elif row["asia_structure_state"] == "CONTAINED_RANGE": daily.at[idx, "asia_expansion_strength_rank"] = "NOT_APPLICABLE"; excluded_counts["asia_expansion_strength_rank_not_applicable_count"] += 1
        else:
            asia_up = max(0.0, safe_divide(row["asia_high"], row["asia_reference_upper_buffer"]) - 1) if not pd.isna(row["asia_reference_upper_buffer"]) else np.nan; asia_down = max(0.0, safe_divide(row["asia_reference_lower_buffer"], row["asia_low"]) - 1) if not pd.isna(row["asia_reference_lower_buffer"]) else np.nan; asia_excess = asia_up if row["asia_structure_state"] == "EXPANSION_UP" else asia_down if row["asia_structure_state"] == "EXPANSION_DOWN" else asia_up + asia_down
            daily.at[idx, "asia_expansion_strength_rank"] = rank_value_against_history(asia_excess, asia_exp_hist[row["asia_structure_state"]][-lookback:], "LOW_EXPANSION_STRENGTH", "MEDIUM_EXPANSION_STRENGTH", "HIGH_EXPANSION_STRENGTH", min_history)
            if daily.at[idx, "asia_expansion_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["asia_expansion_strength_rank_insufficient_history_count"] += 1
        if row["europe_structure_state"] == "CONTAINED_RANGE": daily.at[idx, "europe_expansion_strength_rank"] = "NOT_APPLICABLE"; excluded_counts["europe_expansion_strength_rank_not_applicable_count"] += 1
        else:
            eu_up = max(0.0, safe_divide(row["europe_high"], row["europe_reference_upper_buffer"]) - 1); eu_down = max(0.0, safe_divide(row["europe_reference_lower_buffer"], row["europe_low"]) - 1); eu_excess = eu_up if row["europe_structure_state"] == "EXPANSION_UP" else eu_down if row["europe_structure_state"] == "EXPANSION_DOWN" else eu_up + eu_down
            daily.at[idx, "europe_expansion_strength_rank"] = rank_value_against_history(eu_excess, europe_exp_hist[row["europe_structure_state"]][-lookback:], "LOW_EXPANSION_STRENGTH", "MEDIUM_EXPANSION_STRENGTH", "HIGH_EXPANSION_STRENGTH", min_history)
            if daily.at[idx, "europe_expansion_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["europe_expansion_strength_rank_insufficient_history_count"] += 1
        if not row["asia_reference_available"]: daily.at[idx, "asia_reference_overlap_rank"] = "REFERENCE_UNAVAILABLE"; excluded_counts["asia_reference_overlap_rank_reference_unavailable_count"] += 1
        else:
            asia_overlap = max(0.0, min(row["asia_high"], row["asia_reference_us_high"]) - max(row["asia_low"], row["asia_reference_us_low"])); asia_union = max(row["asia_high"], row["asia_reference_us_high"]) - min(row["asia_low"], row["asia_reference_us_low"]); asia_overlap_value = safe_divide(asia_overlap, asia_union)
            daily.at[idx, "asia_reference_overlap_rank"] = rank_value_against_history(asia_overlap_value, asia_overlap_hist[-lookback:], "LOW_OVERLAP", "MEDIUM_OVERLAP", "HIGH_OVERLAP", min_history)
            if daily.at[idx, "asia_reference_overlap_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["asia_reference_overlap_rank_insufficient_history_count"] += 1
        europe_overlap = max(0.0, min(row["europe_high"], row["europe_reference_asia_high"]) - max(row["europe_low"], row["europe_reference_asia_low"])); europe_union = max(row["europe_high"], row["europe_reference_asia_high"]) - min(row["europe_low"], row["europe_reference_asia_low"]); europe_overlap_value = safe_divide(europe_overlap, europe_union)
        daily.at[idx, "europe_reference_overlap_rank"] = rank_value_against_history(europe_overlap_value, europe_overlap_hist[-lookback:], "LOW_OVERLAP", "MEDIUM_OVERLAP", "HIGH_OVERLAP", min_history)
        if daily.at[idx, "europe_reference_overlap_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["europe_reference_overlap_rank_insufficient_history_count"] += 1

        daily.at[idx, "us_range_rank"] = rank_value_against_history(row["us_range_pct"], us_range_hist[-lookback:], "LOW_RANGE", "MEDIUM_RANGE", "HIGH_RANGE", min_history)
        if daily.at[idx, "us_range_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_range_rank_insufficient_history_count"] += 1

        us_direction_strength = abs(row["us_efficiency"]) if not pd.isna(row["us_efficiency"]) else np.nan
        if row["us_direction"] == "NO_DIRECTION" or pd.isna(row["us_direction"]):
            daily.at[idx, "us_direction_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["us_direction_strength_rank_not_applicable_count"] += 1
        else:
            daily.at[idx, "us_direction_strength_rank"] = rank_value_against_history(us_direction_strength, us_direction_strength_hist[-lookback:], "LOW_DIRECTION_STRENGTH", "MEDIUM_DIRECTION_STRENGTH", "HIGH_DIRECTION_STRENGTH", min_history)
            if daily.at[idx, "us_direction_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_direction_strength_rank_insufficient_history_count"] += 1

        if row["us_high_reach_state"] == "HIGH_NOT_REACHED":
            daily.at[idx, "us_high_reach_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["us_high_reach_strength_rank_not_applicable_count"] += 1
        elif row["us_high_reach_state"] == "HIGH_INTERNAL_REACH":
            high_hist = us_high_internal_reach_hist
            daily.at[idx, "us_high_reach_strength_rank"] = rank_value_against_history(row["us_high_reach_value"], high_hist[-lookback:], "LOW_REACH", "MEDIUM_REACH", "HIGH_REACH", min_history)
            if daily.at[idx, "us_high_reach_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_high_reach_strength_rank_insufficient_history_count"] += 1
        elif row["us_high_reach_state"] == "HIGH_OUTER_EXPANSION":
            high_hist = us_high_outer_expansion_hist
            daily.at[idx, "us_high_reach_strength_rank"] = rank_value_against_history(row["us_high_reach_value"], high_hist[-lookback:], "LOW_REACH", "MEDIUM_REACH", "HIGH_REACH", min_history)
            if daily.at[idx, "us_high_reach_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_high_reach_strength_rank_insufficient_history_count"] += 1
        else:
            daily.at[idx, "us_high_reach_strength_rank"] = "REFERENCE_UNAVAILABLE"
            excluded_counts["us_high_reach_strength_rank_reference_unavailable_count"] += 1

        if row["us_low_reach_state"] == "LOW_NOT_REACHED":
            daily.at[idx, "us_low_reach_strength_rank"] = "NOT_APPLICABLE"
            excluded_counts["us_low_reach_strength_rank_not_applicable_count"] += 1
        elif row["us_low_reach_state"] == "LOW_INTERNAL_REACH":
            low_hist = us_low_internal_reach_hist
            daily.at[idx, "us_low_reach_strength_rank"] = rank_value_against_history(row["us_low_reach_value"], low_hist[-lookback:], "LOW_REACH", "MEDIUM_REACH", "HIGH_REACH", min_history)
            if daily.at[idx, "us_low_reach_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_low_reach_strength_rank_insufficient_history_count"] += 1
        elif row["us_low_reach_state"] == "LOW_OUTER_EXPANSION":
            low_hist = us_low_outer_expansion_hist
            daily.at[idx, "us_low_reach_strength_rank"] = rank_value_against_history(row["us_low_reach_value"], low_hist[-lookback:], "LOW_REACH", "MEDIUM_REACH", "HIGH_REACH", min_history)
            if daily.at[idx, "us_low_reach_strength_rank"] == "INSUFFICIENT_HISTORY": excluded_counts["us_low_reach_strength_rank_insufficient_history_count"] += 1
        else:
            daily.at[idx, "us_low_reach_strength_rank"] = "REFERENCE_UNAVAILABLE"
            excluded_counts["us_low_reach_strength_rank_reference_unavailable_count"] += 1

        if not pd.isna(row["asia_range_pct"]): asia_range_hist.append(float(row["asia_range_pct"]))
        if not pd.isna(row["europe_range_pct"]): europe_range_hist.append(float(row["europe_range_pct"]))
        if row["asia_context_direction"] in ["UP", "DOWN"] and not pd.isna(asia_direction_strength): asia_dir_hist.append(float(asia_direction_strength))
        if row["europe_context_direction"] in ["UP", "DOWN"] and not pd.isna(europe_direction_strength): europe_dir_hist.append(float(europe_direction_strength))
        if row["asia_reference_available"] and row["asia_structure_state"] in asia_exp_hist: asia_exp_hist[row["asia_structure_state"]].append(float(asia_excess))
        if row["europe_structure_state"] in europe_exp_hist: europe_exp_hist[row["europe_structure_state"]].append(float(eu_excess))
        if row["asia_reference_available"] and not pd.isna(asia_overlap_value): asia_overlap_hist.append(float(asia_overlap_value))
        if not pd.isna(europe_overlap_value): europe_overlap_hist.append(float(europe_overlap_value))
        if not pd.isna(row["us_range_pct"]): us_range_hist.append(float(row["us_range_pct"]))
        if row["us_direction"] in ["UP", "DOWN"] and not pd.isna(us_direction_strength): us_direction_strength_hist.append(float(us_direction_strength))
        if row["us_high_reach_state"] == "HIGH_INTERNAL_REACH" and not pd.isna(row["us_high_reach_value"]): us_high_internal_reach_hist.append(float(row["us_high_reach_value"]))
        if row["us_high_reach_state"] == "HIGH_OUTER_EXPANSION" and not pd.isna(row["us_high_reach_value"]): us_high_outer_expansion_hist.append(float(row["us_high_reach_value"]))
        if row["us_low_reach_state"] == "LOW_INTERNAL_REACH" and not pd.isna(row["us_low_reach_value"]): us_low_internal_reach_hist.append(float(row["us_low_reach_value"]))
        if row["us_low_reach_state"] == "LOW_OUTER_EXPANSION" and not pd.isna(row["us_low_reach_value"]): us_low_outer_expansion_hist.append(float(row["us_low_reach_value"]))
    return daily, excluded_counts


def classify_outer_high_buffer_tag(us_high: float, us_close: float, outer_high: float, outer_high_buffer: float) -> str:
    if any(pd.isna(x) for x in [us_high, us_close, outer_high, outer_high_buffer]): return "HIGH_BUFFER_UNCLASSIFIED"
    if us_high <= outer_high: return "HIGH_NOT_TOUCHED"
    if us_high <= outer_high_buffer and us_close <= outer_high: return "HIGH_TOUCH_WITHIN_BUFFER_REJECTED"
    if us_high <= outer_high_buffer and us_close > outer_high: return "HIGH_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if us_high > outer_high_buffer and us_close <= outer_high_buffer: return "HIGH_BREAK_BEYOND_BUFFER_RECOVERED"
    return "HIGH_BREAK_BEYOND_BUFFER_ACCEPTED"


def classify_outer_low_buffer_tag(us_low: float, us_close: float, outer_low: float, outer_low_buffer: float) -> str:
    if any(pd.isna(x) for x in [us_low, us_close, outer_low, outer_low_buffer]): return "LOW_BUFFER_UNCLASSIFIED"
    if us_low >= outer_low: return "LOW_NOT_TOUCHED"
    if us_low >= outer_low_buffer and us_close >= outer_low: return "LOW_TOUCH_WITHIN_BUFFER_REJECTED"
    if us_low >= outer_low_buffer and us_close < outer_low: return "LOW_TOUCH_WITHIN_BUFFER_ACCEPTED"
    if us_low < outer_low_buffer and us_close >= outer_low_buffer: return "LOW_BREAK_BEYOND_BUFFER_RECOVERED"
    return "LOW_BREAK_BEYOND_BUFFER_ACCEPTED"


def classify_us_high_reach_state(us_high: float, inner_high: float, outer_high_buffer: float) -> str:
    if pd.isna(us_high) or pd.isna(inner_high) or pd.isna(outer_high_buffer): return "HIGH_REACH_UNCLASSIFIED"
    if us_high > outer_high_buffer: return "HIGH_OUTER_EXPANSION"
    if us_high > inner_high: return "HIGH_INTERNAL_REACH"
    return "HIGH_NOT_REACHED"


def classify_us_low_reach_state(us_low: float, inner_low: float, outer_low_buffer: float) -> str:
    if pd.isna(us_low) or pd.isna(inner_low) or pd.isna(outer_low_buffer): return "LOW_REACH_UNCLASSIFIED"
    if us_low < outer_low_buffer: return "LOW_OUTER_EXPANSION"
    if us_low < inner_low: return "LOW_INTERNAL_REACH"
    return "LOW_NOT_REACHED"


def classify_us_relative_structure(high_reach_state: str, low_reach_state: str) -> str:
    mapping = {
        ("HIGH_OUTER_EXPANSION", "LOW_OUTER_EXPANSION"): "BOTH_OUTER_EXPANSION",
        ("HIGH_OUTER_EXPANSION", "LOW_INTERNAL_REACH"): "UP_OUTER_WITH_LOW_INTERNAL",
        ("HIGH_OUTER_EXPANSION", "LOW_NOT_REACHED"): "UP_OUTER_ONLY",
        ("HIGH_INTERNAL_REACH", "LOW_OUTER_EXPANSION"): "DOWN_OUTER_WITH_HIGH_INTERNAL",
        ("HIGH_INTERNAL_REACH", "LOW_INTERNAL_REACH"): "BOTH_INTERNAL_REACH",
        ("HIGH_INTERNAL_REACH", "LOW_NOT_REACHED"): "UPPER_INTERNAL_ONLY",
        ("HIGH_NOT_REACHED", "LOW_OUTER_EXPANSION"): "DOWN_OUTER_ONLY",
        ("HIGH_NOT_REACHED", "LOW_INTERNAL_REACH"): "LOWER_INTERNAL_ONLY",
        ("HIGH_NOT_REACHED", "LOW_NOT_REACHED"): "NO_INNER_REACH",
    }
    return mapping.get((high_reach_state, low_reach_state), "US_RELATIVE_STRUCTURE_UNCLASSIFIED")


def add_us_reference_structure(daily: pd.DataFrame, args) -> pd.DataFrame:
    daily = daily.copy(); daily["outer_high"] = np.maximum(daily["asia_high"], daily["europe_high"]); daily["inner_high"] = np.minimum(daily["asia_high"], daily["europe_high"]); daily["outer_low"] = np.minimum(daily["asia_low"], daily["europe_low"]); daily["inner_low"] = np.maximum(daily["asia_low"], daily["europe_low"]); daily["outer_range"] = daily["outer_high"] - daily["outer_low"]
    daily["outer_high_buffer"] = daily["outer_high"] * (1 + args.buffer_pct); daily["outer_low_buffer"] = daily["outer_low"] * (1 - args.buffer_pct)
    daily["us_outer_high_buffer_tag"] = [classify_outer_high_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["us_high"], daily["us_close"], daily["outer_high"], daily["outer_high_buffer"])]; daily["us_outer_low_buffer_tag"] = [classify_outer_low_buffer_tag(a,b,c,d) for a,b,c,d in zip(daily["us_low"], daily["us_close"], daily["outer_low"], daily["outer_low_buffer"])]
    return daily


def add_us_relative_structure(daily: pd.DataFrame, args) -> pd.DataFrame:
    daily = daily.copy()
    daily["us_direction"] = daily["us_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance))
    daily["us_high_reach_state"] = [classify_us_high_reach_state(a, b, c) for a, b, c in zip(daily["us_high"], daily["inner_high"], daily["outer_high_buffer"])]
    daily["us_low_reach_state"] = [classify_us_low_reach_state(a, b, c) for a, b, c in zip(daily["us_low"], daily["inner_low"], daily["outer_low_buffer"])]
    daily["us_relative_structure"] = [classify_us_relative_structure(a, b) for a, b in zip(daily["us_high_reach_state"], daily["us_low_reach_state"])]
    daily["us_high_reach_value"] = safe_divide(np.maximum(0.0, daily["us_high"] - daily["inner_high"]), daily["outer_range"])
    daily.loc[daily["outer_range"] <= 0, "us_high_reach_value"] = np.nan
    daily["us_low_reach_value"] = safe_divide(np.maximum(0.0, daily["inner_low"] - daily["us_low"]), daily["outer_range"])
    daily.loc[daily["outer_range"] <= 0, "us_low_reach_value"] = np.nan
    return daily


def build_outcome_metrics(g: pd.DataFrame, total_n: int) -> dict:
    us_direction_label, us_direction_pct = most_common_with_pct(g["us_direction"])
    us_relative_structure_label, us_relative_structure_pct = most_common_with_pct(g["us_relative_structure"])
    return {
        "sample_n": int(len(g)),
        "sample_pct": float(safe_divide(len(g), total_n)),
        "us_direction_up_pct": g["us_direction"].eq("UP").mean(),
        "us_direction_down_pct": g["us_direction"].eq("DOWN").mean(),
        "us_direction_no_direction_pct": g["us_direction"].eq("NO_DIRECTION").mean(),
        "both_outer_expansion_pct": g["us_relative_structure"].eq("BOTH_OUTER_EXPANSION").mean(),
        "up_outer_with_low_internal_pct": g["us_relative_structure"].eq("UP_OUTER_WITH_LOW_INTERNAL").mean(),
        "up_outer_only_pct": g["us_relative_structure"].eq("UP_OUTER_ONLY").mean(),
        "down_outer_with_high_internal_pct": g["us_relative_structure"].eq("DOWN_OUTER_WITH_HIGH_INTERNAL").mean(),
        "both_internal_reach_pct": g["us_relative_structure"].eq("BOTH_INTERNAL_REACH").mean(),
        "upper_internal_only_pct": g["us_relative_structure"].eq("UPPER_INTERNAL_ONLY").mean(),
        "down_outer_only_pct": g["us_relative_structure"].eq("DOWN_OUTER_ONLY").mean(),
        "lower_internal_only_pct": g["us_relative_structure"].eq("LOWER_INTERNAL_ONLY").mean(),
        "no_inner_reach_pct": g["us_relative_structure"].eq("NO_INNER_REACH").mean(),
        "most_common_us_direction": us_direction_label,
        "most_common_us_direction_pct": us_direction_pct,
        "most_common_us_relative_structure": us_relative_structure_label,
        "most_common_us_relative_structure_pct": us_relative_structure_pct,
        "median_us_range_pct": g["us_range_pct"].median(),
        "median_us_efficiency": g["us_efficiency"].median(),
        "median_us_close_position": g["us_close_position"].median(),
    }


def summarize_outcome_groups(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows=[]; total_n=len(df)
    for key,g in df.groupby(group_cols, dropna=False):
        if not isinstance(key, tuple): key=(key,)
        row={col: val for col,val in zip(group_cols, key)}; row.update(build_outcome_metrics(g,total_n)); rows.append(row)
    summary=pd.DataFrame(rows)
    if not summary.empty: summary=summary.sort_values(["sample_n"] + group_cols, ascending=[False]+[True]*len(group_cols)).reset_index(drop=True)
    return summary


def summarize_rank_groups(df: pd.DataFrame, group_cols: list[str], allowed_values: set[str] | None = None) -> pd.DataFrame:
    filtered=df.copy()
    if allowed_values is not None and len(group_cols)==1: filtered=filtered.loc[filtered[group_cols[0]].isin(allowed_values)].copy()
    elif allowed_values is not None and len(group_cols)==2: filtered=filtered.loc[filtered[group_cols[-1]].isin(allowed_values)].copy()
    return summarize_outcome_groups(filtered, group_cols)


def build_buffer_tag_summary(daily: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return summarize_outcome_groups(daily, cols)


def build_context_summaries(daily: pd.DataFrame) -> dict[str, pd.DataFrame | list[tuple[str, pd.DataFrame]]]:
    asia_eligible = daily.loc[daily["asia_reference_available"] & daily["asia_session_context"].notna()].copy(); europe_eligible = daily.loc[daily["europe_session_context"].notna()].copy(); combo_eligible = daily.loc[daily["asia_session_context"].notna() & daily["europe_session_context"].notna()].copy()
    range_values={"LOW_RANGE","MEDIUM_RANGE","HIGH_RANGE"}; dir_strength_values={"LOW_DIRECTION_STRENGTH","MEDIUM_DIRECTION_STRENGTH","HIGH_DIRECTION_STRENGTH"}; exp_strength_values={"LOW_EXPANSION_STRENGTH","MEDIUM_EXPANSION_STRENGTH","HIGH_EXPANSION_STRENGTH"}; overlap_values={"LOW_OVERLAP","MEDIUM_OVERLAP","HIGH_OVERLAP"}; reach_values={"LOW_REACH","MEDIUM_REACH","HIGH_REACH"}
    combo_summary=summarize_outcome_groups(combo_eligible,["asia_session_context","europe_session_context"])
    if not combo_summary.empty: combo_summary=combo_summary.loc[combo_summary["sample_n"]>=10].reset_index(drop=True)
    pre_us_combo = summarize_outcome_groups(daily, ["us_open_vs_asia_value", "us_open_vs_europe_value"])
    if not pre_us_combo.empty: pre_us_combo = pre_us_combo.loc[pre_us_combo["sample_n"] >= 10].reset_index(drop=True)
    us_relative_direction_summary = summarize_outcome_groups(daily, ["us_relative_structure", "us_direction"])
    if not us_relative_direction_summary.empty:
        us_relative_direction_summary = us_relative_direction_summary.loc[us_relative_direction_summary["sample_n"] >= 10].reset_index(drop=True)
    return {
        "asia_context_summary": summarize_outcome_groups(asia_eligible,["asia_session_context"]),
        "europe_context_summary": summarize_outcome_groups(europe_eligible,["europe_session_context"]),
        "asia_europe_context_combo_summary": combo_summary,
        "asia_structure_summary": summarize_outcome_groups(asia_eligible,["asia_structure_state"]),
        "europe_structure_summary": summarize_outcome_groups(europe_eligible,["europe_structure_state"]),
        "asia_direction_summary": summarize_outcome_groups(asia_eligible,["asia_context_direction"]),
        "europe_direction_summary": summarize_outcome_groups(europe_eligible,["europe_context_direction"]),
        "asia_range_rank_summary": summarize_rank_groups(daily,["asia_range_rank"], range_values),
        "europe_range_rank_summary": summarize_rank_groups(daily,["europe_range_rank"], range_values),
        "asia_direction_strength_rank_summary": summarize_rank_groups(daily,["asia_direction_strength_rank"], dir_strength_values),
        "europe_direction_strength_rank_summary": summarize_rank_groups(daily,["europe_direction_strength_rank"], dir_strength_values),
        "asia_expansion_strength_rank_summary": summarize_rank_groups(daily,["asia_structure_state","asia_expansion_strength_rank"], exp_strength_values),
        "europe_expansion_strength_rank_summary": summarize_rank_groups(daily,["europe_structure_state","europe_expansion_strength_rank"], exp_strength_values),
        "asia_reference_overlap_rank_summary": summarize_rank_groups(daily,["asia_reference_overlap_rank"], overlap_values),
        "europe_reference_overlap_rank_summary": summarize_rank_groups(daily,["europe_reference_overlap_rank"], overlap_values),
        "us_direction_summary": summarize_outcome_groups(daily, ["us_direction"]),
        "us_relative_structure_summary": summarize_outcome_groups(daily, ["us_relative_structure"]),
        "us_range_rank_summary": summarize_rank_groups(daily, ["us_range_rank"], range_values),
        "us_direction_strength_rank_summary": summarize_rank_groups(daily, ["us_direction_strength_rank"], dir_strength_values),
        "us_high_reach_strength_rank_summary": summarize_rank_groups(daily, ["us_high_reach_state", "us_high_reach_strength_rank"], reach_values),
        "us_low_reach_strength_rank_summary": summarize_rank_groups(daily, ["us_low_reach_state", "us_low_reach_strength_rank"], reach_values),
        "us_relative_structure_by_direction_summary": us_relative_direction_summary,
        "asia_close_vs_own_value_summary": summarize_outcome_groups(daily, ["asia_close_vs_own_value"]),
        "europe_close_vs_own_value_summary": summarize_outcome_groups(daily, ["europe_close_vs_own_value"]),
        "europe_close_vs_asia_value_summary": summarize_outcome_groups(daily, ["europe_close_vs_asia_value"]),
        "us_open_vs_asia_value_summary": summarize_outcome_groups(daily, ["us_open_vs_asia_value"]),
        "us_open_vs_europe_value_summary": summarize_outcome_groups(daily, ["us_open_vs_europe_value"]),
        "pre_us_value_combo_summary": pre_us_combo,
        "asia_buffer_tag_tables": [("A. asia_high_buffer_tag", build_buffer_tag_summary(asia_eligible,["asia_high_buffer_tag"])), ("B. asia_low_buffer_tag", build_buffer_tag_summary(asia_eligible,["asia_low_buffer_tag"]))],
        "europe_buffer_tag_tables": [("A. europe_high_buffer_tag", build_buffer_tag_summary(europe_eligible,["europe_high_buffer_tag"])), ("B. europe_low_buffer_tag", build_buffer_tag_summary(europe_eligible,["europe_low_buffer_tag"]))],
        "us_buffer_tag_tables": [("A. us_outer_high_buffer_tag", build_buffer_tag_summary(daily,["us_outer_high_buffer_tag"])), ("B. us_outer_low_buffer_tag", build_buffer_tag_summary(daily,["us_outer_low_buffer_tag"]))],
    }


def sample_latest_rows_per_group(source: pd.DataFrame, group_cols: list[str], n: int, label: str) -> pd.DataFrame:
    rows=[]
    for key,g in source.sort_values(["anchor_utc_date","session_day_wib"], ascending=[False,False]).groupby(group_cols, dropna=False):
        sampled=g.head(n).copy(); sampled["sample_source"]=label
        if not isinstance(key, tuple): key=(key,)
        sampled["sample_group"]=" | ".join(f"{col}={val}" for col,val in zip(group_cols,key)); rows.append(sampled)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def sample_manual_validation_rows(df: pd.DataFrame) -> pd.DataFrame:
    parts=[]; asia_eligible=df.loc[df["asia_reference_available"] & df["asia_session_context"].notna()].copy(); europe_eligible=df.loc[df["europe_session_context"].notna()].copy(); combo_eligible=df.loc[df["asia_session_context"].notna() & df["europe_session_context"].notna()].copy()
    combo_counts=combo_eligible.groupby(["asia_session_context","europe_session_context"], dropna=False).size().reset_index(name="sample_n"); eligible_combos=combo_counts.loc[combo_counts["sample_n"]>=10,["asia_session_context","europe_session_context"]]
    if not eligible_combos.empty: parts.append(sample_latest_rows_per_group(combo_eligible.merge(eligible_combos,on=["asia_session_context","europe_session_context"], how="inner"), ["asia_session_context","europe_session_context"], 5, "asia_europe_session_context_combo"))
    parts.append(sample_latest_rows_per_group(asia_eligible,["asia_session_context"],5,"asia_session_context")); parts.append(sample_latest_rows_per_group(europe_eligible,["europe_session_context"],5,"europe_session_context")); parts.append(sample_latest_rows_per_group(df,["us_direction"],5,"us_direction")); parts.append(sample_latest_rows_per_group(df,["us_relative_structure"],5,"us_relative_structure"))
    out=pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()
    if out.empty: return out
    keep_cols=[c for c in ["sample_source","sample_group","session_day_wib","asia_start_utc_str","asia_end_utc_str","europe_start_utc_str","europe_end_utc_str","us_start_utc_str","us_end_utc_str","asia_start_wib","asia_end_wib","europe_start_wib","europe_end_wib","us_start_wib","us_end_wib","asia_reference_available","asia_reference_us_high","asia_reference_us_low","asia_reference_upper_buffer","asia_reference_lower_buffer","asia_open","asia_high","asia_low","asia_close","asia_open_position","asia_close_position","asia_poc","asia_val","asia_vah","asia_open_vs_own_value","asia_close_vs_own_value","asia_range_pct","asia_efficiency","asia_structure_state","asia_context_direction","asia_session_context","asia_high_buffer_tag","asia_low_buffer_tag","europe_reference_asia_high","europe_reference_asia_low","europe_reference_upper_buffer","europe_reference_lower_buffer","europe_open","europe_high","europe_low","europe_close","europe_open_position","europe_close_position","europe_poc","europe_val","europe_vah","europe_open_vs_own_value","europe_close_vs_own_value","europe_open_vs_asia_value","europe_close_vs_asia_value","europe_range_pct","europe_efficiency","europe_structure_state","europe_context_direction","europe_session_context","europe_high_buffer_tag","europe_low_buffer_tag","us_open","us_high","us_low","us_close","us_open_position","us_close_position","us_poc","us_val","us_vah","us_open_vs_own_value","us_close_vs_own_value","us_open_vs_asia_value","us_open_vs_europe_value","us_close_vs_asia_value","us_close_vs_europe_value","outer_high","inner_high","inner_low","outer_low","us_direction","us_high_reach_state","us_low_reach_state","us_relative_structure","outer_high_buffer","outer_low_buffer","us_high_reach_value","us_low_reach_value","us_range_rank","us_direction_strength_rank","us_high_reach_strength_rank","us_low_reach_strength_rank","us_outer_high_buffer_tag","us_outer_low_buffer_tag"] if c in out.columns]
    return out[keep_cols].drop_duplicates().sort_values(["sample_source","sample_group","session_day_wib"], ascending=[True,True,False]).reset_index(drop=True)


def build_daily_export(daily: pd.DataFrame) -> pd.DataFrame:
    session_cols = ["session_day_wib","anchor_utc_date","asia_start_utc_str","asia_end_utc_str","europe_start_utc_str","europe_end_utc_str","us_start_utc_str","us_end_utc_str","asia_start_wib","asia_end_wib","europe_start_wib","europe_end_wib","us_start_wib","us_end_wib"]
    asia_cols = ["asia_reference_available","asia_reference_us_high","asia_reference_us_low","asia_reference_upper_buffer","asia_reference_lower_buffer","asia_open","asia_high","asia_low","asia_close","asia_open_position","asia_close_position","asia_range_pct","asia_efficiency","asia_profile_bins","asia_profile_bin_width","asia_profile_total_volume","asia_poc","asia_poc_volume","asia_poc_volume_pct","asia_val","asia_vah","asia_value_area_width","asia_value_area_width_pct","asia_value_area_volume","asia_value_area_volume_pct","asia_open_vs_own_value","asia_close_vs_own_value","asia_open_to_own_poc_pct","asia_close_to_own_poc_pct","asia_structure_state","asia_context_direction","asia_session_context","asia_high_buffer_tag","asia_low_buffer_tag","asia_range_rank","asia_direction_strength_rank","asia_expansion_strength_rank","asia_reference_overlap_rank"]
    europe_cols = ["europe_reference_asia_high","europe_reference_asia_low","europe_reference_upper_buffer","europe_reference_lower_buffer","europe_open","europe_high","europe_low","europe_close","europe_open_position","europe_close_position","europe_range_pct","europe_efficiency","europe_profile_bins","europe_profile_bin_width","europe_profile_total_volume","europe_poc","europe_poc_volume","europe_poc_volume_pct","europe_val","europe_vah","europe_value_area_width","europe_value_area_width_pct","europe_value_area_volume","europe_value_area_volume_pct","europe_open_vs_own_value","europe_close_vs_own_value","europe_open_to_own_poc_pct","europe_close_to_own_poc_pct","europe_open_vs_asia_value","europe_close_vs_asia_value","europe_open_to_asia_poc_pct","europe_close_to_asia_poc_pct","europe_structure_state","europe_context_direction","europe_session_context","europe_high_buffer_tag","europe_low_buffer_tag","europe_range_rank","europe_direction_strength_rank","europe_expansion_strength_rank","europe_reference_overlap_rank"]
    us_cols = ["asia_europe_session_context_combo","overlap","union_range","overlap_of_union","overlap_of_asia","overlap_of_europe","us_open","us_high","us_low","us_close","us_open_position","us_close_position","us_range_pct","us_efficiency","us_direction","us_high_reach_state","us_low_reach_state","us_relative_structure","us_profile_bins","us_profile_bin_width","us_profile_total_volume","us_poc","us_poc_volume","us_poc_volume_pct","us_val","us_vah","us_value_area_width","us_value_area_width_pct","us_value_area_volume","us_value_area_volume_pct","us_open_vs_own_value","us_close_vs_own_value","us_open_to_own_poc_pct","us_close_to_own_poc_pct","us_open_vs_asia_value","us_close_vs_asia_value","us_open_to_asia_poc_pct","us_close_to_asia_poc_pct","us_open_vs_europe_value","us_close_vs_europe_value","us_open_to_europe_poc_pct","us_close_to_europe_poc_pct","outer_high","inner_high","inner_low","outer_low","outer_high_buffer","outer_low_buffer","us_high_reach_value","us_low_reach_value","us_range_rank","us_direction_strength_rank","us_high_reach_strength_rank","us_low_reach_strength_rank","us_outer_high_buffer_tag","us_outer_low_buffer_tag"]
    keep_cols=[c for c in session_cols+asia_cols+europe_cols+us_cols if c in daily.columns]
    return daily[keep_cols].copy()


def validate_sheet_names(sheet_names: list[str]) -> None:
    if len(sheet_names) != len(set(sheet_names)):
        raise ValueError("Duplicate Excel worksheet names detected.")
    oversized = [name for name in sheet_names if len(name) > 31]
    if oversized:
        raise ValueError(f"Excel worksheet names exceed 31 characters: {oversized}")


def auto_adjust_and_format(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    ws = writer.book[sheet_name]; ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    for idx,col in enumerate(df.columns, start=1):
        values=[str(col)] + ["" if pd.isna(v) else str(v) for v in df[col].head(500)]; ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = min(max(len(v) for v in values)+2, 50)
        col_lower = str(col).lower(); fmt=None
        if "to_" in col_lower and col_lower.endswith("_pct"): fmt="0.0000%"
        elif any(token in col_lower for token in ["poc_volume_pct","value_area_volume_pct","value_area_width_pct","range_pct","body_pct"]): fmt="0.0000%"
        elif any(token in col_lower for token in ["_poc","_vah","_val","profile_bin_width","value_area_width"]): fmt="0.0000"
        elif any(keyword in col_lower for keyword in ["pct","rate","share","position","_r","efficiency","overlap_of_"]): fmt="0.00%"
        elif any(keyword in col_lower for keyword in ["open","high","low","close","range","volume","notional","count","median","buffer","overlap"]) and pd.api.types.is_numeric_dtype(df[col]): fmt="0.000000"
        if fmt:
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2, max_row=ws.max_row):
                for c in cell: c.number_format = fmt


def write_multi_table_sheet(writer: pd.ExcelWriter, sheet_name: str, tables: list[tuple[str, pd.DataFrame]]) -> None:
    start_row=0
    for title,df in tables:
        pd.DataFrame({title: []}).to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row); start_row += 1
        if df.empty: pd.DataFrame({"note": ["No rows met the applicable filter."]}).to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row); start_row += 4
        else: df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=start_row); start_row += len(df)+3


def export_workbook(output_path: Path, config_df: pd.DataFrame, daily_export: pd.DataFrame, us_directional: pd.DataFrame, us_structure: pd.DataFrame, context_summaries: dict[str, pd.DataFrame | list[tuple[str, pd.DataFrame]]], manual_validation: pd.DataFrame) -> None:
    ensure_parent_dir(output_path); print(f"Writing Excel workbook: {output_path}")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        sheets = {
            "1_config": make_excel_safe(config_df),
            "2_daily_sessions": make_excel_safe(daily_export),
            "3_us_direction": make_excel_safe(us_directional),
            "4_us_relative_structure": make_excel_safe(us_structure),
            "5_asia_context": make_excel_safe(context_summaries["asia_context_summary"]),
            "6_europe_context": make_excel_safe(context_summaries["europe_context_summary"]),
            "7_asia_europe_combo": make_excel_safe(context_summaries["asia_europe_context_combo_summary"]),
            "8_asia_structure": make_excel_safe(context_summaries["asia_structure_summary"]),
            "9_europe_structure": make_excel_safe(context_summaries["europe_structure_summary"]),
            "10_asia_direction": make_excel_safe(context_summaries["asia_direction_summary"]),
            "11_europe_direction": make_excel_safe(context_summaries["europe_direction_summary"]),
            "12_asia_range_rank": make_excel_safe(context_summaries["asia_range_rank_summary"]),
            "13_europe_range_rank": make_excel_safe(context_summaries["europe_range_rank_summary"]),
            "14_asia_dir_strength": make_excel_safe(context_summaries["asia_direction_strength_rank_summary"]),
            "15_europe_dir_strength": make_excel_safe(context_summaries["europe_direction_strength_rank_summary"]),
            "16_asia_exp_strength": make_excel_safe(context_summaries["asia_expansion_strength_rank_summary"]),
            "17_europe_exp_strength": make_excel_safe(context_summaries["europe_expansion_strength_rank_summary"]),
            "18_asia_ref_overlap": make_excel_safe(context_summaries["asia_reference_overlap_rank_summary"]),
            "19_europe_ref_overlap": make_excel_safe(context_summaries["europe_reference_overlap_rank_summary"]),
            "20_us_range_rank": make_excel_safe(context_summaries["us_range_rank_summary"]),
            "21_us_dir_strength": make_excel_safe(context_summaries["us_direction_strength_rank_summary"]),
            "22_us_high_reach": make_excel_safe(context_summaries["us_high_reach_strength_rank_summary"]),
            "23_us_low_reach": make_excel_safe(context_summaries["us_low_reach_strength_rank_summary"]),
            "24_us_rel_x_dir": make_excel_safe(context_summaries["us_relative_structure_by_direction_summary"]),
            "25_manual_validation": make_excel_safe(manual_validation),
            PROFILE_SHEET_NAMES["asia_close_vs_own_value_summary"]: make_excel_safe(context_summaries["asia_close_vs_own_value_summary"]),
            PROFILE_SHEET_NAMES["europe_close_vs_own_value_summary"]: make_excel_safe(context_summaries["europe_close_vs_own_value_summary"]),
            PROFILE_SHEET_NAMES["europe_close_vs_asia_value_summary"]: make_excel_safe(context_summaries["europe_close_vs_asia_value_summary"]),
            PROFILE_SHEET_NAMES["us_open_vs_asia_value_summary"]: make_excel_safe(context_summaries["us_open_vs_asia_value_summary"]),
            PROFILE_SHEET_NAMES["us_open_vs_europe_value_summary"]: make_excel_safe(context_summaries["us_open_vs_europe_value_summary"]),
            PROFILE_SHEET_NAMES["pre_us_value_combo_summary"]: make_excel_safe(context_summaries["pre_us_value_combo_summary"]),
        }
        validate_sheet_names(list(sheets.keys()) + ["30_asia_buffer_tags", "31_eur_buffer_tags", "32_us_buffer_tags"])
        for sheet_name,df in sheets.items(): df.to_excel(writer, sheet_name=sheet_name, index=False)
        write_multi_table_sheet(writer, "30_asia_buffer_tags", [(t, make_excel_safe(df)) for t,df in context_summaries["asia_buffer_tag_tables"]]); write_multi_table_sheet(writer, "31_eur_buffer_tags", [(t, make_excel_safe(df)) for t,df in context_summaries["europe_buffer_tag_tables"]]); write_multi_table_sheet(writer, "32_us_buffer_tags", [(t, make_excel_safe(df)) for t,df in context_summaries["us_buffer_tag_tables"]])
        for sheet_name,df in sheets.items(): auto_adjust_and_format(writer, sheet_name, df)

def build_config_sheet(args, raw_row_count: int, daily_count: int, stats: dict, reference_stats: dict, rank_stats: dict) -> pd.DataFrame:
    items={"run_timestamp_utc": format_ts_utc(pd.Timestamp.now(tz=UTC_TZ)),"input_file": args.input,"symbol": args.symbol,"output_xlsx": args.output_xlsx,"raw_row_count": raw_row_count,"complete_daily_rows": daily_count,"version": "V4","title": "US direction and buffered nine-condition relative-structure research with prior-only strength ranks and volume-profile context.","session_times": "Asia 23:00-07:00 UTC | Europe 07:00-15:00 UTC | US 15:00-23:00 UTC","buffer_pct": args.buffer_pct,"context_direction_tolerance": args.context_direction_tolerance,"quality_rank_lookback": args.quality_rank_lookback,"quality_rank_min_history": args.quality_rank_min_history,"volume_profile_bins": args.volume_profile_bins,"value_area_pct": args.value_area_pct,"volume_profile_method": "Equal-width session-relative histogram; contiguous value-area expansion from POC; equal adjacent volumes include both sides.","volume_profile_weight": "Quantity-weighted","volume_profile_internal_precision": "Full precision internally","volume_profile_export_precision": "Four-decimal Excel display","volume_profile_nodes_used": "HVN/LVN not executed","quality_rank_method": "Fixed terciles of prior eligible observations only; current row excluded; no future data; no full-sample thresholds.","note_asia_reference": "Asia reference = immediately preceding completed US session only when exact consecutive session-day continuity exists.","note_europe_reference": "Europe reference = current completed Asia session.","note_profile_sessions": "Asia/Europe/US volume profiles use exactly the trades inside each existing eight-hour session window.","note_profile_temporal_validity": "No future data is used for pre-US profile context.","note_us_logic": "US direction and buffered nine-condition relative-structure methodology with prior-only strength ranks.","removed_legacy_logic": "Removed legacy US V3 outcome labels, legacy US structural extensions, and deprecated combined US structural classification fields."}
    items.update(reference_stats); items.update(rank_stats); items.update(stats)
    return pd.DataFrame({"parameter": list(items.keys()), "value": list(items.values())})


def main() -> None:
    parser=build_arg_parser(); args=parser.parse_args(); validate_args(args); windows=ctx_build_session_windows(args)
    print("Step 1/7: Loading raw trades for V4..."); df=ctx_load_raw_aggtrades(Path(args.input)); raw_row_count=len(df)
    print("Step 2/7: Building daily Asia/Europe/US sessions with volume profiles..."); daily,stats=ctx_build_daily_sessions(df, windows, args.min_trades_per_session, args.volume_profile_bins, args.value_area_pct)
    print("Step 3/7: Adding Asia/Europe primary context model..."); daily=ctx_add_overlap_diagnostics(daily); daily,reference_stats=ctx_add_asia_europe_contexts(daily,args)
    print("Step 4/7: Adding US reference structure, direction, and relative-structure attributes..."); daily=add_us_reference_structure(daily,args); daily=add_us_relative_structure(daily,args)
    print("Step 5/7: Adding Asia/Europe/US quality ranks..."); daily,rank_stats=apply_prior_rolling_ranks(daily,args)
    print("Step 6/7: Building summaries and manual validation samples..."); us_directional=summarize_outcome_groups(daily,["us_direction"]); us_structure=summarize_outcome_groups(daily,["us_relative_structure"]); context_summaries=build_context_summaries(daily); manual_validation=sample_manual_validation_rows(daily); daily_export=build_daily_export(daily); config_df=build_config_sheet(args, raw_row_count, len(daily), stats, reference_stats, rank_stats)
    print("Step 7/7: Exporting V4 workbook..."); export_workbook(Path(args.output_xlsx), config_df, daily_export, us_directional, us_structure, context_summaries, manual_validation); print("Done.")


if __name__ == "__main__":
    main()
