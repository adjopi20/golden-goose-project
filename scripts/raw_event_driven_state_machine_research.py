from __future__ import annotations

# 1. Imports
import json
import math
import os
import sys
import warnings
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# Add project root to path for terminal execution from scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))


# 2. Config
RAW_AGGTRADES_PATH = "storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2024-06_to_2026-05.parquet"

OUTPUT_DIR = "research/140626/avaxusdc_raw_event_driven_state_machine_0.005risk"

SYMBOL = "AVAXUSDC"

SESSION_START_UTC = "13:30:00"
VALUE_AREA_PCT = 0.70

PROFILE_PRICE_BIN_SIZE = None
PROFILE_PRICE_BIN_METHOD = "auto"

BUBBLE_MODE = "single_aggtrade"
BUBBLE_SIZE_METRIC = "qty"

BUBBLE_Q_MEDIUM = 0.99
BUBBLE_Q_LARGE = 0.995
BUBBLE_Q_EXTREME = 0.999
MIN_BUBBLE_TIER = "medium"

SETUP_REACTION_WINDOW_SECONDS = 30
SETUP_MFE_THRESHOLD_PCT = 0.001
SETUP_EFFICIENCY_THRESHOLD = 0.80

STOP_BUFFER_PCT = 0.005
TP1_R = 1.0

CANDIDATE_TIMEOUT_SECONDS = 900
CONTINUATION_WINDOW_SECONDS = 900
MIN_TRIGGER_SECONDS = 60
CONTINUATION_MIN_TRIGGER_SECONDS = MIN_TRIGGER_SECONDS
RETRACEMENT_INVALIDATION_PCT = 0.50

THRESHOLD_MODE = "rolling_30_session"
THRESHOLD_LOOKBACK_SESSIONS = 30
THRESHOLD_QUANTILES = [0.75, 0.80, 0.90]
PRIMARY_THRESHOLD_QUANTILE = 0.80

FIXED_TRAIN_START = "2024-06-01"
FIXED_TRAIN_END = "2025-06-01"
FIXED_TEST_START = "2025-06-01"
FIXED_TEST_END = "2026-06-01"

RUN_ROBUSTNESS_QUANTILES = True


SESSION_START_HOUR = 13
SESSION_START_MINUTE = 30
TIER_RANK = {"medium": 1, "large": 2, "extreme": 3}

CONFIRMED_BUBBLE_COLUMNS = [
    "confirmed_bubble_id",
    "bubble_id",
    "raw_index",
    "agg_trade_id",
    "bubble_timestamp",
    "confirmation_timestamp",
    "confirmation_raw_index",
    "candidate_start_timestamp",
    "candidate_anchor_price",
    "timestamp",
    "price",
    "qty",
    "notional",
    "aggressive_side",
    "directional_bias",
    "session_id",
    "session_date",
    "previous_session_id",
    "previous_val",
    "previous_vah",
    "previous_poc",
    "bubble_tier",
    "reaction_mfe_pct",
    "reaction_mae_pct",
    "reaction_efficiency",
    "reaction_window_seconds",
    "setup_mfe_threshold_pct",
    "setup_efficiency_threshold",
    "stop_buffer_pct",
]

REACTION_CONFIRMATION_DIAGNOSTIC_COLUMNS = [
    "bubble_id",
    "bubble_timestamp",
    "directional_bias",
    "price",
    "reaction_window_valid",
    "reaction_skip_reason",
    "start_idx",
    "end_idx",
    "raw_len",
    "bubble_ts_ns",
    "end_ns",
    "raw_timestamp_at_start",
    "timestamp_alignment_warning",
    "mfe_pct",
    "mae_pct",
    "efficiency",
    "pass_mfe",
    "pass_efficiency",
    "pass_both",
    "bubble_tier",
    "session_id",
]


# 3. Utility functions
def print_stage(message: str) -> None:
    print(f"\n{'=' * 80}\n{message}\n{'=' * 80}")


def ensure_output_dir(output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)


def parse_session_start(session_start_utc: str) -> tuple[int, int, int]:
    parts = session_start_utc.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SESSION_START_UTC: {session_start_utc}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def infer_decimal_places(value: float) -> int:
    try:
        text = format(float(value), ".12f").rstrip("0").rstrip(".")
        if "." not in text:
            return 0
        return len(text.split(".")[1])
    except Exception:
        return 6


def round_to_sensible_decimal(value: float, min_decimals: int = 0, max_decimals: int = 8) -> float:
    if value <= 0:
        raise ValueError(f"Bin size must be positive, got {value}")
    decimals = min(max(int(math.ceil(-math.log10(value))) + 2, min_decimals), max_decimals)
    rounded = round(float(value), decimals)
    if rounded <= 0:
        rounded = value
    return float(rounded)


def safe_efficiency(mfe_pct: float, mae_pct: float) -> float:
    denom = float(mfe_pct) + float(mae_pct)
    if denom <= 0:
        return 0.0
    return float(mfe_pct) / denom


def direction_adjusted_move_pct(direction: str, anchor_price: float, current_price: float) -> float:
    if direction == "long":
        return (current_price - anchor_price) / anchor_price
    return (anchor_price - current_price) / anchor_price


def direction_adjusted_r(direction: str, anchor_price: float, current_price: float, risk_abs: float) -> float:
    if risk_abs <= 0:
        return 0.0
    if direction == "long":
        return (current_price - anchor_price) / risk_abs
    return (anchor_price - current_price) / risk_abs

def normalize_timestamp_column(s: pd.Series) -> pd.Series:
    """
    Normalize timestamp column to UTC datetime.

    Handles:
    - already datetime-like
    - numeric Binance timestamps in ms/us/ns
    - numeric strings
    """

    if pd.api.types.is_datetime64_any_dtype(s):
        return pd.to_datetime(s, utc=True)

    numeric = pd.to_numeric(s, errors="coerce")

    if numeric.isna().all():
        # fallback for ISO-like strings
        return pd.to_datetime(s, utc=True, errors="raise")

    median_abs = float(numeric.dropna().abs().median())

    # Typical epoch magnitudes:
    # seconds:      ~1.7e9
    # milliseconds: ~1.7e12
    # microseconds: ~1.7e15
    # nanoseconds:  ~1.7e18

    if median_abs >= 1e18:
        unit = "ns"
    elif median_abs >= 1e15:
        unit = "us"
    elif median_abs >= 1e12:
        unit = "ms"
    elif median_abs >= 1e9:
        unit = "s"
    else:
        raise ValueError(
            f"Timestamp magnitude too small/unknown: median_abs={median_abs}. "
            "Inspect raw timestamp column manually."
        )

    print(f"Detected numeric timestamp unit: {unit} | median_abs={median_abs:,.0f}")

    out = pd.to_datetime(numeric, unit=unit, utc=True, errors="raise")

    # sanity check
    min_ts = out.min()
    max_ts = out.max()
    if min_ts.year < 2017 or max_ts.year > 2035:
        raise ValueError(
            f"Suspicious parsed timestamp range: {min_ts} → {max_ts}. "
            f"Detected unit={unit}. Check timestamp unit."
        )

    return out

def first_index_at_or_after(timestamps_ns: np.ndarray, ts_ns: int) -> int:
    return int(np.searchsorted(timestamps_ns, ts_ns, side="left"))


def first_index_strictly_after(timestamps_ns: np.ndarray, ts_ns: int) -> int:
    return int(np.searchsorted(timestamps_ns, ts_ns, side="right"))


def window_indices(timestamps_ns: np.ndarray, start_ns: int, end_ns: int, include_start: bool = True) -> tuple[int, int]:
    left_side = "left" if include_start else "right"
    start_idx = int(np.searchsorted(timestamps_ns, start_ns, side=left_side))
    end_idx = int(np.searchsorted(timestamps_ns, end_ns, side="right"))
    return start_idx, end_idx


def qty_quantile(values: np.ndarray, q: float) -> float:
    if len(values) == 0:
        return float("nan")
    return float(np.quantile(values, q, method="linear"))


def pick_close_index(timestamps_ns: np.ndarray, requested_ns: int) -> int:
    idx = int(np.searchsorted(timestamps_ns, requested_ns, side="left"))
    return min(idx, len(timestamps_ns) - 1)


def price_to_bin_index(price: float, bin_size: float) -> int:
    return int(math.floor(float(price) / float(bin_size)))


def bin_index_to_bounds(bin_index: int, bin_size: float) -> tuple[float, float]:
    low = float(bin_index) * float(bin_size)
    high = low + float(bin_size)
    return low, high


def compute_value_area_from_bins(bin_indices: np.ndarray, volumes: np.ndarray, value_area_pct: float) -> tuple[float, float, float]:
    if len(bin_indices) == 0:
        raise ValueError("Cannot compute value area from empty profile")

    total_volume = float(volumes.sum())
    poc_pos = int(np.argmax(volumes))
    poc_bin_index = int(bin_indices[poc_pos])
    target_volume = total_volume * float(value_area_pct)

    included_positions = {poc_pos}
    cumulative = float(volumes[poc_pos])
    left = poc_pos - 1
    right = poc_pos + 1

    while cumulative < target_volume and (left >= 0 or right < len(bin_indices)):
        left_vol = float(volumes[left]) if left >= 0 else -1.0
        right_vol = float(volumes[right]) if right < len(bin_indices) else -1.0
        if left_vol >= right_vol:
            if left >= 0:
                included_positions.add(left)
                cumulative += float(volumes[left])
                left -= 1
            elif right < len(bin_indices):
                included_positions.add(right)
                cumulative += float(volumes[right])
                right += 1
        else:
            if right < len(bin_indices):
                included_positions.add(right)
                cumulative += float(volumes[right])
                right += 1
            elif left >= 0:
                included_positions.add(left)
                cumulative += float(volumes[left])
                left -= 1

    low_pos = min(included_positions)
    high_pos = max(included_positions)
    return float(poc_bin_index), float(bin_indices[low_pos]), float(bin_indices[high_pos])


def infer_profile_bin_size(prices: pd.Series) -> float:
    prices_arr = prices.astype(float).to_numpy()
    if len(prices_arr) < 2:
        raise ValueError("Need at least two prices to infer bin size")

    unique_prices = np.unique(prices_arr)
    diffs = np.diff(unique_prices)
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        raise ValueError("Could not infer bin size from constant-price dataset")

    rounded_text_counts: dict[str, int] = {}
    for diff in diffs[: min(len(diffs), 200_000)]:
        try:
            normalized = Decimal(str(diff)).normalize()
            text = format(normalized, "f")
        except (InvalidOperation, ValueError):
            text = format(float(diff), ".8f").rstrip("0").rstrip(".")
        rounded_text_counts[text] = rounded_text_counts.get(text, 0) + 1

    common_text, common_count = max(rounded_text_counts.items(), key=lambda x: (x[1], -len(x[0])))
    common_diff = float(common_text)
    median_diff = float(np.median(diffs))
    chosen = common_diff if common_count >= 3 else median_diff
    chosen = round_to_sensible_decimal(chosen)
    print(f"Selected profile bin size = {chosen} using {PROFILE_PRICE_BIN_METHOD} method (common_diff={common_diff}, median_diff={median_diff})")
    return chosen


def export_dataframe(df: pd.DataFrame, parquet_path: str, csv_path: str | None = None) -> None:
    df.to_parquet(parquet_path, index=False)
    if csv_path is not None:
        df.to_csv(csv_path, index=False)


def summarize_final_r(trades_only: pd.DataFrame) -> dict[str, float]:
    if trades_only.empty:
        return {
            "entered_trades": 0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "mean_final_R": 0.0,
            "median_final_R": 0.0,
            "total_R": 0.0,
            "profit_factor": 0.0,
            "mean_max_R_before_close": 0.0,
            "median_max_R_before_close": 0.0,
        }
    wins = trades_only[trades_only["final_R"] > 0]
    losses = trades_only[trades_only["final_R"] < 0]
    gross_profit = float(wins["final_R"].sum()) if not wins.empty else 0.0
    gross_loss = float((-losses["final_R"]).sum()) if not losses.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    return {
        "entered_trades": int(len(trades_only)),
        "win_rate": float((trades_only["final_R"] > 0).mean()),
        "loss_rate": float((trades_only["final_R"] < 0).mean()),
        "mean_final_R": float(trades_only["final_R"].mean()),
        "median_final_R": float(trades_only["final_R"].median()),
        "total_R": float(trades_only["final_R"].sum()),
        "profit_factor": float(profit_factor),
        "mean_max_R_before_close": float(trades_only["max_R_before_close"].mean()),
        "median_max_R_before_close": float(trades_only["max_R_before_close"].median()),
    }


@dataclass
class Diagnostics:
    missing_previous_profile_count: int = 0
    incomplete_reaction_window_count: int = 0
    incomplete_calibration_window_count: int = 0
    ambiguous_touch_sequence_count: int = 0
    end_of_data_open_trade_count: int = 0
    insufficient_calibration_history_count: int = 0
    warmup_session_count: int = 0


def combine_diagnostics(*diagnostics_objects: Diagnostics) -> Diagnostics:
    combined = Diagnostics()
    for obj in diagnostics_objects:
        combined.missing_previous_profile_count += obj.missing_previous_profile_count
        combined.incomplete_reaction_window_count += obj.incomplete_reaction_window_count
        combined.incomplete_calibration_window_count += obj.incomplete_calibration_window_count
        combined.ambiguous_touch_sequence_count += obj.ambiguous_touch_sequence_count
        combined.end_of_data_open_trade_count += obj.end_of_data_open_trade_count
        combined.insufficient_calibration_history_count += obj.insufficient_calibration_history_count
        combined.warmup_session_count += obj.warmup_session_count
    return combined


# 4. Load raw aggTrades
def load_raw_aggtrades(path: str) -> pd.DataFrame:
    print_stage("4. Load raw aggTrades")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Raw aggTrades parquet not found: {path}")
    raw = pd.read_parquet(path)
    print(f"Loaded raw parquet: {path}")
    print(f"Raw rows loaded: {len(raw):,}")
    return raw


# 5. Normalize schema/timestamps
def normalize_raw_schema(raw: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    print_stage("5. Normalize schema/timestamps")
    expected_cols = ["timestamp", "price", "qty", "is_buyer_maker", "agg_trade_id"]
    missing_required = [c for c in ["timestamp", "price", "qty", "is_buyer_maker"] if c not in raw.columns]
    if missing_required:
        raise ValueError(f"Missing required raw aggTrade columns: {missing_required}")

    raw = raw.copy()
    raw["__original_row_index"] = np.arange(len(raw), dtype=np.int64)
    raw["timestamp"] = normalize_timestamp_column(raw["timestamp"])
    raw["price"] = pd.to_numeric(raw["price"], errors="raise").astype(np.float64)
    raw["qty"] = pd.to_numeric(raw["qty"], errors="raise").astype(np.float64)
    raw["is_buyer_maker"] = raw["is_buyer_maker"].astype(bool)
    raw["notional"] = raw["price"] * raw["qty"]
    raw["aggressive_side"] = np.where(~raw["is_buyer_maker"], "buy", "sell")

    agg_trade_id_missing = "agg_trade_id" not in raw.columns
    if agg_trade_id_missing:
        warnings.warn("agg_trade_id missing; sorting by timestamp and original row index instead.")
        print("WARNING: agg_trade_id missing; sorting by timestamp and original row index instead.")
        raw = raw.sort_values(["timestamp", "__original_row_index"], kind="mergesort").reset_index(drop=True)
    else:
        raw["agg_trade_id"] = pd.to_numeric(raw["agg_trade_id"], errors="coerce")
        if raw["agg_trade_id"].isna().any():
            warnings.warn("agg_trade_id has missing values; filling with original row index fallback for deterministic sorting.")
            print("WARNING: agg_trade_id has missing values; filling with original row index fallback for deterministic sorting.")
            raw["agg_trade_id"] = raw["agg_trade_id"].fillna(raw["__original_row_index"])
        raw["agg_trade_id"] = raw["agg_trade_id"].astype(np.int64)
        raw = raw.sort_values(["timestamp", "agg_trade_id"], kind="mergesort").reset_index(drop=True)

    raw["raw_index"] = np.arange(len(raw), dtype=np.int64)

    print(f"First raw timestamp: {raw['timestamp'].iloc[0]}")
    print(f"Last raw timestamp:  {raw['timestamp'].iloc[-1]}")
    print(f"Expected raw columns present: {[c for c in expected_cols if c in raw.columns]}")
    return raw, agg_trade_id_missing


# 6. Build sessions
def build_sessions(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int], np.ndarray]:
    print_stage("6. Build sessions")
    start_hour, start_minute, start_second = parse_session_start(SESSION_START_UTC)
    offset = pd.Timedelta(hours=start_hour, minutes=start_minute, seconds=start_second)
    shifted = raw["timestamp"] - offset
    session_date = shifted.dt.floor("D")
    session_start = session_date + offset
    session_end = session_start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    raw = raw.copy()
    raw["session_date"] = session_date.dt.date.astype(str)
    raw["session_id"] = raw["session_date"]
    raw["session_start"] = session_start
    raw["session_end"] = session_end

    unique_sessions = pd.DataFrame({
        "session_id": raw["session_id"],
        "session_date": raw["session_date"],
        "session_start": raw["session_start"],
        "session_end": raw["session_end"],
    }).drop_duplicates(subset=["session_id"]).sort_values("session_start").reset_index(drop=True)
    unique_sessions["previous_session_id"] = unique_sessions["session_id"].shift(1)
    session_to_pos = {sid: i for i, sid in enumerate(unique_sessions["session_id"].tolist())}
    raw["previous_session_id"] = raw["session_id"].map(
        unique_sessions.set_index("session_id")["previous_session_id"].to_dict()
    )
    session_positions = raw["session_id"].map(session_to_pos).to_numpy(dtype=np.int64)
    print(f"Session count: {len(unique_sessions):,}")
    return raw, unique_sessions, session_to_pos, session_positions


# 7. Build previous-session volume profiles
def build_session_profiles(raw: pd.DataFrame, sessions_df: pd.DataFrame, bin_size: float) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    print_stage("7. Build previous-session volume profiles")
    profiles: list[dict[str, Any]] = []
    profile_lookup: dict[str, dict[str, Any]] = {}

    grouped = raw.groupby("session_id", sort=False, observed=True)
    for idx, session_row in sessions_df.iterrows():
        session_id = str(session_row["session_id"])
        if session_id not in grouped.groups:
            continue
        session_trades = raw.iloc[grouped.groups[session_id]]
        prices = session_trades["price"].to_numpy(dtype=np.float64)
        qty = session_trades["qty"].to_numpy(dtype=np.float64)
        bin_indices = np.floor(prices / bin_size).astype(np.int64)
        uniq_bins, inverse = np.unique(bin_indices, return_inverse=True)
        volumes = np.bincount(inverse, weights=qty).astype(np.float64)
        poc_bin_index, val_bin_index, vah_bin_index = compute_value_area_from_bins(uniq_bins, volumes, VALUE_AREA_PCT)
        poc_low, poc_high = bin_index_to_bounds(int(poc_bin_index), bin_size)
        val_low, _ = bin_index_to_bounds(int(val_bin_index), bin_size)
        _, vah_high = bin_index_to_bounds(int(vah_bin_index), bin_size)
        row = {
            "session_id": session_id,
            "session_date": session_row["session_date"],
            "session_start": session_row["session_start"],
            "session_end": session_row["session_end"],
            "poc": (poc_low + poc_high) / 2.0,
            "val": val_low,
            "vah": vah_high,
            "total_qty": float(qty.sum()),
            "trade_count": int(len(session_trades)),
            "profile_bin_size": float(bin_size),
            "profile_complete": True,
        }
        profiles.append(row)
        profile_lookup[session_id] = row
        if (idx + 1) % 100 == 0:
            print(f"Built profiles for {idx + 1:,} sessions...")

    session_profiles = pd.DataFrame(profiles)
    print(f"Session profile count: {len(session_profiles):,}")
    return session_profiles, profile_lookup


def attach_previous_profiles(raw: pd.DataFrame, profile_lookup: dict[str, dict[str, Any]]) -> pd.DataFrame:
    prev_val = []
    prev_vah = []
    prev_poc = []
    for prev_session_id in raw["previous_session_id"].tolist():
        if pd.isna(prev_session_id) or prev_session_id not in profile_lookup:
            prev_val.append(np.nan)
            prev_vah.append(np.nan)
            prev_poc.append(np.nan)
        else:
            profile = profile_lookup[str(prev_session_id)]
            prev_val.append(profile["val"])
            prev_vah.append(profile["vah"])
            prev_poc.append(profile["poc"])
    raw = raw.copy()
    raw["previous_val"] = np.array(prev_val, dtype=np.float64)
    raw["previous_vah"] = np.array(prev_vah, dtype=np.float64)
    raw["previous_poc"] = np.array(prev_poc, dtype=np.float64)
    return raw


# 8. Detect aggressive bubbles
def compute_previous_session_bubble_thresholds(raw: pd.DataFrame, sessions_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict[str, float]]]:
    print_stage("8. Detect aggressive bubbles")
    qty_threshold_rows: list[dict[str, Any]] = []
    threshold_lookup: dict[str, dict[str, float]] = {}
    grouped = raw.groupby("session_id", sort=False, observed=True)

    for _, row in sessions_df.iterrows():
        session_id = str(row["session_id"])
        previous_session_id = row["previous_session_id"]
        if pd.isna(previous_session_id) or str(previous_session_id) not in grouped.groups:
            threshold_lookup[session_id] = {
                "medium": float("nan"),
                "large": float("nan"),
                "extreme": float("nan"),
            }
            qty_threshold_rows.append({
                "session_id": session_id,
                "previous_session_id": previous_session_id,
                "bubble_threshold_medium": float("nan"),
                "bubble_threshold_large": float("nan"),
                "bubble_threshold_extreme": float("nan"),
            })
            continue

        previous_qty = raw.iloc[grouped.groups[str(previous_session_id)]]["qty"].to_numpy(dtype=np.float64)
        medium = qty_quantile(previous_qty, BUBBLE_Q_MEDIUM)
        large = qty_quantile(previous_qty, BUBBLE_Q_LARGE)
        extreme = qty_quantile(previous_qty, BUBBLE_Q_EXTREME)
        threshold_lookup[session_id] = {"medium": medium, "large": large, "extreme": extreme}
        qty_threshold_rows.append({
            "session_id": session_id,
            "previous_session_id": previous_session_id,
            "bubble_threshold_medium": medium,
            "bubble_threshold_large": large,
            "bubble_threshold_extreme": extreme,
        })

    threshold_df = pd.DataFrame(qty_threshold_rows)
    return threshold_df, threshold_lookup


def classify_bubble_tier(qty: float, thresholds: dict[str, float]) -> str | None:
    if not np.isfinite(thresholds["medium"]):
        return None
    if qty >= thresholds["extreme"]:
        return "extreme"
    if qty >= thresholds["large"]:
        return "large"
    if qty >= thresholds["medium"]:
        return "medium"
    return None


def detect_qualified_bubbles(raw: pd.DataFrame, threshold_lookup: dict[str, dict[str, float]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    min_rank = TIER_RANK[MIN_BUBBLE_TIER]
    bubble_id = 1
    for row in raw.itertuples(index=False):
        thresholds = threshold_lookup.get(str(row.session_id))
        if thresholds is None:
            continue
        tier = classify_bubble_tier(float(row.qty), thresholds)
        if tier is None or TIER_RANK[tier] < min_rank:
            continue
        rows.append({
            "bubble_id": bubble_id,
            "raw_index": int(row.raw_index),
            "agg_trade_id": int(row.agg_trade_id) if hasattr(row, "agg_trade_id") else np.nan,
            "bubble_timestamp": row.timestamp,
            "timestamp": row.timestamp,
            "price": float(row.price),
            "qty": float(row.qty),
            "notional": float(row.notional),
            "aggressive_side": row.aggressive_side,
            "bubble_tier": tier,
            "bubble_threshold_medium": float(thresholds["medium"]),
            "bubble_threshold_large": float(thresholds["large"]),
            "bubble_threshold_extreme": float(thresholds["extreme"]),
            "session_id": row.session_id,
            "session_date": row.session_date,
            "previous_session_id": row.previous_session_id,
            "previous_val": float(row.previous_val) if pd.notna(row.previous_val) else np.nan,
            "previous_vah": float(row.previous_vah) if pd.notna(row.previous_vah) else np.nan,
            "previous_poc": float(row.previous_poc) if pd.notna(row.previous_poc) else np.nan,
        })
        bubble_id += 1
    qualified_bubbles = pd.DataFrame(rows)
    print(f"Qualified bubble count: {len(qualified_bubbles):,}")
    return qualified_bubbles


# 9. Apply outside-value filter
def apply_outside_value_filter(qualified_bubbles: pd.DataFrame, diagnostics: Diagnostics) -> pd.DataFrame:
    print_stage("9. Apply outside-value filter")
    rows: list[dict[str, Any]] = []
    for row in qualified_bubbles.itertuples(index=False):
        if pd.isna(row.previous_val) or pd.isna(row.previous_vah):
            diagnostics.missing_previous_profile_count += 1
            continue
        directional_bias: str | None = None
        if row.price > row.previous_vah and row.aggressive_side == "buy":
            directional_bias = "long"
        elif row.price < row.previous_val and row.aggressive_side == "sell":
            directional_bias = "short"
        if directional_bias is None:
            continue
        d = row._asdict()
        d["directional_bias"] = directional_bias
        rows.append(d)
    outside_value_bubbles = pd.DataFrame(rows)
    print(f"Outside-value bubble count: {len(outside_value_bubbles):,}")
    return outside_value_bubbles


# 10. Confirm bubbles using reaction MFE + efficiency
def confirm_bubbles(
    outside_value_bubbles: pd.DataFrame,
    raw: pd.DataFrame,
    diagnostics: Diagnostics,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    print_stage("10. Confirm bubbles using reaction MFE + efficiency")
    timestamps_ns = pd.to_datetime(raw["timestamp"], utc=True).dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
    prices = raw["price"].to_numpy(dtype=np.float64)
    confirmed_rows: list[dict[str, Any]] = []
    reaction_diagnostic_rows: list[dict[str, Any]] = []
    confirmed_bubble_id = 1
    reaction_window_ns = int(pd.Timedelta(seconds=SETUP_REACTION_WINDOW_SECONDS).value)

    print(f"outside_value_bubbles input count: {len(outside_value_bubbles):,}")
    print(f"outside_value_bubbles columns: {list(outside_value_bubbles.columns)}")
    sample_cols = [
        c for c in ["bubble_id", "timestamp", "bubble_timestamp", "price", "directional_bias"]
        if c in outside_value_bubbles.columns
    ]
    if sample_cols:
        print(outside_value_bubbles[sample_cols].head(5).to_string(index=False))
    if len(timestamps_ns) > 0:
        print(f"raw timestamp min: {pd.Timestamp(timestamps_ns[0])}")
        print(f"raw timestamp max: {pd.Timestamp(timestamps_ns[-1])}")
    print(f"raw len: {len(timestamps_ns):,}")

    for bubble in outside_value_bubbles.itertuples(index=False):
        bubble_ts_ns = int(pd.Timestamp(bubble.timestamp).value)
        end_ns = bubble_ts_ns + reaction_window_ns
        start_idx = int(bubble.raw_index)
        end_idx = int(np.searchsorted(timestamps_ns, end_ns, side="right"))

        raw_timestamp_at_start = pd.NaT
        timestamp_alignment_warning = None
        if 0 <= start_idx < len(timestamps_ns):
            raw_timestamp_at_start = pd.Timestamp(timestamps_ns[start_idx])
            if int(timestamps_ns[start_idx]) != bubble_ts_ns:
                timestamp_alignment_warning = "raw_index_timestamp_mismatch"

        diagnostic_base = {
            "bubble_id": bubble.bubble_id,
            "bubble_timestamp": bubble.bubble_timestamp,
            "directional_bias": bubble.directional_bias,
            "price": bubble.price,
            "reaction_window_valid": False,
            "reaction_skip_reason": None,
            "start_idx": int(start_idx),
            "end_idx": int(end_idx),
            "raw_len": int(len(timestamps_ns)),
            "bubble_ts_ns": int(bubble_ts_ns),
            "end_ns": int(end_ns),
            "raw_timestamp_at_start": raw_timestamp_at_start,
            "timestamp_alignment_warning": timestamp_alignment_warning,
            "mfe_pct": np.nan,
            "mae_pct": np.nan,
            "efficiency": np.nan,
            "pass_mfe": False,
            "pass_efficiency": False,
            "pass_both": False,
            "bubble_tier": bubble.bubble_tier,
            "session_id": bubble.session_id,
        }

        if start_idx < 0 or start_idx >= len(timestamps_ns):
            diagnostics.incomplete_reaction_window_count += 1
            diagnostic = diagnostic_base.copy()
            diagnostic["reaction_skip_reason"] = "invalid_raw_index"
            reaction_diagnostic_rows.append(diagnostic)
            continue

        if end_idx <= start_idx:
            diagnostics.incomplete_reaction_window_count += 1
            diagnostic = diagnostic_base.copy()
            diagnostic["reaction_skip_reason"] = "empty_window_end_idx_lte_start_idx"
            reaction_diagnostic_rows.append(diagnostic)
            continue

        window_prices = prices[start_idx:end_idx]
        if len(window_prices) == 0:
            diagnostics.incomplete_reaction_window_count += 1
            diagnostic = diagnostic_base.copy()
            diagnostic["reaction_skip_reason"] = "empty_window_prices"
            reaction_diagnostic_rows.append(diagnostic)
            continue

        if end_ns > int(timestamps_ns[-1]):
            diagnostics.incomplete_reaction_window_count += 1

        max_price = float(window_prices.max())
        min_price = float(window_prices.min())
        if bubble.directional_bias == "long":
            mfe_pct = (max_price - bubble.price) / bubble.price
            mae_pct = (bubble.price - min_price) / bubble.price
        else:
            mfe_pct = (bubble.price - min_price) / bubble.price
            mae_pct = (max_price - bubble.price) / bubble.price
        efficiency = safe_efficiency(mfe_pct, mae_pct)
        pass_mfe = bool(mfe_pct >= SETUP_MFE_THRESHOLD_PCT)
        pass_efficiency = bool(efficiency >= SETUP_EFFICIENCY_THRESHOLD)
        pass_both = bool(pass_mfe and pass_efficiency)
        diagnostic = diagnostic_base.copy()
        diagnostic.update({
            "reaction_window_valid": True,
            "reaction_skip_reason": None,
            "mfe_pct": float(mfe_pct),
            "mae_pct": float(mae_pct),
            "efficiency": float(efficiency),
            "pass_mfe": pass_mfe,
            "pass_efficiency": pass_efficiency,
            "pass_both": pass_both,
        })
        reaction_diagnostic_rows.append(diagnostic)

        if pass_both:
            confirmation_raw_index = first_index_at_or_after(timestamps_ns, end_ns)
            if confirmation_raw_index >= len(timestamps_ns):
                diagnostics.incomplete_reaction_window_count += 1
                continue
            confirmation_timestamp = pd.Timestamp(timestamps_ns[confirmation_raw_index])
            candidate_anchor_price = float(prices[confirmation_raw_index])
            confirmed_rows.append({
                "confirmed_bubble_id": confirmed_bubble_id,
                "bubble_id": bubble.bubble_id,
                "raw_index": int(bubble.raw_index),
                "agg_trade_id": bubble.agg_trade_id,
                "bubble_timestamp": bubble.bubble_timestamp,
                "confirmation_timestamp": confirmation_timestamp,
                "confirmation_raw_index": int(confirmation_raw_index),
                "candidate_start_timestamp": confirmation_timestamp,
                "candidate_anchor_price": candidate_anchor_price,
                "timestamp": bubble.timestamp,
                "price": bubble.price,
                "qty": bubble.qty,
                "notional": bubble.notional,
                "aggressive_side": bubble.aggressive_side,
                "directional_bias": bubble.directional_bias,
                "session_id": bubble.session_id,
                "session_date": bubble.session_date,
                "previous_session_id": bubble.previous_session_id,
                "previous_val": bubble.previous_val,
                "previous_vah": bubble.previous_vah,
                "previous_poc": bubble.previous_poc,
                "bubble_tier": bubble.bubble_tier,
                "reaction_mfe_pct": float(mfe_pct),
                "reaction_mae_pct": float(mae_pct),
                "reaction_efficiency": float(efficiency),
                "reaction_window_seconds": SETUP_REACTION_WINDOW_SECONDS,
                "setup_mfe_threshold_pct": SETUP_MFE_THRESHOLD_PCT,
                "setup_efficiency_threshold": SETUP_EFFICIENCY_THRESHOLD,
                "stop_buffer_pct": STOP_BUFFER_PCT,
            })
            confirmed_bubble_id += 1

    reaction_diagnostics = pd.DataFrame(
        reaction_diagnostic_rows,
        columns=REACTION_CONFIRMATION_DIAGNOSTIC_COLUMNS,
    )

    print("\nReaction confirmation diagnostics")
    print(f"outside_value_input_count: {len(outside_value_bubbles):,}")
    print(f"reaction_diagnostic_count: {len(reaction_diagnostics):,}")
    if reaction_diagnostics.empty:
        print("pass_mfe_count: 0")
        print("pass_efficiency_count: 0")
        print("pass_both_count: 0")
        print("reaction_skip_reason counts: none")
        print("\nMFE distribution")
        print("No reaction diagnostics available.")
        print("\nEfficiency distribution")
        print("No reaction diagnostics available.")
        print("\nBy direction")
        print("No reaction diagnostics available.")
    else:
        print(reaction_diagnostics["reaction_skip_reason"].value_counts(dropna=False).to_string())
        valid_reaction_diagnostics = reaction_diagnostics[reaction_diagnostics["reaction_window_valid"] == True].copy()

        print(f"pass_mfe_count: {valid_reaction_diagnostics['pass_mfe'].sum():,}")
        print(f"pass_efficiency_count: {valid_reaction_diagnostics['pass_efficiency'].sum():,}")
        print(f"pass_both_count: {valid_reaction_diagnostics['pass_both'].sum():,}")

        print("\nMFE distribution")
        if valid_reaction_diagnostics.empty:
            print("No valid reaction diagnostics available.")
        else:
            print(valid_reaction_diagnostics["mfe_pct"].describe(percentiles=[0.5, 0.75, 0.9, 0.95, 0.99]).to_string())

        print("\nEfficiency distribution")
        if valid_reaction_diagnostics.empty:
            print("No valid reaction diagnostics available.")
        else:
            print(valid_reaction_diagnostics["efficiency"].describe(percentiles=[0.5, 0.75, 0.9, 0.95, 0.99]).to_string())

        print("\nBy direction")
        if valid_reaction_diagnostics.empty:
            print("No valid reaction diagnostics available.")
        else:
            print(
                valid_reaction_diagnostics.groupby("directional_bias")
                .agg(
                    count=("bubble_id", "count"),
                    median_mfe=("mfe_pct", "median"),
                    p90_mfe=("mfe_pct", lambda x: x.quantile(0.90)),
                    p99_mfe=("mfe_pct", lambda x: x.quantile(0.99)),
                    median_efficiency=("efficiency", "median"),
                    pass_mfe_rate=("pass_mfe", "mean"),
                    pass_efficiency_rate=("pass_efficiency", "mean"),
                    pass_both_rate=("pass_both", "mean"),
                )
                .to_string()
            )

    confirmed = pd.DataFrame(confirmed_rows, columns=CONFIRMED_BUBBLE_COLUMNS)
    print(f"Confirmed bubble count: {len(confirmed):,}")
    return confirmed, reaction_diagnostics


# 11. Build ungrouped confirmed bubble stream
def build_confirmed_bubble_stream(confirmed_bubbles: pd.DataFrame) -> pd.DataFrame:
    print_stage("11. Build ungrouped confirmed bubble stream")
    if confirmed_bubbles.empty:
        return confirmed_bubbles.reindex(columns=CONFIRMED_BUBBLE_COLUMNS)
    confirmed_bubbles = confirmed_bubbles.sort_values(
        ["confirmation_timestamp", "confirmation_raw_index", "confirmed_bubble_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    return confirmed_bubbles


# 12. Build calibration feature table
def build_calibration_feature_table(confirmed_bubbles: pd.DataFrame, raw: pd.DataFrame, diagnostics: Diagnostics) -> pd.DataFrame:
    print_stage("12. Build calibration feature table")
    timestamps_ns = pd.to_datetime(raw["timestamp"], utc=True).dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
    prices = raw["price"].to_numpy(dtype=np.float64)
    rows: list[dict[str, Any]] = []
    min_trigger_ns = int(pd.Timedelta(seconds=MIN_TRIGGER_SECONDS).value)
    timeout_ns = int(pd.Timedelta(seconds=CANDIDATE_TIMEOUT_SECONDS).value)

    for bubble in confirmed_bubbles.itertuples(index=False):
        anchor_ts_ns = int(pd.Timestamp(bubble.candidate_start_timestamp).value)
        start_ns = anchor_ts_ns
        check_start_ns = anchor_ts_ns + min_trigger_ns
        end_ns = anchor_ts_ns + timeout_ns
        start_idx = first_index_at_or_after(timestamps_ns, start_ns)
        check_start_idx = first_index_at_or_after(timestamps_ns, check_start_ns)
        end_idx = int(np.searchsorted(timestamps_ns, end_ns, side="right"))
        if start_idx >= len(timestamps_ns) or end_idx <= start_idx or check_start_idx >= end_idx:
            diagnostics.incomplete_calibration_window_count += 1
            continue
        if end_ns > int(timestamps_ns[-1]):
            diagnostics.incomplete_calibration_window_count += 1
        anchor_price = float(bubble.candidate_anchor_price)
        candidate_r_abs = anchor_price * STOP_BUFFER_PCT
        max_net = -np.inf
        max_range = -np.inf
        max_activity = -np.inf
        window_complete = end_ns <= int(timestamps_ns[-1])
        running_max_price = -np.inf
        running_min_price = np.inf

        for idx in range(start_idx, end_idx):
            current_price = float(prices[idx])
            running_max_price = max(running_max_price, current_price)
            running_min_price = min(running_min_price, current_price)
            elapsed_seconds = max((int(timestamps_ns[idx]) - anchor_ts_ns) / 1_000_000_000, 1e-9)
            net_migration_r = direction_adjusted_r(bubble.directional_bias, anchor_price, current_price, candidate_r_abs)
            price_range_r = (running_max_price - running_min_price) / candidate_r_abs if candidate_r_abs > 0 else 0.0
            trade_activity_rate = (idx - start_idx + 1) / elapsed_seconds
            if idx >= check_start_idx:
                max_net = max(max_net, float(net_migration_r))
                max_range = max(max_range, float(price_range_r))
                max_activity = max(max_activity, float(trade_activity_rate))

        rows.append({
            "confirmed_bubble_id": bubble.confirmed_bubble_id,
            "timestamp": bubble.timestamp,
            "bubble_timestamp": bubble.bubble_timestamp,
            "confirmation_timestamp": bubble.confirmation_timestamp,
            "candidate_start_timestamp": bubble.candidate_start_timestamp,
            "session_id": bubble.session_id,
            "session_date": bubble.session_date,
            "directional_bias": bubble.directional_bias,
            "candidate_anchor_price": anchor_price,
            "candidate_R": candidate_r_abs,
            "calibration_max_net_migration_R": float(max_net if np.isfinite(max_net) else 0.0),
            "calibration_max_price_range_R": float(max_range if np.isfinite(max_range) else 0.0),
            "calibration_max_trade_activity_rate": float(max_activity if np.isfinite(max_activity) else 0.0),
            "calibration_trade_count": int(max(end_idx - start_idx, 0)),
            "calibration_window_complete": bool(window_complete),
        })

    calibration_features = pd.DataFrame(rows)
    print(f"Calibration feature count: {len(calibration_features):,}")
    return calibration_features


# 13. Build threshold provider
def build_session_order_map(sessions_df: pd.DataFrame) -> dict[str, int]:
    return {str(session_id): i for i, session_id in enumerate(sessions_df["session_id"].tolist())}


def slice_sessions_for_mode(sessions_df: pd.DataFrame, mode: str) -> tuple[set[str], set[str], set[str]]:
    session_ids = sessions_df["session_id"].astype(str).tolist()
    if mode == "session_split_60_20_20":
        n = len(session_ids)
        train_end = max(1, int(n * 0.60))
        val_end = max(train_end, int(n * 0.80))
        train = set(session_ids[:train_end])
        val = set(session_ids[train_end:val_end])
        test = set(session_ids[val_end:])
        return train, val, test
    return set(), set(), set()


def get_thresholds_for_candidate(
    candidate_row: pd.Series,
    calibration_features: pd.DataFrame,
    sessions_df: pd.DataFrame,
    quantile: float,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    mode = THRESHOLD_MODE
    session_id = str(candidate_row["session_id"])
    candidate_ts = pd.Timestamp(candidate_row["candidate_start_timestamp"])
    if candidate_ts.tzinfo is None:
        candidate_ts = candidate_ts.tz_localize("UTC")
    else:
        candidate_ts = candidate_ts.tz_convert("UTC")
    session_order_map = build_session_order_map(sessions_df)
    session_ids = sessions_df["session_id"].astype(str).tolist()

    if mode == "rolling_30_session":
        session_pos = session_order_map[session_id]
        start_pos = session_pos - THRESHOLD_LOOKBACK_SESSIONS
        if start_pos < 0:
            diagnostics.insufficient_calibration_history_count += 1
            return {
                "warmup": True,
                "threshold_mode": mode,
                "threshold_lookback_sessions": THRESHOLD_LOOKBACK_SESSIONS,
                "threshold_quantile": quantile,
            }
        lookback_sessions = session_ids[start_pos:session_pos]
        prior = calibration_features[
            calibration_features["session_id"].isin(lookback_sessions)
            & (pd.to_datetime(calibration_features["timestamp"], utc=True) < candidate_ts)
        ]
        if prior.empty:
            diagnostics.insufficient_calibration_history_count += 1
            return {
                "warmup": True,
                "threshold_mode": mode,
                "threshold_lookback_sessions": THRESHOLD_LOOKBACK_SESSIONS,
                "threshold_quantile": quantile,
            }
        return {
            "warmup": False,
            "threshold_mode": mode,
            "threshold_lookback_sessions": THRESHOLD_LOOKBACK_SESSIONS,
            "threshold_quantile": quantile,
            "migration_threshold_used": float(prior["calibration_max_net_migration_R"].quantile(quantile)),
            "range_threshold_used": float(prior["calibration_max_price_range_R"].quantile(quantile)),
            "activity_threshold_used": float(prior["calibration_max_trade_activity_rate"].quantile(quantile)),
            "calibration_start_session": lookback_sessions[0],
            "calibration_end_session": lookback_sessions[-1],
            "calibration_candidate_count": int(len(prior)),
        }

    if mode == "fixed_train_1year":
        train_start = pd.Timestamp(FIXED_TRAIN_START, tz="UTC")
        train_end = pd.Timestamp(FIXED_TRAIN_END, tz="UTC")
        test_start = pd.Timestamp(FIXED_TEST_START, tz="UTC")
        test_end = pd.Timestamp(FIXED_TEST_END, tz="UTC")
        if candidate_ts < test_start or candidate_ts >= test_end:
            diagnostics.insufficient_calibration_history_count += 1
            return {
                "warmup": True,
                "threshold_mode": mode,
                "threshold_quantile": quantile,
            }
        cal_ts = pd.to_datetime(calibration_features["timestamp"], utc=True)
        prior = calibration_features[(cal_ts >= train_start) & (cal_ts < train_end)]
        if prior.empty:
            diagnostics.insufficient_calibration_history_count += 1
            return {"warmup": True, "threshold_mode": mode, "threshold_quantile": quantile}
        return {
            "warmup": False,
            "threshold_mode": mode,
            "threshold_lookback_sessions": None,
            "threshold_quantile": quantile,
            "migration_threshold_used": float(prior["calibration_max_net_migration_R"].quantile(quantile)),
            "range_threshold_used": float(prior["calibration_max_price_range_R"].quantile(quantile)),
            "activity_threshold_used": float(prior["calibration_max_trade_activity_rate"].quantile(quantile)),
            "fixed_train_start": FIXED_TRAIN_START,
            "fixed_train_end": FIXED_TRAIN_END,
            "fixed_test_start": FIXED_TEST_START,
            "fixed_test_end": FIXED_TEST_END,
            "calibration_candidate_count": int(len(prior)),
        }

    if mode == "session_split_60_20_20":
        train_sessions, _, _ = slice_sessions_for_mode(sessions_df, mode)
        if session_id in train_sessions:
            diagnostics.insufficient_calibration_history_count += 1
            return {"warmup": True, "threshold_mode": mode, "threshold_quantile": quantile}
        prior = calibration_features[calibration_features["session_id"].isin(train_sessions)]
        if prior.empty:
            diagnostics.insufficient_calibration_history_count += 1
            return {"warmup": True, "threshold_mode": mode, "threshold_quantile": quantile}
        sorted_train = sorted(train_sessions)
        return {
            "warmup": False,
            "threshold_mode": mode,
            "threshold_lookback_sessions": None,
            "threshold_quantile": quantile,
            "migration_threshold_used": float(prior["calibration_max_net_migration_R"].quantile(quantile)),
            "range_threshold_used": float(prior["calibration_max_price_range_R"].quantile(quantile)),
            "activity_threshold_used": float(prior["calibration_max_trade_activity_rate"].quantile(quantile)),
            "calibration_start_session": sorted_train[0],
            "calibration_end_session": sorted_train[-1],
            "calibration_candidate_count": int(len(prior)),
        }

    raise ValueError(f"Unsupported THRESHOLD_MODE: {mode}")


def compute_running_metrics(
    timestamps_ns: np.ndarray,
    prices: np.ndarray,
    start_idx: int,
    end_idx: int,
    anchor_ts_ns: int,
    anchor_price: float,
    direction: str,
    risk_abs: float,
) -> dict[str, np.ndarray]:
    count = max(end_idx - start_idx, 0)
    net = np.zeros(count, dtype=np.float64)
    rng = np.zeros(count, dtype=np.float64)
    activity = np.zeros(count, dtype=np.float64)
    max_prices = np.zeros(count, dtype=np.float64)
    min_prices = np.zeros(count, dtype=np.float64)
    running_max = -np.inf
    running_min = np.inf
    for offset, idx in enumerate(range(start_idx, end_idx)):
        px = float(prices[idx])
        running_max = max(running_max, px)
        running_min = min(running_min, px)
        elapsed_seconds = max((int(timestamps_ns[idx]) - anchor_ts_ns) / 1_000_000_000, 1e-9)
        net[offset] = direction_adjusted_r(direction, anchor_price, px, risk_abs)
        rng[offset] = (running_max - running_min) / risk_abs if risk_abs > 0 else 0.0
        activity[offset] = (offset + 1) / elapsed_seconds
        max_prices[offset] = running_max
        min_prices[offset] = running_min
    return {
        "net": net,
        "range": rng,
        "activity": activity,
        "running_max": max_prices,
        "running_min": min_prices,
    }


# 14. Run event-driven state-machine simulator
def run_event_driven_state_machine(
    confirmed_bubbles: pd.DataFrame,
    raw: pd.DataFrame,
    calibration_features: pd.DataFrame,
    sessions_df: pd.DataFrame,
    quantile: float,
    diagnostics: Diagnostics,
) -> pd.DataFrame:
    print_stage(f"14. Run event-driven state-machine simulator (Q{int(quantile * 100)})")
    timestamps_ns = pd.to_datetime(raw["timestamp"], utc=True).dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
    prices = raw["price"].to_numpy(dtype=np.float64)

    confirmed = confirmed_bubbles.copy().sort_values(
        ["confirmation_timestamp", "confirmation_raw_index", "confirmed_bubble_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    confirmed_ts_ns = (
        pd.to_datetime(confirmed["confirmation_timestamp"], utc=True).dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
        if not confirmed.empty else np.array([], dtype=np.int64)
    )

    episodes: list[dict[str, Any]] = []
    search_after_ns = int(timestamps_ns[0]) - 1 if len(timestamps_ns) else 0
    episode_id = 1

    while True:
        next_bubble_idx = first_index_strictly_after(confirmed_ts_ns, search_after_ns)
        if next_bubble_idx >= len(confirmed):
            break

        bubble = confirmed.iloc[next_bubble_idx]
        candidate_ts = pd.Timestamp(bubble["candidate_start_timestamp"])
        candidate_ts_ns = int(candidate_ts.value)
        candidate_expiry_ns = candidate_ts_ns + int(pd.Timedelta(seconds=CANDIDATE_TIMEOUT_SECONDS).value)
        candidate_r_abs = float(bubble["candidate_anchor_price"]) * STOP_BUFFER_PCT
        threshold_info = get_thresholds_for_candidate(bubble, calibration_features, sessions_df, quantile, diagnostics)

        active_window_end_ns = candidate_expiry_ns
        candidate_end_bubble_idx = int(np.searchsorted(confirmed_ts_ns, active_window_end_ns, side="right"))
        candidate_diag_slice = confirmed.iloc[next_bubble_idx + 1:candidate_end_bubble_idx]
        same_side_pre = int((candidate_diag_slice["directional_bias"] == bubble["directional_bias"]).sum()) if not candidate_diag_slice.empty else 0
        opp_side_pre = int((candidate_diag_slice["directional_bias"] != bubble["directional_bias"]).sum()) if not candidate_diag_slice.empty else 0
        time_to_first_next = None
        if next_bubble_idx + 1 < len(confirmed):
            time_to_first_next = float((confirmed_ts_ns[next_bubble_idx + 1] - candidate_ts_ns) / 1_000_000_000)

        base_episode = {
            "episode_id": episode_id,
            "symbol": SYMBOL,
            "bias": bubble["directional_bias"],
            "raw_index": int(bubble["raw_index"]),
            "agg_trade_id": bubble["agg_trade_id"],
            "bubble_timestamp": pd.Timestamp(bubble["bubble_timestamp"]),
            "confirmation_timestamp": pd.Timestamp(bubble["confirmation_timestamp"]),
            "confirmation_raw_index": int(bubble["confirmation_raw_index"]),
            "candidate_start_timestamp": candidate_ts,
            "candidate_anchor_price": float(bubble["candidate_anchor_price"]),
            "candidate_R": float(candidate_r_abs),
            "candidate_expiry_timestamp": pd.Timestamp(candidate_expiry_ns),
            "session_id": bubble["session_id"],
            "session_date": bubble["session_date"],
            "previous_session_id": bubble["previous_session_id"],
            "previous_val": float(bubble["previous_val"]),
            "previous_vah": float(bubble["previous_vah"]),
            "previous_poc": float(bubble["previous_poc"]),
            "bubble_qty": float(bubble["qty"]),
            "bubble_notional": float(bubble["notional"]),
            "bubble_side": bubble["aggressive_side"],
            "bubble_tier": bubble["bubble_tier"],
            "reaction_mfe_pct": float(bubble["reaction_mfe_pct"]),
            "reaction_mae_pct": float(bubble["reaction_mae_pct"]),
            "reaction_efficiency": float(bubble["reaction_efficiency"]),
            "threshold_mode": threshold_info.get("threshold_mode", THRESHOLD_MODE),
            "threshold_lookback_sessions": threshold_info.get("threshold_lookback_sessions"),
            "threshold_quantile": threshold_info.get("threshold_quantile", quantile),
            "migration_threshold_used": threshold_info.get("migration_threshold_used"),
            "range_threshold_used": threshold_info.get("range_threshold_used"),
            "activity_threshold_used": threshold_info.get("activity_threshold_used"),
            "calibration_start_session": threshold_info.get("calibration_start_session"),
            "calibration_end_session": threshold_info.get("calibration_end_session"),
            "calibration_candidate_count": threshold_info.get("calibration_candidate_count"),
            "condition_achieved": False,
            "condition_trigger_timestamp": pd.NaT,
            "entry_timestamp": pd.NaT,
            "entry_price": np.nan,
            "entry_delay_seconds": np.nan,
            "migration_R_at_trigger": np.nan,
            "price_range_R_at_trigger": np.nan,
            "trade_activity_rate_at_trigger": np.nan,
            "trade_count_at_trigger": np.nan,
            "trade_entered": False,
            "sl_price": np.nan,
            "tp1_price": np.nan,
            "stop_buffer_pct": STOP_BUFFER_PCT,
            "tp1_touched": False,
            "tp1_timestamp": pd.NaT,
            "sl_touched": False,
            "sl_timestamp": pd.NaT,
            "first_touch_event": None,
            "touch_sequence_ambiguous": False,
            "max_R_before_close": np.nan,
            "max_favorable_R_since_entry": np.nan,
            "retracement_pct_at_close": np.nan,
            "close_timestamp": pd.NaT,
            "close_price": np.nan,
            "close_reason": None,
            "final_R": np.nan,
            "holding_seconds": np.nan,
            "continuation_extension_count": 0,
            "continuation_with_confirmation": 0,
            "continuation_without_confirmation": 0,
            "confirmed_bubble_count_during_trade": 0,
            "confirmed_bubble_count_since_candidate_start": int(len(candidate_diag_slice)),
            "same_side_confirmed_bubble_count_since_candidate_start": same_side_pre,
            "opposite_side_confirmed_bubble_count_since_candidate_start": opp_side_pre,
            "confirmation_bubble_before_entry": bool(len(candidate_diag_slice) > 0),
            "time_to_first_next_confirmed_bubble": time_to_first_next,
            "candidate_expired": False,
            "end_of_data_close": False,
        }

        if threshold_info.get("warmup", False):
            base_episode["candidate_expired"] = False
            base_episode["close_timestamp"] = pd.Timestamp(candidate_expiry_ns)
            close_idx = pick_close_index(timestamps_ns, min(candidate_expiry_ns, int(timestamps_ns[-1])))
            base_episode["close_price"] = float(prices[close_idx])
            base_episode["close_reason"] = "insufficient_calibration_history"
            episodes.append(base_episode)
            episode_id += 1
            search_after_ns = candidate_expiry_ns
            continue

        trigger_start_ns = candidate_ts_ns + int(pd.Timedelta(seconds=MIN_TRIGGER_SECONDS).value)
        metric_start_idx = first_index_at_or_after(timestamps_ns, candidate_ts_ns)
        trigger_start_idx = first_index_at_or_after(timestamps_ns, trigger_start_ns)
        candidate_end_idx = int(np.searchsorted(timestamps_ns, candidate_expiry_ns, side="right"))

        if metric_start_idx >= candidate_end_idx or trigger_start_idx >= candidate_end_idx:
            base_episode["candidate_expired"] = True
            base_episode["close_timestamp"] = pd.Timestamp(candidate_expiry_ns)
            close_idx = pick_close_index(timestamps_ns, min(candidate_expiry_ns, int(timestamps_ns[-1])))
            base_episode["close_price"] = float(prices[close_idx])
            base_episode["close_reason"] = "candidate_expired_no_entry"
            episodes.append(base_episode)
            episode_id += 1
            search_after_ns = candidate_expiry_ns
            continue

        running = compute_running_metrics(
            timestamps_ns=timestamps_ns,
            prices=prices,
            start_idx=metric_start_idx,
            end_idx=candidate_end_idx,
            anchor_ts_ns=candidate_ts_ns,
            anchor_price=float(bubble["candidate_anchor_price"]),
            direction=bubble["directional_bias"],
            risk_abs=candidate_r_abs,
        )
        eligible_offsets = np.arange(candidate_end_idx - metric_start_idx) >= max(trigger_start_idx - metric_start_idx, 0)
        condition_mask = eligible_offsets & (
            (running["net"] >= float(threshold_info["migration_threshold_used"]))
            & (running["range"] >= float(threshold_info["range_threshold_used"]))
            & (running["activity"] >= float(threshold_info["activity_threshold_used"]))
        )

        if not condition_mask.any():
            base_episode["candidate_expired"] = True
            base_episode["close_timestamp"] = pd.Timestamp(candidate_expiry_ns)
            close_idx = pick_close_index(timestamps_ns, min(candidate_expiry_ns, int(timestamps_ns[-1])))
            base_episode["close_price"] = float(prices[close_idx])
            base_episode["close_reason"] = "candidate_expired_no_entry"
            episodes.append(base_episode)
            episode_id += 1
            search_after_ns = candidate_expiry_ns
            continue

        trigger_offset = int(np.argmax(condition_mask))
        trigger_idx = metric_start_idx + trigger_offset
        trigger_ts_ns = int(timestamps_ns[trigger_idx])
        entry_idx = first_index_strictly_after(timestamps_ns, trigger_ts_ns)
        if entry_idx >= len(timestamps_ns):
            base_episode["trade_entered"] = True
            base_episode["condition_achieved"] = True
            base_episode["condition_trigger_timestamp"] = pd.Timestamp(trigger_ts_ns)
            base_episode["close_timestamp"] = pd.Timestamp(timestamps_ns[-1])
            base_episode["close_price"] = float(prices[-1])
            base_episode["close_reason"] = "end_of_data_open_trade"
            base_episode["end_of_data_close"] = True
            diagnostics.end_of_data_open_trade_count += 1
            episodes.append(base_episode)
            episode_id += 1
            break

        entry_ts_ns = int(timestamps_ns[entry_idx])
        entry_price = float(prices[entry_idx])
        direction = str(bubble["directional_bias"])
        sl_price = entry_price * (1 - STOP_BUFFER_PCT) if direction == "long" else entry_price * (1 + STOP_BUFFER_PCT)
        tp1_price = entry_price * (1 + STOP_BUFFER_PCT) if direction == "long" else entry_price * (1 - STOP_BUFFER_PCT)
        trade_r_abs = abs(entry_price - sl_price)

        base_episode.update({
            "condition_achieved": True,
            "condition_trigger_timestamp": pd.Timestamp(trigger_ts_ns),
            "entry_timestamp": pd.Timestamp(entry_ts_ns),
            "entry_price": entry_price,
            "entry_delay_seconds": float((entry_ts_ns - candidate_ts_ns) / 1_000_000_000),
            "migration_R_at_trigger": float(running["net"][trigger_offset]),
            "price_range_R_at_trigger": float(running["range"][trigger_offset]),
            "trade_activity_rate_at_trigger": float(running["activity"][trigger_offset]),
            "trade_count_at_trigger": int(trigger_idx - metric_start_idx + 1),
            "trade_entered": True,
            "sl_price": float(sl_price),
            "tp1_price": float(tp1_price),
        })

        segment_start_ns = entry_ts_ns
        current_deadline_ns = entry_ts_ns + int(pd.Timedelta(seconds=CONTINUATION_WINDOW_SECONDS).value)
        continuation_extension_count = 0
        tp1_touched = False
        tp1_ts_ns: int | None = None
        sl_ts_ns: int | None = None
        first_touch_event: str | None = None
        touch_sequence_ambiguous = False
        max_favorable_r_since_entry = -np.inf
        max_r_before_close = -np.inf
        retracement_pct_at_close = np.nan
        continuation_with_confirmation = 0
        continuation_without_confirmation = 0
        confirmed_bubble_count_during_trade = 0

        trade_open = True
        trade_close_reason = None
        close_ts_ns = None
        close_price = None
        final_r = None
        post_tp1 = False
        trade_cursor_idx = entry_idx + 1

        while trade_open:
            if trade_cursor_idx >= len(timestamps_ns):
                close_ts_ns = int(timestamps_ns[-1])
                close_price = float(prices[-1])
                current_r = direction_adjusted_r(direction, entry_price, close_price, trade_r_abs)
                final_r = current_r
                trade_close_reason = "end_of_data_open_trade"
                diagnostics.end_of_data_open_trade_count += 1
                break

            deadline_idx_exclusive = int(np.searchsorted(timestamps_ns, current_deadline_ns, side="right"))
            if deadline_idx_exclusive <= trade_cursor_idx:
                close_idx = pick_close_index(timestamps_ns, current_deadline_ns)
                close_ts_ns = int(timestamps_ns[close_idx])
                close_price = float(prices[close_idx])
                final_r = direction_adjusted_r(direction, entry_price, close_price, trade_r_abs)
                trade_close_reason = "tp1_then_no_continuation_900s" if post_tp1 else "no_tp1_no_sl_timeout"
                break

            segment_anchor_idx = max(first_index_at_or_after(timestamps_ns, segment_start_ns), entry_idx)
            segment_anchor_price = float(prices[segment_anchor_idx])
            segment_r_abs = segment_anchor_price * STOP_BUFFER_PCT
            segment_metric_start_idx = first_index_at_or_after(timestamps_ns, segment_start_ns)
            segment_check_start_ns = segment_start_ns + int(pd.Timedelta(seconds=CONTINUATION_MIN_TRIGGER_SECONDS).value)
            segment_check_start_idx = first_index_at_or_after(timestamps_ns, segment_check_start_ns)
            segment_running = compute_running_metrics(
                timestamps_ns=timestamps_ns,
                prices=prices,
                start_idx=segment_metric_start_idx,
                end_idx=deadline_idx_exclusive,
                anchor_ts_ns=segment_start_ns,
                anchor_price=segment_anchor_price,
                direction=direction,
                risk_abs=segment_r_abs,
            )

            eligible_segment_offsets = np.arange(deadline_idx_exclusive - segment_metric_start_idx) >= max(segment_check_start_idx - segment_metric_start_idx, 0)
            continuation_mask = eligible_segment_offsets & (
                (segment_running["net"] >= float(threshold_info["migration_threshold_used"]))
                & (segment_running["range"] >= float(threshold_info["range_threshold_used"]))
                & (segment_running["activity"] >= float(threshold_info["activity_threshold_used"]))
            )
            continuation_idx_set = set((segment_metric_start_idx + np.where(continuation_mask)[0]).tolist())

            extension_consumed = False
            for local_offset, idx in enumerate(range(trade_cursor_idx, deadline_idx_exclusive)):
                ts_ns = int(timestamps_ns[idx])
                px = float(prices[idx])
                current_r = direction_adjusted_r(direction, entry_price, px, trade_r_abs)
                max_favorable_r_since_entry = max(max_favorable_r_since_entry, current_r)
                max_r_before_close = max(max_r_before_close, current_r)

                if direction == "long":
                    sl_hit = px <= sl_price
                    tp1_hit = px >= tp1_price
                else:
                    sl_hit = px >= sl_price
                    tp1_hit = px <= tp1_price

                if not tp1_touched and sl_hit and tp1_hit:
                    touch_sequence_ambiguous = True
                    diagnostics.ambiguous_touch_sequence_count += 1
                    sl_hit = True
                    tp1_hit = False

                if not tp1_touched and sl_hit:
                    sl_ts_ns = ts_ns
                    first_touch_event = "sl"
                    close_ts_ns = ts_ns
                    close_price = px
                    final_r = -1.0
                    trade_close_reason = "sl_before_tp1"
                    trade_open = False
                    break

                if not tp1_touched and tp1_hit:
                    tp1_touched = True
                    tp1_ts_ns = ts_ns
                    first_touch_event = "tp1"
                    post_tp1 = True
                    segment_start_ns = ts_ns
                    current_deadline_ns = ts_ns + int(pd.Timedelta(seconds=CONTINUATION_WINDOW_SECONDS).value)
                    trade_cursor_idx = idx + 1
                    extension_consumed = True
                    break

                if tp1_touched:
                    retracement_pct = 0.0
                    if max_favorable_r_since_entry > 0:
                        retracement_pct = (max_favorable_r_since_entry - current_r) / max_favorable_r_since_entry
                    if retracement_pct >= RETRACEMENT_INVALIDATION_PCT:
                        close_ts_ns = ts_ns
                        close_price = px
                        final_r = current_r
                        retracement_pct_at_close = retracement_pct
                        trade_close_reason = "tp1_then_50pct_retracement"
                        trade_open = False
                        break

                if idx in continuation_idx_set:
                    continuation_trigger_ts_ns = ts_ns
                    window_bubble_start = int(np.searchsorted(confirmed_ts_ns, segment_start_ns, side="right"))
                    window_bubble_end = int(np.searchsorted(confirmed_ts_ns, continuation_trigger_ts_ns, side="right"))
                    confirmation_slice = confirmed.iloc[window_bubble_start:window_bubble_end]
                    if len(confirmation_slice) > 0:
                        continuation_with_confirmation += 1
                    else:
                        continuation_without_confirmation += 1
                    continuation_extension_count += 1
                    segment_start_ns = continuation_trigger_ts_ns
                    current_deadline_ns = continuation_trigger_ts_ns + int(pd.Timedelta(seconds=CONTINUATION_WINDOW_SECONDS).value)
                    trade_cursor_idx = idx + 1
                    extension_consumed = True
                    break

            if not trade_open:
                break
            if extension_consumed:
                continue

            close_idx = pick_close_index(timestamps_ns, current_deadline_ns)
            close_ts_ns = int(timestamps_ns[close_idx])
            close_price = float(prices[close_idx])
            final_r = direction_adjusted_r(direction, entry_price, close_price, trade_r_abs)
            trade_close_reason = "tp1_then_no_continuation_900s" if post_tp1 else "no_tp1_no_sl_timeout"
            break

        lifecycle_end_ns = close_ts_ns if close_ts_ns is not None else candidate_expiry_ns
        diag_start_idx = int(np.searchsorted(confirmed_ts_ns, candidate_ts_ns, side="right"))
        diag_end_idx = int(np.searchsorted(confirmed_ts_ns, lifecycle_end_ns, side="right"))
        lifecycle_diag_slice = confirmed.iloc[diag_start_idx:diag_end_idx]
        confirmed_bubble_count_during_trade = int(len(lifecycle_diag_slice))

        base_episode.update({
            "tp1_touched": tp1_touched,
            "tp1_timestamp": pd.Timestamp(tp1_ts_ns) if tp1_ts_ns is not None else pd.NaT,
            "sl_touched": sl_ts_ns is not None,
            "sl_timestamp": pd.Timestamp(sl_ts_ns) if sl_ts_ns is not None else pd.NaT,
            "first_touch_event": first_touch_event,
            "touch_sequence_ambiguous": touch_sequence_ambiguous,
            "max_R_before_close": float(max_r_before_close if np.isfinite(max_r_before_close) else np.nan),
            "max_favorable_R_since_entry": float(max_favorable_r_since_entry if np.isfinite(max_favorable_r_since_entry) else np.nan),
            "retracement_pct_at_close": float(retracement_pct_at_close) if pd.notna(retracement_pct_at_close) else np.nan,
            "close_timestamp": pd.Timestamp(close_ts_ns) if close_ts_ns is not None else pd.NaT,
            "close_price": float(close_price) if close_price is not None else np.nan,
            "close_reason": trade_close_reason,
            "final_R": float(final_r) if final_r is not None else np.nan,
            "holding_seconds": float((close_ts_ns - entry_ts_ns) / 1_000_000_000) if close_ts_ns is not None else np.nan,
            "continuation_extension_count": continuation_extension_count,
            "continuation_with_confirmation": continuation_with_confirmation,
            "continuation_without_confirmation": continuation_without_confirmation,
            "confirmed_bubble_count_during_trade": confirmed_bubble_count_during_trade,
            "confirmed_bubble_count_since_candidate_start": confirmed_bubble_count_during_trade,
            "same_side_confirmed_bubble_count_since_candidate_start": int((lifecycle_diag_slice["directional_bias"] == direction).sum()) if not lifecycle_diag_slice.empty else 0,
            "opposite_side_confirmed_bubble_count_since_candidate_start": int((lifecycle_diag_slice["directional_bias"] != direction).sum()) if not lifecycle_diag_slice.empty else 0,
            "confirmation_bubble_before_entry": bool(int(np.searchsorted(confirmed_ts_ns, entry_ts_ns, side="left")) > diag_start_idx),
            "end_of_data_close": trade_close_reason == "end_of_data_open_trade",
        })

        episodes.append(base_episode)
        episode_id += 1
        search_after_ns = lifecycle_end_ns
        if trade_close_reason == "end_of_data_open_trade":
            break

    return pd.DataFrame(episodes)


# 15. Create episode dataframe
def finalize_episode_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    print_stage("15. Create episode dataframe")
    desired_columns = [
        "episode_id", "symbol", "bias",
        "raw_index", "agg_trade_id", "bubble_timestamp", "confirmation_timestamp", "confirmation_raw_index",
        "candidate_start_timestamp", "candidate_anchor_price", "candidate_R", "candidate_expiry_timestamp",
        "session_id", "session_date", "previous_session_id", "previous_val", "previous_vah", "previous_poc",
        "bubble_qty", "bubble_notional", "bubble_side", "bubble_tier", "reaction_mfe_pct", "reaction_mae_pct", "reaction_efficiency",
        "threshold_mode", "threshold_lookback_sessions", "threshold_quantile", "migration_threshold_used", "range_threshold_used", "activity_threshold_used",
        "calibration_start_session", "calibration_end_session", "calibration_candidate_count",
        "condition_achieved", "condition_trigger_timestamp", "entry_timestamp", "entry_price", "entry_delay_seconds",
        "migration_R_at_trigger", "price_range_R_at_trigger", "trade_activity_rate_at_trigger", "trade_count_at_trigger",
        "trade_entered", "sl_price", "tp1_price", "stop_buffer_pct",
        "tp1_touched", "tp1_timestamp", "sl_touched", "sl_timestamp", "first_touch_event", "touch_sequence_ambiguous",
        "max_R_before_close", "max_favorable_R_since_entry", "retracement_pct_at_close",
        "close_timestamp", "close_price", "close_reason", "final_R", "holding_seconds",
        "continuation_extension_count", "continuation_with_confirmation", "continuation_without_confirmation", "confirmed_bubble_count_during_trade",
        "confirmed_bubble_count_since_candidate_start", "same_side_confirmed_bubble_count_since_candidate_start", "opposite_side_confirmed_bubble_count_since_candidate_start",
        "confirmation_bubble_before_entry", "time_to_first_next_confirmed_bubble",
        "candidate_expired", "end_of_data_close",
    ]
    if df.empty:
        return pd.DataFrame(columns=desired_columns)
    for col in desired_columns:
        if col not in df.columns:
            df[col] = np.nan
    return df[desired_columns].sort_values("episode_id").reset_index(drop=True)


# 16. Print summaries
def build_summary_tables(
    raw: pd.DataFrame,
    sessions_df: pd.DataFrame,
    qualified_bubbles: pd.DataFrame,
    outside_value_bubbles: pd.DataFrame,
    confirmed_bubbles: pd.DataFrame,
    episodes_by_quantile: dict[float, pd.DataFrame],
    diagnostics: Diagnostics,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    primary_episodes = episodes_by_quantile[PRIMARY_THRESHOLD_QUANTILE]
    trades_only = primary_episodes[primary_episodes["trade_entered"] == True].copy()

    funnel = {
        "raw_trade_count": int(len(raw)),
        "session_count": int(len(sessions_df)),
        "qualified_bubble_count": int(len(qualified_bubbles)),
        "outside_value_bubble_count": int(len(outside_value_bubbles)),
        "confirmed_bubble_count": int(len(confirmed_bubbles)),
        "candidate_episode_count": int(len(primary_episodes)),
        "entered_trade_count": int(primary_episodes["trade_entered"].sum()) if not primary_episodes.empty else 0,
        "expired_candidate_count": int(primary_episodes["candidate_expired"].sum()) if not primary_episodes.empty else 0,
    }
    performance = summarize_final_r(trades_only)
    overall_row = {"total_episodes": int(len(primary_episodes)), **funnel, **performance}
    summary_overall = pd.DataFrame([overall_row])

    if primary_episodes.empty:
        summary_by_close_reason = pd.DataFrame(columns=["close_reason", "count", "mean_final_R", "median_final_R", "mean_max_R_before_close", "median_max_R_before_close"])
        summary_by_direction = pd.DataFrame(columns=["bias", "count", "mean_final_R", "median_final_R", "total_R"])
        summary_by_quantile = pd.DataFrame(columns=["threshold_quantile", "episode_count", "entered_trades", "mean_final_R", "median_final_R", "total_R"])
        summary_by_continuation = pd.DataFrame(columns=["continuation_bucket", "count", "mean_final_R", "median_final_R"])
        return summary_overall, summary_by_close_reason, summary_by_direction, summary_by_quantile, summary_by_continuation

    summary_by_close_reason = (
        primary_episodes.groupby("close_reason", dropna=False)
        .agg(
            count=("episode_id", "count"),
            mean_final_R=("final_R", "mean"),
            median_final_R=("final_R", "median"),
            mean_max_R_before_close=("max_R_before_close", "mean"),
            median_max_R_before_close=("max_R_before_close", "median"),
        )
        .reset_index()
    )

    summary_by_direction = (
        primary_episodes[primary_episodes["trade_entered"] == True]
        .groupby("bias", dropna=False)
        .agg(
            count=("episode_id", "count"),
            mean_final_R=("final_R", "mean"),
            median_final_R=("final_R", "median"),
            total_R=("final_R", "sum"),
        )
        .reset_index()
    )

    quant_rows = []
    for quantile, df in episodes_by_quantile.items():
        trades = df[df["trade_entered"] == True]
        quant_rows.append({
            "threshold_quantile": quantile,
            "episode_count": int(len(df)),
            "entered_trades": int(df["trade_entered"].sum()) if not df.empty else 0,
            "mean_final_R": float(trades["final_R"].mean()) if not trades.empty else np.nan,
            "median_final_R": float(trades["final_R"].median()) if not trades.empty else np.nan,
            "total_R": float(trades["final_R"].sum()) if not trades.empty else 0.0,
        })
    summary_by_quantile = pd.DataFrame(quant_rows)

    continuation_bucket = np.where(
        primary_episodes["continuation_extension_count"] <= 0,
        "no continuation extension",
        np.where(
            primary_episodes["continuation_with_confirmation"] > 0,
            "continuation_with_confirmation",
            "continuation_without_confirmation",
        ),
    )
    tmp = primary_episodes.copy()
    tmp["continuation_bucket"] = continuation_bucket
    summary_by_continuation = (
        tmp.groupby("continuation_bucket", dropna=False)
        .agg(
            count=("episode_id", "count"),
            mean_final_R=("final_R", "mean"),
            median_final_R=("final_R", "median"),
        )
        .reset_index()
    )

    print_stage("16. Print summaries")
    print("Candidate funnel")
    for key, value in funnel.items():
        print(f"- {key}: {value}")
    print("\nOverall performance")
    for key, value in performance.items():
        print(f"- {key}: {value}")
    print("\nBy close_reason")
    print(summary_by_close_reason.to_string(index=False))
    print("\nBy direction")
    print(summary_by_direction.to_string(index=False))
    print("\nBy threshold quantile")
    print(summary_by_quantile.to_string(index=False))
    print("\nBy continuation")
    print(summary_by_continuation.to_string(index=False))
    print("\nData quality diagnostics")
    print(f"- missing previous profile count: {diagnostics.missing_previous_profile_count}")
    print(f"- incomplete reaction window count: {diagnostics.incomplete_reaction_window_count}")
    print(f"- incomplete calibration window count: {diagnostics.incomplete_calibration_window_count}")
    print(f"- ambiguous touch sequence count: {diagnostics.ambiguous_touch_sequence_count}")
    print(f"- end-of-data open trade count: {diagnostics.end_of_data_open_trade_count}")
    print(f"- insufficient calibration history count: {diagnostics.insufficient_calibration_history_count}")

    return summary_overall, summary_by_close_reason, summary_by_direction, summary_by_quantile, summary_by_continuation


# 17. Export results/config
def export_results(
    output_dir: str,
    session_profiles: pd.DataFrame,
    qualified_bubbles: pd.DataFrame,
    outside_value_bubbles: pd.DataFrame,
    confirmed_bubbles: pd.DataFrame,
    reaction_confirmation_diagnostics: pd.DataFrame,
    calibration_features: pd.DataFrame,
    episodes_by_quantile: dict[float, pd.DataFrame],
    summary_overall: pd.DataFrame,
    summary_by_close_reason: pd.DataFrame,
    summary_by_direction: pd.DataFrame,
    summary_by_quantile: pd.DataFrame,
    summary_by_continuation: pd.DataFrame,
    selected_bin_size: float,
    diagnostics: Diagnostics,
    diagnostics_by_quantile: dict[str, dict[str, int]],
) -> None:
    print_stage("17. Export results/config")
    ensure_output_dir(output_dir)
    export_dataframe(session_profiles, os.path.join(output_dir, "session_profiles.parquet"))
    export_dataframe(qualified_bubbles, os.path.join(output_dir, "qualified_bubbles.parquet"))
    export_dataframe(outside_value_bubbles, os.path.join(output_dir, "outside_value_bubbles.parquet"))
    export_dataframe(confirmed_bubbles, os.path.join(output_dir, "confirmed_bubbles_ungrouped.parquet"))
    export_dataframe(
        reaction_confirmation_diagnostics,
        os.path.join(output_dir, "reaction_confirmation_diagnostics.parquet"),
        csv_path=os.path.join(output_dir, "reaction_confirmation_diagnostics.csv"),
    )
    export_dataframe(calibration_features, os.path.join(output_dir, "calibration_features.parquet"))

    for quantile, df in episodes_by_quantile.items():
        suffix = f"q{int(quantile * 100):02d}"
        csv_path = os.path.join(output_dir, f"event_driven_episodes_{suffix}.csv") if quantile == PRIMARY_THRESHOLD_QUANTILE else None
        export_dataframe(df, os.path.join(output_dir, f"event_driven_episodes_{suffix}.parquet"), csv_path=csv_path)

    summary_overall.to_csv(os.path.join(output_dir, "summary_overall.csv"), index=False)
    summary_by_close_reason.to_csv(os.path.join(output_dir, "summary_by_close_reason.csv"), index=False)
    summary_by_direction.to_csv(os.path.join(output_dir, "summary_by_direction.csv"), index=False)
    summary_by_quantile.to_csv(os.path.join(output_dir, "summary_by_quantile.csv"), index=False)
    summary_by_continuation.to_csv(os.path.join(output_dir, "summary_by_continuation.csv"), index=False)

    config_payload = {
        "RAW_AGGTRADES_PATH": RAW_AGGTRADES_PATH,
        "OUTPUT_DIR": OUTPUT_DIR,
        "SYMBOL": SYMBOL,
        "SESSION_START_UTC": SESSION_START_UTC,
        "VALUE_AREA_PCT": VALUE_AREA_PCT,
        "PROFILE_PRICE_BIN_SIZE": PROFILE_PRICE_BIN_SIZE,
        "PROFILE_PRICE_BIN_METHOD": PROFILE_PRICE_BIN_METHOD,
        "selected_profile_bin_size": selected_bin_size,
        "BUBBLE_MODE": BUBBLE_MODE,
        "BUBBLE_SIZE_METRIC": BUBBLE_SIZE_METRIC,
        "BUBBLE_Q_MEDIUM": BUBBLE_Q_MEDIUM,
        "BUBBLE_Q_LARGE": BUBBLE_Q_LARGE,
        "BUBBLE_Q_EXTREME": BUBBLE_Q_EXTREME,
        "MIN_BUBBLE_TIER": MIN_BUBBLE_TIER,
        "SETUP_REACTION_WINDOW_SECONDS": SETUP_REACTION_WINDOW_SECONDS,
        "SETUP_MFE_THRESHOLD_PCT": SETUP_MFE_THRESHOLD_PCT,
        "SETUP_EFFICIENCY_THRESHOLD": SETUP_EFFICIENCY_THRESHOLD,
        "STOP_BUFFER_PCT": STOP_BUFFER_PCT,
        "TP1_R": TP1_R,
        "CANDIDATE_TIMEOUT_SECONDS": CANDIDATE_TIMEOUT_SECONDS,
        "CONTINUATION_WINDOW_SECONDS": CONTINUATION_WINDOW_SECONDS,
        "MIN_TRIGGER_SECONDS": MIN_TRIGGER_SECONDS,
        "RETRACEMENT_INVALIDATION_PCT": RETRACEMENT_INVALIDATION_PCT,
        "THRESHOLD_MODE": THRESHOLD_MODE,
        "THRESHOLD_LOOKBACK_SESSIONS": THRESHOLD_LOOKBACK_SESSIONS,
        "THRESHOLD_QUANTILES": THRESHOLD_QUANTILES,
        "PRIMARY_THRESHOLD_QUANTILE": PRIMARY_THRESHOLD_QUANTILE,
        "FIXED_TRAIN_START": FIXED_TRAIN_START,
        "FIXED_TRAIN_END": FIXED_TRAIN_END,
        "FIXED_TEST_START": FIXED_TEST_START,
        "FIXED_TEST_END": FIXED_TEST_END,
        "RUN_ROBUSTNESS_QUANTILES": RUN_ROBUSTNESS_QUANTILES,
        "diagnostics": diagnostics.__dict__,
        "diagnostics_by_quantile": diagnostics_by_quantile,
    }
    with open(os.path.join(output_dir, "event_driven_config.json"), "w", encoding="utf-8") as f:
        json.dump(config_payload, f, indent=2, default=str)
    print(f"Exported outputs to: {output_dir}")


def main() -> None:
    pipeline_diagnostics = Diagnostics()
    raw = load_raw_aggtrades(RAW_AGGTRADES_PATH)
    raw, _ = normalize_raw_schema(raw)
    raw, sessions_df, _, _ = build_sessions(raw)

    selected_bin_size = float(PROFILE_PRICE_BIN_SIZE) if PROFILE_PRICE_BIN_SIZE is not None else infer_profile_bin_size(raw["price"])
    session_profiles, profile_lookup = build_session_profiles(raw, sessions_df, selected_bin_size)
    raw = attach_previous_profiles(raw, profile_lookup)
    threshold_df, threshold_lookup = compute_previous_session_bubble_thresholds(raw, sessions_df)
    _ = threshold_df  # Explicitly computed for clarity; detailed thresholds are attached at bubble level.
    qualified_bubbles = detect_qualified_bubbles(raw, threshold_lookup)
    outside_value_bubbles = apply_outside_value_filter(qualified_bubbles, pipeline_diagnostics)
    confirmed_bubbles, reaction_confirmation_diagnostics = confirm_bubbles(outside_value_bubbles, raw, pipeline_diagnostics)
    confirmed_bubbles = build_confirmed_bubble_stream(confirmed_bubbles)
    calibration_features = build_calibration_feature_table(confirmed_bubbles, raw, pipeline_diagnostics)

    if THRESHOLD_MODE == "rolling_30_session":
        pipeline_diagnostics.warmup_session_count = min(THRESHOLD_LOOKBACK_SESSIONS, len(sessions_df))

    first_tradable_session = sessions_df["session_id"].iloc[THRESHOLD_LOOKBACK_SESSIONS] if THRESHOLD_MODE == "rolling_30_session" and len(sessions_df) > THRESHOLD_LOOKBACK_SESSIONS else None

    print_stage("25. Validation prints before simulation")
    print(f"raw rows loaded: {len(raw):,}")
    print(f"first raw timestamp: {normalize_timestamp_column(raw['timestamp']).iloc[0]}")
    print(f"last raw timestamp: {normalize_timestamp_column(raw['timestamp']).iloc[-1]}")
    print(f"session count: {len(sessions_df):,}")
    print(f"session profile count: {len(session_profiles):,}")
    print(f"qualified bubble count: {len(qualified_bubbles):,}")
    print(f"outside-value bubble count: {len(outside_value_bubbles):,}")
    print(f"confirmed bubble count: {len(confirmed_bubbles):,}")
    print(f"threshold mode: {THRESHOLD_MODE}")
    print(f"threshold quantiles: {THRESHOLD_QUANTILES}")
    print(f"first tradable session under rolling mode: {first_tradable_session}")
    print(f"warmup session count: {pipeline_diagnostics.warmup_session_count}")

    quantiles_to_run = THRESHOLD_QUANTILES if RUN_ROBUSTNESS_QUANTILES else [PRIMARY_THRESHOLD_QUANTILE]
    episodes_by_quantile: dict[float, pd.DataFrame] = {}
    simulation_diagnostics_by_quantile: dict[float, Diagnostics] = {}
    for quantile in quantiles_to_run:
        simulation_diagnostics = Diagnostics()
        episodes = run_event_driven_state_machine(
            confirmed_bubbles=confirmed_bubbles,
            raw=raw,
            calibration_features=calibration_features,
            sessions_df=sessions_df,
            quantile=quantile,
            diagnostics=simulation_diagnostics,
        )
        episodes = finalize_episode_dataframe(episodes)
        episodes_by_quantile[quantile] = episodes
        simulation_diagnostics_by_quantile[quantile] = simulation_diagnostics

    if PRIMARY_THRESHOLD_QUANTILE not in episodes_by_quantile:
        simulation_diagnostics = Diagnostics()
        episodes_by_quantile[PRIMARY_THRESHOLD_QUANTILE] = finalize_episode_dataframe(
            run_event_driven_state_machine(
                confirmed_bubbles=confirmed_bubbles,
                raw=raw,
                calibration_features=calibration_features,
                sessions_df=sessions_df,
                quantile=PRIMARY_THRESHOLD_QUANTILE,
                diagnostics=simulation_diagnostics,
            )
        )
        simulation_diagnostics_by_quantile[PRIMARY_THRESHOLD_QUANTILE] = simulation_diagnostics

    primary_episodes = episodes_by_quantile[PRIMARY_THRESHOLD_QUANTILE]
    primary_diagnostics = combine_diagnostics(
        pipeline_diagnostics,
        simulation_diagnostics_by_quantile.get(PRIMARY_THRESHOLD_QUANTILE, Diagnostics()),
    )
    print_stage("Post-simulation validation")
    print(f"episodes generated: {len(primary_episodes):,}")
    print(f"entered trades: {int(primary_episodes['trade_entered'].sum()) if not primary_episodes.empty else 0:,}")
    print(f"candidate expiries: {int(primary_episodes['candidate_expired'].sum()) if not primary_episodes.empty else 0:,}")
    print(f"end-of-data closes: {int(primary_episodes['end_of_data_close'].sum()) if not primary_episodes.empty else 0:,}")
    print(f"ambiguous touch cases: {int(primary_episodes['touch_sequence_ambiguous'].sum()) if not primary_episodes.empty else 0:,}")

    summary_overall, summary_by_close_reason, summary_by_direction, summary_by_quantile, summary_by_continuation = build_summary_tables(
        raw=raw,
        sessions_df=sessions_df,
        qualified_bubbles=qualified_bubbles,
        outside_value_bubbles=outside_value_bubbles,
        confirmed_bubbles=confirmed_bubbles,
        episodes_by_quantile=episodes_by_quantile,
        diagnostics=primary_diagnostics,
    )

    export_results(
        output_dir=OUTPUT_DIR,
        session_profiles=session_profiles,
        qualified_bubbles=qualified_bubbles,
        outside_value_bubbles=outside_value_bubbles,
        confirmed_bubbles=confirmed_bubbles,
        reaction_confirmation_diagnostics=reaction_confirmation_diagnostics,
        calibration_features=calibration_features,
        episodes_by_quantile=episodes_by_quantile,
        summary_overall=summary_overall,
        summary_by_close_reason=summary_by_close_reason,
        summary_by_direction=summary_by_direction,
        summary_by_quantile=summary_by_quantile,
        summary_by_continuation=summary_by_continuation,
        selected_bin_size=selected_bin_size,
        diagnostics=primary_diagnostics,
        diagnostics_by_quantile={f"q{int(q * 100):02d}": d.__dict__ for q, d in simulation_diagnostics_by_quantile.items()},
    )


if __name__ == "__main__":
    main()