from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.parquet as pq

from orb_live_agent.config import load_config
from orb_live_agent.fast_orb_backtest import _stamp, _trades_from_orders, _write_jsonl
from orb_live_agent.fast_orb_sweep import _metrics
from orb_live_agent.paper_broker import PaperBroker
from orb_live_agent.risk_gate import RiskGate


def _signals(observation_dir: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (observation_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    signals = []
    for row in rows:
        if row.get("sample") is not True:
            continue
        if row.get("risk_model") != "opposite_extreme":
            raise ValueError("Observation samples must use opposite_extreme risk")
        signals.append(
            {
                "session_day": row["session_day"],
                "entry_ms": int(datetime.fromisoformat(row["breakout_time"]).timestamp() * 1000),
                "entry": float(row["breakout_price"]),
                "direction": str(row["direction"]),
                "stop_loss": float(row["stop_loss"]),
            }
        )
    return sorted(signals, key=lambda row: row["entry_ms"])


def _variants(tp_values: list[float]) -> list[dict[str, Any]]:
    base = load_config()
    variants = []
    for tp_r in tp_values:
        config = replace(
            base,
            orb_stop_model="opposite_extreme",
            paper_tp1_r=tp_r,
            paper_tp1_fraction=1.0,
            paper_exit_mode="tp1_trail",
            paper_protection_enabled=False,
        )
        variants.append(
            {
                "label": f"tp_{tp_r:g}r",
                "tp_r": tp_r,
                "config": config,
                "timezone": ZoneInfo(config.session_timezone),
                "broker": PaperBroker(config),
                "gate": RiskGate(config.paper_min_stop_risk_pct, config.paper_max_stop_risk_pct),
                "orders": [],
                "decisions": [],
            }
        )
    return variants


def _run_ticks(input_path: Path, signals: list[dict[str, Any]], variants: list[dict[str, Any]]) -> None:
    signal_index = 0
    for batch in pq.ParquetFile(input_path).iter_batches(batch_size=1_000_000, columns=["timestamp", "price"]):
        timestamps = batch.column(0).to_numpy(zero_copy_only=False)
        prices = batch.column(1).to_numpy(zero_copy_only=False)
        index = 0
        while index < len(timestamps):
            if not any(item["broker"].position for item in variants):
                if signal_index >= len(signals):
                    return
                index = int(np.searchsorted(timestamps, signals[signal_index]["entry_ms"], side="left"))
                if index >= len(timestamps):
                    break

            timestamp_ms = int(timestamps[index])
            price = float(prices[index])
            trade = {"timestamp": timestamp_ms, "price": price}
            for item in variants:
                item["orders"].extend(_stamp(item["broker"].on_trade(trade), timestamp_ms, item["timezone"]))

            while signal_index < len(signals) and signals[signal_index]["entry_ms"] <= timestamp_ms:
                signal = signals[signal_index]
                for item in variants:
                    decision = {
                        "decision": "TAKE",
                        "entry_model": "trend",
                        "strategy": "basic_orb_raw_breakout",
                        "direction": signal["direction"],
                        "entry": signal["entry"],
                        "stop_loss": signal["stop_loss"],
                        "snapshot_timestamp_ms": signal["entry_ms"],
                        "session_day": signal["session_day"],
                    }
                    gate = item["gate"].validate(decision, item["broker"].has_open_position())
                    item["decisions"].append({"decision": decision, "gate": gate})
                    event = item["broker"].on_decision(decision, gate)
                    if event:
                        item["orders"].extend(
                            _stamp([event], signal["entry_ms"], item["timezone"])
                        )
                signal_index += 1

            tick = {"high": price, "low": price}
            for item in variants:
                item["orders"].extend(
                    _stamp(item["broker"].on_candle(tick), timestamp_ms, item["timezone"])
                )
            index += 1


def run(input_path: Path, observation_dir: Path, output_dir: Path, tp_values: list[float]) -> list[dict[str, Any]]:
    signals = _signals(observation_dir)
    variants = _variants(tp_values)
    _run_ticks(input_path, signals, variants)
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for item in variants:
        variant_dir = output_dir / item["label"]
        variant_dir.mkdir(parents=True, exist_ok=True)
        trades = _trades_from_orders(item["orders"])
        metrics = _metrics(trades, item["config"].paper_initial_equity)
        summary = {
            "variant": item["label"],
            "entry_mode": "first raw aggTrade crossing the frozen ORB extreme",
            "stop_model": "opposite_extreme",
            "target_r": item["tp_r"],
            "tp_fraction": 1.0,
            "protection_enabled": False,
            "fee_bps": item["config"].paper_fee_bps,
            "slippage_bps": item["config"].paper_slippage_bps,
            "risk_fraction": item["config"].paper_risk_fraction,
            "signals": len(signals),
            "final_equity": item["broker"].equity,
            **metrics,
        }
        _write_jsonl(variant_dir / "decisions.jsonl", item["decisions"])
        _write_jsonl(variant_dir / "paper_orders.jsonl", item["orders"])
        _write_jsonl(variant_dir / "trades.jsonl", trades)
        (variant_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        summaries.append(summary)
        print(json.dumps({"event": "variant_finished", **summary}, sort_keys=True))
    (output_dir / "sweep_summary.json").write_text(json.dumps(summaries, indent=2, sort_keys=True), encoding="utf-8")
    return summaries


def _self_check() -> None:
    config = replace(
        load_config(),
        paper_initial_equity=1000.0,
        paper_risk_fraction=0.01,
        paper_fee_bps=0.0,
        paper_slippage_bps=0.0,
        paper_tp1_r=2.0,
        paper_tp1_fraction=1.0,
        paper_exit_mode="tp1_trail",
        paper_protection_enabled=False,
    )
    broker = PaperBroker(config)
    decision = {
        "decision": "TAKE",
        "entry_model": "trend",
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 99.0,
        "snapshot_timestamp_ms": 0,
    }
    assert broker.on_decision(decision, RiskGate(0, 1).validate(decision, False))["event"] == "paper_open"
    assert broker.on_candle({"high": 102.0, "low": 102.0})[0]["event"] == "paper_close"
    assert broker.equity == 1020.0


def main() -> None:
    _self_check()
    parser = argparse.ArgumentParser(description="Backtest the unfiltered raw-tick ORB breakout baseline.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--observation-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tp-r", action="append", type=float)
    args = parser.parse_args()
    results = run(
        Path(args.input),
        Path(args.observation_dir),
        Path(args.output_dir),
        args.tp_r or [1.0, 2.0, 4.0, 8.0],
    )
    print(json.dumps({"event": "sweep_finished", "variants": len(results), "output_dir": args.output_dir}, indent=2))


if __name__ == "__main__":
    main()
