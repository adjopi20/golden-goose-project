#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from session.context import build_pre_ny_daily_contexts, build_pre_ny_contexts, build_session_windows, load_raw_aggtrades
from strategy.trend_following_ny.config import TrendFollowingNYConfig
from strategy.trend_following_ny.engine import simulate_ny_session
from strategy.trend_following_ny.entry_models.second_breakout import evaluate_second_breakout_entry
from strategy.trend_following_ny.entry_models.value_area_zone import evaluate_value_area_zone_entry
from strategy.trend_following_ny.stop_models.bubble_stop import decide_bubble_stop
from strategy.trend_following_ny.stop_models.inside_value_percentage import decide_inside_value_percentage_stop
from strategy.trend_following_ny.stop_models.nearby_high_low import decide_nearby_high_low_stop

TRADE_RESULTS_COLUMNS = [
    "trade_id", "status", "session_day_wib", "direction", "entry_model", "stop_model", "regime_mode", "regime_filter_complete",
    "signal_timestamp", "signal_raw_index", "signal_price", "entry_timestamp", "entry_raw_index", "entry_market_price", "entry_execution_price",
    "entry_price_before_cost", "entry_price", "stop_price", "stop_reference_price", "initial_risk_price", "initial_risk_pct", "tp1_price",
    "tp1_timestamp", "tp1_fill_price", "trailing_activation_price", "exit_timestamp", "exit_raw_index", "final_exit_market_price",
    "final_exit_execution_price", "exit_reason", "gross_R", "slippage_R", "fee_R", "net_R", "mfe_R", "mae_R",
    "confirmation_complete", "research_status", "position_open_at_ny_end", "position_open_at_end_of_data", "entry_rejected_due_to_active_position",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NY trend-following architecture-validation orchestrator.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--run-tag", required=True)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--asia-start", default="23:00")
    parser.add_argument("--asia-end", default="07:00")
    parser.add_argument("--europe-start", default="07:00")
    parser.add_argument("--europe-end", default="15:00")
    parser.add_argument("--us-start", default="15:00")
    parser.add_argument("--us-end", default="23:00")
    parser.add_argument("--buffer-pct", type=float, default=0.005)
    parser.add_argument("--context-direction-tolerance", type=float, default=0.10)
    parser.add_argument("--min-trades-per-session", type=int, default=10)
    parser.add_argument("--volume-profile-bins", type=int, default=50)
    parser.add_argument("--value-area-pct", type=float, default=0.70)
    parser.add_argument("--entry-model", default="VALUE_AREA_ZONE")
    parser.add_argument("--stop-model", default="INSIDE_VALUE_PERCENTAGE")
    parser.add_argument("--regime-mode", default="ALL_SESSIONS_DIAGNOSTIC")
    parser.add_argument("--eligible-asia-europe-context-combo", action="append", default=[])
    parser.add_argument("--entry-zone-pct", type=float, default=0.005)
    parser.add_argument("--inside-value-stop-pct", type=float, default=0.005)
    parser.add_argument("--fee-pct-per-side", type=float, default=0.0)
    parser.add_argument("--slippage-pct-per-side", type=float, default=0.0)
    parser.add_argument("--entry-latency-ms", type=int, default=0)
    parser.add_argument("--tp1-r", type=float, default=2.0)
    parser.add_argument("--tp1-fraction", type=float, default=0.50)
    parser.add_argument("--trailing-distance-r", type=float, default=1.0)
    parser.add_argument("--confirmation-mode", default="location_only_diagnostic")
    parser.add_argument("--max-trades-per-ny-session",type=int, default=1)
    return parser.parse_args()


def _entry_dispatch(name: str):
    return {"VALUE_AREA_ZONE": evaluate_value_area_zone_entry, "SECOND_BREAKOUT": evaluate_second_breakout_entry}[name]


def _stop_dispatch(name: str):
    return {"INSIDE_VALUE_PERCENTAGE": decide_inside_value_percentage_stop, "BUBBLE_STOP": decide_bubble_stop, "NEARBY_HIGH_LOW": decide_nearby_high_low_stop}[name]


def _regime_eligible(pre_ny_context, config: TrendFollowingNYConfig) -> tuple[bool, str | None]:
    if config.regime_mode == "ALL_SESSIONS_DIAGNOSTIC":
        return True, None
    combo = pre_ny_context.asia_europe_session_context_combo
    if combo in set(config.eligible_asia_europe_context_combos):
        return True, None
    return False, "CONTEXT_COMBO_NOT_ALLOWLISTED"


def _empty_trade_results() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_RESULTS_COLUMNS)


def _summary(trades_df: pd.DataFrame, sessions_df: pd.DataFrame) -> pd.DataFrame:
    completed = trades_df.loc[trades_df["status"].eq("TRADE_COMPLETED")].copy() if (not trades_df.empty and "status" in trades_df.columns) else pd.DataFrame()
    open_trades = trades_df.loc[trades_df["status"].eq("OPEN_AT_END_OF_DATA")].copy() if (not trades_df.empty and "status" in trades_df.columns) else pd.DataFrame()
    net_r = completed["net_R"] if not completed.empty else pd.Series(dtype="float64")
    gross_profit = float(net_r[net_r > 0].sum()) if not net_r.empty else 0.0
    gross_loss = float(-net_r[net_r < 0].sum()) if not net_r.empty else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan
    status_series = sessions_df["simulation_status"] if "simulation_status" in sessions_df.columns else pd.Series(dtype="object")
    stop_problem_statuses = {"NO_VALID_STOP", "STOP_OUTSIDE_VALUE_AREA", "STOP_CONFIGURATION_INVALID"}
    engine_rejection_statuses = {"NO_ELIGIBLE_ENTRY_TRADE_AFTER_SIGNAL", "INSUFFICIENT_DATA"}
    return pd.DataFrame([
        {
            "Total NY sessions evaluated": int(len(sessions_df)),
            "Eligible sessions": int(sessions_df.get("regime_eligible", pd.Series(dtype="bool")).fillna(False).sum()),
            "Ineligible regimes": int(status_series.eq("NO_ELIGIBLE_REGIME").sum()),
            "No entry signal": int(status_series.eq("NO_ENTRY_SIGNAL").sum()),
            "Entry candidates": int(sessions_df["entry_candidate"].fillna(False).sum()),
            "Invalid or unavailable stops": int(status_series.isin(stop_problem_statuses).sum()),
            "Blocked by active position": int(status_series.eq("ACTIVE_POSITION_EXISTS").sum()),
            "Engine rejections": int(status_series.isin(engine_rejection_statuses).sum()),
            "Trades created": int(len(trades_df)),
            "Completed trades": int(len(completed)),
            "Open trades": int(len(open_trades)),
            "Mean net R": float(net_r.mean()) if not net_r.empty else np.nan,
            "Median net R": float(net_r.median()) if not net_r.empty else np.nan,
            "Total net R": float(net_r.sum()) if not net_r.empty else np.nan,
            "Profit factor": profit_factor,
        }
    ])


def main() -> int:
    args = parse_args()
    config = TrendFollowingNYConfig(
        symbol=args.symbol,
        volume_profile_bins=args.volume_profile_bins,
        value_area_pct=args.value_area_pct,
        entry_model=args.entry_model,
        stop_model=args.stop_model,
        regime_mode=args.regime_mode,
        eligible_asia_europe_context_combos=args.eligible_asia_europe_context_combo,
        entry_zone_pct=args.entry_zone_pct,
        inside_value_stop_pct=args.inside_value_stop_pct,
        fee_pct_per_side=args.fee_pct_per_side,
        slippage_pct_per_side=args.slippage_pct_per_side,
        entry_latency_ms=args.entry_latency_ms,
        tp1_R=args.tp1_r,
        tp1_fraction=args.tp1_fraction,
        trailing_distance_R=args.trailing_distance_r,
        max_trades_per_ny_session=args.max_trades_per_ny_session,
        confirmation_mode=args.confirmation_mode,
    )
    run_dir = PROJECT_ROOT / "research" / "trend_following_ny" / args.run_tag
    run_dir.mkdir(parents=True, exist_ok=True)

    raw = load_raw_aggtrades(Path(args.input))
    windows = build_session_windows(args)
    pre_ny_daily = build_pre_ny_daily_contexts(raw, windows, args.min_trades_per_session, config.volume_profile_bins, config.value_area_pct, args.buffer_pct, args.context_direction_tolerance)
    if args.max_sessions is not None:
        pre_ny_daily = pre_ny_daily.head(args.max_sessions).copy()
    contexts = build_pre_ny_contexts(pre_ny_daily, config.volume_profile_bins, config.value_area_pct)

    timestamp_arr = raw["timestamp"].to_numpy()
    timestamp_ns_arr = raw["timestamp_ns"].to_numpy(dtype="int64")
    price_arr = raw["price"].to_numpy(dtype="float64")
    raw_index_arr = raw["raw_index"].to_numpy(dtype="int64")

    diagnostics_rows = []
    trade_rows = []
    blocked_until_timestamp = None
    entry_fn = _entry_dispatch(config.entry_model)
    stop_fn = _stop_dispatch(config.stop_model)

    for context in contexts:
        ny_start_idx = int(np.searchsorted(timestamp_ns_arr, pd.Timestamp(context.ny_start_utc).value, side="left"))
        ny_end_idx = int(np.searchsorted(timestamp_ns_arr, pd.Timestamp(context.ny_end_utc).value, side="left"))
        filtered_start_idx = ny_start_idx
        if blocked_until_timestamp is not None:
            filtered_start_idx = max(filtered_start_idx, int(np.searchsorted(timestamp_ns_arr, pd.Timestamp(blocked_until_timestamp).value, side="right")))

        regime_eligible, regime_reason = _regime_eligible(context, config)
        if filtered_start_idx >= ny_end_idx:
            diagnostics_rows.append({**context.to_dict(), "regime_mode": config.regime_mode, "regime_filter_complete": False, "regime_eligible": False, "simulation_status": "ACTIVE_POSITION_EXISTS", "simulation_reason": "ACTIVE_POSITION_EXISTS", "signal_timestamp": pd.NaT, "signal_raw_index": None, "signal_price": np.nan, "signal_direction": None, "stop_price": np.nan, "stop_reference_price": np.nan, "proposed_entry_timestamp": pd.NaT, "proposed_entry_raw_index": None, "proposed_entry_market_price": np.nan, "trade_created": False, "trade_id": None, "position_blocked_until": blocked_until_timestamp, "entry_rejected_due_to_active_position": True, "entry_candidate": False, "research_status": "ARCHITECTURE_VALIDATION_ONLY", "entry_model": config.entry_model, "stop_model": config.stop_model})
            continue

        if not regime_eligible:
            diagnostics_rows.append({**context.to_dict(), "regime_mode": config.regime_mode, "regime_filter_complete": False, "regime_eligible": False, "simulation_status": "NO_ELIGIBLE_REGIME", "simulation_reason": regime_reason, "signal_timestamp": pd.NaT, "signal_raw_index": None, "signal_price": np.nan, "signal_direction": None, "stop_price": np.nan, "stop_reference_price": np.nan, "proposed_entry_timestamp": pd.NaT, "proposed_entry_raw_index": None, "proposed_entry_market_price": np.nan, "trade_created": False, "trade_id": None, "position_blocked_until": blocked_until_timestamp, "entry_rejected_due_to_active_position": False, "entry_candidate": False, "research_status": "ARCHITECTURE_VALIDATION_ONLY", "entry_model": config.entry_model, "stop_model": config.stop_model})
            continue

        ny_trades = raw.iloc[filtered_start_idx:ny_end_idx]
        entry_decision = entry_fn(context, ny_trades, config)
        entry_candidate = entry_decision.status == "SIGNAL_CREATED"
        stop_decision = stop_fn(context, entry_decision.direction, config) if getattr(entry_decision, "direction", None) else stop_fn(context, None, config) if config.stop_model == "INSIDE_VALUE_PERCENTAGE" else None
        result = simulate_ny_session(context, entry_decision, stop_decision, config, price_arr, timestamp_arr, timestamp_ns_arr, raw_index_arr, filtered_start_idx, ny_end_idx)

        trade_created = result["status"] in {"TRADE_COMPLETED", "OPEN_AT_END_OF_DATA"}
        trade_id = f"{context.session_day_wib}_{len(trade_rows)+1}" if trade_created else None
        if trade_created:
            blocked_until_timestamp = pd.Timestamp.max.tz_localize("UTC") if result["status"] == "OPEN_AT_END_OF_DATA" else result.get("exit_timestamp")
            trade_rows.append({
                "trade_id": trade_id, "status": result["status"], "session_day_wib": context.session_day_wib, "direction": result.get("signal_direction"), "entry_model": config.entry_model, "stop_model": config.stop_model, "regime_mode": config.regime_mode, "regime_filter_complete": False,
                "signal_timestamp": result.get("signal_timestamp"), "signal_raw_index": result.get("signal_raw_index"), "signal_price": result.get("signal_price"), "entry_timestamp": result.get("entry_timestamp"), "entry_raw_index": result.get("entry_raw_index"), "entry_market_price": result.get("entry_market_price"), "entry_execution_price": result.get("entry_execution_price"),
                "entry_price_before_cost": result.get("entry_price_before_cost"), "entry_price": result.get("entry_price"), "stop_price": result.get("stop_price"), "stop_reference_price": result.get("stop_reference_price"), "initial_risk_price": result.get("initial_risk_price"), "initial_risk_pct": (result.get("initial_risk_price") / result.get("entry_price")) if result.get("entry_price") else np.nan,
                "tp1_price": result.get("tp1_price"), "tp1_timestamp": result.get("tp1_timestamp"), "tp1_fill_price": result.get("tp1_fill_price"), "trailing_activation_price": result.get("trailing_activation_price"), "exit_timestamp": result.get("exit_timestamp"), "exit_raw_index": result.get("exit_raw_index"), "final_exit_market_price": result.get("final_exit_market_price"),
                "final_exit_execution_price": result.get("final_exit_execution_price"), "exit_reason": result.get("reason"), "gross_R": result.get("gross_R"), "slippage_R": result.get("slippage_R"), "fee_R": result.get("fee_R"), "net_R": result.get("net_R"), "mfe_R": result.get("mfe_R"), "mae_R": result.get("mae_R"),
                "confirmation_complete": False, "research_status": "ARCHITECTURE_VALIDATION_ONLY", "position_open_at_ny_end": result.get("position_open_at_ny_end"), "position_open_at_end_of_data": result.get("position_open_at_end_of_data"), "entry_rejected_due_to_active_position": False,
            })

        diagnostics_rows.append({**context.to_dict(), "regime_mode": config.regime_mode, "regime_filter_complete": False, "regime_eligible": regime_eligible, "simulation_status": result.get("status"), "simulation_reason": result.get("reason"), "signal_timestamp": result.get("signal_timestamp", getattr(entry_decision, "signal_timestamp", pd.NaT)), "signal_raw_index": result.get("signal_raw_index", getattr(entry_decision, "signal_raw_index", None)), "signal_price": result.get("signal_price", getattr(entry_decision, "signal_price", np.nan)), "signal_direction": result.get("signal_direction", getattr(entry_decision, "direction", None)), "stop_price": result.get("stop_price", getattr(stop_decision, "stop_price", np.nan) if stop_decision else np.nan), "stop_reference_price": result.get("stop_reference_price", getattr(stop_decision, "reference_price", np.nan) if stop_decision else np.nan), "proposed_entry_timestamp": result.get("proposed_entry_timestamp", pd.NaT), "proposed_entry_raw_index": result.get("proposed_entry_raw_index"), "proposed_entry_market_price": result.get("proposed_entry_market_price", np.nan), "trade_created": trade_created, "trade_id": trade_id, "position_blocked_until": blocked_until_timestamp, "entry_rejected_due_to_active_position": result.get("status") == "ACTIVE_POSITION_EXISTS", "entry_candidate": entry_candidate, "research_status": "ARCHITECTURE_VALIDATION_ONLY", "entry_model": config.entry_model, "stop_model": config.stop_model})

    session_diagnostics = pd.DataFrame(diagnostics_rows)
    trade_results = pd.DataFrame(trade_rows, columns=TRADE_RESULTS_COLUMNS) if trade_rows else _empty_trade_results()
    summary = _summary(trade_results, session_diagnostics)

    (run_dir / "backtest_config.json").write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
    session_diagnostics.to_parquet(run_dir / "session_diagnostics.parquet", index=False)
    trade_results.to_parquet(run_dir / "trade_results.parquet", index=False)
    summary.to_csv(run_dir / "backtest_summary.csv", index=False)
    print(f"Wrote outputs to {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
