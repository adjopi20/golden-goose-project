from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib import request

from .config import AgentConfig

VALID_DECISIONS = {"WAIT", "REJECT", "TAKE"}


class AiDecisionService:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.calls_by_day: dict[str, int] = {}
        self.rules_text = self._load_rules(config.rules_file)

    def decide(self, snapshot: dict[str, Any], trigger_observation: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.config.ai_provider == "stub":
            return self._wait(snapshot, "stub_ai_provider")
        if not self.config.ai_live_calls_enabled:
            return self._wait(snapshot, "ai_live_calls_disabled")
        if trigger_observation and not trigger_observation.get("triggered"):
            return self._wait(snapshot, "no_trigger_observed")

        today = date.today().isoformat()
        used = self.calls_by_day.get(today, 0)
        if used >= self.config.max_ai_calls_per_day:
            return self._wait(snapshot, "daily_ai_call_cap_reached")
        self.calls_by_day[today] = used + 1

        try:
            return self._call_chat_api(snapshot, trigger_observation or {})
        except Exception as exc:
            return {**self._wait(snapshot, "ai_call_failed"), "error": str(exc)[:300]}

    def _call_chat_api(self, snapshot: dict[str, Any], trigger_observation: dict[str, Any]) -> dict[str, Any]:
        api_key = self._api_key()
        body = {
            "model": self.config.ai_model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": json.dumps(self._decision_payload(snapshot, trigger_observation), separators=(",", ":"), default=str)},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
            "max_tokens": 2048,
        }
        if self.config.ai_provider == "deepseek":
            body["reasoning_effort"] = "high"
            body["thinking"] = {"type": "enabled"}

        req = request.Request(
            self.config.ai_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with request.urlopen(req, timeout=60) as response:
            response_body = json.loads(response.read().decode("utf-8"))
        content = response_body["choices"][0]["message"]["content"]
        decision = json.loads(content)
        if decision.get("decision") not in VALID_DECISIONS:
            raise ValueError("AI returned invalid decision")
        return {
            **decision,
            "provider": self.config.ai_provider,
            "model": self.config.ai_model,
            "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
        }

    def _api_key(self) -> str:
        if self.config.ai_provider == "deepseek":
            key = os.getenv("DEEPSEEK_API_KEY", "")
        elif self.config.ai_provider == "openai":
            key = os.getenv("OPENAI_API_KEY", "")
        else:
            raise ValueError(f"Unsupported AI_PROVIDER: {self.config.ai_provider}")
        if not key:
            raise ValueError(f"Missing API key for AI_PROVIDER={self.config.ai_provider}")
        return key

    def _system_prompt(self) -> str:
        return "\n".join(
            [
                "You are the ORB live paper-trading decision service.",
                "Return only valid JSON. No markdown. No prose.",
                'Allowed decision values: "WAIT", "REJECT", "TAKE".',
                "For TAKE, include direction, entry, stop_loss, reason, invalidation.",
                "Do not choose take-profit, sizing, fees, slippage, or trailing; deterministic execution handles those.",
                "Use only the provided closed candles and raw-orderflow-derived fields. Do not infer future candles.",
                "Rules:",
                self.rules_text,
            ]
        )

    def _decision_payload(self, snapshot: dict[str, Any], trigger_observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "trigger_observation": trigger_observation,
            "snapshot": {
                "symbol": snapshot.get("symbol"),
                "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
                "target_session_day": snapshot.get("target_session_day"),
                "session_timezone": snapshot.get("session_timezone"),
                "setup_observation_active": snapshot.get("setup_observation_active"),
                "last_candle": snapshot.get("last_candle"),
                "recent_candles": snapshot.get("recent_candles"),
                "session_extremes": snapshot.get("session_extremes"),
                "previous_24h_profile_for_session": self._compact_profile(snapshot.get("previous_24h_profile_for_session")),
                "ny_first_15m_profile": self._compact_profile(snapshot.get("ny_first_15m_profile")),
            },
        }

    @staticmethod
    def _compact_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
        if not profile:
            return None
        keys = [
            "profile_type",
            "frozen_at_session_open",
            "window_start",
            "window_end",
            "timezone",
            "session_low",
            "session_high",
            "total_volume",
            "poc_price",
            "poc_volume",
            "poc_volume_pct",
            "val",
            "vah",
            "value_area_width",
            "value_area_volume_pct",
            "hvn_regions",
            "lvn_regions",
        ]
        return {key: profile.get(key) for key in keys if key in profile}

    @staticmethod
    def _load_rules(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _wait(self, snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "decision": "WAIT",
            "reason": reason,
            "provider": self.config.ai_provider,
            "snapshot_timestamp_ms": snapshot.get("snapshot_timestamp_ms"),
        }
