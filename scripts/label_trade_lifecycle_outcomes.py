import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from loader.trade_loader import load_trades_from_inputs
from utils.export import make_excel_safe


REQUIRED_EVENT_COLUMNS = [
    "event_id",
    "event_timestamp",
    "setup_confirmation_timestamp",
    "confirmation_price",
    "observation_start_timestamp",
    "observation_anchor_price",
    "anchor_price",
    "directional_bias",
    "bubble_side",
    "location",
    "session_id",
    "previous_session_id",
    "previous_val",
    "previous_vah",
    "previous_poc",
    "bubble_tier",
    "reaction_mfe_30s_pct",
    "reaction_efficiency_30s",
]
OPTIONAL_EVENT_COLUMNS = [
    "symbol",
    "bubble_anchor_price",
    "anchor_bubble_qty",
    "anchor_bubble_notional",
    "bubble_percentile_score",
    "reaction_mae_30s_pct",
    "distance_from_val_pct",
    "distance_from_vah_pct",
    "distance_from_poc_pct",
]
R_LEVELS = [1.0, 2.0, 4.0, 6.0, 8.0]
SUPPORTED_LIFECYCLE_END_REASONS = {"max_horizon_expired"}
DIAGNOSTIC_WINDOW_SECONDS = [300, 900, 3600]


@dataclass
class RiskModelConfig:
    name: str
    buffer_pct: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Label independent trade lifecycle outcomes from confirmed setup rows")
    parser.add_argument("--events-parquet", required=True)
    parser.add_argument("--raw-trades", required=True, help="Single path or comma-separated trade paths")
    parser.add_argument("--output-parquet", required=True)
    parser.add_argument("--output-xlsx", required=True)
    parser.add_argument("--max-horizon-seconds", type=int, default=3600)
    parser.add_argument("--impulse-group-gap-seconds", type=int, default=900)
    parser.add_argument(
        "--lifecycle-end-mode",
        default="expiry_only",
        choices=["expiry_only"],
    )
    parser.add_argument("--tight-stop-buffer-pct", type=float, default=0.0005)
    parser.add_argument("--medium-stop-buffer-pct", type=float, default=0.0010)
    parser.add_argument("--wide-stop-buffer-pct", type=float, default=0.0020)
    parser.add_argument(
        "--primary-risk-model",
        default="medium",
        choices=["tight", "medium", "wide"],
    )
    parser.add_argument("--minimum-profitable-r", type=float, default=2.0)
    parser.add_argument("--bubble-trades-parquet", default=None)
    args = parser.parse_args()
    if args.max_horizon_seconds <= 0:
        raise ValueError("--max-horizon-seconds must be > 0")
    if args.impulse_group_gap_seconds <= 0:
        raise ValueError("--impulse-group-gap-seconds must be > 0")
    for arg_name in [
        "tight_stop_buffer_pct",
        "medium_stop_buffer_pct",
        "wide_stop_buffer_pct",
    ]:
        value = float(getattr(args, arg_name))
        if value < 0:
            raise ValueError(f"--{arg_name.replace('_', '-')} must be >= 0")
    if args.minimum_profitable_r <= 0:
        raise ValueError("--minimum-profitable-r must be > 0")
    return args


def require_columns(df: pd.DataFrame, required_columns: list[str], label: str) -> None:
    missing = sorted(set(required_columns) - set(df.columns))
    if missing:
        raise ValueError(f"Missing required {label} columns: {missing}")


def ensure_output_dirs(*paths: str) -> None:
    for path in paths:
        out_dir = os.path.dirname(path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)


def first_true_time_seconds(mask: np.ndarray, timestamps: np.ndarray, start_timestamp: int) -> float | None:
    indices = np.flatnonzero(mask)
    if len(indices) == 0:
        return None
    return (int(timestamps[int(indices[0])]) - int(start_timestamp)) / 1000.0


def reached_price_mask(prices: np.ndarray, threshold: float, direction: str) -> np.ndarray:
    if direction == "long":
        return prices >= threshold
    return prices <= threshold


def stop_hit_mask(prices: np.ndarray, stop_price: float, direction: str) -> np.ndarray:
    if direction == "long":
        return prices <= stop_price
    return prices >= stop_price


def favorable_move_pct(entry_price: float, prices: np.ndarray, direction: str) -> np.ndarray:
    if direction == "long":
        return (prices - entry_price) / entry_price
    return (entry_price - prices) / entry_price


def adverse_move_pct(entry_price: float, prices: np.ndarray, direction: str) -> np.ndarray:
    if direction == "long":
        return (entry_price - prices) / entry_price
    return (prices - entry_price) / entry_price


def current_r_values(entry_price: float, prices: np.ndarray, risk_abs: float, direction: str) -> np.ndarray:
    if risk_abs <= 0:
        return np.full(len(prices), np.nan)
    if direction == "long":
        return (prices - entry_price) / risk_abs
    return (entry_price - prices) / risk_abs


def compute_pullback_ratios(current_r: np.ndarray) -> np.ndarray:
    running_max = np.maximum.accumulate(current_r)
    favorable = np.maximum(running_max, 0.0)
    giveback = running_max - current_r
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.where(favorable > 0, giveback / favorable, np.nan)
    return ratios


def compute_time_to_r_levels(
    timestamps: np.ndarray,
    current_r: np.ndarray,
    start_timestamp: int,
) -> dict[float, float | None]:
    results: dict[float, float | None] = {}
    for r_level in R_LEVELS:
        mask = current_r >= r_level
        results[r_level] = first_true_time_seconds(mask, timestamps, start_timestamp)
    return results


def summarize_giveback(
    timestamps: np.ndarray,
    current_r: np.ndarray,
    start_timestamp: int,
    threshold: float,
) -> tuple[bool, float | None, float | None]:
    running_max = np.maximum.accumulate(current_r)
    ratios = compute_pullback_ratios(current_r)
    hit_idx = np.flatnonzero(ratios >= threshold)
    if len(hit_idx) == 0:
        return False, None, None
    idx = int(hit_idx[0])
    seconds = (int(timestamps[idx]) - int(start_timestamp)) / 1000.0
    return True, seconds, float(running_max[idx])


def summarize_post_2r_entry_retest(
    timestamps: np.ndarray,
    prices: np.ndarray,
    current_r: np.ndarray,
    start_timestamp: int,
    entry_price: float,
    direction: str,
    time_to_2r: float | None,
) -> dict[str, Any]:
    if time_to_2r is None:
        return {
            "pulled_back_to_entry_after_2R": False,
            "time_to_entry_retest_after_2R_seconds": None,
            "max_R_before_entry_retest_after_2R": None,
        }

    seconds = (timestamps.astype(np.int64) - int(start_timestamp)) / 1000.0
    after_mask = seconds >= float(time_to_2r)
    after_indices = np.flatnonzero(after_mask)
    if len(after_indices) == 0:
        return {
            "pulled_back_to_entry_after_2R": False,
            "time_to_entry_retest_after_2R_seconds": None,
            "max_R_before_entry_retest_after_2R": None,
        }

    if direction == "long":
        retest_mask = prices[after_indices] <= entry_price
    else:
        retest_mask = prices[after_indices] >= entry_price

    retest_indices = np.flatnonzero(retest_mask)
    if len(retest_indices) > 0:
        end_pos = int(after_indices[int(retest_indices[0])])
        max_r = float(np.nanmax(current_r[after_indices[0] : end_pos + 1]))
        return {
            "pulled_back_to_entry_after_2R": True,
            "time_to_entry_retest_after_2R_seconds": float(seconds[end_pos]),
            "max_R_before_entry_retest_after_2R": max_r,
        }

    return {
        "pulled_back_to_entry_after_2R": False,
        "time_to_entry_retest_after_2R_seconds": None,
        "max_R_before_entry_retest_after_2R": float(np.nanmax(current_r[after_indices])),
    }


def compute_full_horizon_diagnostics(
    event_row: pd.Series,
    timestamps: np.ndarray,
    prices: np.ndarray,
    start_timestamp: int,
    entry_price: float,
    anchor_price: float,
    direction: str,
) -> dict[str, Any]:
    favorable = favorable_move_pct(entry_price, prices, direction)
    adverse = adverse_move_pct(entry_price, prices, direction)
    time_to_fixed = {}
    for pct in [0.25, 0.50, 1.00]:
        threshold = entry_price * (1 + pct / 100.0) if direction == "long" else entry_price * (1 - pct / 100.0)
        time_to_fixed[pct] = first_true_time_seconds(reached_price_mask(prices, threshold, direction), timestamps, start_timestamp)

    if direction == "long":
        confirmation_retest = prices <= entry_price
        anchor_retest = prices <= anchor_price
        value_reacceptance = prices <= float(event_row["previous_vah"])
    else:
        confirmation_retest = prices >= entry_price
        anchor_retest = prices >= anchor_price
        value_reacceptance = prices >= float(event_row["previous_val"])

    current_r_diag = favorable.copy()
    running_best = np.maximum.accumulate(current_r_diag)
    giveback = running_best - current_r_diag
    with np.errstate(divide="ignore", invalid="ignore"):
        pullback_ratio = np.where(running_best > 0, giveback / running_best, np.nan)

    out = {
        "mfe_1h_pct": float(np.nanmax(favorable)) if len(favorable) else None,
        "mae_1h_pct": float(np.nanmax(adverse)) if len(adverse) else None,
        "time_to_0.25_pct_seconds": time_to_fixed[0.25],
        "time_to_0.50_pct_seconds": time_to_fixed[0.50],
        "time_to_1.00_pct_seconds": time_to_fixed[1.00],
        "time_to_confirmation_retest_seconds": first_true_time_seconds(confirmation_retest, timestamps, start_timestamp),
        "time_to_anchor_retest_seconds": first_true_time_seconds(anchor_retest, timestamps, start_timestamp),
        "time_to_value_reacceptance_seconds": first_true_time_seconds(value_reacceptance, timestamps, start_timestamp),
        "time_to_50pct_pullback_seconds": first_true_time_seconds(pullback_ratio >= 0.50, timestamps, start_timestamp),
        "time_to_70pct_pullback_seconds": first_true_time_seconds(pullback_ratio >= 0.70, timestamps, start_timestamp),
    }

    for seconds in DIAGNOSTIC_WINDOW_SECONDS:
        out[f"invalidated_by_confirmation_retest_{seconds}s"] = (
            out["time_to_confirmation_retest_seconds"] is not None and out["time_to_confirmation_retest_seconds"] <= seconds
        )
        out[f"invalidated_by_anchor_retest_{seconds}s"] = (
            out["time_to_anchor_retest_seconds"] is not None and out["time_to_anchor_retest_seconds"] <= seconds
        )
        out[f"invalidated_by_value_reacceptance_{seconds}s"] = (
            out["time_to_value_reacceptance_seconds"] is not None and out["time_to_value_reacceptance_seconds"] <= seconds
        )
        out[f"invalidated_by_50pct_pullback_{seconds}s"] = (
            out["time_to_50pct_pullback_seconds"] is not None and out["time_to_50pct_pullback_seconds"] <= seconds
        )
        out[f"invalidated_by_70pct_pullback_{seconds}s"] = (
            out["time_to_70pct_pullback_seconds"] is not None and out["time_to_70pct_pullback_seconds"] <= seconds
        )
        out[f"confirmation_retest_within_{seconds}s"] = out[f"invalidated_by_confirmation_retest_{seconds}s"]
        out[f"anchor_retest_within_{seconds}s"] = out[f"invalidated_by_anchor_retest_{seconds}s"]
        out[f"value_reacceptance_within_{seconds}s"] = out[f"invalidated_by_value_reacceptance_{seconds}s"]
        out[f"pullback_50pct_within_{seconds}s"] = out[f"invalidated_by_50pct_pullback_{seconds}s"]
        out[f"pullback_70pct_within_{seconds}s"] = out[f"invalidated_by_70pct_pullback_{seconds}s"]
    return out


def compute_risk_model_metrics(
    timestamps: np.ndarray,
    prices: np.ndarray,
    start_timestamp: int,
    entry_price: float,
    anchor_price: float,
    direction: str,
    risk_model: RiskModelConfig,
) -> dict[str, Any]:
    if direction == "long":
        stop_price = anchor_price * (1 - risk_model.buffer_pct)
        risk_abs = entry_price - stop_price
    else:
        stop_price = anchor_price * (1 + risk_model.buffer_pct)
        risk_abs = stop_price - entry_price

    risk_pct = risk_abs / entry_price if entry_price > 0 else None
    prefix = risk_model.name

    if risk_abs <= 0:
        out = {
            f"{prefix}_stop_price": stop_price,
            f"{prefix}_risk_abs": risk_abs,
            f"{prefix}_risk_pct": risk_pct,
            f"{prefix}_stop_hit_within_horizon": None,
            f"{prefix}_stop_hit_before_2R": None,
            f"{prefix}_time_to_stop_seconds": None,
            f"{prefix}_max_R_within_horizon": None,
            f"{prefix}_max_R_lifecycle": None,
            f"{prefix}_profitable_2R_before_SL": None,
            f"{prefix}_pulled_back_to_entry_after_2R": False,
            f"{prefix}_time_to_entry_retest_after_2R_seconds": None,
            f"{prefix}_max_R_before_entry_retest_after_2R": None,
            f"{prefix}_giveback_50pct_after_max": False,
            f"{prefix}_giveback_70pct_after_max": False,
            f"{prefix}_time_to_50pct_giveback_seconds": None,
            f"{prefix}_time_to_70pct_giveback_seconds": None,
            f"{prefix}_max_R_before_50pct_giveback": None,
            f"{prefix}_max_R_before_70pct_giveback": None,
        }
        for r_level in R_LEVELS:
            label = int(r_level)
            out[f"{prefix}_time_to_{label}R_seconds"] = None
            out[f"{prefix}_reached_{label}R_before_stop"] = None
        return out

    current_r = current_r_values(entry_price, prices, risk_abs, direction)
    stop_time = first_true_time_seconds(stop_hit_mask(prices, stop_price, direction), timestamps, start_timestamp)
    time_to_r = compute_time_to_r_levels(timestamps, current_r, start_timestamp)
    time_to_2r = time_to_r[2.0]
    reached_before_stop: dict[float, bool] = {}
    for r_level in R_LEVELS:
        t_r = time_to_r[r_level]
        reached_before_stop[r_level] = t_r is not None and (stop_time is None or t_r < stop_time)

    post_2r = summarize_post_2r_entry_retest(
        timestamps=timestamps,
        prices=prices,
        current_r=current_r,
        start_timestamp=start_timestamp,
        entry_price=entry_price,
        direction=direction,
        time_to_2r=time_to_2r,
    )
    gb50, gb50_time, gb50_max_r = summarize_giveback(timestamps, current_r, start_timestamp, 0.50)
    gb70, gb70_time, gb70_max_r = summarize_giveback(timestamps, current_r, start_timestamp, 0.70)

    out: dict[str, Any] = {
        f"{prefix}_stop_price": float(stop_price),
        f"{prefix}_risk_abs": float(risk_abs),
        f"{prefix}_risk_pct": float(risk_pct) if risk_pct is not None else None,
        f"{prefix}_stop_hit_within_horizon": stop_time is not None,
        f"{prefix}_stop_hit_before_2R": stop_time is not None and (time_to_2r is None or stop_time < time_to_2r),
        f"{prefix}_time_to_stop_seconds": stop_time,
        f"{prefix}_max_R_within_horizon": float(np.nanmax(current_r)) if len(current_r) else None,
        f"{prefix}_max_R_lifecycle": float(np.nanmax(current_r)) if len(current_r) else None,
        f"{prefix}_profitable_2R_before_SL": reached_before_stop[2.0],
        f"{prefix}_pulled_back_to_entry_after_2R": post_2r["pulled_back_to_entry_after_2R"],
        f"{prefix}_time_to_entry_retest_after_2R_seconds": post_2r["time_to_entry_retest_after_2R_seconds"],
        f"{prefix}_max_R_before_entry_retest_after_2R": post_2r["max_R_before_entry_retest_after_2R"],
        f"{prefix}_giveback_50pct_after_max": gb50,
        f"{prefix}_giveback_70pct_after_max": gb70,
        f"{prefix}_time_to_50pct_giveback_seconds": gb50_time,
        f"{prefix}_time_to_70pct_giveback_seconds": gb70_time,
        f"{prefix}_max_R_before_50pct_giveback": gb50_max_r,
        f"{prefix}_max_R_before_70pct_giveback": gb70_max_r,
    }
    for r_level in R_LEVELS:
        label = int(r_level)
        out[f"{prefix}_time_to_{label}R_seconds"] = time_to_r[r_level]
        out[f"{prefix}_reached_{label}R_before_stop"] = reached_before_stop[r_level]
    return out


def compute_fixed_lifecycle(entry_timestamp: int, max_horizon_seconds: int, lifecycle_end_mode: str) -> tuple[int, str, float]:
    if lifecycle_end_mode != "expiry_only":
        raise ValueError(f"Unsupported lifecycle end mode: {lifecycle_end_mode}")
    return entry_timestamp + int(max_horizon_seconds * 1000), "max_horizon_expired", float(max_horizon_seconds)


def assign_main_outcome_label(row: dict[str, Any], primary_risk_model: str, minimum_profitable_r: float) -> tuple[str, bool]:
    if minimum_profitable_r != 2.0:
        raise ValueError("Only --minimum-profitable-r=2.0 is supported in v1")
    profitable = row.get(f"{primary_risk_model}_profitable_2R_before_SL") is True
    label = "profitable_2R_before_SL" if profitable else "not_profitable_2R_before_SL"
    return label, profitable


def normalize_output_precision(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].round(6)
    return out


def add_time_review_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    timestamp_columns = {
        "event_timestamp": "event_time",
        "entry_timestamp": "entry_time",
        "horizon_end_timestamp": "horizon_end_time",
        "lifecycle_end_timestamp": "lifecycle_end_time",
    }

    for ts_col, prefix in timestamp_columns.items():
        if ts_col not in out.columns:
            continue

        utc_dt = pd.to_datetime(out[ts_col], unit="ms", utc=True, errors="coerce")
        wib_dt = utc_dt.dt.tz_convert("Asia/Jakarta")

        out[f"{prefix}_utc_str"] = utc_dt.dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3] + " UTC"
        out[f"{prefix}_wib_str"] = wib_dt.dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3] + " WIB"

    return out


def validate_raw_trade_coverage(trades_df: pd.DataFrame, events_df: pd.DataFrame, max_horizon_seconds: int) -> None:
    raw_min_timestamp = int(trades_df["timestamp"].min())
    raw_max_timestamp = int(trades_df["timestamp"].max())
    events_min_entry = int(events_df["setup_confirmation_timestamp"].min())
    events_max_required = int(events_df["setup_confirmation_timestamp"].max()) + int(max_horizon_seconds * 1000)
    if raw_min_timestamp > events_min_entry or raw_max_timestamp < events_max_required:
        raise ValueError(
            "Raw trade coverage is insufficient for event labeling: "
            f"events_min_entry_utc={pd.to_datetime(events_min_entry, unit='ms', utc=True)}, "
            f"events_max_required_utc={pd.to_datetime(events_max_required, unit='ms', utc=True)}, "
            f"raw_min_utc={pd.to_datetime(raw_min_timestamp, unit='ms', utc=True)}, "
            f"raw_max_utc={pd.to_datetime(raw_max_timestamp, unit='ms', utc=True)}"
        )


def get_optional_context_columns(events_df: pd.DataFrame) -> list[str]:
    preserved = []
    for column in events_df.columns:
        lower = column.lower()
        if column in REQUIRED_EVENT_COLUMNS or column in OPTIONAL_EVENT_COLUMNS:
            continue
        if "opposite_bubble" in lower or "oppositebubble" in lower:
            preserved.append(column)
    return preserved


def assign_impulse_groups(events_df: pd.DataFrame, impulse_group_gap_seconds: int) -> pd.DataFrame:
    out = events_df.sort_values(
        ["session_id", "directional_bias", "setup_confirmation_timestamp", "event_timestamp"],
        kind="mergesort",
    ).reset_index(drop=True).copy()

    gap_ms = int(impulse_group_gap_seconds * 1000)
    impulse_group_ids: list[str] = []

    for (session_id, direction), group in out.groupby(["session_id", "directional_bias"], sort=False):
        previous_timestamp: int | None = None
        group_number = 0
        for idx in group.index:
            current_timestamp = int(out.at[idx, "setup_confirmation_timestamp"])
            if previous_timestamp is None or (current_timestamp - previous_timestamp) > gap_ms:
                group_number += 1
            impulse_group_ids.append(f"{session_id}_{direction}_{group_number}")
            previous_timestamp = current_timestamp

    out["impulse_group_id"] = impulse_group_ids
    out["impulse_group_gap_seconds"] = int(impulse_group_gap_seconds)
    return out


def validate_output(
    df: pd.DataFrame,
    input_events_count: int,
    max_horizon_seconds: int,
    lifecycle_end_mode: str,
) -> None:
    if not df["lifecycle_id"].is_unique:
        raise ValueError("lifecycle_id must be unique")
    if len(df) > input_events_count:
        raise ValueError("Output lifecycle row count must be <= input confirmed setup row count")
    if not (df["entry_timestamp"] == df["setup_confirmation_timestamp"]).all():
        raise ValueError("entry_timestamp must equal setup_confirmation_timestamp")
    if not (df["entry_price"] == df["confirmation_price"]).all():
        raise ValueError("entry_price must equal confirmation_price")
    if not df["source_event_id"].notna().all():
        raise ValueError("Every lifecycle row must map to one source_event_id")
    if not df["impulse_group_id"].notna().all():
        raise ValueError("Every lifecycle row must map to one impulse_group_id")

    if "entry_dt_utc" in df.columns and "entry_dt_wib" in df.columns:
        utc_values = pd.to_datetime(df["entry_dt_utc"], errors="coerce")
        wib_values = pd.to_datetime(df["entry_dt_wib"], errors="coerce")
        diff_hours = (wib_values - utc_values).dt.total_seconds() / 3600.0
        valid = diff_hours.dropna()
        if not valid.empty and not np.allclose(valid.to_numpy(dtype=np.float64), 7.0):
            raise ValueError("entry_dt_wib must be exactly UTC+7 wall time relative to entry_dt_utc")

    for column in [col for col in df.columns if col.startswith("time_to_") or col.endswith("_seconds")]:
        numeric = pd.to_numeric(df[column], errors="coerce")
        invalid = numeric.notna() & ((numeric < 0) | (numeric > max_horizon_seconds))
        if invalid.any():
            raise ValueError(f"Invalid time values in {column}")

    for model in ["tight", "medium", "wide"]:
        for r_level in R_LEVELS:
            label = int(r_level)
            reached_col = f"{model}_reached_{label}R_before_stop"
            time_col = f"{model}_time_to_{label}R_seconds"
            stop_col = f"{model}_time_to_stop_seconds"
            invalid_missing_time = (df[reached_col] == True) & (df[time_col].isna())
            if invalid_missing_time.any():
                raise ValueError(f"{reached_col} requires non-null {time_col}")
            stop_before_target = (
                df[stop_col].notna()
                & df[time_col].notna()
                & (pd.to_numeric(df[stop_col], errors="coerce") < pd.to_numeric(df[time_col], errors="coerce"))
                & (df[reached_col] == True)
            )
            if stop_before_target.any():
                raise ValueError(f"{reached_col} must be False when {stop_col} is before {time_col}")

    if not df["lifecycle_end_reason"].isin(SUPPORTED_LIFECYCLE_END_REASONS).all():
        raise ValueError("Unexpected lifecycle_end_reason detected")

    if lifecycle_end_mode == "expiry_only":
        if not (df["lifecycle_end_timestamp"] == df["horizon_end_timestamp"]).all():
            raise ValueError("In expiry_only mode, lifecycle_end_timestamp must equal horizon_end_timestamp")
        if not (df["lifecycle_end_reason"] == "max_horizon_expired").all():
            raise ValueError("In expiry_only mode, lifecycle_end_reason must be max_horizon_expired")
        if not (pd.to_numeric(df["lifecycle_duration_seconds"], errors="coerce") == float(max_horizon_seconds)).all():
            raise ValueError("In expiry_only mode, lifecycle_duration_seconds must equal max_horizon_seconds")


def print_summary(
    df: pd.DataFrame,
    input_count: int,
    grouped_member_count: int,
    primary_risk_model: str,
    minimum_profitable_r: float,
) -> None:
    print("\nLifecycle Labeling Summary:")
    print(f"input confirmed setup rows: {input_count}")
    print(f"lifecycle output rows: {len(df)}")
    print(f"non-representative grouped setup rows: {grouped_member_count}")
    print("\nlifecycle count by direction:")
    print(df["directional_bias"].value_counts(dropna=False))
    print("\nmain outcome label distribution:")
    print(df["main_outcome_label"].value_counts(dropna=False))
    print(f"\nprimary risk model: {primary_risk_model}")
    print(f"minimum profitable R: {minimum_profitable_r}")
    print(f"profitable rate using primary risk model: {df['primary_profitable_before_SL'].mean(skipna=True):.4f}")
    for model in ["tight", "medium", "wide"]:
        print(f"\n{model} profitable_2R_before_SL rate: {df[f'{model}_profitable_2R_before_SL'].mean(skipna=True):.4f}")
        print(f"{model} stop_hit_within_horizon rate: {df[f'{model}_stop_hit_within_horizon'].mean(skipna=True):.4f}")
        for r_level in [1, 2, 4, 6, 8]:
            print(f"{model} reached_{r_level}R_before_stop rate: {df[f'{model}_reached_{r_level}R_before_stop'].mean(skipna=True):.4f}")
    print("\ndiagnostic retest/pullback rates:")
    for col in [
        "confirmation_retest_within_300s",
        "anchor_retest_within_300s",
        "value_reacceptance_within_300s",
        "pullback_50pct_within_300s",
        "pullback_70pct_within_300s",
    ]:
        print(f"{col}: {df[col].mean(skipna=True):.4f}")


def main() -> None:
    args = parse_args()
    ensure_output_dirs(args.output_parquet, args.output_xlsx)

    print(f"Loading confirmed setups from {args.events_parquet}")
    events_df = pd.read_parquet(args.events_parquet)
    require_columns(events_df, REQUIRED_EVENT_COLUMNS, "event parquet")
    for optional_col in OPTIONAL_EVENT_COLUMNS:
        if optional_col not in events_df.columns:
            events_df[optional_col] = None
    if "bubble_anchor_price" not in events_df.columns:
        events_df["bubble_anchor_price"] = events_df["anchor_price"]
    events_df["bubble_anchor_price"] = events_df["bubble_anchor_price"].fillna(events_df["anchor_price"])
    optional_context_columns = get_optional_context_columns(events_df)

    print(f"Loading raw trades from {args.raw_trades}")
    trades_df = load_trades_from_inputs(args.raw_trades)
    if trades_df.empty:
        raise ValueError("No raw trades were loaded from the provided input dataset")

    events_df = assign_impulse_groups(events_df, args.impulse_group_gap_seconds)
    trades_df = trades_df.sort_values(["timestamp"], kind="mergesort").reset_index(drop=True)
    validate_raw_trade_coverage(trades_df, events_df, args.max_horizon_seconds)

    trades_timestamps = trades_df["timestamp"].to_numpy(dtype=np.int64)
    trades_prices = trades_df["price"].to_numpy(dtype=np.float64)
    symbol_value = events_df["symbol"].dropna().iloc[0] if "symbol" in events_df.columns and events_df["symbol"].notna().any() else "BTCUSDT"

    risk_models = [
        RiskModelConfig("tight", float(args.tight_stop_buffer_pct)),
        RiskModelConfig("medium", float(args.medium_stop_buffer_pct)),
        RiskModelConfig("wide", float(args.wide_stop_buffer_pct)),
    ]

    lifecycle_rows: list[dict[str, Any]] = []
    representative_events_df = events_df.groupby("impulse_group_id", sort=False, as_index=False).first()
    grouped_member_count = len(events_df) - len(representative_events_df)

    for _, event_row in representative_events_df.iterrows():
        session_id = str(event_row["session_id"])
        direction = str(event_row["directional_bias"])
        entry_timestamp = int(event_row["setup_confirmation_timestamp"])
        entry_dt_utc = pd.to_datetime(entry_timestamp, unit="ms", utc=True)
        entry_dt_wib = entry_dt_utc.tz_convert("Asia/Jakarta")

        horizon_end_timestamp = entry_timestamp + int(args.max_horizon_seconds * 1000)
        start_idx = int(np.searchsorted(trades_timestamps, entry_timestamp, side="right"))
        end_idx = int(np.searchsorted(trades_timestamps, horizon_end_timestamp, side="right"))
        window_timestamps = trades_timestamps[start_idx:end_idx]
        window_prices = trades_prices[start_idx:end_idx]
        if len(window_timestamps) == 0:
            continue

        entry_price = float(event_row["confirmation_price"])
        anchor_price = float(event_row["bubble_anchor_price"] if pd.notna(event_row["bubble_anchor_price"]) else event_row["anchor_price"])

        diagnostics = compute_full_horizon_diagnostics(
            event_row=event_row,
            timestamps=window_timestamps,
            prices=window_prices,
            start_timestamp=entry_timestamp,
            entry_price=entry_price,
            anchor_price=anchor_price,
            direction=direction,
        )
        risk_results: dict[str, Any] = {}
        for risk_model in risk_models:
            risk_results.update(
                compute_risk_model_metrics(
                    timestamps=window_timestamps,
                    prices=window_prices,
                    start_timestamp=entry_timestamp,
                    entry_price=entry_price,
                    anchor_price=anchor_price,
                    direction=direction,
                    risk_model=risk_model,
                )
            )

        lifecycle_end_timestamp, lifecycle_end_reason, lifecycle_duration_seconds = compute_fixed_lifecycle(
            entry_timestamp=entry_timestamp,
            max_horizon_seconds=args.max_horizon_seconds,
            lifecycle_end_mode=args.lifecycle_end_mode,
        )

        lifecycle_favorable = favorable_move_pct(entry_price, window_prices, direction)
        lifecycle_adverse = adverse_move_pct(entry_price, window_prices, direction)

        row: dict[str, Any] = {
            "lifecycle_id": uuid.uuid4().hex,
            "source_event_id": event_row["event_id"],
            "symbol": event_row["symbol"] if pd.notna(event_row["symbol"]) else symbol_value,
            "session_id": session_id,
            "previous_session_id": event_row["previous_session_id"],
            "directional_bias": direction,
            "location": event_row["location"],
            "event_timestamp": int(event_row["event_timestamp"]),
            "setup_confirmation_timestamp": entry_timestamp,
            "entry_timestamp": entry_timestamp,
            "entry_dt_utc": entry_dt_utc.tz_localize(None),
            "entry_dt_wib": entry_dt_wib.tz_localize(None),
            "horizon_end_timestamp": horizon_end_timestamp,
            "lifecycle_end_timestamp": lifecycle_end_timestamp,
            "lifecycle_end_reason": lifecycle_end_reason,
            "lifecycle_duration_seconds": lifecycle_duration_seconds,
            "entry_price": entry_price,
            "confirmation_price": entry_price,
            "anchor_price": float(event_row["anchor_price"]),
            "previous_val": float(event_row["previous_val"]),
            "previous_vah": float(event_row["previous_vah"]),
            "previous_poc": float(event_row["previous_poc"]),
            "bubble_tier": event_row["bubble_tier"],
            "anchor_bubble_qty": event_row["anchor_bubble_qty"],
            "anchor_bubble_notional": event_row["anchor_bubble_notional"],
            "bubble_percentile_score": event_row["bubble_percentile_score"],
            "reaction_mfe_30s_pct": event_row["reaction_mfe_30s_pct"],
            "reaction_mae_30s_pct": event_row["reaction_mae_30s_pct"],
            "reaction_efficiency_30s": event_row["reaction_efficiency_30s"],
            "distance_from_val_pct": event_row["distance_from_val_pct"],
            "distance_from_vah_pct": event_row["distance_from_vah_pct"],
            "distance_from_poc_pct": event_row["distance_from_poc_pct"],
            "lifecycle_mfe_pct": float(np.nanmax(lifecycle_favorable)) if len(lifecycle_favorable) else None,
            "lifecycle_mae_pct": float(np.nanmax(lifecycle_adverse)) if len(lifecycle_adverse) else None,
            "inside_lifecycle_confirmed_setup_count": 0,
            "inside_lifecycle_same_side_setup_count": 0,
            "inside_lifecycle_opposite_side_setup_count": 0,
            "first_inside_lifecycle_setup_seconds": None,
            "first_same_side_inside_lifecycle_setup_seconds": None,
            "first_opposite_side_inside_lifecycle_setup_seconds": None,
            "impulse_group_id": event_row["impulse_group_id"],
            "impulse_group_gap_seconds": int(event_row["impulse_group_gap_seconds"]),
            "primary_risk_model": args.primary_risk_model,
            "minimum_profitable_r": float(args.minimum_profitable_r),
        }
        for column in optional_context_columns:
            row[column] = event_row[column]
        row.update(diagnostics)
        row.update(risk_results)
        label, primary_profitable = assign_main_outcome_label(row, args.primary_risk_model, float(args.minimum_profitable_r))
        row["main_outcome_label"] = label
        row["primary_profitable_before_SL"] = primary_profitable
        row["reached_2R_before_stop"] = row.get(f"{args.primary_risk_model}_reached_2R_before_stop")
        row["stop_before_2R"] = row.get(f"{args.primary_risk_model}_stop_hit_before_2R")

        lifecycle_rows.append(row)

    output_df = pd.DataFrame(lifecycle_rows)
    if output_df.empty:
        print("No lifecycle rows created")
        return

    output_df = add_time_review_columns(output_df)
    output_df = normalize_output_precision(output_df)
    validate_output(
        output_df,
        input_events_count=len(events_df),
        max_horizon_seconds=args.max_horizon_seconds,
        lifecycle_end_mode=args.lifecycle_end_mode,
    )
    print(f"Writing parquet to {args.output_parquet}")
    output_df.to_parquet(args.output_parquet, index=False)
    print(f"Writing xlsx to {args.output_xlsx}")
    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        make_excel_safe(output_df).to_excel(writer, sheet_name="trade_lifecycle_outcomes", index=False)
    print_summary(
        output_df,
        input_count=len(events_df),
        grouped_member_count=grouped_member_count,
        primary_risk_model=args.primary_risk_model,
        minimum_profitable_r=float(args.minimum_profitable_r),
    )


if __name__ == "__main__":
    main()
