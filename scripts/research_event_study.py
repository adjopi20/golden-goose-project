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
    "very_strict": {"medium": 99.5, "large": 99.9,"extreme": 99.95},
}
PRICE_TARGETS = [0.05, 0.10, 0.20, 0.50]
EXPANSION_TARGETS = [0.25, 0.50, 1.00, 2.00, 3.00]
INITIATIVE_EFFICIENCY_THRESHOLD = 0.80
ABSORBED_EFFICIENCY_THRESHOLD = 0.40


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dataset-driven event study for outside-value bubbles")
    parser.add_argument("--input", required=True, help="Single path or comma-separated trade input paths")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--min-bubble-tier", default="medium", choices=sorted(TIER_RANK.keys()))
    parser.add_argument("--cluster-window-seconds", type=int, default=10)
    parser.add_argument("--retest-zone-pct", type=float, default=0.0002)
    parser.add_argument("--reaction-windows", default="10,30,60")
    parser.add_argument("--research-windows", default="30,60,300,900,3600")
    parser.add_argument(
        "--bubble-percentile-combination",
        default="strict",
        choices=sorted(BUBBLE_PERCENTILE_COMBINATIONS.keys()),
        help="Previous-session qty percentile threshold preset for bubble tiering",
    )
    parser.add_argument("--setup-reaction-window-seconds", type=int, default=30)
    parser.add_argument("--setup-mfe-threshold-pct", type=float, default=0.001)
    parser.add_argument("--setup-efficiency-threshold", type=float, default=0.80)
    # Legacy CLI compatibility only; unused by percentile-tiered v1 research flow.
    parser.add_argument("--min-qty", type=float, default=1.0)
    # Legacy CLI compatibility only; unused by percentile-tiered v1 research flow.
    parser.add_argument("--min-notional", type=float, default=10000.0)
    parser.add_argument("--output-parquet", default="research/event_study.parquet")
    parser.add_argument(
        "--write-bubble-trades",
        action="store_true",
        help="Write candidate bubble trades that pass the configured threshold to a separate parquet file.",
    )
    parser.add_argument(
        "--bubble-trades-output-parquet",
        default="research/bubble_trades.parquet",
        help="Output parquet path for candidate bubble trades when --write-bubble-trades is used.",
    )
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
    return args


def parse_windows(raw_value: str, arg_name: str) -> list[int]:
    windows = sorted({int(part.strip()) for part in raw_value.split(",") if part.strip()})
    if not windows:
        raise ValueError(f"{arg_name} must contain at least one integer value")
    if any(window <= 0 for window in windows):
        raise ValueError(f"{arg_name} values must all be > 0")
    return windows


def add_session_id_column(
    trades_df: pd.DataFrame,
    session_start_hour: int,
    session_start_minute: int,
) -> pd.DataFrame:
    out = trades_df.copy()

    offset = pd.Timedelta(hours=session_start_hour, minutes=session_start_minute)

    ts = pd.to_datetime(out["timestamp"], unit="ms", utc=True)
    shifted = ts - offset

    out["session_id"] = shifted.dt.date.astype(str)

    return out


def validate_session_continuity(session_ids: list[str]) -> None:
    if not session_ids:
        raise ValueError("No sessions discovered from the provided dataset")
    dates = [date.fromisoformat(session_id) for session_id in session_ids]
    expected_dates: list[str] = []
    current = dates[0]
    end = dates[-1]
    while current <= end:
        expected_dates.append(current.isoformat())
        current += timedelta(days=1)
    actual = set(session_ids)
    missing = [session_id for session_id in expected_dates if session_id not in actual]
    if missing:
        raise ValueError(f"Input dataset has missing session continuity: {missing}")


def session_window_ms(session_id: str, session_start_hour: int, session_start_minute: int) -> tuple[int, int]:
    session_start = pd.Timestamp(
        year=date.fromisoformat(session_id).year,
        month=date.fromisoformat(session_id).month,
        day=date.fromisoformat(session_id).day,
        hour=session_start_hour,
        minute=session_start_minute,
        tz="UTC",
    )
    session_end = session_start + pd.Timedelta(hours=24)
    return int(session_start.timestamp() * 1000), int(session_end.timestamp() * 1000)


def is_session_window_available(
    session_id: str,
    dataset_min_timestamp: int,
    dataset_max_timestamp: int,
    session_start_hour: int,
    session_start_minute: int,
) -> bool:
    session_start_ms, session_end_ms = session_window_ms(session_id, session_start_hour, session_start_minute)
    return dataset_min_timestamp <= session_start_ms and dataset_max_timestamp >= session_end_ms - 1


def clamp_metrics(favorable_move: float, adverse_move: float) -> tuple[float, float, float | None]:
    mfe = max(0.0, float(favorable_move))
    mae = max(0.0, float(adverse_move))
    efficiency = mfe / (mfe + mae) if (mfe + mae) > 0 else None
    return mfe, mae, efficiency


def get_time_window_indices(
    timestamps: np.ndarray,
    start_timestamp: int,
    end_timestamp: int,
) -> tuple[int, int]:
    """
    Return array slice indices for:
    timestamp > start_timestamp
    timestamp <= end_timestamp

    timestamps must be sorted ascending.
    """
    start_idx = int(np.searchsorted(timestamps, start_timestamp, side="right"))
    end_idx = int(np.searchsorted(timestamps, end_timestamp, side="right"))
    return start_idx, end_idx


def get_time_window_arrays(
    timestamps: np.ndarray,
    prices: np.ndarray,
    start_timestamp: int,
    end_timestamp: int,
) -> tuple[np.ndarray, np.ndarray]:
    start_idx, end_idx = get_time_window_indices(
        timestamps=timestamps,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    return timestamps[start_idx:end_idx], prices[start_idx:end_idx]


def calculate_directional_moves(anchor_price: float, bubble_side: str, min_price: float, max_price: float) -> tuple[float, float]:
    if bubble_side == "sell":
        favorable_move = (anchor_price - min_price) / anchor_price
        adverse_move = (max_price - anchor_price) / anchor_price
    else:
        favorable_move = (max_price - anchor_price) / anchor_price
        adverse_move = (anchor_price - min_price) / anchor_price
    return favorable_move, adverse_move


def calculate_reaction_metrics_np(
    cluster_end_timestamp: int,
    anchor_price: float,
    bubble_side: str,
    future_timestamps: np.ndarray,
    future_prices: np.ndarray,
    windows: list[int],
    initiative_mfe_threshold_pct: float,
    initiative_efficiency_threshold: float,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}

    for window in windows:
        window_end = cluster_end_timestamp + window * 1000
        end_idx = int(np.searchsorted(future_timestamps, window_end, side="right"))
        window_prices = future_prices[:end_idx]

        if len(window_prices) == 0:
            metrics[f"reaction_mfe_{window}s_pct"] = None
            metrics[f"reaction_mae_{window}s_pct"] = None
            metrics[f"reaction_efficiency_{window}s"] = None
            continue

        min_price = float(np.min(window_prices))
        max_price = float(np.max(window_prices))
        favorable_move, adverse_move = calculate_directional_moves(anchor_price, bubble_side, min_price, max_price)
        mfe, mae, efficiency = clamp_metrics(favorable_move, adverse_move)
        metrics[f"reaction_mfe_{window}s_pct"] = mfe
        metrics[f"reaction_mae_{window}s_pct"] = mae
        metrics[f"reaction_efficiency_{window}s"] = efficiency

    reaction_30s_pct = metrics.get("reaction_mfe_30s_pct")
    reaction_efficiency_30s = metrics.get("reaction_efficiency_30s")
    if (
        reaction_efficiency_30s is not None
        and reaction_30s_pct is not None
        and reaction_efficiency_30s >= initiative_efficiency_threshold
        and reaction_30s_pct >= initiative_mfe_threshold_pct
    ):
        metrics["event_reaction_type"] = "initiative"
    elif reaction_efficiency_30s is not None and reaction_efficiency_30s < ABSORBED_EFFICIENCY_THRESHOLD:
        metrics["event_reaction_type"] = "absorbed"
    else:
        metrics["event_reaction_type"] = "neutral"
    return metrics


def calculate_future_metrics_np(
    start_timestamp: int,
    anchor_price: float,
    bubble_side: str,
    future_timestamps: np.ndarray,
    future_prices: np.ndarray,
    windows: list[int],
    retest_zone_pct: float,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {"retest_zone_pct": retest_zone_pct}

    for window in windows:
        window_end = start_timestamp + window * 1000
        end_idx = int(np.searchsorted(future_timestamps, window_end, side="right"))
        window_prices = future_prices[:end_idx]
        window_timestamps = future_timestamps[:end_idx]

        if len(window_prices) == 0:
            metrics[f"mfe_{window}s_pct"] = None
            metrics[f"mae_{window}s_pct"] = None
            metrics[f"efficiency_{window}s"] = None
            for target in PRICE_TARGETS:
                metrics.setdefault(f"seconds_to_{target:.2f}_pct", None)
            continue

        window_min = float(np.min(window_prices))
        window_max = float(np.max(window_prices))
        favorable_window, adverse_window = calculate_directional_moves(anchor_price, bubble_side, window_min, window_max)
        mfe_window, mae_window, efficiency_window = clamp_metrics(favorable_window, adverse_window)
        metrics[f"mfe_{window}s_pct"] = mfe_window
        metrics[f"mae_{window}s_pct"] = mae_window
        metrics[f"efficiency_{window}s"] = efficiency_window

        for target in PRICE_TARGETS:
            target_pct = target / 100
            if bubble_side == "sell":
                target_price = anchor_price * (1 - target_pct)
                target_indices = np.where(window_prices <= target_price)[0]
            else:
                target_price = anchor_price * (1 + target_pct)
                target_indices = np.where(window_prices >= target_price)[0]

            key = f"seconds_to_{target:.2f}_pct"
            if len(target_indices) > 0 and metrics.get(key) is None:
                first_idx = int(target_indices[0])
                metrics[key] = (int(window_timestamps[first_idx]) - start_timestamp) / 1000.0
            else:
                metrics.setdefault(key, None)

    min_price = float(np.min(future_prices))
    max_price = float(np.max(future_prices))
    favorable_move, adverse_move = calculate_directional_moves(anchor_price, bubble_side, min_price, max_price)
    mfe_pct, _, _ = clamp_metrics(favorable_move, adverse_move)
    metrics["max_expansion_pct"] = mfe_pct
    for target in EXPANSION_TARGETS:
        metrics[f"reached_{target:.2f}_pct"] = mfe_pct >= target / 100

    if bubble_side == "sell":
        retest_condition = future_prices >= anchor_price * (1 - retest_zone_pct)
    else:
        retest_condition = future_prices <= anchor_price * (1 + retest_zone_pct)

    retest_indices = np.where(retest_condition)[0]
    bubble_retested = len(retest_indices) > 0
    metrics["bubble_retested"] = bubble_retested
    metrics["retest_count"] = int(len(retest_indices))

    if bubble_retested:
        first_retest_idx = int(retest_indices[0])
        metrics["time_to_retest_seconds"] = (int(future_timestamps[first_retest_idx]) - start_timestamp) / 1000.0
        prices_before = future_prices[: first_retest_idx + 1]
        timestamps_before = future_timestamps[: first_retest_idx + 1]
    else:
        metrics["time_to_retest_seconds"] = None
        prices_before = future_prices
        timestamps_before = future_timestamps

    min_price_before = float(np.min(prices_before))
    max_price_before = float(np.max(prices_before))
    favorable_before, adverse_before = calculate_directional_moves(anchor_price, bubble_side, min_price_before, max_price_before)
    max_favorable_pct, max_adverse_pct, _ = clamp_metrics(favorable_before, adverse_before)

    if bubble_side == "sell":
        max_favorable_price = min_price_before
        max_adverse_price = max_price_before
        max_favorable_idx = int(np.argmin(prices_before))
    else:
        max_favorable_price = max_price_before
        max_adverse_price = min_price_before
        max_favorable_idx = int(np.argmax(prices_before))

    metrics["max_favorable_before_retest_pct"] = max_favorable_pct
    metrics["max_favorable_before_retest_price"] = max_favorable_price
    metrics["max_adverse_before_retest_pct"] = max_adverse_pct
    metrics["max_adverse_before_retest_price"] = max_adverse_price
    metrics["time_to_max_favorable_before_retest_seconds"] = (
        int(timestamps_before[max_favorable_idx]) - start_timestamp
    ) / 1000.0
    return metrics


def build_qty_percentile_thresholds(
    previous_session_df: pd.DataFrame,
    percentile_combination: str,
) -> dict[str, Any]:
    if previous_session_df.empty:
        raise ValueError("previous_session_df must not be empty")

    preset = BUBBLE_PERCENTILE_COMBINATIONS[percentile_combination]
    qty_values = previous_session_df["qty"].to_numpy(dtype=np.float64)
    thresholds = np.percentile(
        qty_values,
        [preset["medium"], preset["large"], preset["extreme"]],
    )

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


def process_session_events_sequential(
    current_session_df: pd.DataFrame,
    previous_session_profile: dict[str, Any],
    qty_thresholds: dict[str, Any],
    session_id: str,
    symbol: str,
    min_bubble_tier: str,
    all_timestamps: np.ndarray,
    all_prices: np.ndarray,
    last_available_trade_timestamp: int,
    reaction_windows: list[int],
    research_windows: list[int],
    retest_zone_pct: float,
    setup_reaction_window_seconds: int,
    setup_mfe_threshold_pct: float,
    setup_efficiency_threshold: float,
    config_metadata: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
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

    correct_long = above_vah & is_buy_aggression
    correct_short = below_val & is_sell_aggression
    correct_side_mask = correct_long | correct_short

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

    candidate_mask = correct_side_mask & passes_min_tier
    candidate_indices = np.where(candidate_mask)[0]

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
            location = "above_vah"
            directional_bias = "long"
            aggressive_side = "buy"
        elif below_val[i]:
            location = "below_val"
            directional_bias = "short"
            aggressive_side = "sell"
        else:
            continue

        if qty >= extreme_threshold:
            bubble_tier = "extreme"
            bubble_percentile_score = float(qty_thresholds["bubble_extreme_percentile"])
        elif qty >= large_threshold:
            bubble_tier = "large"
            bubble_percentile_score = float(qty_thresholds["bubble_large_percentile"])
        elif qty >= medium_threshold:
            bubble_tier = "medium"
            bubble_percentile_score = float(qty_thresholds["bubble_medium_percentile"])
        else:
            continue

        timestamp_utc = pd.to_datetime(timestamp_ms, unit="ms", utc=True)
        timestamp_wib = timestamp_utc.tz_convert("Asia/Jakarta")
        bubble_trade_records.append(
            {
                "bubble_trade_id": uuid.uuid4().hex,
                "symbol": symbol,
                "session_id": session_id,
                "session_date": session_date,
                "previous_session_id": prev_session_id,
                "timestamp": timestamp_ms,
                "timestamp_utc": timestamp_utc,
                "timestamp_wib": timestamp_wib,
                "price": price,
                "qty": qty,
                "notional": notional,
                "is_buyer_maker": bool(session_is_buyer_maker[i]),
                "aggressive_side": aggressive_side,
                "directional_bias": directional_bias,
                "location": location,
                "previous_val": prev_val,
                "previous_vah": prev_vah,
                "previous_poc": prev_poc,
                "distance_from_val_pct": (price - prev_val) / prev_val if prev_val > 0 else None,
                "distance_from_vah_pct": (price - prev_vah) / prev_vah if prev_vah > 0 else None,
                "distance_from_poc_pct": (price - prev_poc) / prev_poc if prev_poc > 0 else None,
                "bubble_tier": bubble_tier,
                "bubble_percentile_score": bubble_percentile_score,
                **qty_thresholds,
                "min_bubble_tier": str(min_bubble_tier),
                "session_start_hour": int(config_metadata["session_start_hour"]),
                "session_start_minute": int(config_metadata["session_start_minute"]),
            }
        )

        reaction_required_end = timestamp_ms + max_reaction_window_ms
        if reaction_required_end > last_available_trade_timestamp:
            stats["skipped_incomplete_reaction_future"] += 1
            continue

        reaction_timestamps, reaction_prices = get_time_window_arrays(
            timestamps=all_timestamps,
            prices=all_prices,
            start_timestamp=timestamp_ms,
            end_timestamp=reaction_required_end,
        )
        if len(reaction_prices) == 0:
            stats["skipped_incomplete_reaction_future"] += 1
            continue

        reaction_metrics = calculate_reaction_metrics_np(
            cluster_end_timestamp=timestamp_ms,
            anchor_price=price,
            bubble_side=aggressive_side,
            future_timestamps=reaction_timestamps,
            future_prices=reaction_prices,
            windows=reaction_windows,
            initiative_mfe_threshold_pct=setup_mfe_threshold_pct,
            initiative_efficiency_threshold=setup_efficiency_threshold,
        )
        setup_mfe_key = f"reaction_mfe_{setup_reaction_window_seconds}s_pct"
        setup_eff_key = f"reaction_efficiency_{setup_reaction_window_seconds}s"
        setup_mfe_value = reaction_metrics.get(setup_mfe_key)
        setup_eff_value = reaction_metrics.get(setup_eff_key)
        setup_confirmed = (
            setup_mfe_value is not None
            and setup_eff_value is not None
            and setup_mfe_value >= setup_mfe_threshold_pct
            and setup_eff_value >= setup_efficiency_threshold
        )
        if not setup_confirmed:
            stats["skipped_unconfirmed_setup"] += 1
            continue

        setup_confirmation_timestamp = timestamp_ms + setup_reaction_window_seconds * 1000
        confirmation_start_idx, confirmation_end_idx = get_time_window_indices(
            timestamps=all_timestamps,
            start_timestamp=timestamp_ms,
            end_timestamp=setup_confirmation_timestamp,
        )
        confirmation_prices = all_prices[confirmation_start_idx:confirmation_end_idx]

        if len(confirmation_prices) == 0:
            confirmation_price = price
        else:
            confirmation_price = float(confirmation_prices[-1])

        observation_start_timestamp = setup_confirmation_timestamp
        observation_anchor_price = confirmation_price

        required_end = observation_start_timestamp + max_research_window_ms
        if required_end > last_available_trade_timestamp:
            stats["skipped_incomplete_future"] += 1
            continue

        future_timestamps, future_prices = get_time_window_arrays(
            timestamps=all_timestamps,
            prices=all_prices,
            start_timestamp=observation_start_timestamp,
            end_timestamp=required_end,
        )
        if len(future_prices) == 0:
            stats["skipped_incomplete_future"] += 1
            continue

        future_metrics = calculate_future_metrics_np(
            start_timestamp=observation_start_timestamp,
            anchor_price=observation_anchor_price,
            bubble_side=aggressive_side,
            future_timestamps=future_timestamps,
            future_prices=future_prices,
            windows=research_windows,
            retest_zone_pct=retest_zone_pct,
        )

        event_id = uuid.uuid4().hex
        cluster_id = event_id
        event_data = {
            "event_id": event_id,
            "cluster_id": cluster_id,
            "event_type": "single_aggtrade_confirmed_setup",
            "event_version": "v1",
            "symbol": symbol,
            "session_id": session_id,
            "session_date": session_date,
            "previous_session_id": prev_session_id,
            "balance_state": "imbalance",
            "location": location,
            "directional_bias": directional_bias,
            "bubble_side": aggressive_side,
            "cluster_side": aggressive_side,
            "bubble_tier": bubble_tier,
            "previous_val": prev_val,
            "previous_vah": prev_vah,
            "previous_poc": prev_poc,
            "distance_from_val_pct": (price - prev_val) / prev_val if prev_val > 0 else None,
            "distance_from_vah_pct": (price - prev_vah) / prev_vah if prev_vah > 0 else None,
            "distance_from_poc_pct": (price - prev_poc) / prev_poc if prev_poc > 0 else None,
            "cluster_start_timestamp": timestamp_ms,
            "cluster_end_timestamp": timestamp_ms,
            "cluster_bubble_count": 1,
            "cluster_total_qty": qty,
            "cluster_total_notional": notional,
            "cluster_size": notional,
            "cluster_max_bubble_score": bubble_percentile_score,
            "cluster_mean_bubble_score": bubble_percentile_score,
            "anchor_price": price,
            "bubble_anchor_price": price,
            "anchor_bubble_price": price,
            "anchor_bubble_qty": qty,
            "anchor_bubble_notional": notional,
            "anchor_bubble_score": bubble_percentile_score,
            "bubble_timestamp": timestamp_ms,
            "event_timestamp": timestamp_ms,
            "setup_confirmation_timestamp": setup_confirmation_timestamp,
            "setup_confirmation_delay_seconds": int(setup_reaction_window_seconds),
            "confirmation_price": confirmation_price,
            "observation_start_timestamp": observation_start_timestamp,
            "observation_anchor_price": observation_anchor_price,
            "chart_replay_date": session_date.isoformat(),
            "setup_confirmed": True,
            "setup_reaction_window_seconds": int(setup_reaction_window_seconds),
            "setup_mfe_threshold_pct": float(setup_mfe_threshold_pct),
            "setup_efficiency_threshold": float(setup_efficiency_threshold),
            "bubble_percentile_score": bubble_percentile_score,
            **qty_thresholds,
            **config_metadata,
        }
        event_data.update(reaction_metrics)
        event_data.update(future_metrics)
        event_records.append(event_data)
        stats["total_confirmed_setups"] += 1

    return event_records, bubble_trade_records, stats


def build_events_dataset(
    trades_df: pd.DataFrame,
    symbol: str,
    session_start_hour: int,
    session_start_minute: int,
    min_qty: float,
    min_notional: float,
    min_bubble_tier: str,
    cluster_window_seconds: int,
    bubble_percentile_combination: str,
    reaction_windows: list[int],
    research_windows: list[int],
    retest_zone_pct: float,
    setup_reaction_window_seconds: int,
    setup_mfe_threshold_pct: float,
    setup_efficiency_threshold: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    print("Adding session_id column...", flush=True)
    trades_df = add_session_id_column(trades_df, session_start_hour, session_start_minute)
    print("Discovering sessions...", flush=True)
    session_ids = sorted(trades_df["session_id"].unique().tolist())
    print(f"Discovered {len(session_ids)} sessions", flush=True)
    validate_session_continuity(session_ids)
    all_timestamps = trades_df["timestamp"].to_numpy(dtype=np.int64)
    all_prices = trades_df["price"].to_numpy(dtype=np.float64)
    if len(all_timestamps) > 1 and np.any(all_timestamps[1:] < all_timestamps[:-1]):
        raise ValueError("trades_df timestamps must be sorted ascending before searchsorted optimization")
    dataset_min_timestamp = int(trades_df["timestamp"].min())
    last_available_trade_timestamp = int(trades_df["timestamp"].max())
    max_research_window_ms = max(research_windows) * 1000
    max_research_window_seconds = max(research_windows)
    grouped_sessions = {session_id: session_df.copy() for session_id, session_df in trades_df.groupby("session_id", sort=True)}
    events: list[dict[str, Any]] = []
    bubble_trades: list[dict[str, Any]] = []
    skipped_incomplete_previous_session = 0
    researched_sessions = 0
    reaction_windows_str = ",".join(str(window) for window in reaction_windows)
    research_windows_str = ",".join(str(window) for window in research_windows)
    aggregate_stats = {
        "total_deep_trades": 0,
        "skipped_inside_value": 0,
        "skipped_wrong_side": 0,
        "skipped_below_medium_percentile": 0,
        "skipped_below_min_bubble_tier": 0,
        "candidate_bubbles_after_tier_filter": 0,
        "skipped_incomplete_reaction_future": 0,
        "skipped_unconfirmed_setup": 0,
        "skipped_incomplete_future": 0,
        "total_confirmed_setups": 0,
    }
    config_metadata = {
        "min_qty": float(min_qty) if min_qty is not None else None,
        "min_notional": float(min_notional) if min_notional is not None else None,
        "min_bubble_tier": str(min_bubble_tier),
        "cluster_window_seconds": int(cluster_window_seconds),
        "retest_zone_pct": float(retest_zone_pct),
        "reaction_windows": reaction_windows_str,
        "research_windows": research_windows_str,
        "session_start_hour": int(session_start_hour),
        "session_start_minute": int(session_start_minute),
        "max_research_window_seconds": int(max_research_window_seconds),
    }

    total_sessions_to_process = len(session_ids) - 1

    for idx in range(1, len(session_ids)):
        previous_session_id = session_ids[idx - 1]
        session_id = session_ids[idx]
        
        print(
            f"[{idx}/{total_sessions_to_process}] "
            f"Processing session {session_id} "
            f"(prev={previous_session_id})"
        )
            
        if not is_session_window_available(
            previous_session_id,
            dataset_min_timestamp=dataset_min_timestamp,
            dataset_max_timestamp=last_available_trade_timestamp,
            session_start_hour=session_start_hour,
            session_start_minute=session_start_minute,
        ):
            skipped_incomplete_previous_session += 1
            continue
        previous_session_df = grouped_sessions[previous_session_id]
        current_session_df = grouped_sessions[session_id]
        previous_session_profile = build_volume_profile(previous_session_df, n_bins=50)
        qty_thresholds = build_qty_percentile_thresholds(previous_session_df, bubble_percentile_combination)
        session_events, session_bubble_trades, session_stats = process_session_events_sequential(
            current_session_df=current_session_df,
            previous_session_profile=previous_session_profile,
            qty_thresholds=qty_thresholds,
            session_id=session_id,
            symbol=symbol,
            min_bubble_tier=min_bubble_tier,
            all_timestamps=all_timestamps,
            all_prices=all_prices,
            last_available_trade_timestamp=last_available_trade_timestamp,
            reaction_windows=reaction_windows,
            research_windows=research_windows,
            retest_zone_pct=retest_zone_pct,
            setup_reaction_window_seconds=setup_reaction_window_seconds,
            setup_mfe_threshold_pct=setup_mfe_threshold_pct,
            setup_efficiency_threshold=setup_efficiency_threshold,
            config_metadata=config_metadata,
        )
        
        print(
            f"    deep_trades={session_stats['total_deep_trades']:,} "
            f"candidates_after_tier={session_stats['candidate_bubbles_after_tier_filter']:,} "
            f"confirmed_setups={session_stats['total_confirmed_setups']:,}"
        )

        researched_sessions += 1
        events.extend(session_events)
        bubble_trades.extend(session_bubble_trades)
        for key in aggregate_stats:
            aggregate_stats[key] += int(session_stats.get(key, 0))

        print(
            f"    complete | "
            f"events_so_far={len(events):,}"
        )

    events_df = pd.DataFrame(events)
    bubble_trades_df = pd.DataFrame(bubble_trades)
    if not events_df.empty:
        if not events_df["event_id"].is_unique:
            raise ValueError("event_id must be unique")
        if not (events_df["cluster_id"] == events_df["event_id"]).all():
            raise ValueError("cluster_id must equal event_id for all rows")
        expected_setup_confirmation_timestamp = (
            events_df["event_timestamp"] + events_df["setup_reaction_window_seconds"] * 1000
        )
        if not (events_df["setup_confirmation_timestamp"] == expected_setup_confirmation_timestamp).all():
            raise ValueError("setup_confirmation_timestamp must equal event_timestamp + setup_reaction_window_seconds * 1000")
        if not (events_df["observation_start_timestamp"] == events_df["setup_confirmation_timestamp"]).all():
            raise ValueError("observation_start_timestamp must equal setup_confirmation_timestamp")
        if not (events_df["observation_anchor_price"] == events_df["confirmation_price"]).all():
            raise ValueError("observation_anchor_price must equal confirmation_price")

    stats = {
        "total_sessions_available": int(len(session_ids)),
        "total_sessions_researched": int(researched_sessions),
        "total_deep_trades": int(aggregate_stats["total_deep_trades"]),
        "skipped_inside_value": int(aggregate_stats["skipped_inside_value"]),
        "skipped_wrong_side": int(aggregate_stats["skipped_wrong_side"]),
        "skipped_below_medium_percentile": int(aggregate_stats["skipped_below_medium_percentile"]),
        "skipped_below_min_bubble_tier": int(aggregate_stats["skipped_below_min_bubble_tier"]),
        "candidate_bubbles_after_tier_filter": int(aggregate_stats["candidate_bubbles_after_tier_filter"]),
        "skipped_incomplete_reaction_future": int(aggregate_stats["skipped_incomplete_reaction_future"]),
        "skipped_unconfirmed_setup": int(aggregate_stats["skipped_unconfirmed_setup"]),
        "skipped_incomplete_future": int(aggregate_stats["skipped_incomplete_future"]),
        "total_confirmed_setups": int(aggregate_stats["total_confirmed_setups"]),
        "total_bubbles": int(aggregate_stats["candidate_bubbles_after_tier_filter"]),
        "total_candidate_bubbles_before_setup_confirmation": int(aggregate_stats["candidate_bubbles_after_tier_filter"]),
        "skipped_incomplete_previous_session": int(skipped_incomplete_previous_session),
        "average_cluster_size": (float(events_df["cluster_bubble_count"].mean()) if aggregate_stats["total_confirmed_setups"] > 0 else None),
    }

    return events_df, bubble_trades_df, stats


def normalize_output_precision(events_df: pd.DataFrame) -> pd.DataFrame:
    out = events_df.copy()

    pct_columns = [column for column in out.columns if "_pct" in column]
    efficiency_columns = [column for column in out.columns if "efficiency" in column]

    price_columns = [
        "previous_val",
        "previous_vah",
        "previous_poc",
        "anchor_price",
        "bubble_anchor_price",
        "anchor_bubble_price",
        "confirmation_price",
        "observation_anchor_price",
        "max_favorable_before_retest_price",
        "max_adverse_before_retest_price",
    ]
    notional_columns = [
        "cluster_total_notional",
        "anchor_bubble_notional",
    ]
    quantity_columns = [
        "cluster_total_qty",
        "anchor_bubble_qty",
    ]
    bubble_score_columns = [
        "cluster_max_bubble_score",
        "cluster_mean_bubble_score",
        "anchor_bubble_score",
        "bubble_medium_qty_threshold",
        "bubble_large_qty_threshold",
        "bubble_extreme_qty_threshold",
        "bubble_percentile_score",
    ]
    percentile_columns = [
        "bubble_medium_percentile",
        "bubble_large_percentile",
        "bubble_extreme_percentile",
    ]
    time_columns = [
        "setup_confirmation_delay_seconds",
        "seconds_to_0.05_pct",
        "seconds_to_0.10_pct",
        "seconds_to_0.20_pct",
        "seconds_to_0.50_pct",
        "time_to_retest_seconds",
        "time_to_max_favorable_before_retest_seconds",
    ]

    for column in pct_columns:
        out[column] = out[column].round(4)

    for column in efficiency_columns:
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

    for column in bubble_score_columns:
        if column in out.columns:
            out[column] = out[column].round(3)

    for column in percentile_columns:
        if column in out.columns:
            out[column] = out[column].round(4)

    for column in time_columns:
        if column in out.columns:
            out[column] = out[column].round(2)

    return out


def normalize_bubble_trade_output_precision(bubble_trades_df: pd.DataFrame) -> pd.DataFrame:
    out = bubble_trades_df.copy()

    pct_columns = [column for column in out.columns if "_pct" in column]
    price_columns = [
        "price",
        "previous_val",
        "previous_vah",
        "previous_poc",
    ]
    notional_columns = ["notional"]
    quantity_columns = [
        "qty",
        "bubble_medium_qty_threshold",
        "bubble_large_qty_threshold",
        "bubble_extreme_qty_threshold",
    ]
    score_columns = ["bubble_percentile_score"]
    percentile_columns = [
        "bubble_medium_percentile",
        "bubble_large_percentile",
        "bubble_extreme_percentile",
    ]

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
            out[column] = out[column].round(3)

    for column in score_columns:
        if column in out.columns:
            out[column] = out[column].round(3)

    for column in percentile_columns:
        if column in out.columns:
            out[column] = out[column].round(4)

    return out


def main() -> None:
    args = parse_args()
    reaction_windows = parse_windows(args.reaction_windows, "--reaction-windows")
    reaction_windows = sorted(set(reaction_windows + [args.setup_reaction_window_seconds]))
    research_windows = parse_windows(args.research_windows, "--research-windows")
    output_dir = os.path.dirname(args.output_parquet)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    if args.write_bubble_trades:
        bubble_output_dir = os.path.dirname(args.bubble_trades_output_parquet)
        if bubble_output_dir:
            os.makedirs(bubble_output_dir, exist_ok=True)
    if args.write_csv:
        csv_output_dir = os.path.dirname(args.output_csv)
        if csv_output_dir:
            os.makedirs(csv_output_dir, exist_ok=True)
    print(f"Loading trade data from {args.input}")
    trades_df = load_trades_from_inputs(args.input)
    if trades_df.empty:
        raise ValueError("No trades were loaded from the provided input dataset")
    print("Processing dataset-driven session event study...")
    events_df, bubble_trades_df, stats = build_events_dataset(
        trades_df=trades_df,
        symbol=args.symbol,
        session_start_hour=args.session_start_hour,
        session_start_minute=args.session_start_minute,
        min_qty=args.min_qty,
        min_notional=args.min_notional,
        min_bubble_tier=args.min_bubble_tier,
        cluster_window_seconds=args.cluster_window_seconds,
        bubble_percentile_combination=args.bubble_percentile_combination,
        reaction_windows=reaction_windows,
        research_windows=research_windows,
        retest_zone_pct=args.retest_zone_pct,
        setup_reaction_window_seconds=args.setup_reaction_window_seconds,
        setup_mfe_threshold_pct=args.setup_mfe_threshold_pct,
        setup_efficiency_threshold=args.setup_efficiency_threshold,
    )
    if args.write_bubble_trades:
        print(f"Bubble trade rows: {len(bubble_trades_df)}")
        print(f"Bubble trade output: {args.bubble_trades_output_parquet}")
        if bubble_trades_df.empty:
            print("No bubble trades detected; skipping bubble trade parquet write")
        else:
            bubble_trades_df = normalize_bubble_trade_output_precision(bubble_trades_df)
            bubble_trades_df.to_parquet(args.bubble_trades_output_parquet)
    if events_df.empty:
        print("No complete event-study rows detected")
        print("\nValidation Statistics:")
        for key, value in stats.items():
            print(f"{key}: {value}")
        return
    if args.write_csv:
        print(f"Saving results to {args.output_parquet} and {args.output_csv}")
    else:
        print(f"Saving results to {args.output_parquet}")
    events_df = normalize_output_precision(events_df)
    events_df.to_parquet(args.output_parquet)
    if args.write_csv:
        events_df.to_csv(args.output_csv, index=False)
    print("\nValidation Statistics:")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("\nEvents by directional_bias:")
    print(events_df["directional_bias"].value_counts(dropna=False))
    print("\nEvents by location:")
    print(events_df["location"].value_counts(dropna=False))
    print("\nEvents by event_reaction_type:")
    print(events_df["event_reaction_type"].value_counts(dropna=False))
    print("\nBubble tier distribution:")
    print(events_df["bubble_tier"].value_counts(dropna=False))
    print(f"Dataset row count: {len(events_df)}")


if __name__ == "__main__":
    main()