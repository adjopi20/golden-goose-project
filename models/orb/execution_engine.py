from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExecutionConfig:
    fee_bps: float = 4.0
    slippage_bps: float = 5.0
    tp1_r: float = 4.0
    tp1_fraction: float = 0.5
    runner_trail_tp1_fraction: float = 0.5


@dataclass
class ExecutionPosition:
    direction: str
    entry: float
    stop_loss: float
    initial_risk: float
    qty_total: float
    qty_open: float
    tp1_price: float
    tp1_fraction: float
    runner_trail_distance: float
    fee_paid: float
    tp1_hit: bool = False
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
    requested_entry: float,
    stop_loss: float,
    equity: float,
    risk_fraction: float,
    config: ExecutionConfig,
) -> tuple[ExecutionPosition | None, dict]:
    entry = execution_price(requested_entry, direction, "entry", config.slippage_bps)
    risk = entry - stop_loss if direction == "long" else stop_loss - entry
    if risk <= 0:
        return None, {"event": "paper_reject", "reason": "zero_or_invalid_risk"}
    qty = equity * risk_fraction / risk
    entry_fee = fee(entry, qty, config.fee_bps)
    tp1 = entry + config.tp1_r * risk if direction == "long" else entry - config.tp1_r * risk
    position = ExecutionPosition(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        initial_risk=risk,
        qty_total=qty,
        qty_open=qty,
        tp1_price=tp1,
        tp1_fraction=config.tp1_fraction,
        runner_trail_distance=abs(tp1 - entry) * config.runner_trail_tp1_fraction,
        fee_paid=entry_fee,
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
    if not position.tp1_hit:
        stopped = price <= position.stop_loss if position.direction == "long" else price >= position.stop_loss
        hit_tp1 = price >= position.tp1_price if position.direction == "long" else price <= position.tp1_price
        if stopped:
            return _exit_event(position, "initial_stop", position.stop_loss, position.qty_open, config)
        if hit_tp1:
            qty = position.qty_total * position.tp1_fraction
            event = _exit_event(position, "tp1", position.tp1_price, qty, config)
            position.qty_open -= qty
            position.tp1_hit = True
            position.best_price = price
            position.runner_stop = _runner_stop(position)
            event["position"] = asdict(position)
            return event
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


def _runner_stop(position: ExecutionPosition) -> float:
    assert position.best_price is not None
    if position.direction == "long":
        return float(position.best_price) - position.runner_trail_distance
    return float(position.best_price) + position.runner_trail_distance
