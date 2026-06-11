import argparse
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from indicator.volume_profile import build_volume_profile
from loader.trade_loader import load_trades_from_inputs


TIER_RANK = {"medium": 1, "large": 2, "extreme": 3}
BUBBLE_PERCENTILE_COMBINATIONS = {
    "loose": {"medium": 90.0, "large": 95.0, "extreme": 99.0},
    "medium": {"medium": 95.0, "large": 99.0, "extreme": 99.5},
    "strict": {"medium": 99.0, "large": 99.5, "extreme": 99.9},
    "very_strict": {"medium": 99.5, "large": 99.9, "extreme": 99.95},
}
RISK_MODEL_STOP_BUFFER = {
    "tight": 0.0005,
    "medium": 0.0010,
    "wide": 0.0020,
}
ABSORBED_EFFICIENCY_THRESHOLD = 0.40
FINAL_EVENT_COLUMNS = [
    "trade_event_id", "event_id", "symbol", "session_id", "session_date", "previous_session_id",
    "event_timestamp", "setup_confirmation_timestamp", "location", "directional_bias", "bubble_tier",
    "bubble_percentile_score", "previous_val", "previous_vah", "previous_poc", "distance_from_val_pct",
    "distance_from_vah_pct", "distance_from_poc_pct", "anchor_price", "anchor_bubble_qty",
    "anchor_bubble_notional", "confirmation_price", "entry_price", "stop_price", "stop_buffer_pct",
    "risk_per_unit", "setup_reaction_window_seconds", "setup_mfe_threshold_pct",
    "setup_efficiency_threshold", "bubble_percentile_combination", "bubble_medium_qty_threshold",
    "bubble_large_qty_threshold", "bubble_extreme_qty_threshold", "min_bubble_tier", "primary_risk_model",
    "reaction_mfe_30s_pct", "reaction_mae_30s_pct", "reaction_efficiency_30s",
    "confirmation_same_side_bubble_count", "confirmation_same_side_bubble_total_qty",
    "confirmation_same_side_bubble_total_notional", "confirmation_same_side_bubble_max_qty",
    "confirmation_same_side_bubble_max_notional", "confirmation_same_side_bubble_tiers",
    "confirmation_opposite_side_bubble_count", "confirmation_opposite_side_bubble_total_qty",
    "confirmation_opposite_side_bubble_total_notional", "confirmation_opposite_side_bubble_max_qty",
    "confirmation_opposite_side_bubble_max_notional", "confirmation_opposite_side_bubble_tiers",
    "confirmation_first_opposite_side_bubble_absorbed_10s",
    "confirmation_first_opposite_side_bubble_reversed_10s", "trade_event_setup_count",
    "first_setup_timestamp", "last_setup_timestamp", "first_setup_confirmation_timestamp",
    "last_setup_confirmation_timestamp", "seconds_from_first_to_last_setup", "bubble_retested",
    "time_to_retest_seconds", "max_favorable_before_retest_pct", "max_R_before_SL",
    "max_expansion_pct_before_SL", "sl_touched", "sl_touch_timestamp",
]


def bubble_passes_min_tier(qty: float, min_bubble_tier: str, thresholds: dict[str, Any]) -> bool:
    medium_threshold = float(thresholds["bubble_medium_qty_threshold"])
    large_threshold = float(thresholds["bubble_large_qty_threshold"])
    extreme_threshold = float(thresholds["bubble_extreme_qty_threshold"])
    if min_bubble_tier == "medium":
        return qty >= medium_threshold
    if min_bubble_tier == "large":
        return qty >= large_threshold
    if min_bubble_tier == "extreme":
        return qty >= extreme_threshold
    raise ValueError(f"Unsupported min_bubble_tier: {min_bubble_tier}")


def classify_bubble_tier_and_score(qty: float, thresholds: dict[str, Any]) -> tuple[str, float] | tuple[None, None]:
    medium_threshold = float(thresholds["bubble_medium_qty_threshold"])
    large_threshold = float(thresholds["bubble_large_qty_threshold"])
    extreme_threshold = float(thresholds["bubble_extreme_qty_threshold"])
    if qty >= extreme_threshold:
        return "extreme", float(thresholds["bubble_extreme_percentile"])
    if qty >= large_threshold:
        return "large", float(thresholds["bubble_large_percentile"])
    if qty >= medium_threshold:
        return "medium", float(thresholds["bubble_medium_percentile"])
    return None, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dataset-driven event study for outside-value bubbles")
    parser.add_argument("--input", required=True, help="Single path or comma-separated trade input paths")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--min-bubble-tier", default="medium", choices=sorted(TIER_RANK.keys()))
    parser.add_argument("--cluster-window-seconds", type=int, default=10)
    parser.add_argument("--retest-zone-pct", type=float, default=0.0002)
    parser.add_argument("--reaction-windows", default="10,30,60")
    parser.add_argument("--research-windows", default="30,60,300,900,3600")
    parser.add_argument("--bubble-percentile-combination", default="strict", choices=sorted(BUBBLE_PERCENTILE_COMBINATIONS.keys()), help="Previous-session qty percentile threshold preset for bubble tiering")
    parser.add_argument("--setup-reaction-window-seconds", type=int, default=30)
    parser.add_argument("--setup-mfe-threshold-pct", type=float, default=0.0005)
    parser.add_argument("--setup-efficiency-threshold", type=float, default=0.50)
    parser.add_argument("--primary-risk-model", default="medium", choices=sorted(RISK_MODEL_STOP_BUFFER.keys()))
    parser.add_argument("--stop-buffer-pct", type=float, default=None)
    parser.add_argument("--trade-event-gap-seconds", type=int, default=900)
    parser.add_argument("--min-qty", type=float, default=1.0)
    parser.add_argument("--min-notional", type=float, default=10000.0)
    parser.add_argument("--output-parquet", default="research/event_study.parquet")
    parser.add_argument("--write-bubble-trades", action="store_true", help="Write candidate bubble trades that pass the configured threshold to a separate parquet file.")
    parser.add_argument("--bubble-trades-output-parquet", default="research/bubble_trades.parquet", help="Output parquet path for candidate bubble trades when --write-bubble-trades is used.")
    parser.add_argument("--output-csv", default="research/event_study.csv")
    parser.add_argument("--write-csv", action="store_true", help="Write CSV output in addition to parquet")
    parser.add_argument("--session-start-hour", type=int, default=13)
    parser.add_argument("--session-start-minute", type=int, default=30)
    args = parser.parse_args()
    if args.setup_reaction_window_seconds <= 0:
        raise ValueError("--setup-reaction-window-seconds must be > 0")
    if args.setup_mfe_threshold_pct < 0:
        raise ValueError("--setup-mfe-threshold-pct must be >= 0")
    if not 0 <= args.setup_efficiency_threshold <= 1:
        raise ValueError("--setup-efficiency-threshold must be between 0 and 1 inclusive")
    if args.trade_event_gap_seconds <= 0:
        raise ValueError("--trade-event-gap-seconds must be > 0")
    if args.stop_buffer_pct is not None and args.stop_buffer_pct < 0:
        raise ValueError("--stop-buffer-pct must be >= 0")
    return args


def resolve_stop_buffer_pct(args: argparse.Namespace) -> float:
    return float(args.stop_buffer_pct) if args.stop_buffer_pct is not None else float(RISK_MODEL_STOP_BUFFER[args.primary_risk_model])


def parse_windows(raw_value: str, arg_name: str) -> list[int]:
    windows = sorted({int(part.strip()) for part in raw_value.split(",") if part.strip()})
    if not windows:
        raise ValueError(f"{arg_name} must contain at least one integer value")
    if any(window <= 0 for window in windows):
        raise ValueError(f"{arg_name} values must all be > 0")
    return windows


def add_session_id_column(trades_df: pd.DataFrame, session_start_hour: int, session_start_minute: int) -> pd.DataFrame:
    out = trades_df.copy()
    offset = pd.Timedelta(hours=session_start_hour, minutes=session_start_minute)
    ts = pd.to_datetime(out["timestamp"], unit="ms", utc=True)
    out["session_id"] = (ts - offset).dt.date.astype(str)
    return out


def validate_session_continuity(session_ids: list[str]) -> None:
    if not session_ids:
        raise ValueError("No sessions discovered from the provided dataset")
    dates = [date.fromisoformat(session_id) for session_id in session_ids]
    current, end = dates[0], dates[-1]
    expected_dates: list[str] = []
    while current <= end:
        expected_dates.append(current.isoformat())
        current += timedelta(days=1)
    missing = [session_id for session_id in expected_dates if session_id not in set(session_ids)]
    if missing:
        raise ValueError(f"Input dataset has missing session continuity: {missing}")


def session_window_ms(session_id: str, session_start_hour: int, session_start_minute: int) -> tuple[int, int]:
    session_date = date.fromisoformat(session_id)
    session_start = pd.Timestamp(year=session_date.year, month=session_date.month, day=session_date.day, hour=session_start_hour, minute=session_start_minute, tz="UTC")
    session_end = session_start + pd.Timedelta(hours=24)
    return int(session_start.timestamp() * 1000), int(session_end.timestamp() * 1000)


def is_session_window_available(session_id: str, dataset_min_timestamp: int, dataset_max_timestamp: int, session_start_hour: int, session_start_minute: int) -> bool:
    session_start_ms, session_end_ms = session_window_ms(session_id, session_start_hour, session_start_minute)
    return dataset_min_timestamp <= session_start_ms and dataset_max_timestamp >= session_end_ms - 1


def clamp_metrics(favorable_move: float, adverse_move: float) -> tuple[float, float, float | None]:
    mfe = max(0.0, float(favorable_move))
    mae = max(0.0, float(adverse_move))
    efficiency = mfe / (mfe + mae) if (mfe + mae) > 0 else None
    return mfe, mae, efficiency


def get_time_window_indices(timestamps: np.ndarray, start_timestamp: int, end_timestamp: int) -> tuple[int, int]:
    return int(np.searchsorted(timestamps, start_timestamp, side="right")), int(np.searchsorted(timestamps, end_timestamp, side="right"))


def get_time_window_arrays(timestamps: np.ndarray, prices: np.ndarray, start_timestamp: int, end_timestamp: int) -> tuple[np.ndarray, np.ndarray]:
    start_idx, end_idx = get_time_window_indices(timestamps, start_timestamp, end_timestamp)
    return timestamps[start_idx:end_idx], prices[start_idx:end_idx]


def calculate_directional_moves(anchor_price: float, bubble_side: str, min_price: float, max_price: float) -> tuple[float, float]:
    if bubble_side == "sell":
        return (anchor_price - min_price) / anchor_price, (max_price - anchor_price) / anchor_price
    return (max_price - anchor_price) / anchor_price, (anchor_price - min_price) / anchor_price


def calculate_reaction_metrics_np(event_timestamp: int, anchor_price: float, bubble_side: str, future_timestamps: np.ndarray, future_prices: np.ndarray, windows: list[int]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for window in windows:
        end_idx = int(np.searchsorted(future_timestamps, event_timestamp + window * 1000, side="right"))
        window_prices = future_prices[:end_idx]
        if len(window_prices) == 0:
            metrics[f"reaction_mfe_{window}s_pct"] = None
            metrics[f"reaction_mae_{window}s_pct"] = None
            metrics[f"reaction_efficiency_{window}s"] = None
            continue
        favorable_move, adverse_move = calculate_directional_moves(anchor_price, bubble_side, float(np.min(window_prices)), float(np.max(window_prices)))
        mfe, mae, efficiency = clamp_metrics(favorable_move, adverse_move)
        metrics[f"reaction_mfe_{window}s_pct"] = mfe
        metrics[f"reaction_mae_{window}s_pct"] = mae
        metrics[f"reaction_efficiency_{window}s"] = efficiency
    return metrics


def summarize_qualifying_bubbles(rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    if not rows:
        return {f"{prefix}_count": 0, f"{prefix}_total_qty": 0.0, f"{prefix}_total_notional": 0.0, f"{prefix}_max_qty": None, f"{prefix}_max_notional": None, f"{prefix}_tiers": None}
    return {
        f"{prefix}_count": int(len(rows)),
        f"{prefix}_total_qty": float(sum(row["qty"] for row in rows)),
        f"{prefix}_total_notional": float(sum(row["notional"] for row in rows)),
        f"{prefix}_max_qty": float(max(row["qty"] for row in rows)),
        f"{prefix}_max_notional": float(max(row["notional"] for row in rows)),
        f"{prefix}_tiers": ",".join(sorted({str(row["tier"]) for row in rows})),
    }


def compute_confirmation_horizon_bubble_metrics(event_timestamp: int, setup_confirmation_timestamp: int, directional_bias: str, qty_thresholds: dict[str, Any], min_bubble_tier: str, all_timestamps: np.ndarray, all_prices: np.ndarray, all_qtys: np.ndarray, all_is_buyer_maker: np.ndarray) -> dict[str, Any]:
    start_idx, end_idx = get_time_window_indices(all_timestamps, event_timestamp, setup_confirmation_timestamp)
    out: dict[str, Any] = {}
    out.update(summarize_qualifying_bubbles([], "confirmation_same_side_bubble"))
    out.update(summarize_qualifying_bubbles([], "confirmation_opposite_side_bubble"))
    out["confirmation_first_opposite_side_bubble_absorbed_10s"] = None
    out["confirmation_first_opposite_side_bubble_reversed_10s"] = None
    if start_idx >= end_idx:
        return out
    window_timestamps = all_timestamps[start_idx:end_idx]
    window_prices = all_prices[start_idx:end_idx]
    window_qtys = all_qtys[start_idx:end_idx]
    window_is_buyer_maker = all_is_buyer_maker[start_idx:end_idx]
    if directional_bias == "long":
        same_side_mask, opposite_side_mask, opposite_side = ~window_is_buyer_maker, window_is_buyer_maker, "sell"
    elif directional_bias == "short":
        same_side_mask, opposite_side_mask, opposite_side = window_is_buyer_maker, ~window_is_buyer_maker, "buy"
    else:
        raise ValueError(f"Unsupported directional_bias: {directional_bias}")
    same_side_rows: list[dict[str, Any]] = []
    opposite_side_rows: list[dict[str, Any]] = []
    for idx in range(len(window_timestamps)):
        qty = float(window_qtys[idx])
        if not bubble_passes_min_tier(qty, min_bubble_tier, qty_thresholds):
            continue
        tier, _ = classify_bubble_tier_and_score(qty, qty_thresholds)
        if tier is None:
            continue
        row = {"timestamp": int(window_timestamps[idx]), "price": float(window_prices[idx]), "qty": qty, "notional": float(window_prices[idx] * window_qtys[idx]), "tier": tier}
        if same_side_mask[idx]:
            same_side_rows.append(row)
        elif opposite_side_mask[idx]:
            opposite_side_rows.append(row)
    out.update(summarize_qualifying_bubbles(same_side_rows, "confirmation_same_side_bubble"))
    out.update(summarize_qualifying_bubbles(opposite_side_rows, "confirmation_opposite_side_bubble"))
    if not opposite_side_rows:
        return out
    first_row = opposite_side_rows[0]
    first_ts = int(first_row["timestamp"])
    reaction_timestamps, reaction_prices = get_time_window_arrays(all_timestamps, all_prices, first_ts, first_ts + 10 * 1000)
    reaction_metrics = calculate_reaction_metrics_np(first_ts, float(first_row["price"]), opposite_side, reaction_timestamps, reaction_prices, [10])
    reaction_eff = reaction_metrics.get("reaction_efficiency_10s")
    reversed_10s = bool(len(reaction_prices) > 0 and ((np.min(reaction_prices) <= float(first_row["price"])) if directional_bias == "long" else (np.max(reaction_prices) >= float(first_row["price"]))))
    out["confirmation_first_opposite_side_bubble_absorbed_10s"] = reaction_eff is not None and reaction_eff < ABSORBED_EFFICIENCY_THRESHOLD
    out["confirmation_first_opposite_side_bubble_reversed_10s"] = reversed_10s
    return out


def calculate_trade_result_before_sl_np(entry_timestamp: int, entry_price: float, directional_bias: str, future_timestamps: np.ndarray, future_prices: np.ndarray, stop_buffer_pct: float, retest_zone_pct: float) -> dict[str, Any]:
    if directional_bias == "long":
        stop_price = entry_price * (1 - stop_buffer_pct)
        sl_mask = future_prices <= stop_price
        retest_mask = future_prices <= entry_price * (1 + retest_zone_pct)
    elif directional_bias == "short":
        stop_price = entry_price * (1 + stop_buffer_pct)
        sl_mask = future_prices >= stop_price
        retest_mask = future_prices >= entry_price * (1 - retest_zone_pct)
    else:
        raise ValueError(f"Unsupported directional_bias: {directional_bias}")
    risk_per_unit = abs(entry_price - stop_price)
    if risk_per_unit <= 0:
        raise ValueError("risk_per_unit must be > 0")
    sl_indices = np.flatnonzero(sl_mask)
    sl_touched = len(sl_indices) > 0
    sl_touch_timestamp = int(future_timestamps[int(sl_indices[0])]) if sl_touched else None
    valid_prices = future_prices[: int(sl_indices[0])] if sl_touched else future_prices
    if len(valid_prices) == 0:
        max_expansion_pct_before_sl = 0.0
        max_r_before_sl = 0.0
    elif directional_bias == "long":
        highest_price = float(np.max(valid_prices))
        max_expansion_pct_before_sl = max(0.0, (highest_price - entry_price) / entry_price)
        max_r_before_sl = max(0.0, (highest_price - entry_price) / risk_per_unit)
    else:
        lowest_price = float(np.min(valid_prices))
        max_expansion_pct_before_sl = max(0.0, (entry_price - lowest_price) / entry_price)
        max_r_before_sl = max(0.0, (entry_price - lowest_price) / risk_per_unit)
    retest_indices = np.flatnonzero(retest_mask)
    bubble_retested = len(retest_indices) > 0
    if bubble_retested:
        first_retest_idx = int(retest_indices[0])
        time_to_retest_seconds = (int(future_timestamps[first_retest_idx]) - entry_timestamp) / 1000.0
        prices_before_retest = future_prices[: first_retest_idx + 1]
    else:
        time_to_retest_seconds = None
        prices_before_retest = future_prices
    if len(prices_before_retest) == 0:
        max_favorable_before_retest_pct = 0.0
    elif directional_bias == "long":
        max_favorable_before_retest_pct = max(0.0, (float(np.max(prices_before_retest)) - entry_price) / entry_price)
    else:
        max_favorable_before_retest_pct = max(0.0, (entry_price - float(np.min(prices_before_retest))) / entry_price)
    return {
        "entry_price": float(entry_price), "stop_price": float(stop_price), "stop_buffer_pct": float(stop_buffer_pct), "risk_per_unit": float(risk_per_unit),
        "max_R_before_SL": float(max_r_before_sl), "max_expansion_pct_before_SL": float(max_expansion_pct_before_sl), "sl_touched": bool(sl_touched), "sl_touch_timestamp": sl_touch_timestamp,
        "bubble_retested": bool(bubble_retested), "time_to_retest_seconds": time_to_retest_seconds, "max_favorable_before_retest_pct": float(max_favorable_before_retest_pct),
    }


def build_qty_percentile_thresholds(previous_session_df: pd.DataFrame, percentile_combination: str) -> dict[str, Any]:
    if previous_session_df.empty:
        raise ValueError("previous_session_df must not be empty")
    preset = BUBBLE_PERCENTILE_COMBINATIONS[percentile_combination]
    thresholds = np.percentile(previous_session_df["qty"].to_numpy(dtype=np.float64), [preset["medium"], preset["large"], preset["extreme"]])
    return {
        "bubble_tier_mode": "previous_session_qty_percentile",
        "bubble_percentile_combination": str(percentile_combination),
        "bubble_tier_metric": "qty",
        "bubble_medium_percentile": float(preset["medium"]),
        "bubble_large_percentile": float(preset["large"]),
        "bubble_extreme_percentile": float(preset["extreme"]),
        "bubble_medium_qty_threshold": float(thresholds[0]),
        "bubble_large_qty_threshold": float(thresholds[1]),
        "bubble_extreme_qty_threshold": float(thresholds[2]),
    }


def process_session_events_sequential(current_session_df: pd.DataFrame, previous_session_profile: dict[str, Any], qty_thresholds: dict[str, Any], session_id: str, symbol: str, min_bubble_tier: str, all_timestamps: np.ndarray, all_prices: np.ndarray, all_qtys: np.ndarray, all_is_buyer_maker: np.ndarray, last_available_trade_timestamp: int, reaction_windows: list[int], research_windows: list[int], retest_zone_pct: float, setup_reaction_window_seconds: int, setup_mfe_threshold_pct: float, setup_efficiency_threshold: float, stop_buffer_pct: float, primary_risk_model: str, config_metadata: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    session_date = date.fromisoformat(session_id)
    prev_val = float(previous_session_profile["val"])
    prev_vah = float(previous_session_profile["vah"])
    prev_poc = float(previous_session_profile["poc_price"])
    prev_session_id = (session_date - timedelta(days=1)).isoformat()
    max_reaction_window_ms = max(reaction_windows) * 1000
    max_research_window_ms = max(research_windows) * 1000
    session_timestamps = current_session_df["timestamp"].to_numpy(dtype=np.int64)
    session_prices = current_session_df["price"].to_numpy(dtype=np.float64)
    session_qtys = current_session_df["qty"].to_numpy(dtype=np.float64)
    session_is_buyer_maker = current_session_df["is_buyer_maker"].to_numpy(dtype=bool)
    notionals = session_prices * session_qtys
    is_buy_aggression = ~session_is_buyer_maker
    is_sell_aggression = session_is_buyer_maker
    above_vah = session_prices > prev_vah
    below_val = session_prices < prev_val
    inside_value = ~(above_vah | below_val)
    outside_value = above_vah | below_val
    correct_side_mask = (above_vah & is_buy_aggression) | (below_val & is_sell_aggression)
    medium_threshold = float(qty_thresholds["bubble_medium_qty_threshold"])
    large_threshold = float(qty_thresholds["bubble_large_qty_threshold"])
    extreme_threshold = float(qty_thresholds["bubble_extreme_qty_threshold"])
    passes_medium = session_qtys >= medium_threshold
    passes_large = session_qtys >= large_threshold
    passes_extreme = session_qtys >= extreme_threshold
    if min_bubble_tier == "medium":
        passes_min_tier = passes_medium
    elif min_bubble_tier == "large":
        passes_min_tier = passes_large
    elif min_bubble_tier == "extreme":
        passes_min_tier = passes_extreme
    else:
        raise ValueError(f"Unsupported min_bubble_tier: {min_bubble_tier}")
    candidate_indices = np.where(correct_side_mask & passes_min_tier)[0]
    stats = {
        "total_deep_trades": int(len(session_timestamps)),
        "skipped_inside_value": int(np.sum(inside_value)),
        "skipped_wrong_side": int(np.sum(outside_value & ~correct_side_mask)),
        "skipped_below_medium_percentile": int(np.sum(correct_side_mask & ~passes_medium)),
        "skipped_below_min_bubble_tier": int(np.sum(correct_side_mask & passes_medium & ~passes_min_tier)),
        "candidate_bubbles_after_tier_filter": int(len(candidate_indices)),
        "skipped_incomplete_reaction_future": 0,
        "skipped_unconfirmed_setup": 0,
        "skipped_incomplete_future": 0,
        "total_confirmed_setups": 0,
    }
    event_records: list[dict[str, Any]] = []
    bubble_trade_records: list[dict[str, Any]] = []
    for i in candidate_indices:
        timestamp_ms = int(session_timestamps[i])
        price = float(session_prices[i])
        qty = float(session_qtys[i])
        notional = float(notionals[i])
        if above_vah[i]:
            location, directional_bias, aggressive_side = "above_vah", "long", "buy"
        elif below_val[i]:
            location, directional_bias, aggressive_side = "below_val", "short", "sell"
        else:
            continue
        bubble_tier, bubble_percentile_score = classify_bubble_tier_and_score(qty, qty_thresholds)
        if bubble_tier is None or bubble_percentile_score is None:
            continue
        timestamp_utc = pd.to_datetime(timestamp_ms, unit="ms", utc=True)
        bubble_trade_records.append({
            "bubble_trade_id": uuid.uuid4().hex, "symbol": symbol, "session_id": session_id, "session_date": session_date,
            "previous_session_id": prev_session_id, "timestamp": timestamp_ms, "timestamp_utc": timestamp_utc,
            "timestamp_wib": timestamp_utc.tz_convert("Asia/Jakarta"), "price": price, "qty": qty, "notional": notional,
            "is_buyer_maker": bool(session_is_buyer_maker[i]), "aggressive_side": aggressive_side, "directional_bias": directional_bias,
            "location": location, "previous_val": prev_val, "previous_vah": prev_vah, "previous_poc": prev_poc,
            "distance_from_val_pct": (price - prev_val) / prev_val if prev_val > 0 else None,
            "distance_from_vah_pct": (price - prev_vah) / prev_vah if prev_vah > 0 else None,
            "distance_from_poc_pct": (price - prev_poc) / prev_poc if prev_poc > 0 else None,
            "bubble_tier": bubble_tier, "bubble_percentile_score": bubble_percentile_score, **qty_thresholds,
            "min_bubble_tier": str(min_bubble_tier), "primary_risk_model": str(primary_risk_model), "stop_buffer_pct": float(stop_buffer_pct),
            "session_start_hour": int(config_metadata["session_start_hour"]), "session_start_minute": int(config_metadata["session_start_minute"]),
        })
        if timestamp_ms + max_reaction_window_ms > last_available_trade_timestamp:
            stats["skipped_incomplete_reaction_future"] += 1
            continue
        reaction_timestamps, reaction_prices = get_time_window_arrays(all_timestamps, all_prices, timestamp_ms, timestamp_ms + max_reaction_window_ms)
        if len(reaction_prices) == 0:
            stats["skipped_incomplete_reaction_future"] += 1
            continue
        reaction_metrics = calculate_reaction_metrics_np(timestamp_ms, price, aggressive_side, reaction_timestamps, reaction_prices, reaction_windows)
        setup_mfe_value = reaction_metrics.get(f"reaction_mfe_{setup_reaction_window_seconds}s_pct")
        setup_eff_value = reaction_metrics.get(f"reaction_efficiency_{setup_reaction_window_seconds}s")
        if not (setup_mfe_value is not None and setup_eff_value is not None and setup_mfe_value >= setup_mfe_threshold_pct and setup_eff_value >= setup_efficiency_threshold):
            stats["skipped_unconfirmed_setup"] += 1
            continue
        setup_confirmation_timestamp = timestamp_ms + setup_reaction_window_seconds * 1000
        confirmation_start_idx, confirmation_end_idx = get_time_window_indices(all_timestamps, timestamp_ms, setup_confirmation_timestamp)
        confirmation_prices = all_prices[confirmation_start_idx:confirmation_end_idx]
        confirmation_price = price if len(confirmation_prices) == 0 else float(confirmation_prices[-1])
        entry_timestamp = setup_confirmation_timestamp
        if entry_timestamp + max_research_window_ms > last_available_trade_timestamp:
            stats["skipped_incomplete_future"] += 1
            continue
        future_timestamps, future_prices = get_time_window_arrays(all_timestamps, all_prices, entry_timestamp, entry_timestamp + max_research_window_ms)
        if len(future_prices) == 0:
            stats["skipped_incomplete_future"] += 1
            continue
        result_metrics = calculate_trade_result_before_sl_np(entry_timestamp, confirmation_price, directional_bias, future_timestamps, future_prices, stop_buffer_pct, retest_zone_pct)
        confirmation_horizon_metrics = compute_confirmation_horizon_bubble_metrics(timestamp_ms, setup_confirmation_timestamp, directional_bias, qty_thresholds, min_bubble_tier, all_timestamps, all_prices, all_qtys, all_is_buyer_maker)
        event_data = {
            "event_id": uuid.uuid4().hex, "symbol": symbol, "session_id": session_id, "session_date": session_date,
            "previous_session_id": prev_session_id, "location": location, "directional_bias": directional_bias,
            "bubble_tier": bubble_tier, "bubble_percentile_score": bubble_percentile_score, "previous_val": prev_val,
            "previous_vah": prev_vah, "previous_poc": prev_poc,
            "distance_from_val_pct": (price - prev_val) / prev_val if prev_val > 0 else None,
            "distance_from_vah_pct": (price - prev_vah) / prev_vah if prev_vah > 0 else None,
            "distance_from_poc_pct": (price - prev_poc) / prev_poc if prev_poc > 0 else None,
            "anchor_price": price, "anchor_bubble_qty": qty, "anchor_bubble_notional": notional,
            "event_timestamp": timestamp_ms, "setup_confirmation_timestamp": setup_confirmation_timestamp,
            "confirmation_price": confirmation_price, "setup_reaction_window_seconds": int(setup_reaction_window_seconds),
            "setup_mfe_threshold_pct": float(setup_mfe_threshold_pct), "setup_efficiency_threshold": float(setup_efficiency_threshold),
            "bubble_percentile_combination": qty_thresholds["bubble_percentile_combination"],
            "bubble_medium_qty_threshold": qty_thresholds["bubble_medium_qty_threshold"],
            "bubble_large_qty_threshold": qty_thresholds["bubble_large_qty_threshold"],
            "bubble_extreme_qty_threshold": qty_thresholds["bubble_extreme_qty_threshold"],
            "min_bubble_tier": str(min_bubble_tier), "primary_risk_model": str(primary_risk_model),
            "reaction_mfe_30s_pct": reaction_metrics.get("reaction_mfe_30s_pct"), "reaction_mae_30s_pct": reaction_metrics.get("reaction_mae_30s_pct"),
            "reaction_efficiency_30s": reaction_metrics.get("reaction_efficiency_30s"),
        }
        event_data.update(confirmation_horizon_metrics)
        event_data.update(result_metrics)
        event_records.append(event_data)
        stats["total_confirmed_setups"] += 1
    return event_records, bubble_trade_records, stats


def _build_trade_event_row(group_rows: list[dict[str, Any]]) -> dict[str, Any]:
    first_row = dict(group_rows[0])
    first_setup_timestamp = int(min(int(row["event_timestamp"]) for row in group_rows))
    last_setup_timestamp = int(max(int(row["event_timestamp"]) for row in group_rows))
    first_setup_confirmation_timestamp = int(min(int(row["setup_confirmation_timestamp"]) for row in group_rows))
    last_setup_confirmation_timestamp = int(max(int(row["setup_confirmation_timestamp"]) for row in group_rows))
    first_row.update({
        "trade_event_id": uuid.uuid4().hex, "trade_event_setup_count": int(len(group_rows)),
        "first_setup_timestamp": first_setup_timestamp, "last_setup_timestamp": last_setup_timestamp,
        "first_setup_confirmation_timestamp": first_setup_confirmation_timestamp, "last_setup_confirmation_timestamp": last_setup_confirmation_timestamp,
        "seconds_from_first_to_last_setup": (last_setup_timestamp - first_setup_timestamp) / 1000.0,
    })
    return first_row


def group_confirmed_setups_into_trade_events(confirmed_setups_df: pd.DataFrame, trade_event_gap_seconds: int) -> pd.DataFrame:
    if confirmed_setups_df.empty:
        return confirmed_setups_df.copy()
    working_df = confirmed_setups_df.sort_values(["session_id", "directional_bias", "setup_confirmation_timestamp", "event_timestamp", "event_id"]).reset_index(drop=True)
    grouped_rows: list[dict[str, Any]] = []
    gap_ms = trade_event_gap_seconds * 1000
    for (_, _), group_df in working_df.groupby(["session_id", "directional_bias"], sort=True):
        current_group: list[dict[str, Any]] = []
        current_anchor_confirmation_ts: int | None = None
        for row in group_df.to_dict("records"):
            row_confirmation_ts = int(row["setup_confirmation_timestamp"])
            if current_anchor_confirmation_ts is None:
                current_group = [row]
                current_anchor_confirmation_ts = row_confirmation_ts
            elif row_confirmation_ts - current_anchor_confirmation_ts <= gap_ms:
                current_group.append(row)
            else:
                grouped_rows.append(_build_trade_event_row(current_group))
                current_group = [row]
                current_anchor_confirmation_ts = row_confirmation_ts
        if current_group:
            grouped_rows.append(_build_trade_event_row(current_group))
    return pd.DataFrame(grouped_rows)


def validate_trade_event_rows(events_df: pd.DataFrame, trade_event_gap_seconds: int) -> None:
    if events_df.empty:
        return
    if not events_df["trade_event_id"].is_unique:
        raise ValueError("trade_event_id must be unique per final row")
    if not events_df["event_id"].is_unique:
        raise ValueError("event_id must identify the first confirmed setup of each trade event")
    expected_setup_confirmation_timestamp = events_df["event_timestamp"] + events_df["setup_reaction_window_seconds"] * 1000
    if not (events_df["setup_confirmation_timestamp"] == expected_setup_confirmation_timestamp).all():
        raise ValueError("setup_confirmation_timestamp must equal event_timestamp + setup_reaction_window_seconds * 1000")
    if not (events_df["first_setup_confirmation_timestamp"] <= events_df["last_setup_confirmation_timestamp"]).all():
        raise ValueError("first_setup_confirmation_timestamp must be <= last_setup_confirmation_timestamp")
    if not (events_df["trade_event_setup_count"] >= 1).all():
        raise ValueError("trade_event_setup_count must be >= 1")
    if not (pd.to_numeric(events_df["max_R_before_SL"], errors="coerce") >= 0).all():
        raise ValueError("max_R_before_SL should never be negative")
    if not (pd.to_numeric(events_df["max_expansion_pct_before_SL"], errors="coerce") >= 0).all():
        raise ValueError("max_expansion_pct_before_SL should never be negative")
    sl_touched_mask = events_df["sl_touched"] == True
    if events_df.loc[sl_touched_mask, "sl_touch_timestamp"].isna().any():
        raise ValueError("If sl_touched=True, sl_touch_timestamp must not be null")
    if events_df.loc[~sl_touched_mask, "sl_touch_timestamp"].notna().any():
        raise ValueError("If sl_touched=False, sl_touch_timestamp must be null")
    gap_ms = trade_event_gap_seconds * 1000
    sorted_df = events_df.sort_values(["session_id", "directional_bias", "first_setup_confirmation_timestamp"])
    for (_, _), group_df in sorted_df.groupby(["session_id", "directional_bias"], sort=True):
        anchor_values = group_df["first_setup_confirmation_timestamp"].tolist()
        for idx in range(1, len(anchor_values)):
            if int(anchor_values[idx]) - int(anchor_values[idx - 1]) <= gap_ms:
                raise ValueError("Rows within the same session_id and directional_bias must start new trade events only after the grouping gap")


def build_events_dataset(trades_df: pd.DataFrame, symbol: str, session_start_hour: int, session_start_minute: int, min_qty: float, min_notional: float, min_bubble_tier: str, cluster_window_seconds: int, bubble_percentile_combination: str, reaction_windows: list[int], research_windows: list[int], retest_zone_pct: float, setup_reaction_window_seconds: int, setup_mfe_threshold_pct: float, setup_efficiency_threshold: float, stop_buffer_pct: float, primary_risk_model: str, trade_event_gap_seconds: int) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    print("Adding session_id column...", flush=True)
    trades_df = add_session_id_column(trades_df, session_start_hour, session_start_minute)
    print("Discovering sessions...", flush=True)
    session_ids = sorted(trades_df["session_id"].unique().tolist())
    print(f"Discovered {len(session_ids)} sessions", flush=True)
    validate_session_continuity(session_ids)
    all_timestamps = trades_df["timestamp"].to_numpy(dtype=np.int64)
    all_prices = trades_df["price"].to_numpy(dtype=np.float64)
    all_qtys = trades_df["qty"].to_numpy(dtype=np.float64)
    all_is_buyer_maker = trades_df["is_buyer_maker"].to_numpy(dtype=bool)
    if len(all_timestamps) > 1 and np.any(all_timestamps[1:] < all_timestamps[:-1]):
        raise ValueError("trades_df timestamps must be sorted ascending before searchsorted optimization")
    dataset_min_timestamp = int(trades_df["timestamp"].min())
    last_available_trade_timestamp = int(trades_df["timestamp"].max())
    grouped_sessions = {session_id: session_df.copy() for session_id, session_df in trades_df.groupby("session_id", sort=True)}
    events: list[dict[str, Any]] = []
    bubble_trades: list[dict[str, Any]] = []
    skipped_incomplete_previous_session = 0
    researched_sessions = 0
    aggregate_stats = {key: 0 for key in ["total_deep_trades", "skipped_inside_value", "skipped_wrong_side", "skipped_below_medium_percentile", "skipped_below_min_bubble_tier", "candidate_bubbles_after_tier_filter", "skipped_incomplete_reaction_future", "skipped_unconfirmed_setup", "skipped_incomplete_future", "total_confirmed_setups"]}
    config_metadata = {
        "min_qty": float(min_qty) if min_qty is not None else None, "min_notional": float(min_notional) if min_notional is not None else None,
        "min_bubble_tier": str(min_bubble_tier), "cluster_window_seconds": int(cluster_window_seconds), "retest_zone_pct": float(retest_zone_pct),
        "reaction_windows": ",".join(str(window) for window in reaction_windows), "research_windows": ",".join(str(window) for window in research_windows),
        "session_start_hour": int(session_start_hour), "session_start_minute": int(session_start_minute), "max_research_window_seconds": int(max(research_windows)),
    }
    total_sessions_to_process = len(session_ids) - 1
    for idx in range(1, len(session_ids)):
        previous_session_id = session_ids[idx - 1]
        session_id = session_ids[idx]
        print(f"[{idx}/{total_sessions_to_process}] Processing session {session_id} (prev={previous_session_id})")
        if not is_session_window_available(previous_session_id, dataset_min_timestamp, last_available_trade_timestamp, session_start_hour, session_start_minute):
            skipped_incomplete_previous_session += 1
            continue
        previous_session_df = grouped_sessions[previous_session_id]
        current_session_df = grouped_sessions[session_id]
        previous_session_profile = build_volume_profile(previous_session_df, n_bins=50)
        qty_thresholds = build_qty_percentile_thresholds(previous_session_df, bubble_percentile_combination)
        session_events, session_bubble_trades, session_stats = process_session_events_sequential(current_session_df, previous_session_profile, qty_thresholds, session_id, symbol, min_bubble_tier, all_timestamps, all_prices, all_qtys, all_is_buyer_maker, last_available_trade_timestamp, reaction_windows, research_windows, retest_zone_pct, setup_reaction_window_seconds, setup_mfe_threshold_pct, setup_efficiency_threshold, stop_buffer_pct, primary_risk_model, config_metadata)
        print(f"    deep_trades={session_stats['total_deep_trades']:,} candidates_after_tier={session_stats['candidate_bubbles_after_tier_filter']:,} confirmed_setups={session_stats['total_confirmed_setups']:,}")
        researched_sessions += 1
        events.extend(session_events)
        bubble_trades.extend(session_bubble_trades)
        for key in aggregate_stats:
            aggregate_stats[key] += int(session_stats.get(key, 0))
        print(f"    complete | events_so_far={len(events):,}")
    confirmed_setups_df = pd.DataFrame(events)
    bubble_trades_df = pd.DataFrame(bubble_trades)
    if not confirmed_setups_df.empty and not confirmed_setups_df["event_id"].is_unique:
        raise ValueError("event_id must be unique among confirmed setups")
    trade_events_df = group_confirmed_setups_into_trade_events(confirmed_setups_df, trade_event_gap_seconds)
    final_events_df = trade_events_df if trade_events_df.empty else trade_events_df.reindex(columns=FINAL_EVENT_COLUMNS)
    validate_trade_event_rows(final_events_df, trade_event_gap_seconds)
    stats = {
        "total_sessions_available": int(len(session_ids)), "total_sessions_researched": int(researched_sessions), "total_deep_trades": int(aggregate_stats["total_deep_trades"]),
        "skipped_inside_value": int(aggregate_stats["skipped_inside_value"]), "skipped_wrong_side": int(aggregate_stats["skipped_wrong_side"]),
        "skipped_below_medium_percentile": int(aggregate_stats["skipped_below_medium_percentile"]), "skipped_below_min_bubble_tier": int(aggregate_stats["skipped_below_min_bubble_tier"]),
        "candidate_bubbles_after_tier_filter": int(aggregate_stats["candidate_bubbles_after_tier_filter"]), "skipped_incomplete_reaction_future": int(aggregate_stats["skipped_incomplete_reaction_future"]),
        "skipped_unconfirmed_setup": int(aggregate_stats["skipped_unconfirmed_setup"]), "skipped_incomplete_future": int(aggregate_stats["skipped_incomplete_future"]),
        "total_confirmed_setups": int(aggregate_stats["total_confirmed_setups"]), "total_bubbles": int(aggregate_stats["candidate_bubbles_after_tier_filter"]),
        "total_candidate_bubbles_before_setup_confirmation": int(aggregate_stats["candidate_bubbles_after_tier_filter"]), "skipped_incomplete_previous_session": int(skipped_incomplete_previous_session),
    }
    return final_events_df, bubble_trades_df, stats


def normalize_output_precision(events_df: pd.DataFrame) -> pd.DataFrame:
    out = events_df.copy()
    pct_columns = [column for column in out.columns if "_pct" in column]
    efficiency_columns = [column for column in out.columns if "efficiency" in column]
    price_columns = ["previous_val", "previous_vah", "previous_poc", "anchor_price", "confirmation_price", "entry_price", "stop_price"]
    notional_columns = ["anchor_bubble_notional", "confirmation_same_side_bubble_total_notional", "confirmation_same_side_bubble_max_notional", "confirmation_opposite_side_bubble_total_notional", "confirmation_opposite_side_bubble_max_notional"]
    quantity_columns = ["anchor_bubble_qty", "bubble_medium_qty_threshold", "bubble_large_qty_threshold", "bubble_extreme_qty_threshold", "confirmation_same_side_bubble_total_qty", "confirmation_same_side_bubble_max_qty", "confirmation_opposite_side_bubble_total_qty", "confirmation_opposite_side_bubble_max_qty"]
    score_columns = ["bubble_percentile_score", "stop_buffer_pct", "risk_per_unit"]
    time_columns = ["time_to_retest_seconds", "seconds_from_first_to_last_setup"]
    for column in pct_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    for column in efficiency_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    for column in price_columns:
        if column in out.columns:
            out[column] = out[column].round(2)
    for column in notional_columns:
        if column in out.columns:
            out[column] = out[column].round(2)
    for column in quantity_columns:
        if column in out.columns:
            out[column] = out[column].round(3)
    for column in score_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    for column in time_columns:
        if column in out.columns:
            out[column] = out[column].round(2)
    return out


def normalize_bubble_trade_output_precision(bubble_trades_df: pd.DataFrame) -> pd.DataFrame:
    out = bubble_trades_df.copy()
    pct_columns = [column for column in out.columns if "_pct" in column]
    price_columns = ["price", "previous_val", "previous_vah", "previous_poc"]
    notional_columns = ["notional"]
    quantity_columns = ["qty", "bubble_medium_qty_threshold", "bubble_large_qty_threshold", "bubble_extreme_qty_threshold", "stop_buffer_pct"]
    score_columns = ["bubble_percentile_score"]
    percentile_columns = ["bubble_medium_percentile", "bubble_large_percentile", "bubble_extreme_percentile"]
    for column in pct_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    for column in price_columns:
        if column in out.columns:
            out[column] = out[column].round(2)
    for column in notional_columns:
        if column in out.columns:
            out[column] = out[column].round(2)
    for column in quantity_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    for column in score_columns:
        if column in out.columns:
            out[column] = out[column].round(3)
    for column in percentile_columns:
        if column in out.columns:
            out[column] = out[column].round(4)
    return out


def main() -> None:
    args = parse_args()
    stop_buffer_pct = resolve_stop_buffer_pct(args)
    reaction_windows = sorted(set(parse_windows(args.reaction_windows, "--reaction-windows") + [args.setup_reaction_window_seconds, 30]))
    research_windows = parse_windows(args.research_windows, "--research-windows")
    for path in [args.output_parquet, args.bubble_trades_output_parquet if args.write_bubble_trades else None, args.output_csv if args.write_csv else None]:
        if path:
            out_dir = os.path.dirname(path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
    print(f"Loading trade data from {args.input}")
    trades_df = load_trades_from_inputs(args.input)
    if trades_df.empty:
        raise ValueError("No trades were loaded from the provided input dataset")
    print("Processing dataset-driven session event study...")
    events_df, bubble_trades_df, stats = build_events_dataset(trades_df, args.symbol, args.session_start_hour, args.session_start_minute, args.min_qty, args.min_notional, args.min_bubble_tier, args.cluster_window_seconds, args.bubble_percentile_combination, reaction_windows, research_windows, args.retest_zone_pct, args.setup_reaction_window_seconds, args.setup_mfe_threshold_pct, args.setup_efficiency_threshold, stop_buffer_pct, args.primary_risk_model, args.trade_event_gap_seconds)
    if args.write_bubble_trades:
        print(f"Bubble trade rows: {len(bubble_trades_df)}")
        print(f"Bubble trade output: {args.bubble_trades_output_parquet}")
        if bubble_trades_df.empty:
            print("No bubble trades detected; skipping bubble trade parquet write")
        else:
            normalize_bubble_trade_output_precision(bubble_trades_df).to_parquet(args.bubble_trades_output_parquet)
    if events_df.empty:
        print("No complete event-study rows detected")
        print("\nValidation Statistics:")
        for key, value in stats.items():
            print(f"{key}: {value}")
        return
    print(f"Saving results to {args.output_parquet}" + (f" and {args.output_csv}" if args.write_csv else ""))
    events_df = normalize_output_precision(events_df)
    events_df.to_parquet(args.output_parquet)
    if args.write_csv:
        events_df.to_csv(args.output_csv, index=False)
    print("\nValidation Statistics:")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("\nTrade events by directional_bias:")
    print(events_df["directional_bias"].value_counts(dropna=False))
    print("\nTrade events by location:")
    print(events_df["location"].value_counts(dropna=False))
    print("\nBubble tier distribution:")
    print(events_df["bubble_tier"].value_counts(dropna=False))
    print(f"Dataset row count: {len(events_df)}")


if __name__ == "__main__":
    main()
