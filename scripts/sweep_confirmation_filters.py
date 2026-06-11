import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from loader.trade_loader import load_trades_from_inputs


R_LEVELS = [1.0, 2.0, 4.0, 6.0, 8.0]
BASE_REQUIRED_EVENT_COLUMNS = [
    "event_id",
    "event_timestamp",
    "setup_confirmation_timestamp",
    "confirmation_price",
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


@dataclass
class RiskModelConfig:
    name: str
    buffer_pct: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sweep reaction MFE / efficiency confirmation filters over broad candidate event parquet"
    )
    parser.add_argument("--events-parquet", required=True)
    parser.add_argument("--raw-trades", required=True, help="Single path or comma-separated trade paths")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reaction-window-seconds", type=int, default=30)
    parser.add_argument(
        "--mfe-thresholds-pct",
        default="0,0.00025,0.0005,0.00075,0.001,0.0015,0.002",
    )
    parser.add_argument(
        "--efficiency-thresholds",
        default="0.40,0.50,0.60,0.70,0.80,0.90",
    )
    parser.add_argument("--max-horizon-seconds", type=int, default=3600)
    parser.add_argument("--impulse-group-gap-seconds", type=int, default=900)
    parser.add_argument("--primary-risk-model", choices=["tight", "medium", "wide"], default="medium")
    parser.add_argument("--tight-stop-buffer-pct", type=float, default=0.0005)
    parser.add_argument("--medium-stop-buffer-pct", type=float, default=0.0010)
    parser.add_argument("--wide-stop-buffer-pct", type=float, default=0.0020)
    parser.add_argument("--write-combo-lifecycle-rows", action="store_true")
    parser.add_argument("--max-histogram-r", type=float, default=10.0)
    args = parser.parse_args()

    if args.reaction_window_seconds <= 0:
        raise ValueError("--reaction-window-seconds must be > 0")
    if args.max_horizon_seconds <= 0:
        raise ValueError("--max-horizon-seconds must be > 0")
    if args.impulse_group_gap_seconds <= 0:
        raise ValueError("--impulse-group-gap-seconds must be > 0")
    if args.max_histogram_r <= 0:
        raise ValueError("--max-histogram-r must be > 0")
    for arg_name in [
        "tight_stop_buffer_pct",
        "medium_stop_buffer_pct",
        "wide_stop_buffer_pct",
    ]:
        value = float(getattr(args, arg_name))
        if value < 0:
            raise ValueError(f"--{arg_name.replace('_', '-')} must be >= 0")
    return args


def parse_float_list(raw_value: str, arg_name: str) -> list[float]:
    values = [float(part.strip()) for part in raw_value.split(",") if part.strip()]
    if not values:
        raise ValueError(f"{arg_name} must contain at least one numeric value")
    unique_sorted = sorted(set(values))
    return unique_sorted


def require_columns(df: pd.DataFrame, required_columns: list[str], label: str) -> None:
    missing = sorted(set(required_columns) - set(df.columns))
    if missing:
        raise ValueError(f"Missing required {label} columns: {missing}")


def ensure_output_dirs(output_dir: str, write_combo_lifecycle_rows: bool) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    histogram_dir = os.path.join(output_dir, "histograms")
    os.makedirs(histogram_dir, exist_ok=True)
    combo_dir = os.path.join(output_dir, "combo_lifecycle_rows")
    if write_combo_lifecycle_rows:
        os.makedirs(combo_dir, exist_ok=True)
    return {
        "output_dir": output_dir,
        "histogram_dir": histogram_dir,
        "combo_dir": combo_dir,
    }


def load_events(events_parquet: str, reaction_window_seconds: int) -> pd.DataFrame:
    mfe_col = f"reaction_mfe_{reaction_window_seconds}s_pct"
    eff_col = f"reaction_efficiency_{reaction_window_seconds}s"
    required_columns = BASE_REQUIRED_EVENT_COLUMNS + [mfe_col, eff_col]

    events_df = pd.read_parquet(events_parquet)
    require_columns(events_df, required_columns, "event parquet")

    for optional_col in OPTIONAL_EVENT_COLUMNS:
        if optional_col not in events_df.columns:
            events_df[optional_col] = None

    if "bubble_anchor_price" not in events_df.columns:
        events_df["bubble_anchor_price"] = events_df["anchor_price"]
    events_df["bubble_anchor_price"] = events_df["bubble_anchor_price"].fillna(events_df["anchor_price"])

    numeric_columns = [
        "event_timestamp",
        "setup_confirmation_timestamp",
        "confirmation_price",
        "anchor_price",
        "bubble_anchor_price",
        "previous_val",
        "previous_vah",
        "previous_poc",
        mfe_col,
        eff_col,
    ]
    for column in numeric_columns:
        events_df[column] = pd.to_numeric(events_df[column], errors="coerce")

    events_df = events_df.sort_values(
        ["session_id", "directional_bias", "setup_confirmation_timestamp", "event_timestamp", "event_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    return events_df


def validate_raw_trades(trades_df: pd.DataFrame) -> pd.DataFrame:
    require_columns(trades_df, ["timestamp", "price"], "raw trades")
    trades_df = trades_df.sort_values(["timestamp", "price"], kind="mergesort").reset_index(drop=True)
    trades_df["timestamp"] = pd.to_numeric(trades_df["timestamp"], errors="raise").astype("int64")
    trades_df["price"] = pd.to_numeric(trades_df["price"], errors="raise").astype("float64")
    return trades_df


def validate_raw_trade_coverage(trades_df: pd.DataFrame, events_df: pd.DataFrame, max_horizon_seconds: int) -> None:
    if events_df.empty:
        return
    raw_min_timestamp = int(trades_df["timestamp"].min())
    raw_max_timestamp = int(trades_df["timestamp"].max())
    events_min_entry = int(events_df["setup_confirmation_timestamp"].min())
    events_max_required = int(events_df["setup_confirmation_timestamp"].max()) + int(max_horizon_seconds * 1000)
    if raw_min_timestamp > events_min_entry or raw_max_timestamp < events_max_required:
        raise ValueError(
            "Raw trade coverage is insufficient for sweep labeling: "
            f"events_min_entry_utc={pd.to_datetime(events_min_entry, unit='ms', utc=True)}, "
            f"events_max_required_utc={pd.to_datetime(events_max_required, unit='ms', utc=True)}, "
            f"raw_min_utc={pd.to_datetime(raw_min_timestamp, unit='ms', utc=True)}, "
            f"raw_max_utc={pd.to_datetime(raw_max_timestamp, unit='ms', utc=True)}"
        )


def filter_events_for_combo(
    events_df: pd.DataFrame,
    reaction_window_seconds: int,
    mfe_threshold: float,
    efficiency_threshold: float,
) -> pd.DataFrame:
    mfe_col = f"reaction_mfe_{reaction_window_seconds}s_pct"
    eff_col = f"reaction_efficiency_{reaction_window_seconds}s"
    filtered = events_df[
        (events_df[mfe_col] >= mfe_threshold)
        & (events_df[eff_col] >= efficiency_threshold)
    ].copy()
    return filtered


def assign_impulse_groups(events_df: pd.DataFrame, impulse_group_gap_seconds: int) -> pd.DataFrame:
    out = events_df.sort_values(
        ["session_id", "directional_bias", "setup_confirmation_timestamp", "event_timestamp", "event_id"],
        kind="mergesort",
    ).reset_index(drop=True).copy()

    gap_ms = int(impulse_group_gap_seconds * 1000)
    impulse_group_ids: list[str] = [""] * len(out)

    for (_, _), group in out.groupby(["session_id", "directional_bias"], sort=False):
        previous_timestamp: int | None = None
        group_number = 0
        for idx in group.index:
            current_timestamp = int(out.at[idx, "setup_confirmation_timestamp"])
            if previous_timestamp is None or (current_timestamp - previous_timestamp) > gap_ms:
                group_number += 1
            impulse_group_ids[int(idx)] = (
                f"{out.at[idx, 'session_id']}_{out.at[idx, 'directional_bias']}_{group_number}"
            )
            previous_timestamp = current_timestamp

    out["impulse_group_id"] = impulse_group_ids
    out["impulse_group_gap_seconds"] = int(impulse_group_gap_seconds)
    return out


def first_true_time_seconds(mask: np.ndarray, timestamps: np.ndarray, start_timestamp: int) -> float | None:
    indices = np.flatnonzero(mask)
    if len(indices) == 0:
        return None
    return (int(timestamps[int(indices[0])]) - int(start_timestamp)) / 1000.0


def stop_hit_mask(prices: np.ndarray, stop_price: float, direction: str) -> np.ndarray:
    if direction == "long":
        return prices <= stop_price
    return prices >= stop_price


def current_r_values(entry_price: float, prices: np.ndarray, risk_abs: float, direction: str) -> np.ndarray:
    if risk_abs <= 0:
        return np.full(len(prices), np.nan)
    if direction == "long":
        return (prices - entry_price) / risk_abs
    return (entry_price - prices) / risk_abs


def compute_time_to_r_levels(
    timestamps: np.ndarray,
    current_r: np.ndarray,
    start_timestamp: int,
) -> dict[float, float | None]:
    results: dict[float, float | None] = {}
    for r_level in R_LEVELS:
        results[r_level] = first_true_time_seconds(current_r >= r_level, timestamps, start_timestamp)
    return results


def compute_max_r_before_stop(
    timestamps: np.ndarray,
    current_r: np.ndarray,
    start_timestamp: int,
    stop_time: float | None,
) -> tuple[float | None, float | None]:
    if len(current_r) == 0:
        return None, None

    seconds_from_entry = (timestamps.astype(np.int64) - int(start_timestamp)) / 1000.0
    if stop_time is not None:
        valid_mask = seconds_from_entry <= float(stop_time)
        valid_indices = np.flatnonzero(valid_mask)
        if len(valid_indices) == 0:
            return None, None
        valid_r = current_r[valid_indices]
        max_r = float(np.nanmax(valid_r))
        max_match = valid_indices[np.flatnonzero(valid_r == max_r)]
        first_idx = int(max_match[0])
        return max_r, float(seconds_from_entry[first_idx])

    max_r = float(np.nanmax(current_r))
    max_indices = np.flatnonzero(current_r == max_r)
    if len(max_indices) == 0:
        return None, None
    first_idx = int(max_indices[0])
    return max_r, float(seconds_from_entry[first_idx])


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

    prefix = risk_model.name
    risk_pct = risk_abs / entry_price if entry_price > 0 else None

    if risk_abs <= 0:
        out: dict[str, Any] = {
            f"{prefix}_stop_price": stop_price,
            f"{prefix}_risk_abs": risk_abs,
            f"{prefix}_risk_pct": risk_pct,
            f"{prefix}_time_to_stop_seconds": None,
            f"{prefix}_stop_hit_within_horizon": None,
            f"{prefix}_stop_hit_before_2R": None,
            f"{prefix}_max_R_within_horizon": None,
            f"{prefix}_max_R_before_stop": None,
            f"{prefix}_time_to_max_R_before_stop_seconds": None,
            f"{prefix}_profitable_2R_before_SL": None,
        }
        for r_level in R_LEVELS:
            label = int(r_level)
            out[f"{prefix}_time_to_{label}R_seconds"] = None
            out[f"{prefix}_reached_{label}R_before_stop"] = None
        return out

    current_r = current_r_values(entry_price, prices, risk_abs, direction)
    stop_time = first_true_time_seconds(stop_hit_mask(prices, stop_price, direction), timestamps, start_timestamp)
    max_r_before_stop, time_to_max_r_before_stop = compute_max_r_before_stop(
        timestamps=timestamps,
        current_r=current_r,
        start_timestamp=start_timestamp,
        stop_time=stop_time,
    )
    time_to_r = compute_time_to_r_levels(timestamps, current_r, start_timestamp)
    time_to_2r = time_to_r[2.0]

    reached_before_stop: dict[float, bool] = {}
    for r_level in R_LEVELS:
        t_r = time_to_r[r_level]
        reached_before_stop[r_level] = t_r is not None and (stop_time is None or t_r < stop_time)

    out = {
        f"{prefix}_stop_price": float(stop_price),
        f"{prefix}_risk_abs": float(risk_abs),
        f"{prefix}_risk_pct": float(risk_pct) if risk_pct is not None else None,
        f"{prefix}_time_to_stop_seconds": stop_time,
        f"{prefix}_stop_hit_within_horizon": stop_time is not None,
        f"{prefix}_stop_hit_before_2R": stop_time is not None and (time_to_2r is None or stop_time < time_to_2r),
        f"{prefix}_time_to_1R_seconds": time_to_r[1.0],
        f"{prefix}_time_to_2R_seconds": time_to_r[2.0],
        f"{prefix}_time_to_4R_seconds": time_to_r[4.0],
        f"{prefix}_time_to_6R_seconds": time_to_r[6.0],
        f"{prefix}_time_to_8R_seconds": time_to_r[8.0],
        f"{prefix}_reached_1R_before_stop": reached_before_stop[1.0],
        f"{prefix}_reached_2R_before_stop": reached_before_stop[2.0],
        f"{prefix}_reached_4R_before_stop": reached_before_stop[4.0],
        f"{prefix}_reached_6R_before_stop": reached_before_stop[6.0],
        f"{prefix}_reached_8R_before_stop": reached_before_stop[8.0],
        f"{prefix}_max_R_within_horizon": float(np.nanmax(current_r)) if len(current_r) else None,
        f"{prefix}_max_R_before_stop": max_r_before_stop,
        f"{prefix}_time_to_max_R_before_stop_seconds": time_to_max_r_before_stop,
        f"{prefix}_profitable_2R_before_SL": reached_before_stop[2.0],
    }
    return out


def assign_main_outcome_label(row: dict[str, Any], primary_risk_model: str) -> tuple[str, bool]:
    profitable = row.get(f"{primary_risk_model}_profitable_2R_before_SL") is True
    label = "profitable_2R_before_SL" if profitable else "not_profitable_2R_before_SL"
    return label, profitable


def label_lifecycle_for_representatives(
    representatives_df: pd.DataFrame,
    trades_timestamps: np.ndarray,
    trades_prices: np.ndarray,
    risk_models: list[RiskModelConfig],
    max_horizon_seconds: int,
    primary_risk_model: str,
    reaction_window_seconds: int,
) -> pd.DataFrame:
    lifecycle_rows: list[dict[str, Any]] = []
    mfe_col = f"reaction_mfe_{reaction_window_seconds}s_pct"
    mae_col = f"reaction_mae_{reaction_window_seconds}s_pct"
    eff_col = f"reaction_efficiency_{reaction_window_seconds}s"

    for _, event_row in representatives_df.iterrows():
        entry_timestamp = int(event_row["setup_confirmation_timestamp"])
        horizon_end_timestamp = entry_timestamp + int(max_horizon_seconds * 1000)
        start_idx = int(np.searchsorted(trades_timestamps, entry_timestamp, side="right"))
        end_idx = int(np.searchsorted(trades_timestamps, horizon_end_timestamp, side="right"))
        window_timestamps = trades_timestamps[start_idx:end_idx]
        window_prices = trades_prices[start_idx:end_idx]
        if len(window_timestamps) == 0:
            continue

        direction = str(event_row["directional_bias"])
        entry_price = float(event_row["confirmation_price"])
        anchor_price = float(
            event_row["bubble_anchor_price"] if pd.notna(event_row["bubble_anchor_price"]) else event_row["anchor_price"]
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

        row: dict[str, Any] = {
            "lifecycle_id": uuid.uuid4().hex,
            "source_event_id": event_row["event_id"],
            "session_id": event_row["session_id"],
            "previous_session_id": event_row["previous_session_id"],
            "directional_bias": direction,
            "bubble_side": event_row["bubble_side"],
            "location": event_row["location"],
            "event_timestamp": int(event_row["event_timestamp"]),
            "setup_confirmation_timestamp": entry_timestamp,
            "entry_timestamp": entry_timestamp,
            "horizon_end_timestamp": horizon_end_timestamp,
            "entry_price": entry_price,
            "confirmation_price": entry_price,
            "anchor_price": float(event_row["anchor_price"]),
            "bubble_anchor_price": anchor_price,
            "previous_val": float(event_row["previous_val"]),
            "previous_vah": float(event_row["previous_vah"]),
            "previous_poc": float(event_row["previous_poc"]),
            "bubble_tier": event_row["bubble_tier"],
            "impulse_group_id": event_row["impulse_group_id"],
            "impulse_group_gap_seconds": int(event_row["impulse_group_gap_seconds"]),
            mfe_col: event_row[mfe_col],
            eff_col: event_row[eff_col],
            "primary_risk_model": primary_risk_model,
        }
        if mae_col in event_row.index:
            row[mae_col] = event_row[mae_col]
        for optional_col in OPTIONAL_EVENT_COLUMNS:
            if optional_col in event_row.index:
                row[optional_col] = event_row[optional_col]
        row.update(risk_results)
        label, primary_profitable = assign_main_outcome_label(row, primary_risk_model)
        row["main_outcome_label"] = label
        row["primary_profitable_before_SL"] = primary_profitable
        row["reached_2R_before_stop"] = row.get(f"{primary_risk_model}_reached_2R_before_stop")
        row["stop_before_2R"] = row.get(f"{primary_risk_model}_stop_hit_before_2R")
        lifecycle_rows.append(row)

    return pd.DataFrame(lifecycle_rows)


def safe_mean(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.mean())


def safe_median(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.median())


def safe_quantile(series: pd.Series, q: float) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.quantile(q))


def safe_max(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.max())


def safe_rate(series: pd.Series) -> float | None:
    values = series.dropna()
    if values.empty:
        return None
    return float(values.astype(bool).mean())


def format_threshold_value(value: float) -> str:
    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


def summarize_combo(
    combo_id: str,
    lifecycle_df: pd.DataFrame,
    reaction_window_seconds: int,
    mfe_threshold: float,
    efficiency_threshold: float,
    candidate_count_after_filter: int,
    representative_count: int,
    primary_risk_model: str,
) -> dict[str, Any]:
    sample_count = int(len(lifecycle_df))
    primary_max_r_before_stop_col = f"{primary_risk_model}_max_R_before_stop"
    primary_max_r_within_horizon_col = f"{primary_risk_model}_max_R_within_horizon"
    primary_time_to_2r_col = f"{primary_risk_model}_time_to_2R_seconds"
    primary_time_to_stop_col = f"{primary_risk_model}_time_to_stop_seconds"
    primary_profitable_col = f"{primary_risk_model}_profitable_2R_before_SL"
    primary_stop_hit_col = f"{primary_risk_model}_stop_hit_within_horizon"

    if lifecycle_df.empty:
        return {
            "combo_id": combo_id,
            "reaction_window_seconds": int(reaction_window_seconds),
            "mfe_threshold_pct": float(mfe_threshold),
            "efficiency_threshold": float(efficiency_threshold),
            "candidate_count_after_filter": int(candidate_count_after_filter),
            "representative_count": int(representative_count),
            "sample_count": 0,
            "profitable_2R_before_SL_count": 0,
            "profitable_2R_before_SL_rate": None,
            "stop_before_2R_count": 0,
            "stop_before_2R_rate": None,
            "stop_hit_within_horizon_count": 0,
            "stop_hit_within_horizon_rate": None,
            f"mean_{primary_risk_model}_max_R_before_stop": None,
            f"median_{primary_risk_model}_max_R_before_stop": None,
            f"p25_{primary_risk_model}_max_R_before_stop": None,
            f"p75_{primary_risk_model}_max_R_before_stop": None,
            f"p90_{primary_risk_model}_max_R_before_stop": None,
            f"p95_{primary_risk_model}_max_R_before_stop": None,
            f"max_{primary_risk_model}_max_R_before_stop": None,
            f"mean_{primary_risk_model}_max_R_within_horizon": None,
            f"median_{primary_risk_model}_max_R_within_horizon": None,
            f"p90_{primary_risk_model}_max_R_within_horizon": None,
            f"p95_{primary_risk_model}_max_R_within_horizon": None,
            f"max_{primary_risk_model}_max_R_within_horizon": None,
            "mean_time_to_2R_seconds": None,
            "median_time_to_2R_seconds": None,
            "mean_time_to_stop_seconds": None,
            "median_time_to_stop_seconds": None,
        }

    profitable_count = int(lifecycle_df[primary_profitable_col].fillna(False).astype(bool).sum())
    stop_before_2r_count = int(lifecycle_df["stop_before_2R"].fillna(False).astype(bool).sum())
    stop_hit_count = int(lifecycle_df[primary_stop_hit_col].fillna(False).astype(bool).sum())

    return {
        "combo_id": combo_id,
        "reaction_window_seconds": int(reaction_window_seconds),
        "mfe_threshold_pct": float(mfe_threshold),
        "efficiency_threshold": float(efficiency_threshold),
        "candidate_count_after_filter": int(candidate_count_after_filter),
        "representative_count": int(representative_count),
        "sample_count": sample_count,
        "profitable_2R_before_SL_count": profitable_count,
        "profitable_2R_before_SL_rate": safe_rate(lifecycle_df[primary_profitable_col]),
        "stop_before_2R_count": stop_before_2r_count,
        "stop_before_2R_rate": safe_rate(lifecycle_df["stop_before_2R"]),
        "stop_hit_within_horizon_count": stop_hit_count,
        "stop_hit_within_horizon_rate": safe_rate(lifecycle_df[primary_stop_hit_col]),
        f"mean_{primary_risk_model}_max_R_before_stop": safe_mean(lifecycle_df[primary_max_r_before_stop_col]),
        f"median_{primary_risk_model}_max_R_before_stop": safe_median(lifecycle_df[primary_max_r_before_stop_col]),
        f"p25_{primary_risk_model}_max_R_before_stop": safe_quantile(lifecycle_df[primary_max_r_before_stop_col], 0.25),
        f"p75_{primary_risk_model}_max_R_before_stop": safe_quantile(lifecycle_df[primary_max_r_before_stop_col], 0.75),
        f"p90_{primary_risk_model}_max_R_before_stop": safe_quantile(lifecycle_df[primary_max_r_before_stop_col], 0.90),
        f"p95_{primary_risk_model}_max_R_before_stop": safe_quantile(lifecycle_df[primary_max_r_before_stop_col], 0.95),
        f"max_{primary_risk_model}_max_R_before_stop": safe_max(lifecycle_df[primary_max_r_before_stop_col]),
        f"mean_{primary_risk_model}_max_R_within_horizon": safe_mean(lifecycle_df[primary_max_r_within_horizon_col]),
        f"median_{primary_risk_model}_max_R_within_horizon": safe_median(lifecycle_df[primary_max_r_within_horizon_col]),
        f"p90_{primary_risk_model}_max_R_within_horizon": safe_quantile(lifecycle_df[primary_max_r_within_horizon_col], 0.90),
        f"p95_{primary_risk_model}_max_R_within_horizon": safe_quantile(lifecycle_df[primary_max_r_within_horizon_col], 0.95),
        f"max_{primary_risk_model}_max_R_within_horizon": safe_max(lifecycle_df[primary_max_r_within_horizon_col]),
        "mean_time_to_2R_seconds": safe_mean(lifecycle_df[primary_time_to_2r_col]),
        "median_time_to_2R_seconds": safe_median(lifecycle_df[primary_time_to_2r_col]),
        "mean_time_to_stop_seconds": safe_mean(lifecycle_df[primary_time_to_stop_col]),
        "median_time_to_stop_seconds": safe_median(lifecycle_df[primary_time_to_stop_col]),
    }


def plot_histogram(
    lifecycle_df: pd.DataFrame,
    combo_summary: dict[str, Any],
    primary_risk_model: str,
    output_path: str,
    clipped: bool,
    max_histogram_r: float,
) -> None:
    value_col = f"{primary_risk_model}_max_R_before_stop"
    values = pd.to_numeric(lifecycle_df[value_col], errors="coerce").dropna()
    if values.empty:
        return

    plot_values = values.copy()
    if clipped:
        plot_values = plot_values[(plot_values >= 0) & (plot_values <= max_histogram_r)]
        if plot_values.empty:
            return

    profitable_rate = combo_summary.get("profitable_2R_before_SL_rate")
    profitable_rate_str = "NA" if profitable_rate is None else f"{profitable_rate:.2%}"
    median_value = combo_summary.get(f"median_{primary_risk_model}_max_R_before_stop")
    median_str = "NA" if median_value is None else f"{median_value:.2f}"
    title = (
        f"Reaction {combo_summary['reaction_window_seconds']}s | "
        f"MFE >= {combo_summary['mfe_threshold_pct']:.6f} | "
        f"Eff >= {combo_summary['efficiency_threshold']:.2f}\n"
        f"sample={combo_summary['sample_count']} | profitable_2R_rate={profitable_rate_str} | "
        f"median_max_R_before_stop={median_str}"
    )

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.hist(plot_values.to_numpy(dtype=np.float64), bins=30, edgecolor="black")
    ax.set_title(title)
    ax.set_xlabel(f"{primary_risk_model}_max_R_before_stop")
    ax.set_ylabel("Count")
    if clipped:
        ax.set_xlim(0, max_histogram_r)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_summary_heatmap(
    summary_df: pd.DataFrame,
    value_column: str,
    title: str,
    output_path: str,
) -> None:
    if summary_df.empty:
        return

    pivot = summary_df.pivot_table(
        index="mfe_threshold_pct",
        columns="efficiency_threshold",
        values=value_column,
        aggfunc="first",
    ).sort_index(ascending=True)
    if pivot.empty:
        return

    matrix = pivot.to_numpy(dtype=np.float64)
    fig, ax = plt.subplots(figsize=(10, 6))
    image = ax.imshow(matrix, aspect="auto", origin="lower", interpolation="nearest")
    ax.set_title(title)
    ax.set_xlabel("Efficiency threshold")
    ax.set_ylabel("MFE threshold pct")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([format_threshold_value(float(col)) for col in pivot.columns], rotation=45, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([format_threshold_value(float(idx)) for idx in pivot.index])
    fig.colorbar(image, ax=ax)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def normalize_output_precision(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].round(6)
    return out


def combo_id_for_thresholds(mfe_threshold: float, efficiency_threshold: float) -> str:
    mfe_part = format_threshold_value(mfe_threshold).replace(".", "p")
    eff_part = format_threshold_value(efficiency_threshold).replace(".", "p")
    return f"mfe_{mfe_part}__eff_{eff_part}"


def main() -> None:
    args = parse_args()
    mfe_thresholds = parse_float_list(args.mfe_thresholds_pct, "--mfe-thresholds-pct")
    efficiency_thresholds = parse_float_list(args.efficiency_thresholds, "--efficiency-thresholds")
    paths = ensure_output_dirs(args.output_dir, args.write_combo_lifecycle_rows)

    print(f"Loading broad candidate events from {args.events_parquet}")
    events_df = load_events(args.events_parquet, args.reaction_window_seconds)
    print(f"Loaded {len(events_df):,} event rows")

    print(f"Loading raw trades from {args.raw_trades}")
    trades_df = validate_raw_trades(load_trades_from_inputs(args.raw_trades))
    if trades_df.empty:
        raise ValueError("No raw trades were loaded from the provided input dataset")
    validate_raw_trade_coverage(trades_df, events_df, args.max_horizon_seconds)

    trades_timestamps = trades_df["timestamp"].to_numpy(dtype=np.int64)
    trades_prices = trades_df["price"].to_numpy(dtype=np.float64)
    risk_models = [
        RiskModelConfig("tight", float(args.tight_stop_buffer_pct)),
        RiskModelConfig("medium", float(args.medium_stop_buffer_pct)),
        RiskModelConfig("wide", float(args.wide_stop_buffer_pct)),
    ]

    summary_rows: list[dict[str, Any]] = []
    total_combos = len(mfe_thresholds) * len(efficiency_thresholds)
    combo_number = 0

    for mfe_threshold in mfe_thresholds:
        for efficiency_threshold in efficiency_thresholds:
            combo_number += 1
            combo_id = combo_id_for_thresholds(mfe_threshold, efficiency_threshold)
            print(
                f"[{combo_number}/{total_combos}] Sweeping combo {combo_id} "
                f"(mfe>={mfe_threshold}, eff>={efficiency_threshold})"
            )

            filtered_df = filter_events_for_combo(
                events_df=events_df,
                reaction_window_seconds=args.reaction_window_seconds,
                mfe_threshold=mfe_threshold,
                efficiency_threshold=efficiency_threshold,
            )
            candidate_count_after_filter = len(filtered_df)

            if filtered_df.empty:
                summary_rows.append(
                    summarize_combo(
                        combo_id=combo_id,
                        lifecycle_df=pd.DataFrame(),
                        reaction_window_seconds=args.reaction_window_seconds,
                        mfe_threshold=mfe_threshold,
                        efficiency_threshold=efficiency_threshold,
                        candidate_count_after_filter=0,
                        representative_count=0,
                        primary_risk_model=args.primary_risk_model,
                    )
                )
                continue

            grouped_df = assign_impulse_groups(filtered_df, args.impulse_group_gap_seconds)
            representatives_df = grouped_df.groupby("impulse_group_id", sort=False, as_index=False).first()
            lifecycle_df = label_lifecycle_for_representatives(
                representatives_df=representatives_df,
                trades_timestamps=trades_timestamps,
                trades_prices=trades_prices,
                risk_models=risk_models,
                max_horizon_seconds=args.max_horizon_seconds,
                primary_risk_model=args.primary_risk_model,
                reaction_window_seconds=args.reaction_window_seconds,
            )
            lifecycle_df = normalize_output_precision(lifecycle_df)

            summary_row = summarize_combo(
                combo_id=combo_id,
                lifecycle_df=lifecycle_df,
                reaction_window_seconds=args.reaction_window_seconds,
                mfe_threshold=mfe_threshold,
                efficiency_threshold=efficiency_threshold,
                candidate_count_after_filter=candidate_count_after_filter,
                representative_count=len(representatives_df),
                primary_risk_model=args.primary_risk_model,
            )
            summary_rows.append(summary_row)

            if args.write_combo_lifecycle_rows and not lifecycle_df.empty:
                combo_output_path = os.path.join(paths["combo_dir"], f"{combo_id}.parquet")
                lifecycle_df.to_parquet(combo_output_path, index=False)

            if not lifecycle_df.empty:
                full_hist_path = os.path.join(paths["histogram_dir"], f"{combo_id}_max_R_before_stop_hist.png")
                clipped_hist_path = os.path.join(
                    paths["histogram_dir"],
                    f"{combo_id}_max_R_before_stop_hist_clipped.png",
                )
                plot_histogram(
                    lifecycle_df=lifecycle_df,
                    combo_summary=summary_row,
                    primary_risk_model=args.primary_risk_model,
                    output_path=full_hist_path,
                    clipped=False,
                    max_histogram_r=args.max_histogram_r,
                )
                plot_histogram(
                    lifecycle_df=lifecycle_df,
                    combo_summary=summary_row,
                    primary_risk_model=args.primary_risk_model,
                    output_path=clipped_hist_path,
                    clipped=True,
                    max_histogram_r=args.max_histogram_r,
                )

    summary_df = pd.DataFrame(summary_rows)
    if summary_df.empty:
        raise ValueError("No summary rows were produced")

    primary_median_col = f"median_{args.primary_risk_model}_max_R_before_stop"
    summary_df = summary_df.sort_values(
        [primary_median_col, "profitable_2R_before_SL_rate"],
        ascending=[False, False],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    summary_df = normalize_output_precision(summary_df)

    summary_parquet_path = os.path.join(args.output_dir, "confirmation_sweep_summary.parquet")
    summary_csv_path = os.path.join(args.output_dir, "confirmation_sweep_summary.csv")
    print(f"Writing summary parquet to {summary_parquet_path}")
    summary_df.to_parquet(summary_parquet_path, index=False)
    print(f"Writing summary csv to {summary_csv_path}")
    summary_df.to_csv(summary_csv_path, index=False)

    plot_summary_heatmap(
        summary_df=summary_df,
        value_column="profitable_2R_before_SL_rate",
        title="Profitable 2R Before SL Rate",
        output_path=os.path.join(args.output_dir, "summary_profitable_rate_heatmap.png"),
    )
    plot_summary_heatmap(
        summary_df=summary_df,
        value_column=primary_median_col,
        title=f"Median {args.primary_risk_model}_max_R_before_stop",
        output_path=os.path.join(args.output_dir, "summary_median_max_R_before_stop_heatmap.png"),
    )
    plot_summary_heatmap(
        summary_df=summary_df,
        value_column="sample_count",
        title="Sample Count",
        output_path=os.path.join(args.output_dir, "summary_sample_count_heatmap.png"),
    )

    print("Sweep completed")
    print(summary_df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()