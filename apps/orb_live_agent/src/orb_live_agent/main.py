from __future__ import annotations

import asyncio
from typing import Any

from .ai_decision import AiDecisionService
from .config import load_config
from .market_stream import stream_market_events
from .paper_broker import PaperBroker
from .risk_gate import RiskGate
from .state_builder import LiveStateBuilder, parse_aggtrade
from .storage import JsonlStorage
from .trigger_observer import observe_triggers


def _pre_ai_wait_decision(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if not snapshot["setup_observation_active"]:
        reason = "outside_setup_observation_window"
    elif snapshot.get("previous_24h_profile_for_session") is None:
        reason = "missing_previous_24h_profile"
    elif snapshot.get("ny_first_15m_profile") is None:
        reason = "missing_ny_first_15m_profile"
    else:
        return None
    return {
        "decision": "WAIT",
        "reason": reason,
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
    }


async def run() -> None:
    config = load_config()
    storage = JsonlStorage(config.log_dir)
    state = LiveStateBuilder(config)
    ai = AiDecisionService(config)
    gate = RiskGate()
    broker = PaperBroker(config)
    storage.write("system", {"event": "started", "symbol": config.symbol, "mode": "paper"})
    bootstrap_trades = storage.read_recent_raw_aggtrades()
    for trade in bootstrap_trades:
        state.push_trade(trade)
    storage.write("system", {"event": "bootstrapped", "raw_aggtrade_rows": len(bootstrap_trades)})

    async for event in stream_market_events(config):
        stream = event.get("stream", "")
        data = event.get("data", {})
        if stream == "system":
            storage.write("system", data)
            continue
        if stream.endswith("@kline_1m"):
            storage.write("raw_kline_1m", data)
            continue
        if not stream.endswith("@aggTrade"):
            continue

        trade = parse_aggtrade(data)
        storage.write("raw_aggtrade", trade)
        for close_event in broker.on_trade(trade):
            storage.write("paper_orders", close_event)
        closed = state.push_trade(trade)
        if closed is None:
            continue

        storage.write("candles_1m", closed.candle)
        for bubble in closed.bubbles:
            storage.write("order_bubbles", bubble)
        storage.write("session_snapshots", closed.snapshot)
        trigger_observation = observe_triggers(closed.snapshot, closed.bubbles, closed.trigger_reference_levels)
        storage.write("trigger_observations", trigger_observation)

        for close_event in broker.on_candle(closed.candle):
            storage.write("paper_orders", close_event)

        decision = _pre_ai_wait_decision(closed.snapshot)
        if decision is None:
            decision = ai.decide(closed.snapshot, trigger_observation)
        storage.write("ai_decisions", decision)
        gate_result = gate.validate(decision, has_open_position=broker.has_open_position())
        storage.write("risk_gate", {"decision": decision, "gate": gate_result})
        open_event = broker.on_decision(decision, gate_result)
        if open_event is not None:
            storage.write("paper_orders", open_event)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
