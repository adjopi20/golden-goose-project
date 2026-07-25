from __future__ import annotations

import argparse
import json
from collections import deque
from dataclasses import asdict, replace
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from models.orb.btc_opening_range_breakout import (
    BTCOpeningRangeBreakoutConfig,
    ema_runner_stop,
    opening_range_breakout_decision,
)
from scripts.backtest import write_shared_backtest_result

from .config import load_config
from .feature_cache import FeatureSet, build_or_load_feature_set, in_entry_window, parse_date, parse_time
from .paper_broker import PaperBroker
from .risk_gate import RiskGate
from .strategies.orb_trend_following import _stop_level, decide_algorithm


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _stamp(events: list[dict[str, Any]], timestamp_ms: int, tz: ZoneInfo) -> list[dict[str, Any]]:
    for event in events:
        event["timestamp_ms"] = int(timestamp_ms)
        event["time"] = datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    return events


def _session_limit_gate(
    gate_result: dict[str, Any],
    session_day: date,
    trades_per_session: dict[date, int],
    max_trades_per_session: int | None,
) -> dict[str, Any]:
    if (
        gate_result.get("accepted")
        and max_trades_per_session is not None
        and trades_per_session.get(session_day, 0) >= max_trades_per_session
    ):
        return {
            "accepted": False,
            "reason": "max_trades_per_session_reached",
            "max_trades_per_session": max_trades_per_session,
        }
    return gate_result


def _input_symbol(path: Path, fallback: str) -> str:
    marker = path.name.lower().find("-aggtrades")
    return path.name[:marker].upper() if marker > 0 else fallback


def _btc_runtime_config(config: Any, strategy: BTCOpeningRangeBreakoutConfig) -> Any:
    return replace(
        config,
        session_timezone="UTC",
        ny_open_time=strategy.session_start,
        orb_session_start_time=strategy.session_start,
        orb_entry_start_time="13:45",
        paper_risk_fraction=strategy.risk_fraction,
        paper_tp1_r=strategy.tp1_r,
        paper_tp1_fraction=strategy.tp1_fraction,
        paper_slippage_bps=float(config.paper_slippage_bps) + strategy.assumed_spread_bps / 2.0,
        paper_protection_enabled=False,
        paper_max_hold_exit_time=strategy.entry_end,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    strategy_name = getattr(args, "strategy", "legacy_orb")
    strategy_config = BTCOpeningRangeBreakoutConfig()
    input_path = Path(args.input)
    base_config = load_config()
    if strategy_name == "btc_opening_range_breakout":
        base_config = replace(base_config, symbol=_input_symbol(input_path, base_config.symbol))
    config = _btc_runtime_config(base_config, strategy_config) if strategy_name == "btc_opening_range_breakout" else base_config
    start_day = parse_date(args.start_date)
    end_day = parse_date(args.end_date)
    features = build_or_load_feature_set(
        input_path,
        start_day,
        end_day,
        config,
        Path(args.cache_dir) if args.cache_dir else None,
        bool(args.use_cache),
        bool(args.refresh_cache),
    )
    if strategy_name == "btc_opening_range_breakout":
        return run_btc_orb_with_features(
            base_config, features, start_day, end_day, Path(args.output_dir), strategy_config
        )
    return run_with_features(
        config,
        features,
        start_day,
        end_day,
        Path(args.output_dir),
        entry_mode=getattr(args, "entry_mode", "strategy"),
        failure_filter=bool(getattr(args, "failure_filter", False)),
        max_trades_per_session=getattr(args, "max_trades_per_session", None),
    )


def run_with_features(
    config: Any,
    features: FeatureSet,
    start_day: date,
    end_day: date,
    output_dir: Path,
    write_decisions: bool = True,
    entry_mode: str = "strategy",
    failure_filter: bool = False,
    max_trades_per_session: int | None = None,
) -> dict[str, Any]:
    if entry_mode not in {"strategy", "follow_candle", "persistent_acceptance", "first_breakout_retest"}:
        raise ValueError(f"Unsupported entry mode: {entry_mode}")
    if max_trades_per_session is not None and max_trades_per_session < 1:
        raise ValueError("max_trades_per_session must be at least 1")
    tz = ZoneInfo(config.session_timezone)
    output_dir.mkdir(parents=True, exist_ok=True)
    gate = RiskGate(config.paper_min_stop_risk_pct, config.paper_max_stop_risk_pct)
    broker = PaperBroker(config)
    orders: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    recent: deque[dict[str, Any]] = deque(maxlen=30)
    entry_start = parse_time(getattr(config, "orb_entry_start_time", "09:45"))
    follow_states: dict[date, dict[str, Any]] = {}
    pending_entries: dict[int, dict[str, Any]] = {}
    trades_per_session: dict[date, int] = {}

    for candle in features.candles:
        candle_ms = int(candle["timestamp_ms"])
        if broker.position and broker.position.max_hold_exit_ms is not None and candle_ms >= broker.position.max_hold_exit_ms:
            force_trade = features.force_exit_trades.get(int(broker.position.max_hold_exit_ms))
            if force_trade:
                orders.extend(_stamp(broker.on_trade(force_trade), int(force_trade["timestamp"]), tz))

        if entry_mode in {"follow_candle", "persistent_acceptance", "first_breakout_retest"} and candle_ms in pending_entries:
            entry_day = datetime.fromtimestamp(candle_ms / 1000.0, timezone.utc).astimezone(tz).date()
            decision = {**pending_entries.pop(candle_ms), "entry": float(candle["open"])}
            gate_result = (
                gate.validate(decision, broker.has_open_position())
                if _entry_open_is_outside(decision)
                else {"accepted": False, "reason": "entry_opened_inside_orb"}
            )
            gate_result = _session_limit_gate(
                gate_result, entry_day, trades_per_session, max_trades_per_session
            )
            if write_decisions:
                decisions.append({"decision": decision, "gate": gate_result})
            open_event = broker.on_decision(decision, gate_result)
            if open_event:
                orders.extend(_stamp([open_event], candle_ms, tz))
                if open_event.get("event") == "paper_open":
                    trades_per_session[entry_day] = trades_per_session.get(entry_day, 0) + 1

        orders.extend(_stamp(broker.on_candle(candle), candle_ms, tz))
        recent.append(candle)

        day, active_entry_window = in_entry_window(candle_ms, tz, config.orb_entry_window_minutes, entry_start)
        if day < start_day or day > end_day or not active_entry_window:
            continue
        context = features.contexts.get(day)
        if not context:
            continue

        if entry_mode in {"follow_candle", "persistent_acceptance", "first_breakout_retest"}:
            if entry_mode == "follow_candle":
                decision = _follow_candle_decision(
                    candle, day, context, follow_states, pending_entries, config, failure_filter, active_entry_window
                )
            elif entry_mode == "persistent_acceptance":
                decision = _persistent_acceptance_decision(candle, day, context, follow_states, pending_entries, config)
            else:
                decision = _first_breakout_retest_decision(candle, day, context, follow_states, pending_entries, config)
            if write_decisions and decision is not None:
                decisions.append({"decision": decision, "gate": {"accepted": False, "reason": decision["reason"]}})
            continue

        snapshot = {
            "symbol": config.symbol,
            "snapshot_timestamp_ms": candle_ms,
            "target_session_day": day.isoformat(),
            "session_timezone": config.session_timezone,
            "setup_observation_active": True,
            "last_candle": candle,
            "recent_candles": list(recent),
            "orb_entry_start_time": getattr(config, "orb_entry_start_time", "09:45"),
            "orb_entry_window_minutes": config.orb_entry_window_minutes,
            "orb_min_volume_expansion_ratio": config.orb_min_volume_expansion_ratio,
            "orb_min_supportive_bubble_qty_ratio": config.orb_min_supportive_bubble_qty_ratio,
            "orb_min_candidate_body_ratio": config.orb_min_candidate_body_ratio,
            "orb_short_max_close_position": config.orb_short_max_close_position,
            "orb_long_min_close_position": config.orb_long_min_close_position,
            "orb_require_directional_delta": config.orb_require_directional_delta,
            "orb_min_preentry_delta_ratio": config.orb_min_preentry_delta_ratio,
            "orb_preentry_delta_lookback_minutes": config.orb_preentry_delta_lookback_minutes,
            "orb_opposite_touch_policy": config.orb_opposite_touch_policy,
            "orb_direct_min_body_ratio": config.orb_direct_min_body_ratio,
            "orb_direct_short_max_close_position": config.orb_direct_short_max_close_position,
            "orb_direct_long_min_close_position": config.orb_direct_long_min_close_position,
            "orb_direct_min_range_ratio": config.orb_direct_min_range_ratio,
            "orb_direct_min_delta_ratio": config.orb_direct_min_delta_ratio,
            "orb_stop_model": config.orb_stop_model,
            **context,
        }
        trigger = features.triggers.get(candle_ms)
        if trigger is None:
            continue
        decision = decide_algorithm(snapshot, trigger)
        gate_result = gate.validate(decision, broker.has_open_position())
        gate_result = _session_limit_gate(gate_result, day, trades_per_session, max_trades_per_session)
        open_event = broker.on_decision(decision, gate_result, snapshot)
        if write_decisions:
            decisions.append({"decision": decision, "gate": gate_result, "trigger": trigger})
        if open_event:
            if open_event.get("event") == "paper_open":
                trades_per_session[day] = trades_per_session.get(day, 0) + 1
            open_event["timestamp_ms"] = candle_ms
            open_event["time"] = datetime.fromtimestamp(candle_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
            orders.append(open_event)

    trades = _trades_from_orders(orders)
    opened = sum(order.get("event") == "paper_open" for order in orders)
    summary = {
        "event": "fast_orb_backtest_finished",
        "entry_mode": entry_mode,
        "failure_filter": failure_filter,
        "max_trades_per_session": max_trades_per_session,
        "rows_loaded": features.rows_loaded,
        "candles": int(len(features.candles)),
        "trades_taken": len(trades),
        "positions_opened": opened,
        "unclosed_positions": opened - len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "losses": sum(1 for t in trades if t["pnl"] <= 0),
        "net_pnl": sum(float(t["pnl"]) for t in trades),
        "final_equity": broker.equity,
        "output_dir": str(output_dir),
    }
    return write_shared_backtest_result(
        output_dir, trades, config.paper_initial_equity, start_day, end_day, summary,
        orders=orders, decisions=decisions if write_decisions else None,
    )


def run_btc_orb_with_features(
    config: Any,
    features: FeatureSet,
    start_day: date,
    end_day: date,
    output_dir: Path,
    strategy_config: BTCOpeningRangeBreakoutConfig | None = None,
    write_decisions: bool = True,
) -> dict[str, Any]:
    strategy = strategy_config or BTCOpeningRangeBreakoutConfig()
    config = _btc_runtime_config(config, strategy)
    tz = ZoneInfo("UTC")
    broker = PaperBroker(config, long_only_spot=True)
    recent: deque[dict[str, Any]] = deque(maxlen=300)
    orders: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    current_day: date | None = None
    day_start_equity = broker.equity
    consecutive_losses = 0
    active_trade_start_equity: float | None = None
    trades_per_day: dict[date, int] = {}

    def record(events: list[dict[str, Any]], timestamp_ms: int) -> None:
        nonlocal consecutive_losses, active_trade_start_equity
        if not events:
            return
        orders.extend(_stamp(events, timestamp_ms, tz))
        if any(event.get("event") == "paper_close" for event in events):
            trade_pnl = broker.equity - (
                active_trade_start_equity if active_trade_start_equity is not None else broker.equity
            )
            consecutive_losses = consecutive_losses + 1 if trade_pnl < 0 else 0
            active_trade_start_equity = None

    for candle in features.candles:
        candle_ms = int(candle["timestamp_ms"])
        candle_dt = datetime.fromtimestamp(candle_ms / 1000.0, timezone.utc)
        day = candle_dt.date()
        if day != current_day:
            current_day = day
            day_start_equity = broker.equity
            consecutive_losses = 0

        if broker.position and broker.position.max_hold_exit_ms is not None and candle_ms >= broker.position.max_hold_exit_ms:
            record(broker.on_trade({"timestamp": candle_ms, "price": float(candle["open"])}), candle_ms)

        if broker.position and broker.position.trail_active:
            ema_stop = ema_runner_stop(list(recent), strategy)
            if ema_stop is not None:
                if float(candle["open"]) <= ema_stop:
                    record(broker.force_close(float(candle["open"]), "ema9_pullback"), candle_ms)
                elif broker.position is not None:
                    broker.position.runner_stop = max(float(broker.position.runner_stop), ema_stop)

        if pending and int(pending["entry_timestamp_ms"]) == candle_ms:
            decision = {**pending, "entry": float(candle["open"]), "snapshot_timestamp_ms": candle_ms}
            daily_loss_hit = broker.equity <= day_start_equity * (1.0 - strategy.max_daily_loss_fraction)
            if day < start_day or day > end_day:
                gate_result = {"accepted": False, "reason": "outside_backtest_dates"}
            elif candle_dt.time() >= datetime.strptime(strategy.entry_end, "%H:%M").time():
                gate_result = {"accepted": False, "reason": "hard_flat_time_reached"}
            elif broker.has_open_position():
                gate_result = {"accepted": False, "reason": "position_already_open"}
            elif daily_loss_hit:
                gate_result = {"accepted": False, "reason": "max_daily_loss_reached"}
            elif consecutive_losses >= strategy.max_consecutive_losses:
                gate_result = {"accepted": False, "reason": "three_consecutive_losses"}
            elif float(decision["entry"]) <= float(decision["stop_loss"]):
                gate_result = {"accepted": False, "reason": "entry_gapped_through_stop"}
            else:
                gate_result = {"accepted": True, "reason": "accepted"}
            gate_result = _session_limit_gate(gate_result, day, trades_per_day, 1)
            if write_decisions:
                decisions.append({"decision": decision, "gate": gate_result})
            before_open = broker.equity
            open_event = broker.on_decision(decision, gate_result)
            if open_event:
                record([open_event], candle_ms)
                if open_event.get("event") == "paper_open":
                    active_trade_start_equity = before_open
                    trades_per_day[day] = trades_per_day.get(day, 0) + 1
            pending = None

        record(broker.on_candle(candle), candle_ms)
        recent.append(candle)

        if (
            day < start_day
            or day > end_day
            or broker.has_open_position()
            or pending
            or trades_per_day.get(day, 0) >= 1
        ):
            continue
        decision = opening_range_breakout_decision(list(recent), strategy)
        if write_decisions and decision.get("decision") == "TAKE":
            decisions.append({"decision": decision, "gate": {"accepted": False, "reason": "next_bar_open_pending"}})
        if decision.get("decision") == "TAKE":
            pending = {
                **decision,
                "entry_timestamp_ms": candle_ms + 60_000,
                "snapshot_timestamp_ms": candle_ms + 60_000,
            }

    if broker.position and features.candles:
        last = features.candles[-1]
        record(
            broker.force_close(float(last["close"]), "end_of_data"),
            int(last["timestamp_ms"]) + 60_000,
        )

    trades = _trades_from_orders(orders)
    opened = sum(order.get("event") == "paper_open" for order in orders)
    summary = {
        "event": "fast_orb_backtest_finished",
        "strategy": "btc_opening_range_breakout",
        "market": f"{config.symbol} spot",
        "direction": "long_only",
        "spread_model": "fixed_assumption",
        "assumed_spread_bps": strategy.assumed_spread_bps,
        "strategy_config": asdict(strategy),
        "rows_loaded": features.rows_loaded,
        "candles": len(features.candles),
        "trades_taken": len(trades),
        "positions_opened": opened,
        "unclosed_positions": opened - len(trades),
        "wins": sum(float(trade["pnl"]) > 0 for trade in trades),
        "losses": sum(float(trade["pnl"]) <= 0 for trade in trades),
        "net_pnl": sum(float(trade["pnl"]) for trade in trades),
        "final_equity": broker.equity,
        "output_dir": str(output_dir),
    }
    return write_shared_backtest_result(
        output_dir, trades, config.paper_initial_equity, start_day, end_day, summary,
        orders=orders, decisions=decisions if write_decisions else None,
    )


def _follow_candle_decision(
    candle: dict[str, Any],
    day: date,
    context: dict[str, Any],
    states: dict[date, dict[str, Any]],
    pending_entries: dict[int, dict[str, Any]],
    config: Any,
    failure_filter: bool,
    active_entry_window: bool,
) -> dict[str, Any] | None:
    candle_ms = int(candle["timestamp_ms"])
    state = states.get(day)
    if state is not None:
        if state.get("done") or candle_ms != int(state["breakout_timestamp_ms"]) + 60_000:
            return None
        direction = str(state["direction"])
        sign = 1.0 if direction == "long" else -1.0
        conditions = {
            "adverse_or_flat_body": sign * (float(candle["close"]) - float(candle["open"])) <= 0,
            "close_inside_orb": sign * (float(candle["close"]) - float(state["breakout_level"])) <= 0,
            "adverse_or_flat_delta": sign * float(candle.get("delta", 0.0)) <= 0,
        }
        state["done"] = True
        invalidated = float(candle["low"]) <= float(state["stop_loss"]) if direction == "long" else float(candle["high"]) >= float(state["stop_loss"])
        rejected = failure_filter and all(conditions.values())
        reason = "invalidated_before_entry" if invalidated else "three_condition_failure" if rejected else "follow_candle_accepted"
        decision = {
            "decision": "WAIT" if invalidated or rejected else "TAKE",
            "entry_model": "trend",
            "strategy": "orb_follow_candle",
            "direction": direction,
            "stop_loss": float(state["stop_loss"]),
            "reason": reason,
            "snapshot_timestamp_ms": candle_ms,
            "breakout_timestamp_ms": int(state["breakout_timestamp_ms"]),
            "breakout_level": float(state["breakout_level"]),
            "follow_candle": candle,
            "failure_conditions": conditions,
        }
        entry_ms = candle_ms + 60_000
        if decision["decision"] == "TAKE":
            pending_entries[entry_ms] = {**decision, "snapshot_timestamp_ms": entry_ms}
            decision = {**decision, "decision": "WAIT", "reason": "entry_scheduled_next_candle_open"}
        return decision

    if not active_entry_window:
        return None
    profile = context.get("ny_first_15m_profile") or {}
    orb_low = profile.get("session_low")
    orb_high = profile.get("session_high")
    if orb_low is None or orb_high is None:
        return None
    long_breakout = float(candle["high"]) > float(orb_high)
    short_breakout = float(candle["low"]) < float(orb_low)
    if not long_breakout and not short_breakout:
        return None
    if long_breakout and short_breakout:
        states[day] = {"done": True}
        return {
            "decision": "WAIT",
            "strategy": "orb_follow_candle",
            "reason": "ambiguous_two_sided_breakout_candle",
            "snapshot_timestamp_ms": candle_ms,
        }

    direction = "long" if long_breakout else "short"
    stop = _stop_level({**context, "orb_stop_model": config.orb_stop_model}, direction, float(orb_low), float(orb_high))
    if stop is None:
        states[day] = {"done": True}
        return {
            "decision": "WAIT",
            "strategy": "orb_follow_candle",
            "reason": "missing_or_invalid_orb_stop_level",
            "snapshot_timestamp_ms": candle_ms,
        }
    states[day] = {
        "direction": direction,
        "breakout_timestamp_ms": candle_ms,
        "breakout_level": float(orb_high if direction == "long" else orb_low),
        "stop_loss": float(stop[0]),
        "done": False,
    }
    return {
        "decision": "WAIT",
        "strategy": "orb_follow_candle",
        "direction": direction,
        "reason": "waiting_for_completed_follow_candle",
        "snapshot_timestamp_ms": candle_ms,
        "breakout_level": states[day]["breakout_level"],
    }


def _entry_open_is_outside(decision: dict[str, Any]) -> bool:
    level = decision.get("required_outside_level")
    if level is None:
        return True
    return float(decision["entry"]) > float(level) if decision["direction"] == "long" else float(decision["entry"]) < float(level)


def _first_breakout_retest_decision(
    candle: dict[str, Any],
    day: date,
    context: dict[str, Any],
    states: dict[date, dict[str, Any]],
    pending_entries: dict[int, dict[str, Any]],
    config: Any,
) -> dict[str, Any] | None:
    candle_ms = int(candle["timestamp_ms"])
    state = states.get(day)
    if state and state.get("done"):
        return None

    profile = context.get("ny_first_15m_profile") or {}
    orb_low = profile.get("session_low")
    orb_high = profile.get("session_high")
    if orb_low is None or orb_high is None:
        return None
    orb_low, orb_high = float(orb_low), float(orb_high)
    high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])

    if state is None:
        long_breakout, short_breakout = high > orb_high, low < orb_low
        if not long_breakout and not short_breakout:
            return None
        if long_breakout and short_breakout:
            states[day] = {"done": True}
            return {
                "decision": "WAIT",
                "strategy": "orb_first_breakout_retest",
                "reason": "ambiguous_two_sided_first_breakout_cancel_day",
                "snapshot_timestamp_ms": candle_ms,
            }

        direction = "long" if long_breakout else "short"
        breakout_level = orb_high if direction == "long" else orb_low
        accepted = close > breakout_level if direction == "long" else close < breakout_level
        if not accepted:
            states[day] = {"done": True}
            return {
                "decision": "WAIT",
                "strategy": "orb_first_breakout_retest",
                "direction": direction,
                "reason": "first_breakout_closed_inside_cancel_day",
                "snapshot_timestamp_ms": candle_ms,
                "breakout_level": breakout_level,
            }

        stop = _stop_level({**context, "orb_stop_model": config.orb_stop_model}, direction, orb_low, orb_high)
        if stop is None:
            states[day] = {"done": True}
            return {
                "decision": "WAIT",
                "strategy": "orb_first_breakout_retest",
                "reason": "missing_or_invalid_orb_stop_level",
                "snapshot_timestamp_ms": candle_ms,
            }
        states[day] = {
            "direction": direction,
            "breakout_level": breakout_level,
            "stop_loss": float(stop[0]),
            "breakout_timestamp_ms": candle_ms,
            "done": False,
        }
        return {
            "decision": "WAIT",
            "entry_model": "trend",
            "strategy": "orb_first_breakout_retest",
            "direction": direction,
            "reason": "first_breakout_accepted_waiting_for_retest",
            "snapshot_timestamp_ms": candle_ms,
            "breakout_level": breakout_level,
        }

    direction = str(state["direction"])
    breakout_level = float(state["breakout_level"])
    opposite_touched = low <= orb_low if direction == "long" else high >= orb_high
    if opposite_touched:
        state["done"] = True
        return {
            "decision": "WAIT",
            "strategy": "orb_first_breakout_retest",
            "direction": direction,
            "reason": "opposite_orb_extreme_touched_cancel_day",
            "snapshot_timestamp_ms": candle_ms,
            "breakout_level": breakout_level,
        }

    closes_inside = close <= breakout_level if direction == "long" else close >= breakout_level
    if closes_inside:
        state["done"] = True
        return {
            "decision": "WAIT",
            "strategy": "orb_first_breakout_retest",
            "direction": direction,
            "reason": "closed_inside_orb_before_entry_cancel_day",
            "snapshot_timestamp_ms": candle_ms,
            "breakout_level": breakout_level,
        }

    retest_touched = low <= breakout_level if direction == "long" else high >= breakout_level
    if not retest_touched:
        return {
            "decision": "WAIT",
            "strategy": "orb_first_breakout_retest",
            "direction": direction,
            "reason": "waiting_for_retest",
            "snapshot_timestamp_ms": candle_ms,
            "breakout_level": breakout_level,
        }

    entry_ms = candle_ms + 60_000
    pending_entries[entry_ms] = {
        "decision": "TAKE",
        "entry_model": "trend",
        "strategy": "orb_first_breakout_retest",
        "direction": direction,
        "stop_loss": float(state["stop_loss"]),
        "reason": "retest_held_entry",
        "snapshot_timestamp_ms": entry_ms,
        "breakout_timestamp_ms": int(state["breakout_timestamp_ms"]),
        "breakout_level": breakout_level,
        "required_outside_level": breakout_level,
    }
    state["done"] = True
    return {
        "decision": "WAIT",
        "entry_model": "trend",
        "strategy": "orb_first_breakout_retest",
        "direction": direction,
        "reason": "retest_held_entry_scheduled_next_candle_open",
        "snapshot_timestamp_ms": candle_ms,
        "breakout_level": breakout_level,
    }
def _persistent_acceptance_decision(
    candle: dict[str, Any],
    day: date,
    context: dict[str, Any],
    states: dict[date, dict[str, Any]],
    pending_entries: dict[int, dict[str, Any]],
    config: Any,
) -> dict[str, Any] | None:
    candle_ms = int(candle["timestamp_ms"])
    state = states.get(day)
    if state and state.get("done"):
        return None

    profile = context.get("ny_first_15m_profile") or {}
    orb_low = profile.get("session_low")
    orb_high = profile.get("session_high")
    if orb_low is None or orb_high is None:
        return None
    orb_low, orb_high = float(orb_low), float(orb_high)
    close = float(candle["close"])
    close_direction = "long" if close > orb_high else "short" if close < orb_low else None

    if state is None:
        long_breakout = float(candle["high"]) > orb_high
        short_breakout = float(candle["low"]) < orb_low
        if not long_breakout and not short_breakout:
            return None
        bias = close_direction or ("long" if long_breakout and not short_breakout else "short" if short_breakout and not long_breakout else None)
        state = states[day] = {
            "bias": bias,
            "outside_closes": 1 if close_direction else 0,
            "breakout_timestamp_ms": candle_ms,
            "done": False,
        }
        reason = "breakout_close_accepted" if close_direction else "breakout_closed_inside_wait"
    elif close_direction is None:
        state["outside_closes"] = 0
        reason = "closed_inside_orb_wait"
    elif close_direction != state.get("bias"):
        state["bias"] = close_direction
        state["outside_closes"] = 1
        reason = "opposite_direction_close_bias_flip"
    else:
        state["outside_closes"] = int(state["outside_closes"]) + 1
        reason = "same_direction_rebreak_accepted" if state["outside_closes"] == 1 else "persistent_acceptance_confirmed"

    direction = state.get("bias")
    decision = {
        "decision": "WAIT",
        "entry_model": "trend",
        "strategy": "orb_persistent_acceptance",
        "direction": direction,
        "reason": reason,
        "snapshot_timestamp_ms": candle_ms,
        "breakout_timestamp_ms": int(state["breakout_timestamp_ms"]),
        "acceptance_closes": int(state["outside_closes"]),
    }
    if direction is None or state["outside_closes"] < 2:
        return decision

    stop = _stop_level({**context, "orb_stop_model": config.orb_stop_model}, direction, orb_low, orb_high)
    if stop is None:
        state["done"] = True
        return {**decision, "reason": "missing_or_invalid_orb_stop_level"}

    entry_ms = candle_ms + 60_000
    pending_entries[entry_ms] = {
        **decision,
        "decision": "TAKE",
        "stop_loss": float(stop[0]),
        "breakout_level": orb_high if direction == "long" else orb_low,
        "snapshot_timestamp_ms": entry_ms,
    }
    state["done"] = True
    return {**decision, "reason": "entry_scheduled_next_candle_open"}


def _self_check_persistent_acceptance() -> None:
    day = date(2024, 1, 1)
    context = {"ny_first_15m_profile": {"session_low": 99.0, "session_high": 101.0}}
    states: dict[date, dict[str, Any]] = {}
    pending: dict[int, dict[str, Any]] = {}
    config = type("Config", (), {"orb_stop_model": "opposite_extreme"})()
    closes = [(102.0, 100.0, 100.5), (101.5, 100.4, 101.4), (101.5, 100.5, 100.7), (100.8, 98.5, 98.8), (98.9, 98.5, 98.6)]
    reasons = []
    for minute, (high, low, close) in enumerate(closes):
        decision = _persistent_acceptance_decision(
            {"timestamp_ms": minute * 60_000, "open": close, "high": high, "low": low, "close": close},
            day,
            context,
            states,
            pending,
            config,
        )
        reasons.append(decision["reason"] if decision else None)
    assert reasons == [
        "breakout_closed_inside_wait",
        "same_direction_rebreak_accepted",
        "closed_inside_orb_wait",
        "opposite_direction_close_bias_flip",
        "entry_scheduled_next_candle_open",
    ]
    assert pending[5 * 60_000]["direction"] == "short"

    states, pending = {}, {}
    for minute, (high, low, close) in enumerate([(102.0, 100.5, 101.4), (101.6, 101.1, 101.3), (101.4, 100.9, 101.2)]):
        _first_breakout_retest_decision(
            {"timestamp_ms": minute * 60_000, "open": close, "high": high, "low": low, "close": close},
            day,
            context,
            states,
            pending,
            config,
        )
    assert pending[3 * 60_000]["direction"] == "long"
    assert _entry_open_is_outside({**pending[3 * 60_000], "entry": 101.1})
    assert not _entry_open_is_outside({**pending[3 * 60_000], "entry": 100.9})

    gate = {"accepted": True, "reason": "accepted"}
    assert _session_limit_gate(gate, day, {}, 1) is gate
    blocked = _session_limit_gate(gate, day, {day: 1}, 1)
    assert blocked["reason"] == "max_trades_per_session_reached"


def _trades_from_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for order in orders:
        if order.get("event") == "paper_open":
            pos = order["position"]
            qty = float(pos["qty_total"])
            entry_requested = float(order["entry_requested"])
            entry_fill = float(order["entry_fill"])
            current = {
                "direction": pos["direction"],
                "entry_time": order.get("time"),
                "entry_timestamp_ms": order.get("timestamp_ms"),
                "entry_requested": entry_requested,
                "entry": entry_fill,
                "stop_loss": pos["stop_loss"],
                "entry_fee": order["entry_fee"],
                "exit_fees": 0.0,
                "fees": float(order["entry_fee"]),
                "slippage": abs(entry_fill - entry_requested) * qty,
                "gross_pnl_before_costs": 0.0,
                "gross_pnl_after_slippage": 0.0,
                "risk_dollars": float(pos["initial_risk"]) * qty,
                "pnl": -float(order["entry_fee"]),
                "close_reason": None,
            }
        elif current and order.get("event") in {"paper_tp1", "paper_close"}:
            qty = float(order.get("qty_closed", 0.0))
            requested = float(order.get("exit_requested", order.get("exit_fill", 0.0)))
            fill = float(order.get("exit_fill", requested))
            if current["direction"] == "long":
                current["gross_pnl_before_costs"] += (requested - float(current["entry_requested"])) * qty
            else:
                current["gross_pnl_before_costs"] += (float(current["entry_requested"]) - requested) * qty
            current["gross_pnl_after_slippage"] += float(order.get("gross_pnl", 0.0))
            current["exit_fees"] += float(order.get("exit_fee", 0.0))
            current["fees"] += float(order.get("exit_fee", 0.0))
            current["slippage"] += abs(requested - fill) * qty
            current["pnl"] += float(order.get("pnl", 0.0))
            if order.get("event") == "paper_close":
                current["exit_time"] = order.get("time")
                current["exit_timestamp_ms"] = order.get("timestamp_ms")
                current["exit"] = order.get("exit_fill")
                current["close_reason"] = order.get("reason")
                current["close_equity"] = order.get("equity")
                current["r"] = current["pnl"] / current["risk_dollars"] if current["risk_dollars"] else None
                current["gross_r"] = (
                    current["gross_pnl_before_costs"] / current["risk_dollars"]
                    if current["risk_dollars"] else None
                )
                trades.append(current)
                current = None
    return trades


def main() -> None:
    _self_check_persistent_acceptance()
    parser = argparse.ArgumentParser(description="Fast no-lookahead ORB strategy backtest.")
    parser.add_argument(
        "--strategy",
        choices=("btc_opening_range_breakout", "legacy_orb"),
        default="btc_opening_range_breakout",
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument(
        "--entry-mode",
        choices=("strategy", "follow_candle", "persistent_acceptance", "first_breakout_retest"),
        default="strategy",
    )
    parser.add_argument("--failure-filter", action="store_true")
    parser.add_argument("--max-trades-per-session", type=int)
    print(json.dumps(run(parser.parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
