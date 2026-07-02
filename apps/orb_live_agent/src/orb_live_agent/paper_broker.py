from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from models.orb.execution_engine import ExecutionConfig, ExecutionPosition, force_exit, on_price, open_position

from .config import AgentConfig


class PaperBroker:
    def __init__(self, config: AgentConfig) -> None:
        self.equity = float(config.paper_initial_equity)
        self.risk_fraction = float(config.paper_risk_fraction)
        self.session_tz = ZoneInfo(config.session_timezone)
        self.max_hold_exit_time = _parse_time(config.pre_ny_start_time)
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

    def on_decision(self, decision: dict[str, Any], gate: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if decision.get("decision") != "TAKE" or not gate.get("accepted"):
            return None
        if decision.get("snapshot_timestamp_ms") is None:
            return {"event": "paper_reject", "reason": "missing_snapshot_timestamp_ms"}
        entry_model = _entry_model(decision)
        targets = self._targets(entry_model, str(decision["direction"]), snapshot)
        if targets.get("event") == "paper_reject":
            return targets
        position, event = open_position(
            direction=str(decision["direction"]),
            entry_model=entry_model,
            requested_entry=float(decision["entry"]),
            stop_loss=float(decision["stop_loss"]),
            equity=self.equity,
            risk_fraction=self.risk_fraction,
            config=self.execution_config,
            max_hold_exit_ms=self._max_hold_exit_ms(int(decision["snapshot_timestamp_ms"])),
            tp1_price=targets["tp1_price"],
            tp2_price=targets["tp2_price"],
        )
        if position is None:
            return event

        self.position = position
        self.equity -= position.fee_paid
        event["equity"] = self.equity
        return event

    def on_trade(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        if self.position is None or self.position.max_hold_exit_ms is None:
            return []
        if int(trade["timestamp"]) < self.position.max_hold_exit_ms:
            return []
        event = force_exit(
            self.position,
            float(trade["price"]),
            self.execution_config,
            "overnight_time_invalidation",
        )
        event["timestamp"] = int(trade["timestamp"])
        return self._apply_event(event)

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

    def _targets(self, entry_model: str, direction: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        if entry_model != "mean_reversion":
            return {"tp1_price": None, "tp2_price": None}
        profile = (snapshot or {}).get("previous_24h_profile_for_session")
        if not profile:
            return {"event": "paper_reject", "reason": "missing_mr_profile"}
        tp1 = profile.get("poc_price")
        tp2 = profile.get("vah") if direction == "long" else profile.get("val")
        if tp1 is None or tp2 is None:
            return {"event": "paper_reject", "reason": "missing_mr_targets"}
        return {"tp1_price": float(tp1), "tp2_price": float(tp2)}

    def _max_hold_exit_ms(self, opened_at_ms: int) -> int:
        opened_at = datetime.fromtimestamp(opened_at_ms / 1000.0, tz=ZoneInfo("UTC")).astimezone(self.session_tz)
        cutoff_date = opened_at.date() + timedelta(days=1)
        cutoff = datetime.combine(cutoff_date, self.max_hold_exit_time, tzinfo=self.session_tz)
        return int(cutoff.timestamp() * 1000)

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


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))


def _entry_model(decision: dict[str, Any]) -> str:
    value = str(decision.get("entry_model", decision.get("model", "trend"))).lower()
    return "mean_reversion" if value in {"mr", "mean_reversion", "structural_mr"} else "trend"
