from __future__ import annotations

from dataclasses import asdict
from typing import Any

from models.orb.execution_engine import ExecutionConfig, ExecutionPosition, on_price, open_position

from .config import AgentConfig


class PaperBroker:
    def __init__(self, config: AgentConfig) -> None:
        self.equity = float(config.paper_initial_equity)
        self.risk_fraction = float(config.paper_risk_fraction)
        self.execution_config = ExecutionConfig(
            fee_bps=float(config.paper_fee_bps),
            slippage_bps=float(config.paper_slippage_bps),
            tp1_r=float(config.paper_tp1_r),
            tp1_fraction=float(config.paper_tp1_fraction),
            runner_trail_tp1_fraction=float(config.paper_runner_trail_tp1_fraction),
        )
        self.position: ExecutionPosition | None = None

    def has_open_position(self) -> bool:
        return self.position is not None

    def on_decision(self, decision: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any] | None:
        if decision.get("decision") != "TAKE" or not gate.get("accepted"):
            return None
        position, event = open_position(
            direction=str(decision["direction"]),
            requested_entry=float(decision["entry"]),
            stop_loss=float(decision["stop_loss"]),
            equity=self.equity,
            risk_fraction=self.risk_fraction,
            config=self.execution_config,
        )
        if position is None:
            return event

        self.position = position
        self.equity -= position.fee_paid
        event["equity"] = self.equity
        return event

    def on_candle(self, candle: dict[str, Any]) -> list[dict[str, Any]]:
        if self.position is None:
            return []
        pos = self.position
        high = float(candle["high"])
        low = float(candle["low"])

        if not pos.tp1_hit:
            trigger_price = self._initial_stop_or_tp1_price(pos, high, low)
            if trigger_price is None:
                return []
            event = on_price(pos, trigger_price, self.execution_config)
            return self._apply_event(event)

        events: list[dict[str, Any]] = []
        favorable_price = high if pos.direction == "long" else low
        event = on_price(pos, favorable_price, self.execution_config)
        events.extend(self._apply_event(event))
        if self.position is None:
            return events

        runner_stop = float(self.position.runner_stop)
        hit_runner_stop = low <= runner_stop if self.position.direction == "long" else high >= runner_stop
        if hit_runner_stop:
            event = on_price(self.position, runner_stop, self.execution_config)
            events.extend(self._apply_event(event))
        return events

    def _initial_stop_or_tp1_price(self, pos: ExecutionPosition, high: float, low: float) -> float | None:
        if pos.direction == "long":
            if low <= pos.stop_loss:
                return pos.stop_loss
            if high >= pos.tp1_price:
                return pos.tp1_price
            return None
        if high >= pos.stop_loss:
            return pos.stop_loss
        if low <= pos.tp1_price:
            return pos.tp1_price
        return None

    def _apply_event(self, event: dict[str, Any] | None) -> list[dict[str, Any]]:
        if event is None:
            return []
        if event["event"] in {"paper_tp1", "paper_close"}:
            self.equity += float(event["pnl"])
            event["equity"] = self.equity
        if event["event"] == "paper_tp1" and self.position is not None:
            event["position"] = asdict(self.position)
        if event["event"] == "paper_close":
            if self.position is not None:
                event["position"] = asdict(self.position)
            self.position = None
        return [event]
