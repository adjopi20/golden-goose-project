from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import AgentConfig


@dataclass
class PaperPosition:
    direction: str
    entry: float
    stop_loss: float
    take_profit: float | None
    qty: float


class PaperBroker:
    def __init__(self, config: AgentConfig) -> None:
        self.equity = float(config.paper_initial_equity)
        self.risk_fraction = float(config.paper_risk_fraction)
        self.position: PaperPosition | None = None

    def has_open_position(self) -> bool:
        return self.position is not None

    def on_decision(self, decision: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any] | None:
        if decision.get("decision") != "TAKE" or not gate.get("accepted"):
            return None
        entry = float(decision["entry"])
        stop = float(decision["stop_loss"])
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return {"event": "paper_reject", "reason": "zero_risk"}
        risk_usd = self.equity * self.risk_fraction
        self.position = PaperPosition(
            direction=str(decision["direction"]),
            entry=entry,
            stop_loss=stop,
            take_profit=float(decision["take_profit"]) if decision.get("take_profit") is not None else None,
            qty=risk_usd / risk_per_unit,
        )
        return {"event": "paper_open", "position": asdict(self.position)}

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
            pnl = (exit_price - pos.entry) * pos.qty if exit_price is not None else 0.0
        else:
            if high >= pos.stop_loss:
                exit_price, reason = pos.stop_loss, "stop_loss"
            elif pos.take_profit is not None and low <= pos.take_profit:
                exit_price, reason = pos.take_profit, "take_profit"
            pnl = (pos.entry - exit_price) * pos.qty if exit_price is not None else 0.0
        if exit_price is None:
            return None
        self.equity += pnl
        closed = asdict(pos)
        self.position = None
        return {"event": "paper_close", "reason": reason, "exit_price": exit_price, "pnl": pnl, "equity": self.equity, "position": closed}
