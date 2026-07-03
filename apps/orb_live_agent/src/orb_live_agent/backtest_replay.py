from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .ai_decision import AiDecisionService
from .config import load_config
from .main import _pre_ai_wait_decision
from .paper_broker import PaperBroker
from .risk_gate import RiskGate
from .state_builder import LiveStateBuilder
from .trigger_observer import observe_triggers


class ReplayLog:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, stream: str, record: dict[str, Any]) -> None:
        path = self.output_dir / f"{stream}.jsonl"
        row = {"logged_at_utc": datetime.now(timezone.utc).isoformat(), **record}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


class DecisionCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    self.rows[str(row["cache_key"])] = row

    def get(self, key: str) -> dict[str, Any] | None:
        return self.rows.get(key)

    def put(self, row: dict[str, Any]) -> None:
        key = str(row["cache_key"])
        if key in self.rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        self.rows[key] = row


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", maxsplit=1)
    return int(hour), int(minute)


def _ny_ms(day: date, hhmm: str, tz: ZoneInfo) -> int:
    hour, minute = _parse_hhmm(hhmm)
    dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _load_trades(path: Path, start_ms: int, end_ms: int) -> pd.DataFrame:
    df = pd.read_parquet(path, filters=[("timestamp", ">=", start_ms), ("timestamp", "<", end_ms)])
    required = {"timestamp", "price", "qty", "is_buyer_maker"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input parquet missing columns: {sorted(missing)}")
    if "trade_id" not in df.columns and "agg_trade_id" not in df.columns:
        df["trade_id"] = range(len(df))
    return df.sort_values(["timestamp"], kind="mergesort").reset_index(drop=True)


def _row_to_trade(row: pd.Series) -> dict[str, Any]:
    trade_id = int(row["agg_trade_id"] if "agg_trade_id" in row else row["trade_id"])
    ts = int(row["timestamp"])
    return {
        "event_type": "aggTrade",
        "event_time": ts,
        "agg_trade_id": trade_id,
        "price": float(row["price"]),
        "qty": float(row["qty"]),
        "first_trade_id": trade_id,
        "last_trade_id": trade_id,
        "timestamp": ts,
        "is_buyer_maker": bool(row["is_buyer_maker"]),
    }


def _cache_key(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _decide(
    *,
    ai: AiDecisionService,
    cache: DecisionCache,
    log: ReplayLog,
    snapshot: dict[str, Any],
    trigger: dict[str, Any],
    fresh_ai: bool,
) -> dict[str, Any]:
    if ai.config.ai_provider in {"algorithm", "rule", "rules"}:
        return ai.decide(snapshot, trigger)

    body = ai.build_chat_body(snapshot, trigger)
    key = _cache_key(body)
    cached = None if fresh_ai else cache.get(key)
    if cached:
        decision = {**cached["decision"], "cached": True}
        log.write("ai_requests", {"cache_key": key, "cached": True, "request_body": cached["request_body"], "response_body": cached.get("response_body")})
        return decision

    decision = ai.decide(snapshot, trigger)
    row = {
        "cache_key": key,
        "decision": decision,
        "request_body": ai.last_request_body or body,
        "response_body": ai.last_response_body,
    }
    log.write("ai_requests", {**row, "cached": False})
    if ai.last_response_body is not None and decision.get("reason") != "ai_call_failed":
        cache.put(row)
    return decision


def run_replay(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    if (config.ai_provider == "stub" or not config.ai_live_calls_enabled) and not args.allow_disabled_ai:
        raise ValueError("Set AI_PROVIDER=deepseek and AI_LIVE_CALLS_ENABLED=true in apps/orb_live_agent/.env, or pass --allow-disabled-ai.")

    start_day = _parse_date(args.start_date)
    end_day = _parse_date(args.end_date)
    if end_day < start_day:
        raise ValueError("--end-date must be >= --start-date")

    output_dir = Path(args.output_dir) if args.output_dir else config.log_dir / "backtests" / f"{start_day}_to_{end_day}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    config = replace(config, log_dir=output_dir)
    tz = ZoneInfo(config.session_timezone)
    preload_start_ms = _ny_ms(start_day, config.ny_open_time, tz) - 25 * 60 * 60 * 1000
    replay_end_ms = _ny_ms(end_day + timedelta(days=1), config.pre_ny_start_time, tz) + 60 * 60 * 1000

    trades = _load_trades(Path(args.input), preload_start_ms, replay_end_ms)
    log = ReplayLog(output_dir)
    cache = DecisionCache(Path(args.cache) if args.cache else config.log_dir.parent / "decision_cache.jsonl")
    state = LiveStateBuilder(config)
    ai = AiDecisionService(config)
    gate = RiskGate(config.paper_min_stop_risk_pct, config.paper_max_stop_risk_pct)
    broker = PaperBroker(config)
    trades_taken = 0

    log.write("system", {
        "event": "backtest_started",
        "input": str(args.input),
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "rows_loaded": len(trades),
        "ai_provider": config.ai_provider,
        "ai_model": config.ai_model,
    })

    for _, row in trades.iterrows():
        trade = _row_to_trade(row)
        for close_event in broker.on_trade(trade):
            log.write("paper_orders", close_event)

        closed = state.push_trade(trade)
        if closed is None:
            continue

        snapshot_day = _parse_date(str(closed.snapshot["target_session_day"]))
        in_requested_days = start_day <= snapshot_day <= end_day
        log.write("candles_1m", closed.candle)

        trigger = observe_triggers(closed.snapshot, closed.bubbles, closed.trigger_reference_levels)
        log.write("trigger_observations", trigger)

        for close_event in broker.on_candle(closed.candle):
            log.write("paper_orders", close_event)

        if not in_requested_days:
            continue

        decision = _pre_ai_wait_decision(closed.snapshot)
        if decision is None:
            if trigger.get("triggered") and config.ai_provider not in {"algorithm", "rule", "rules"}:
                log.write("trigger_snapshots", {"trigger": trigger, "snapshot": closed.snapshot})
            decision = _decide(ai=ai, cache=cache, log=log, snapshot=closed.snapshot, trigger=trigger, fresh_ai=bool(args.fresh_ai))
        log.write("ai_decisions", decision)

        gate_result = gate.validate(decision, has_open_position=broker.has_open_position())
        log.write("risk_gate", {"decision": decision, "gate": gate_result})
        open_event = broker.on_decision(decision, gate_result, closed.snapshot)
        if open_event is not None:
            trades_taken += int(open_event.get("event") == "paper_open")
            log.write("paper_orders", open_event)

    summary = {
        "event": "backtest_finished",
        "output_dir": str(output_dir),
        "rows_loaded": len(trades),
        "trades_taken": trades_taken,
        "final_equity": broker.equity,
        "has_open_position": broker.has_open_position(),
    }
    log.write("system", summary)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical aggTrades through the ORB live agent loop.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True, help="NY session date, inclusive, for AI decisions.")
    parser.add_argument("--end-date", required=True, help="NY session date, inclusive, for AI decisions.")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--cache", default="")
    parser.add_argument("--fresh-ai", action="store_true")
    parser.add_argument("--allow-disabled-ai", action="store_true")
    summary = run_replay(parser.parse_args())
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
