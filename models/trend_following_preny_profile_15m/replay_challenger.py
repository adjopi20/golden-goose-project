"""Replay a frozen Pre-NY plug study through the shared execution engine.

This is an experiment adapter, not a new backtest engine. Existing test-fold
decisions are reused; no outcome selects a gate or an alternative entry.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path

from backtest_engine.replay_aggtrades import Config, run as replay_raw
from backtest_engine.results import calculate_shared_backtest_metrics
from .evaluate_plugs import _gate, read_jsonl

PLUG = "delta_without_result"
MODEL = Path(__file__).resolve().parent
REPO = MODEL.parent.parent


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows), encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relocated(path):
    # Explicit migration only; never search for a similarly named dataset.
    value = str(path).replace("\\", "/").replace(
        "models/daily_profile_1h/", "models/trend_following_preny_profile_15m/")
    result = Path(value)
    return result if result.is_absolute() else REPO / result


def select_signals(signals, predictions, folds):
    """Gate the matched historical execution cohort, ignoring outcome fields."""
    by_day = {}
    for signal in signals:
        if signal["profile_window"] != "pre_ny":
            raise ValueError("Only Pre-NY signals allowed")
        day = signal["session_day"]
        if day in by_day:
            raise ValueError(f"Duplicate signal day {day}")
        by_day[day] = signal
    fold_map = {f["fold"]: f for f in folds}
    if len(fold_map) != len(folds):
        raise ValueError("Duplicate fold")
    baseline, challenger, decisions = [], [], []
    seen = set()
    for row in sorted(predictions, key=lambda r: r["session_day"]):
        day = row["session_day"]
        if day in seen:
            raise ValueError(f"Duplicate prediction {day}")
        seen.add(day)
        fold = fold_map[row["fold"]]
        if not (fold["train_end_exclusive"] < fold["test_start"] <= day < fold["test_end_exclusive"]):
            raise ValueError(f"Train/test leakage or wrong fold on {day}")
        if row["thresholds"] != fold["thresholds"]:
            raise ValueError("Prediction thresholds differ from frozen fold")
        gate = _gate(row["features"], PLUG, fold["thresholds"])
        if gate is not row["gate"]:
            raise ValueError("Frozen decision does not reproduce")
        signal = by_day[day]
        if signal["symbol"].upper() != row["symbol"].upper():
            raise ValueError("Symbol mismatch")
        if int(signal["feature_as_of_ms"]) > int(signal["entry_eligible_timestamp_ms"]):
            raise ValueError("Future signal features")
        baseline.append(signal)
        if gate is not False:
            challenger.append(signal)
        decisions.append(dict(sample_id=signal["sample_id"], session_day=day,
                              fold=row["fold"], gate=gate, thresholds=fold["thresholds"],
                              features={k: row["features"][k] for k in fold["thresholds"]},
                              feature_as_of_ms=signal["feature_as_of_ms"],
                              mfe_lower_pct=row.get("mfe_lower_pct"),
                              mfe_lower_atr=row.get("mfe_lower_atr")))
    if not baseline:
        raise ValueError("No matched test candidates")
    return baseline, challenger, decisions


def prepare(study, symbol, raw, output, config):
    study_manifest = json.loads((study / "manifest.json").read_text(encoding="utf-8"))
    datasets = [d for d in study_manifest["datasets"] if d["symbol"].upper() == symbol]
    if len(datasets) != 1 or float(datasets[0]["fee_bps"]) != config.fee_bps:
        raise ValueError("Missing/duplicate asset or fee differs from frozen study")
    obs = relocated(datasets[0]["observation_dir"])
    signal_path = obs / "pre_ny/signals.jsonl"
    match = lambda r: r["symbol"].upper() == symbol and r["profile_window"] == "pre_ny" and r["plug"] == PLUG
    predictions = [r for r in read_jsonl(study / "predictions.jsonl") if match(r)]
    folds = [r for r in read_jsonl(study / "folds.jsonl") if match(r)]
    baseline, challenger, decisions = select_signals(read_jsonl(signal_path), predictions, folds)
    stat = raw.stat()
    manifest = dict(schema=1, symbol=symbol, plug=PLUG, profile="pre_ny", config=asdict(config),
                    raw=str(raw.resolve()), raw_size=stat.st_size, raw_mtime_ns=stat.st_mtime_ns,
                    study=str(study.resolve()), signal_source=str(signal_path),
                    hashes={str(p): digest(p) for p in (signal_path, study / "manifest.json",
                             study / "predictions.jsonl", study / "folds.jsonl")},
                    start_date=baseline[0]["session_day"], end_date=baseline[-1]["session_day"],
                    baseline_candidates=len(baseline), challenger_candidates=len(challenger),
                    cohort="matched historical executed candidates in frozen test folds; not all calendar sessions",
                    missing_features="keep baseline", replacement_entry=False,
                    warning="Previously inspected history, not untouched OOS. No new threshold optimization.")
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("Inputs/config changed: use a new output directory")
    write_json(manifest_path, manifest)
    write_rows(output / "signals/baseline/signals.jsonl", baseline)
    write_rows(output / "signals/challenger/signals.jsonl", challenger)
    write_rows(output / "decisions.jsonl", decisions)
    write_rows(output / "folds.jsonl", folds)
    return manifest


def run_replay(output, manifest, config):
    complete = output / "replay_complete.json"
    fingerprint = digest(output / "manifest.json")
    if complete.exists():
        saved = json.loads(complete.read_text())
        if saved["manifest_hash"] != fingerprint or any(digest(Path(p)) != h for p, h in saved["artifacts"].items()):
            raise ValueError("Replay completion receipt does not match files")
        return
    sources = {label: output / f"signals/{label}/signals.jsonl" for label in ("baseline", "challenger")}
    replay_raw(Path(manifest["raw"]), sources, output / "replay", config,
               date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"]))
    artifacts = [output / f"replay/{label}/{name}" for label in sources
                 for name in ("trades.jsonl", "summary.json", "equity_curve.jsonl", "decisions.jsonl", "paper_orders.jsonl")]
    write_json(complete, dict(manifest_hash=fingerprint, artifacts={str(p): digest(p) for p in artifacts}))


def scaled_ledger(trades, equity):
    """Exact cached-fill rescaling for this engine's proportional risk/leverage sizing.

    Only for nonoverlapping positions, unchanged fills/fees/stops; not a fresh
    market replay. No fixed lot rounding or size-dependent impact is modeled.
    """
    result, previous_exit = [], -1
    for original in sorted(trades, key=lambda r: r["entry_timestamp_ms"]):
        if original["entry_timestamp_ms"] <= previous_exit:
            raise ValueError("Cached rescaling requires nonoverlapping trades")
        previous_exit = original["exit_timestamp_ms"]
        factor = equity / original["equity_before_entry"]
        row = dict(original)
        for key in ("pnl", "fees", "entry_fee", "exit_fee", "quantity", "actual_initial_risk", "gross_pnl_before_costs"):
            row[key] = original[key] * factor
        row["equity_before_entry"] = equity
        equity += row["pnl"]
        if equity <= 0:
            raise ValueError("Insolvent cached-fill portfolio")
        row["equity_after_exit"] = equity
        result.append(row)
    return result


def report(output, manifest, random_repeats, seed):
    if not (output / "replay_complete.json").exists():
        raise ValueError("Replay incomplete; refusing partial analysis")
    initial = manifest["config"]["initial_equity"]
    ledgers = {label: read_jsonl(output / f"replay/{label}/trades.jsonl") for label in ("baseline", "challenger")}
    decisions = read_jsonl(output / "decisions.jsonl")
    by_id = {r["sample_id"]: r for r in decisions}
    base = {r["sample_id"]: r for r in ledgers["baseline"]}
    kept = {r["sample_id"]: r for r in ledgers["challenger"]}
    if set(base) != set(by_id) or set(kept) != {key for key, d in by_id.items() if d["gate"] is not False}:
        raise ValueError("Replay cohort differs from prepared study; inspect execution exclusions")
    # Validate cached controls against genuine challenger raw replay before using them.
    rescaled = {r["sample_id"]: r for r in scaled_ledger([base[k] for k in kept], initial)}
    for key, trade in kept.items():
        for field in ("entry_timestamp_ms", "exit_timestamp_ms", "entry_price", "exit_price", "stop"):
            if trade[field] != base[key][field]:
                raise ValueError("Gate unexpectedly changed entry/exit execution")
        for field in ("pnl", "fees", "equity_after_exit", "r"):
            if not math.isclose(trade[field], rescaled[key][field], rel_tol=1e-9, abs_tol=1e-7):
                raise ValueError("Cached rescaling differs from shared-engine replay")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    measures = lambda rows: calculate_shared_backtest_metrics(rows, initial, start, end)
    metrics = {label: measures(rows) for label, rows in ledgers.items()}
    keys = ("trade_count", "win_rate", "expectancy_r", "profit_factor_r", "total_r", "final_equity", "max_drawdown", "longest_loss_streak", "total_fees")
    summary = [dict(symbol=manifest["symbol"], variant=label, **{k: m[k] for k in keys}) for label, m in metrics.items()]
    write_csv(output / "comparison.csv", summary)
    groups = defaultdict(list)
    for key, d in by_id.items():
        groups[d["fold"]].append(key)
    period_rows = []
    for fold, ids in sorted(groups.items()):
        for label, ledger in (("baseline", base), ("challenger", kept)):
            rows = [ledger[key] for key in ids if key in ledger]
            m = measures(scaled_ledger(rows, initial))
            period_rows.append(dict(fold=fold, variant=label, trades=len(rows), expectancy_r=m["expectancy_r"], total_r=m["total_r"], win_rate=m["win_rate"]))
    write_csv(output / "fold_comparison.csv", period_rows)
    opportunity = []
    for key, d in by_id.items():
        opportunity.append(dict(sample_id=key, session_day=d["session_day"], retained=key in kept,
                                net_r=base[key]["r"], winner=base[key]["pnl"] > 0,
                                mfe_lower_pct=d["mfe_lower_pct"], mfe_lower_atr=d["mfe_lower_atr"]))
    write_rows(output / "opportunity_cost.jsonl", opportunity)
    loss_report = dict(rejected=sum(not r["retained"] for r in opportunity),
                       rejected_losers=sum(not r["retained"] and r["net_r"] < 0 for r in opportunity),
                       rejected_winners=sum(not r["retained"] and r["winner"] for r in opportunity))
    for name, field in (("2pct", "mfe_lower_pct"), ("2atr", "mfe_lower_atr")):
        tail = [r for r in opportunity if r[field] is not None and r[field] >= 2]
        loss_report[f"tail_{name}_baseline"] = len(tail)
        loss_report[f"tail_{name}_rejected"] = sum(not r["retained"] for r in tail)
    write_json(output / "opportunity_cost.json", loss_report)
    rng, random_rows = random.Random(seed), []
    for trial in range(random_repeats):
        ids = [key for group in groups.values()
               for key in rng.sample(group, sum(key in kept for key in group))]
        m = measures(scaled_ledger([base[key] for key in ids], initial))
        random_rows.append(dict(trial=trial, **{k: m[k] for k in keys}))
    write_csv(output / "random_same_frequency.csv", random_rows)
    random_summary = dict(seed=seed, repeats=random_repeats,
                          method="Same retained count per test fold; cached-fill proportional equity rescaling verified against raw challenger",
                          warning="Diagnostic null, not a causal entry rule or multiplicity-adjusted significance test")
    for key in ("expectancy_r", "total_r", "final_equity", "max_drawdown"):
        values = sorted(r[key] for r in random_rows)
        random_summary[key] = dict(p05=values[int(.05*(len(values)-1))], median=values[len(values)//2],
                                   p95=values[int(.95*(len(values)-1))], challenger=metrics["challenger"][key],
                                   fraction_random_at_least_as_good=sum(
                                       v <= metrics["challenger"][key] if key == "max_drawdown" else v >= metrics["challenger"][key]
                                       for v in values)/len(values))
    write_json(output / "random_summary.json", random_summary)
    write_json(output / "complete.json", dict(manifest_hash=digest(output / "manifest.json"), status="complete"))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--dataset", nargs=2, action="append", required=True, metavar=("SYMBOL", "RAW_PARQUET"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("prepare", "all"), default="all")
    parser.add_argument("--random-repeats", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260926)
    args = parser.parse_args()
    if args.random_repeats < 1:
        parser.error("random-repeats must be positive")
    symbols = [s.upper() for s, _ in args.dataset]
    if len(symbols) != len(set(symbols)):
        parser.error("Duplicate asset")
    config, plans = Config(), []
    # Preflight every dataset before starting any long replay.
    for symbol, raw in args.dataset:
        symbol = symbol.upper()
        output = args.output_dir.resolve() / symbol.lower()
        manifest = prepare(args.study_dir.resolve(), symbol, Path(raw).resolve(), output, config)
        plans.append((output, manifest))
        print(f"{symbol}: prepared {manifest['baseline_candidates']} baseline / {manifest['challenger_candidates']} challenger", flush=True)
    combined = []
    if args.stage == "all":
        for output, manifest in plans:
            print(f"{manifest['symbol']}: shared raw replay (or verified resume)", flush=True)
            run_replay(output, manifest, config)
            combined.extend(report(output, manifest, args.random_repeats, args.seed))
            print(f"{manifest['symbol']}: complete", flush=True)
        write_csv(args.output_dir / "comparison_all_assets.csv", combined)
        common_start = max(m["start_date"] for _, m in plans)
        common_end = min(m["end_date"] for _, m in plans)
        common = []
        if common_start <= common_end:
            for output, manifest in plans:
                for label in ("baseline", "challenger"):
                    rows = [r for r in read_jsonl(output / f"replay/{label}/trades.jsonl")
                            if common_start <= r["session_day"] <= common_end]
                    m = calculate_shared_backtest_metrics(scaled_ledger(rows, config.initial_equity),
                        config.initial_equity, date.fromisoformat(common_start), date.fromisoformat(common_end))
                    common.append(dict(symbol=manifest["symbol"], variant=label,
                        start=common_start, end=common_end, method="cached fills rescaled to common-period initial equity",
                        **{k: m[k] for k in ("trade_count", "win_rate", "expectancy_r", "profit_factor_r",
                                             "total_r", "final_equity", "max_drawdown")}))
            write_csv(args.output_dir / "common_period_comparison.csv", common)
    print("Done. Assets remain independent; no combined portfolio equity.")


if __name__ == "__main__":
    main()
