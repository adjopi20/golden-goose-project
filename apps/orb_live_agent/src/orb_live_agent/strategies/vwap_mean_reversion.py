from __future__ import annotations

from typing import Any


def decide_algorithm(snapshot: dict[str, Any], trigger_observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": "WAIT",
        "reason": "strategy_placeholder",
        "provider": "algorithm",
        "strategy": "vwap_mean_reversion",
        "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
    }
