from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExecutionConfig:
    fee_bps: float = 4.0
    slippage_bps: float = 5.0
    tp1_r: float = 4.0
    tp1_fraction: float = 0.5
    runner_trail_tp1_fraction: float = 0.5
    exit_mode: str = "tp1_trail"
    trail_activation_r: float = 4.0
    trail_distance_r: float = 2.0
    protection_enabled: bool = True
    protection_activation_r: float = 1.0
    protection_stop_r: float = 0.0
    protection_fraction: float = 0.0


@dataclass
class ExecutionPosition:
    entry_model: str
    direction: str
    entry: float
    stop_loss: float
    initial_risk: float
    qty_total: float
    qty_open: float
    tp1_price: float
    tp2_price: float | None
    tp1_fraction: float
    exit_mode: str
    trail_activation_price: float
    runner_trail_distance: float
    fee_paid: float
    max_hold_exit_ms: int | None = None
    protection_hit: bool = False
    protection_scaled_out: bool = False
    tp1_hit: bool = False
    trail_active: bool = False
    best_price: float | None = None
    runner_stop: float | None = None


def execution_price(price: float, direction: str, phase: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000.0
    if direction == "long":
        return price * (1.0 + slip) if phase == "entry" else price * (1.0 - slip)
    return price * (1.0 - slip) if phase == "entry" else price * (1.0 + slip)


def fee(price: float, qty: float, fee_bps: float) -> float:
    return abs(price * qty) * fee_bps / 10_000.0


def open_position(
    *,
    direction: str,
    entry_model: str = "trend",
    requested_entry: float,
    stop_loss: float,
    equity: float,
    risk_fraction: float,
    config: ExecutionConfig,
    max_hold_exit_ms: int | None = None,
    tp1_price: float | None = None,
    tp2_price: float | None = None,
) -> tuple[ExecutionPosition | None, dict]:
    if config.exit_mode not in {"tp1_trail", "trail_only"}:
        return None, {"event": "paper_reject", "reason": "invalid_exit_mode"}
    if not 0.0 < config.tp1_fraction <= 1.0:
        return None, {"event": "paper_reject", "reason": "invalid_tp1_fraction"}
    if config.trail_activation_r <= 0 or config.trail_distance_r <= 0:
        return None, {"event": "paper_reject", "reason": "invalid_trailing_config"}
    if config.protection_enabled and not 0 <= config.protection_stop_r < config.protection_activation_r:
        return None, {"event": "paper_reject", "reason": "invalid_protection_config"}
    if not 0.0 <= config.protection_fraction < 1.0:
        return None, {"event": "paper_reject", "reason": "invalid_protection_fraction"}
    entry = execution_price(requested_entry, direction, "entry", config.slippage_bps)
    risk = entry - stop_loss if direction == "long" else stop_loss - entry
    if risk <= 0:
        return None, {"event": "paper_reject", "reason": "zero_or_invalid_risk"}
    qty = equity * risk_fraction / risk
    entry_fee = fee(entry, qty, config.fee_bps)
    tp1 = tp1_price if tp1_price is not None else entry + config.tp1_r * risk if direction == "long" else entry - config.tp1_r * risk
    trail_activation = entry + config.trail_activation_r * risk if direction == "long" else entry - config.trail_activation_r * risk
    if direction == "long" and (tp1 <= entry or (tp2_price is not None and tp2_price <= tp1)):
        return None, {"event": "paper_reject", "reason": "invalid_long_targets"}
    if direction == "short" and (tp1 >= entry or (tp2_price is not None and tp2_price >= tp1)):
        return None, {"event": "paper_reject", "reason": "invalid_short_targets"}
    position = ExecutionPosition(
        entry_model=entry_model,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        initial_risk=risk,
        qty_total=qty,
        qty_open=qty,
        tp1_price=float(tp1),
        tp2_price=tp2_price,
        tp1_fraction=config.tp1_fraction,
        exit_mode="tp1_trail" if entry_model != "trend" else config.exit_mode,
        trail_activation_price=float(trail_activation),
        runner_trail_distance=config.trail_distance_r * risk,
        fee_paid=entry_fee,
        max_hold_exit_ms=max_hold_exit_ms,
    )
    return position, {
        "event": "paper_open",
        "entry_requested": requested_entry,
        "entry_fill": entry,
        "entry_fee": entry_fee,
        "position": asdict(position),
    }


def _gross_pnl(position: ExecutionPosition, exit_fill: float, qty: float) -> float:
    if position.direction == "long":
        return (exit_fill - position.entry) * qty
    return (position.entry - exit_fill) * qty


def _exit_event(position: ExecutionPosition, reason: str, requested_exit: float, qty: float, config: ExecutionConfig) -> dict:
    fill = execution_price(requested_exit, position.direction, "exit", config.slippage_bps)
    exit_fee = fee(fill, qty, config.fee_bps)
    gross = _gross_pnl(position, fill, qty)
    return {
        "event": "paper_close" if qty >= position.qty_open else "paper_tp1",
        "reason": reason,
        "exit_requested": requested_exit,
        "exit_fill": fill,
        "exit_fee": exit_fee,
        "pnl": gross - exit_fee,
        "gross_pnl": gross,
        "qty_closed": qty,
    }


def on_price(position: ExecutionPosition, price: float, config: ExecutionConfig) -> dict | None:
    if not position.trail_active:
        stopped = price <= position.stop_loss if position.direction == "long" else price >= position.stop_loss
        hit_tp1 = (
            position.exit_mode == "tp1_trail"
            and (price >= position.tp1_price if position.direction == "long" else price <= position.tp1_price)
        )
        hit_protection = (
            config.protection_enabled
            and position.entry_model == "trend"
            and (price >= _protection_price(position, config) if position.direction == "long" else price <= _protection_price(position, config))
        )
        if stopped:
            reason = "protected_stop" if position.protection_hit and not position.protection_scaled_out else "initial_stop"
            return _exit_event(position, reason, position.stop_loss, position.qty_open, config)
        if hit_protection and not position.protection_hit:
            position.protection_hit = True
            if config.protection_fraction > 0:
                qty = min(position.qty_open, position.qty_total * config.protection_fraction)
                event = _exit_event(position, "protection_scale_out", price, qty, config)
                if event["event"] == "paper_close":
                    return event
                position.qty_open -= qty
                position.protection_scaled_out = True
                event["position"] = asdict(position)
                return event
            position.stop_loss = _protection_stop(position, config)
            if hit_tp1:
                qty = position.qty_total * position.tp1_fraction
                event = _exit_event(position, "tp1", position.tp1_price, qty, config)
                event["protection_applied"] = True
                if event["event"] == "paper_close":
                    return event
                position.qty_open -= qty
                position.tp1_hit = True
                position.trail_active = True
                position.best_price = price
                position.runner_stop = _bounded_runner_stop(position)
                event["position"] = asdict(position)
                return event
            reason = (
                "one_r_entry_protection"
                if config.protection_activation_r == 1.0 and config.protection_stop_r == 0.0
                else "configured_entry_protection"
            )
            return {"event": "paper_protection_update", "reason": reason, "position": asdict(position)}
        if position.exit_mode == "trail_only":
            hit_activation = price >= position.trail_activation_price if position.direction == "long" else price <= position.trail_activation_price
            if hit_activation:
                _activate_trailing(position, price)
                return {"event": "paper_trail_activation", "position": asdict(position)}
            return None
        if hit_tp1:
            qty = position.qty_total * position.tp1_fraction
            event = _exit_event(position, "tp1", position.tp1_price, qty, config)
            if event["event"] == "paper_close":
                return event
            position.qty_open -= qty
            position.tp1_hit = True
            position.trail_active = True
            position.best_price = price
            position.runner_stop = position.entry if position.tp2_price is not None else _bounded_runner_stop(position)
            event["position"] = asdict(position)
            return event
        return None

    if position.tp2_price is not None:
        hit_tp2 = price >= position.tp2_price if position.direction == "long" else price <= position.tp2_price
        stopped = price <= float(position.runner_stop) if position.direction == "long" else price >= float(position.runner_stop)
        if hit_tp2:
            return _exit_event(position, "tp2", position.tp2_price, position.qty_open, config)
        if stopped:
            return _exit_event(position, "protected_stop", float(position.runner_stop), position.qty_open, config)
        return None

    old_stop = position.runner_stop
    if position.direction == "long":
        position.best_price = max(float(position.best_price), price)
        position.runner_stop = max(float(position.runner_stop), _runner_stop(position))
        if price <= float(position.runner_stop):
            return _exit_event(position, "runner_trailing_stop", float(position.runner_stop), position.qty_open, config)
    else:
        position.best_price = min(float(position.best_price), price)
        position.runner_stop = min(float(position.runner_stop), _runner_stop(position))
        if price >= float(position.runner_stop):
            return _exit_event(position, "runner_trailing_stop", float(position.runner_stop), position.qty_open, config)

    if position.runner_stop != old_stop:
        return {"event": "paper_trail_update", "position": asdict(position)}
    return None


def force_exit(position: ExecutionPosition, price: float, config: ExecutionConfig, reason: str) -> dict:
    return _exit_event(position, reason, price, position.qty_open, config)


def _runner_stop(position: ExecutionPosition) -> float:
    assert position.best_price is not None
    if position.direction == "long":
        return float(position.best_price) - position.runner_trail_distance
    return float(position.best_price) + position.runner_trail_distance


def _bounded_runner_stop(position: ExecutionPosition) -> float:
    stop = _runner_stop(position)
    return max(position.stop_loss, stop) if position.direction == "long" else min(position.stop_loss, stop)


def _activate_trailing(position: ExecutionPosition, price: float) -> None:
    position.trail_active = True
    position.best_price = price
    position.runner_stop = _bounded_runner_stop(position)


def _protection_price(position: ExecutionPosition, config: ExecutionConfig) -> float:
    if position.direction == "long":
        return position.entry + config.protection_activation_r * position.initial_risk
    return position.entry - config.protection_activation_r * position.initial_risk


def _protection_stop(position: ExecutionPosition, config: ExecutionConfig) -> float:
    if position.direction == "long":
        return position.entry + config.protection_stop_r * position.initial_risk
    return position.entry - config.protection_stop_r * position.initial_risk
