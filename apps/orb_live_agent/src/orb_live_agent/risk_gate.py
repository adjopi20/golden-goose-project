from __future__ import annotations

from typing import Any


VALID_DECISIONS = {"WAIT", "REJECT", "TAKE"}


class RiskGate:
    def validate(self, decision: dict[str, Any], has_open_position: bool) -> dict[str, Any]:
        action = decision.get("decision")
        if action not in VALID_DECISIONS:
            return {"accepted": False, "reason": "invalid_decision"}
        if action != "TAKE":
            return {"accepted": True, "reason": "non_trade_decision"}
        if has_open_position:
            return {"accepted": False, "reason": "open_position_overlap"}
        required = ["direction", "entry", "stop_loss"]
        missing = [key for key in required if key not in decision]
        if missing:
            return {"accepted": False, "reason": "missing_trade_fields", "missing": missing}
        entry_model = str(decision.get("entry_model", "trend")).lower()
        if entry_model != "trend":
            return {"accepted": False, "reason": "unsupported_entry_model", "entry_model": entry_model}
        direction = decision["direction"]
        entry = float(decision["entry"])
        stop = float(decision["stop_loss"])
        if direction == "long" and stop >= entry:
            return {"accepted": False, "reason": "invalid_long_stop"}
        if direction == "short" and stop <= entry:
            return {"accepted": False, "reason": "invalid_short_stop"}
        if direction not in {"long", "short"}:
            return {"accepted": False, "reason": "invalid_direction"}
        return {"accepted": True, "reason": "accepted"}
