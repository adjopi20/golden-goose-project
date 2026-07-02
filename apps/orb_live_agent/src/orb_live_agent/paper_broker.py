from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import AgentConfig


@dataclass
class PaperPosition:
    direction: str
    entry: float
    stop_loss: float
    initial_risk: float
    take_profit: float | None
    qty: float
    fee_paid: float
    best_price: float


class PaperBroker:
    def __init__(self, config: AgentConfig) -> None:
        self.equity = float(config.paper_initial_equity)
        self.risk_fraction = float(config.paper_risk_fraction)
        self.fee_bps = float(config.paper_fee_bps)
        self.slippage_bps = float(config.paper_slippage_bps)
        self.trailing_enabled = bool(config.paper_trailing_enabled)
        self.trailing_r_multiple = float(config.paper_trailing_r_multiple)
        self.position: PaperPosition | None = None

    def has_open_position(self) -> bool:
        return self.position is not None

    def on_decision(self, decision: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any] | None:
        if decision.get("decision") != "TAKE" or not gate.get("accepted"):
            return None
        direction = str(decision["direction"])
        entry = self._execution_price(float(decision["entry"]), direction, "entry")
        stop = float(decision["stop_loss"])
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return {"event": "paper_reject", "reason": "zero_risk"}
        risk_usd = self.equity * self.risk_fraction
        qty = risk_usd / risk_per_unit
        entry_fee = self._fee(entry, qty)
        self.equity -= entry_fee
        self.position = PaperPosition(
            direction=direction,
            entry=entry,
            stop_loss=stop,
            initial_risk=risk_per_unit,
            take_profit=float(decision["take_profit"]) if decision.get("take_profit") is not None else None,
            qty=qty,
            fee_paid=entry_fee,
            best_price=entry,
        )
        return {
            "event": "paper_open",
            "entry_requested": float(decision["entry"]),
            "entry_fill": entry,
            "entry_fee": entry_fee,
            "equity": self.equity,
            "position": asdict(self.position),
        }

    def on_candle(self, candle: dict[str, Any]) -> dict[str, Any] | None:
        if self.position is None:
            return None
        pos = self.position
        high = float(candle["high"])
        low = float(candle["low"])
        exit_price = None
        reason = None
        if pos.direction == "long":
            if low <= pos.stop_loss:
                exit_price, reason = pos.stop_loss, "stop_loss"
            elif pos.take_profit is not None and high >= pos.take_profit:
                exit_price, reason = pos.take_profit, "take_profit"
            if exit_price is None:
                if self._update_trailing_stop(high):
                    return {"event": "paper_trail_update", "position": asdict(pos)}
            pnl = (self._execution_price(exit_price, pos.direction, "exit") - pos.entry) * pos.qty if exit_price is not None else 0.0
        else:
            if high >= pos.stop_loss:
                exit_price, reason = pos.stop_loss, "stop_loss"
            elif pos.take_profit is not None and low <= pos.take_profit:
                exit_price, reason = pos.take_profit, "take_profit"
            if exit_price is None:
                if self._update_trailing_stop(low):
                    return {"event": "paper_trail_update", "position": asdict(pos)}
            pnl = (pos.entry - self._execution_price(exit_price, pos.direction, "exit")) * pos.qty if exit_price is not None else 0.0
        if exit_price is None:
            return None
        exit_fill = self._execution_price(exit_price, pos.direction, "exit")
        exit_fee = self._fee(exit_fill, pos.qty)
        self.equity += pnl - exit_fee
        net_pnl = pnl - pos.fee_paid - exit_fee
        closed = asdict(pos)
        self.position = None
        return {
            "event": "paper_close",
            "reason": reason,
            "exit_requested": exit_price,
            "exit_fill": exit_fill,
            "exit_fee": exit_fee,
            "fees_total": pos.fee_paid + exit_fee,
            "pnl": net_pnl,
            "gross_pnl": pnl,
            "equity": self.equity,
            "position": closed,
        }

    def _execution_price(self, price: float, direction: str, phase: str) -> float:
        slip = self.slippage_bps / 10_000.0
        if direction == "long":
            return price * (1.0 + slip) if phase == "entry" else price * (1.0 - slip)
        return price * (1.0 - slip) if phase == "entry" else price * (1.0 + slip)

    def _fee(self, price: float, qty: float) -> float:
        return abs(price * qty) * self.fee_bps / 10_000.0

    def _update_trailing_stop(self, favorable_price: float) -> bool:
        pos = self.position
        if pos is None or not self.trailing_enabled or self.trailing_r_multiple <= 0:
            return False
        old_stop = pos.stop_loss
        distance = pos.initial_risk * self.trailing_r_multiple
        if pos.direction == "long":
            pos.best_price = max(pos.best_price, favorable_price)
            pos.stop_loss = max(pos.stop_loss, pos.best_price - distance)
        else:
            pos.best_price = min(pos.best_price, favorable_price)
            pos.stop_loss = min(pos.stop_loss, pos.best_price + distance)
        return pos.stop_loss != old_stop
