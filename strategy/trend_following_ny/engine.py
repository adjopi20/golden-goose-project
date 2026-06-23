from __future__ import annotations

import numpy as np
import pandas as pd


def _apply_entry_slippage(price: float, direction: str, slippage_pct: float) -> float:
    return price * (1 + slippage_pct) if direction == "LONG" else price * (1 - slippage_pct)


def _apply_exit_slippage(price: float, direction: str, slippage_pct: float) -> float:
    return price * (1 - slippage_pct) if direction == "LONG" else price * (1 + slippage_pct)


def _pnl(direction: str, entry_price: float, exit_price: float) -> float:
    return exit_price - entry_price if direction == "LONG" else entry_price - exit_price


def _find_entry_index(signal_timestamp: pd.Timestamp, signal_raw_index: int, timestamp_ns_arr: np.ndarray, raw_index_arr: np.ndarray, start_idx: int, end_idx: int, entry_latency_ms: int) -> int | None:
    min_ts_ns = int((signal_timestamp + pd.Timedelta(milliseconds=entry_latency_ms)).value)
    for idx in range(start_idx, end_idx):
        if timestamp_ns_arr[idx] >= min_ts_ns and raw_index_arr[idx] > signal_raw_index:
            return idx
    return None


def simulate_ny_session(pre_ny_context, entry_decision, stop_decision, config, price_arr: np.ndarray, timestamp_arr: np.ndarray, timestamp_ns_arr: np.ndarray, raw_index_arr: np.ndarray, ny_start_idx: int, ny_end_idx: int):
    if ny_end_idx <= ny_start_idx:
        return {"status": "INSUFFICIENT_DATA", "reason": "EMPTY_NY_TRADES"}
    if entry_decision.status != "SIGNAL_CREATED":
        return {"status": entry_decision.status, "reason": entry_decision.reason, "signal_timestamp": entry_decision.signal_timestamp, "signal_raw_index": entry_decision.signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": entry_decision.direction}
    if stop_decision.status != "STOP_CREATED":
        return {"status": stop_decision.status, "reason": stop_decision.reason, "signal_timestamp": entry_decision.signal_timestamp, "signal_raw_index": entry_decision.signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": entry_decision.direction, "stop_price": stop_decision.stop_price, "stop_reference_price": stop_decision.reference_price}

    signal_timestamp = pd.Timestamp(entry_decision.signal_timestamp)
    signal_raw_index = int(entry_decision.signal_raw_index)
    entry_idx = _find_entry_index(signal_timestamp, signal_raw_index, timestamp_ns_arr, raw_index_arr, ny_start_idx, ny_end_idx, config.entry_latency_ms)
    if entry_idx is None:
        return {"status": "NO_ELIGIBLE_ENTRY_TRADE_AFTER_SIGNAL", "reason": "NO_ELIGIBLE_ENTRY_TRADE_AFTER_SIGNAL", "signal_timestamp": signal_timestamp, "signal_raw_index": signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": entry_decision.direction, "stop_price": stop_decision.stop_price, "stop_reference_price": stop_decision.reference_price, "entry_raw_index": None}

    direction = str(entry_decision.direction)
    entry_market_price = float(price_arr[entry_idx])
    entry_execution_price = _apply_entry_slippage(entry_market_price, direction, config.slippage_pct_per_side)
    entry_timestamp = pd.Timestamp(timestamp_arr[entry_idx])
    entry_raw_index = int(raw_index_arr[entry_idx])
    stop_price = float(stop_decision.stop_price)
    if (direction == "LONG" and not stop_price < entry_execution_price) or (direction == "SHORT" and not stop_price > entry_execution_price):
        return {"status": "STOP_CONFIGURATION_INVALID", "reason": "STOP_ON_WRONG_SIDE", "signal_timestamp": signal_timestamp, "signal_raw_index": signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": direction, "stop_price": stop_price, "stop_reference_price": stop_decision.reference_price, "proposed_entry_timestamp": entry_timestamp, "proposed_entry_raw_index": entry_raw_index, "proposed_entry_market_price": entry_market_price}

    initial_risk_price = abs(entry_execution_price - stop_price)
    tp1_price = entry_execution_price + config.tp1_R * initial_risk_price if direction == "LONG" else entry_execution_price - config.tp1_R * initial_risk_price
    tp1_hit = False
    tp1_timestamp = pd.NaT
    tp1_fill_price = np.nan
    trailing_activation_price = np.nan
    trailing_stop = stop_price
    mfe_r = float("-inf")
    mae_r = 0.0

    for idx in range(entry_idx + 1, len(price_arr)):
        market_price = float(price_arr[idx])
        ts = pd.Timestamp(timestamp_arr[idx])
        current_r = _pnl(direction, entry_execution_price, market_price) / initial_risk_price
        mfe_r = max(mfe_r, current_r)
        mae_r = min(mae_r, current_r)

        if not tp1_hit:
            stop_hit = (direction == "LONG" and market_price <= stop_price) or (direction == "SHORT" and market_price >= stop_price)
            tp1_cross = (direction == "LONG" and market_price >= tp1_price) or (direction == "SHORT" and market_price <= tp1_price)
            if stop_hit:
                final_exit_market_price = market_price
                final_exit_execution_price = _apply_exit_slippage(final_exit_market_price, direction, config.slippage_pct_per_side)
                gross_r = _pnl(direction, entry_market_price, final_exit_market_price) / initial_risk_price
                execution_r = _pnl(direction, entry_execution_price, final_exit_execution_price) / initial_risk_price
                slippage_r = gross_r - execution_r
                fee_price = entry_execution_price * config.fee_pct_per_side + final_exit_execution_price * config.fee_pct_per_side
                fee_r = fee_price / initial_risk_price
                return {"status": "TRADE_COMPLETED", "reason": "STOP_HIT", "signal_timestamp": signal_timestamp, "signal_raw_index": signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": direction, "stop_price": stop_price, "stop_reference_price": stop_decision.reference_price, "proposed_entry_timestamp": entry_timestamp, "proposed_entry_raw_index": entry_raw_index, "proposed_entry_market_price": entry_market_price, "entry_timestamp": entry_timestamp, "entry_raw_index": entry_raw_index, "entry_market_price": entry_market_price, "entry_execution_price": entry_execution_price, "entry_price_before_cost": entry_market_price, "entry_price": entry_execution_price, "initial_risk_price": initial_risk_price, "tp1_price": tp1_price, "tp1_timestamp": tp1_timestamp, "tp1_fill_price": tp1_fill_price, "trailing_activation_price": trailing_activation_price, "exit_timestamp": ts, "exit_raw_index": int(raw_index_arr[idx]), "final_exit_market_price": final_exit_market_price, "final_exit_execution_price": final_exit_execution_price, "gross_R": gross_r, "slippage_R": slippage_r, "fee_R": fee_r, "net_R": gross_r - slippage_r - fee_r, "mfe_R": mfe_r if np.isfinite(mfe_r) else np.nan, "mae_R": mae_r, "position_open_at_ny_end": ts > pre_ny_context.ny_end_utc, "position_open_at_end_of_data": False}
            if tp1_cross:
                tp1_hit = True
                tp1_timestamp = ts
                tp1_fill_price = tp1_price
                trailing_activation_price = market_price
                if direction == "LONG":
                    trailing_stop = max(trailing_stop, market_price - config.trailing_distance_R * initial_risk_price)
                else:
                    trailing_stop = min(trailing_stop, market_price + config.trailing_distance_R * initial_risk_price)
                continue

        if tp1_hit:
            if direction == "LONG":
                trailing_stop = max(trailing_stop, market_price - config.trailing_distance_R * initial_risk_price)
                trail_hit = market_price <= trailing_stop
            else:
                trailing_stop = min(trailing_stop, market_price + config.trailing_distance_R * initial_risk_price)
                trail_hit = market_price >= trailing_stop
            if trail_hit:
                final_exit_market_price = market_price
                final_exit_execution_price = _apply_exit_slippage(final_exit_market_price, direction, config.slippage_pct_per_side)
                remaining = 1.0 - config.tp1_fraction
                gross_r = config.tp1_fraction * (_pnl(direction, entry_market_price, tp1_fill_price) / initial_risk_price) + remaining * (_pnl(direction, entry_market_price, final_exit_market_price) / initial_risk_price)
                execution_r = config.tp1_fraction * (_pnl(direction, entry_execution_price, tp1_fill_price) / initial_risk_price) + remaining * (_pnl(direction, entry_execution_price, final_exit_execution_price) / initial_risk_price)
                slippage_r = gross_r - execution_r
                fee_price = entry_execution_price * config.fee_pct_per_side + tp1_fill_price * config.tp1_fraction * config.fee_pct_per_side + final_exit_execution_price * remaining * config.fee_pct_per_side
                fee_r = fee_price / initial_risk_price
                return {"status": "TRADE_COMPLETED", "reason": "TRAILING_STOP_HIT", "signal_timestamp": signal_timestamp, "signal_raw_index": signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": direction, "stop_price": stop_price, "stop_reference_price": stop_decision.reference_price, "proposed_entry_timestamp": entry_timestamp, "proposed_entry_raw_index": entry_raw_index, "proposed_entry_market_price": entry_market_price, "entry_timestamp": entry_timestamp, "entry_raw_index": entry_raw_index, "entry_market_price": entry_market_price, "entry_execution_price": entry_execution_price, "entry_price_before_cost": entry_market_price, "entry_price": entry_execution_price, "initial_risk_price": initial_risk_price, "tp1_price": tp1_price, "tp1_timestamp": tp1_timestamp, "tp1_fill_price": tp1_fill_price, "trailing_activation_price": trailing_activation_price, "exit_timestamp": ts, "exit_raw_index": int(raw_index_arr[idx]), "final_exit_market_price": final_exit_market_price, "final_exit_execution_price": final_exit_execution_price, "gross_R": gross_r, "slippage_R": slippage_r, "fee_R": fee_r, "net_R": gross_r - slippage_r - fee_r, "mfe_R": mfe_r if np.isfinite(mfe_r) else np.nan, "mae_R": mae_r, "position_open_at_ny_end": ts > pre_ny_context.ny_end_utc, "position_open_at_end_of_data": False}

    return {"status": "OPEN_AT_END_OF_DATA", "reason": "OPEN_AT_END_OF_DATA", "signal_timestamp": signal_timestamp, "signal_raw_index": signal_raw_index, "signal_price": entry_decision.signal_price, "signal_direction": direction, "stop_price": stop_price, "stop_reference_price": stop_decision.reference_price, "proposed_entry_timestamp": entry_timestamp, "proposed_entry_raw_index": entry_raw_index, "proposed_entry_market_price": entry_market_price, "entry_timestamp": entry_timestamp, "entry_raw_index": entry_raw_index, "entry_market_price": entry_market_price, "entry_execution_price": entry_execution_price, "entry_price_before_cost": entry_market_price, "entry_price": entry_execution_price, "initial_risk_price": initial_risk_price, "tp1_price": tp1_price, "tp1_timestamp": tp1_timestamp, "tp1_fill_price": tp1_fill_price, "trailing_activation_price": trailing_activation_price, "exit_timestamp": pd.NaT, "exit_raw_index": None, "final_exit_market_price": np.nan, "final_exit_execution_price": np.nan, "gross_R": np.nan, "slippage_R": np.nan, "fee_R": np.nan, "net_R": np.nan, "mfe_R": mfe_r if np.isfinite(mfe_r) else np.nan, "mae_R": mae_r, "position_open_at_ny_end": True, "position_open_at_end_of_data": True}