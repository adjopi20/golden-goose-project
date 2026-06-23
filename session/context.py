from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from indicator.volume_profile import build_basic_volume_profile

WIB_TZ = "Asia/Jakarta"
UTC_TZ = "UTC"
UNCLASSIFIED_VALUE_RELATION = "VALUE_UNAVAILABLE"


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
    h, m = value.split(":")
    h_i = int(h)
    m_i = int(m)
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


@dataclass(frozen=True)
class PreNYContext:
    session_day_wib: str
    ny_start_utc: pd.Timestamp
    ny_end_utc: pd.Timestamp
    asia_session_context: str | None
    europe_session_context: str | None
    asia_europe_session_context_combo: str | None
    europe_poc: float
    europe_val: float
    europe_vah: float
    profile_bins: int
    value_area_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


def validate_session_windows(windows: list[SessionWindow]) -> None:
    if len(windows) != 3:
        raise ValueError("Expected exactly three session windows: Asia, Europe, US")
    anchor = pd.Timestamp("2026-01-02 00:00:00", tz=UTC_TZ)
    spans = []
    for window in windows:
        start = anchor + window.start_offset()
        end = anchor + window.end_offset()
        if end <= start:
            raise ValueError(f"Session {window.name} must end after start")
        spans.append((window.name, start, end))
        hours = (end - start).total_seconds() / 3600.0
        if abs(hours - 8.0) > 1e-9:
            raise ValueError(f"Session {window.name} must be exactly 8 hours")
    total_hours = sum((end - start).total_seconds() / 3600.0 for _, start, end in spans)
    if abs(total_hours - 24.0) > 1e-9:
        raise ValueError("Asia, Europe, and US windows must cover exactly 24 hours")
    for idx in range(1, len(spans)):
        if spans[idx - 1][2] != spans[idx][1]:
            raise ValueError("Asia, Europe, and US windows must be contiguous")


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
    validate_session_windows(windows)
    return windows


def load_raw_aggtrades(path: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    columns = pf.schema.names
    ts_col = pick_col(columns, ["timestamp", "event_timestamp", "T", "time", "transact_time"])
    price_col = pick_col(columns, ["price", "p"])
    qty_col = pick_col(columns, ["qty", "quantity", "q"])
    maker_col = pick_col(columns, ["is_buyer_maker", "m"], required=False)
    agg_id_col = pick_col(columns, ["agg_trade_id", "aggregate_trade_id", "a"], required=False)
    read_cols = [ts_col, price_col, qty_col] + ([maker_col] if maker_col else []) + ([agg_id_col] if agg_id_col else [])
    df = pd.read_parquet(path, columns=read_cols)
    df["source_raw_index"] = np.arange(len(df), dtype=np.int64)
    rename_map = {ts_col: "timestamp", price_col: "price", qty_col: "qty"}
    if maker_col:
        rename_map[maker_col] = "is_buyer_maker"
    if agg_id_col:
        rename_map[agg_id_col] = "agg_trade_id"
    df = df.rename(columns=rename_map)
    if "is_buyer_maker" not in df.columns:
        df["is_buyer_maker"] = np.nan
    df["timestamp"] = normalize_timestamp(df["timestamp"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("float64")
    df = df.dropna(subset=["timestamp", "price", "qty"]).copy()
    sort_cols = ["timestamp"]
    if "agg_trade_id" in df.columns:
        df["agg_trade_id"] = pd.to_numeric(df["agg_trade_id"], errors="coerce")
        if df["agg_trade_id"].notna().any():
            sort_cols.extend(["agg_trade_id", "source_raw_index"])
        else:
            sort_cols.append("source_raw_index")
    else:
        sort_cols.append("source_raw_index")
    df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)
    df["raw_index"] = np.arange(len(df), dtype=np.int64)
    df["timestamp_ns"] = timestamp_series_to_ns_array(df["timestamp"])
    df["timestamp_ms"] = (df["timestamp_ns"] // 1_000_000).astype("int64")
    df["notional"] = df["price"] * df["qty"]
    if df["is_buyer_maker"].notna().any():
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        df["aggressor_side"] = np.where(df["is_buyer_maker"], -1, 1)
    else:
        df["aggressor_side"] = np.nan
    if df.empty:
        raise ValueError("Input parquet is empty after cleaning required fields.")
    return df


def build_daily_windows(anchor_utc_date: pd.Timestamp, windows: list[SessionWindow]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {win.name: (anchor_utc_date + win.start_offset(), anchor_utc_date + win.end_offset()) for win in windows}


def aggregate_session_metrics(session_df: pd.DataFrame, prefix: str) -> dict:
    session_df = session_df.sort_values(["timestamp", "raw_index"], kind="mergesort")
    open_, high_, low_, close_ = float(session_df["price"].iloc[0]), float(session_df["price"].max()), float(session_df["price"].min()), float(session_df["price"].iloc[-1])
    range_, body, abs_body = high_ - low_, close_ - open_, abs(close_ - open_)
    return {f"{prefix}_open": open_, f"{prefix}_high": high_, f"{prefix}_low": low_, f"{prefix}_close": close_, f"{prefix}_range": range_, f"{prefix}_range_pct": safe_divide(range_, open_), f"{prefix}_body": body, f"{prefix}_abs_body": abs_body, f"{prefix}_body_pct": safe_divide(body, open_), f"{prefix}_body_to_range": safe_divide(abs_body, range_), f"{prefix}_efficiency": safe_divide(body, range_), f"{prefix}_open_position": safe_divide(open_ - low_, range_), f"{prefix}_close_position": safe_divide(close_ - low_, range_), f"{prefix}_volume": float(session_df["qty"].sum()), f"{prefix}_notional": float(session_df["notional"].sum()), f"{prefix}_trade_count": int(len(session_df)), f"{prefix}_first_trade_timestamp": session_df["timestamp"].iloc[0], f"{prefix}_last_trade_timestamp": session_df["timestamp"].iloc[-1]}


def first_break_timestamp(session_df: pd.DataFrame, high_level: float, low_level: float, high_cmp: Literal["gt", "gte"], low_cmp: Literal["lt", "lte"]) -> tuple[pd.Timestamp | None, pd.Timestamp | None, str]:
    prices = session_df["price"]
    high_hits = session_df.loc[prices > high_level if high_cmp == "gt" else prices >= high_level, "timestamp"]
    low_hits = session_df.loc[prices < low_level if low_cmp == "lt" else prices <= low_level, "timestamp"]
    high_ts = high_hits.iloc[0] if len(high_hits) else None
    low_ts = low_hits.iloc[0] if len(low_hits) else None
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


def build_daily_sessions(df: pd.DataFrame, windows: list[SessionWindow], min_trades_per_session: int, volume_profile_bins: int, value_area_pct: float) -> tuple[pd.DataFrame, dict]:
    dataset_min_ts, dataset_max_ts = df["timestamp"].min(), df["timestamp"].max()
    candidate_days = pd.date_range(start=dataset_min_ts.floor("D"), end=dataset_max_ts.floor("D"), freq="D", tz=UTC_TZ)
    timestamp_ns = df["timestamp_ns"].to_numpy(dtype="int64") if "timestamp_ns" in df.columns else timestamp_series_to_ns_array(df["timestamp"])
    records = []
    dropped_edge_coverage = dropped_missing_session = dropped_min_trades = 0
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
        missing_session = too_few_trades = False
        for name, (start, end) in windows_map.items():
            start_idx = int(np.searchsorted(timestamp_ns, pd.Timestamp(start).value, side="left"))
            end_idx = int(np.searchsorted(timestamp_ns, pd.Timestamp(end).value, side="left"))
            session_df = df.iloc[start_idx:end_idx]
            if session_df.empty:
                missing_session = True
                break
            if len(session_df) < min_trades_per_session:
                too_few_trades = True
                break
            day_record.update(aggregate_session_metrics(session_df, name))
            profile = build_basic_volume_profile(session_df, n_bins=volume_profile_bins, value_area_pct=value_area_pct)
            day_record.update(profile_to_prefixed_fields(profile, name))
            day_record.update(build_own_value_context(day_record, name))
            if name == "europe":
                day_record.update(build_cross_value_context(day_record, "europe", "asia"))
                high_ts, low_ts, seq = first_break_timestamp(session_df, day_record["asia_high"], day_record["asia_low"], "gt", "lt")
                day_record["europe_first_break_asia_high_timestamp"] = high_ts
                day_record["europe_first_break_asia_low_timestamp"] = low_ts
                day_record["europe_first_break_sequence"] = seq
            elif name == "us":
                day_record.update(build_cross_value_context(day_record, "us", "asia"))
                day_record.update(build_cross_value_context(day_record, "us", "europe"))
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
    stats = {"dataset_min_timestamp_utc": format_ts_utc(dataset_min_ts), "dataset_max_timestamp_utc": format_ts_utc(dataset_max_ts), "candidate_days": int(len(candidate_days)), "complete_days": int(len(daily)), "dropped_edge_coverage": int(dropped_edge_coverage), "dropped_missing_session": int(dropped_missing_session), "dropped_min_trades": int(dropped_min_trades)}
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
    daily["asia_structure_state"] = [classify_structure_state(a, b, c, d) for a, b, c, d in zip(daily["asia_high"], daily["asia_low"], daily["asia_reference_upper_buffer"], daily["asia_reference_lower_buffer"])]
    daily.loc[~daily["asia_reference_available"], "asia_structure_state"] = "REFERENCE_UNAVAILABLE"
    daily["asia_context_direction"] = daily["asia_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance))
    daily.loc[~daily["asia_reference_available"], "asia_context_direction"] = pd.NA
    daily["asia_session_context"] = [combine_session_context(a, b) for a, b in zip(daily["asia_structure_state"], daily["asia_context_direction"])]
    daily["asia_high_buffer_tag"] = [classify_high_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["asia_high"], daily["asia_close"], daily["asia_reference_us_high"], daily["asia_reference_upper_buffer"])]
    daily["asia_low_buffer_tag"] = [classify_low_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["asia_low"], daily["asia_close"], daily["asia_reference_us_low"], daily["asia_reference_lower_buffer"])]
    daily["europe_reference_asia_high"] = daily["asia_high"]
    daily["europe_reference_asia_low"] = daily["asia_low"]
    daily["europe_reference_upper_buffer"] = daily["europe_reference_asia_high"] * (1 + args.buffer_pct)
    daily["europe_reference_lower_buffer"] = daily["europe_reference_asia_low"] * (1 - args.buffer_pct)
    daily["europe_structure_state"] = [classify_structure_state(a, b, c, d) for a, b, c, d in zip(daily["europe_high"], daily["europe_low"], daily["europe_reference_upper_buffer"], daily["europe_reference_lower_buffer"])]
    daily["europe_context_direction"] = daily["europe_efficiency"].map(lambda x: classify_context_direction(x, args.context_direction_tolerance))
    daily["europe_session_context"] = [combine_session_context(a, b) for a, b in zip(daily["europe_structure_state"], daily["europe_context_direction"])]
    daily["europe_high_buffer_tag"] = [classify_high_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["europe_high"], daily["europe_close"], daily["europe_reference_asia_high"], daily["europe_reference_upper_buffer"])]
    daily["europe_low_buffer_tag"] = [classify_low_buffer_tag(a, b, c, d) for a, b, c, d in zip(daily["europe_low"], daily["europe_close"], daily["europe_reference_asia_low"], daily["europe_reference_lower_buffer"])]
    daily["asia_europe_session_context_combo"] = np.where(daily["asia_session_context"].notna() & daily["europe_session_context"].notna(), "ASIA_" + daily["asia_session_context"].astype(str) + "__EUROPE_" + daily["europe_session_context"].astype(str), pd.NA)
    return daily, {"rows_without_previous_us_reference": int((~daily["asia_reference_available"]).sum()), "asia_unclassified_count": int((daily["asia_reference_available"] & daily["asia_session_context"].isna()).sum()), "europe_unclassified_count": int(daily["europe_session_context"].isna().sum())}


def build_pre_ny_contexts(daily: pd.DataFrame, profile_bins: int, value_area_pct: float) -> list[PreNYContext]:
    contexts = []
    for _, row in daily.iterrows():
        contexts.append(
            PreNYContext(
                session_day_wib=str(row["session_day_wib"]),
                ny_start_utc=pd.Timestamp(row["us_start_utc"]),
                ny_end_utc=pd.Timestamp(row["us_end_utc"]),
                asia_session_context=row.get("asia_session_context"),
                europe_session_context=row.get("europe_session_context"),
                asia_europe_session_context_combo=row.get("asia_europe_session_context_combo"),
                europe_poc=float(row["europe_poc"]),
                europe_val=float(row["europe_val"]),
                europe_vah=float(row["europe_vah"]),
                profile_bins=int(profile_bins),
                value_area_pct=float(value_area_pct),
            )
        )
    return contexts


def build_pre_ny_daily_contexts(
    df: pd.DataFrame,
    windows: list[SessionWindow],
    min_trades_per_session: int,
    volume_profile_bins: int,
    value_area_pct: float,
    buffer_pct: float,
    context_direction_tolerance: float,
) -> pd.DataFrame:
    dataset_min_ts, dataset_max_ts = df["timestamp"].min(), df["timestamp"].max()
    candidate_days = pd.date_range(start=dataset_min_ts.floor("D"), end=dataset_max_ts.floor("D"), freq="D", tz=UTC_TZ)
    timestamp_ns = df["timestamp_ns"].to_numpy(dtype="int64")
    records: list[dict] = []

    def _slice(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        left = int(np.searchsorted(timestamp_ns, pd.Timestamp(start).value, side="left"))
        right = int(np.searchsorted(timestamp_ns, pd.Timestamp(end).value, side="left"))
        return df.iloc[left:right]

    for anchor_utc_date in candidate_days:
        windows_map = build_daily_windows(anchor_utc_date, windows)
        asia_start, asia_end = windows_map["asia"]
        europe_start, europe_end = windows_map["europe"]
        us_start, us_end = windows_map["us"]
        prev_us_start = us_start - pd.Timedelta(days=1)
        prev_us_end = us_end - pd.Timedelta(days=1)

        if dataset_min_ts > prev_us_start or dataset_max_ts < europe_end:
            continue

        prev_us_df = _slice(prev_us_start, prev_us_end)
        asia_df = _slice(asia_start, asia_end)
        europe_df = _slice(europe_start, europe_end)
        if any(len(x) < min_trades_per_session for x in [prev_us_df, asia_df, europe_df]):
            continue

        row = {
            "session_day_wib": anchor_utc_date.tz_convert(WIB_TZ).strftime("%Y-%m-%d"),
            "anchor_utc_date": anchor_utc_date,
            "asia_start_utc": asia_start,
            "asia_end_utc": asia_end,
            "europe_start_utc": europe_start,
            "europe_end_utc": europe_end,
            "us_start_utc": us_start,
            "us_end_utc": us_end,
        }
        row.update(aggregate_session_metrics(prev_us_df, "us"))
        row.update(aggregate_session_metrics(asia_df, "asia"))
        row.update(aggregate_session_metrics(europe_df, "europe"))
        europe_profile = build_basic_volume_profile(europe_df, n_bins=volume_profile_bins, value_area_pct=value_area_pct)
        row.update(profile_to_prefixed_fields(europe_profile, "europe"))

        row["asia_reference_available"] = True
        row["asia_reference_us_high"] = row["us_high"]
        row["asia_reference_us_low"] = row["us_low"]
        row["asia_reference_upper_buffer"] = row["asia_reference_us_high"] * (1 + buffer_pct)
        row["asia_reference_lower_buffer"] = row["asia_reference_us_low"] * (1 - buffer_pct)
        row["asia_structure_state"] = classify_structure_state(row["asia_high"], row["asia_low"], row["asia_reference_upper_buffer"], row["asia_reference_lower_buffer"])
        row["asia_context_direction"] = classify_context_direction(row["asia_efficiency"], context_direction_tolerance)
        row["asia_session_context"] = combine_session_context(row["asia_structure_state"], row["asia_context_direction"])
        row["europe_reference_asia_high"] = row["asia_high"]
        row["europe_reference_asia_low"] = row["asia_low"]
        row["europe_reference_upper_buffer"] = row["europe_reference_asia_high"] * (1 + buffer_pct)
        row["europe_reference_lower_buffer"] = row["europe_reference_asia_low"] * (1 - buffer_pct)
        row["europe_structure_state"] = classify_structure_state(row["europe_high"], row["europe_low"], row["europe_reference_upper_buffer"], row["europe_reference_lower_buffer"])
        row["europe_context_direction"] = classify_context_direction(row["europe_efficiency"], context_direction_tolerance)
        row["europe_session_context"] = combine_session_context(row["europe_structure_state"], row["europe_context_direction"])
        row["asia_europe_session_context_combo"] = None
        if pd.notna(row["asia_session_context"]) and pd.notna(row["europe_session_context"]):
            row["asia_europe_session_context_combo"] = f"ASIA_{row['asia_session_context']}__EUROPE_{row['europe_session_context']}"
        records.append(row)

    return pd.DataFrame(records)