"""Causal, one-at-a-time entry-gate research for the daily-profile model.

This does not change signals, stops, fills, or exits. A rejected baseline trade
contributes zero R. Thresholds are fitted only on earlier sessions in each fold.
The gates are research probes, not approved live-trading rules.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PLUGS = (
    "vwap_persistence",
    "value_edge_acceptance",
    "volume_without_result",
    "delta_without_result",
    "atr_activation",
    "fee_stop_burden",
)
PROFILE_WINDOWS = ("pre_ny",)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ny_day(timestamp_ms: int) -> str:
    return str(datetime.fromtimestamp(timestamp_ms / 1000, NY).date())


def slot(timestamp_ms: int) -> int:
    clock = datetime.fromtimestamp(timestamp_ms / 1000, NY)
    return clock.hour * 4 + clock.minute // 15


def _bars_by_day(observation_dir: Path) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for bar in read_jsonl(observation_dir / "prepared_bars_15m.jsonl"):
        result[ny_day(int(bar["open_timestamp_ms"]))].append(bar)
    for bars in result.values():
        bars.sort(key=lambda row: int(row["close_timestamp_ms"]))
    return result


def _same_clock_relative_volume(
    day: str, recent: list[dict], bars_by_day: dict[str, list[dict]],
    available_days: list[str],
) -> float | None:
    wanted = {slot(int(bar["open_timestamp_ms"])) for bar in recent}
    if len(wanted) != len(recent):
        return None
    history = []
    position = bisect.bisect_left(available_days, day)
    for past_day in reversed(available_days[:position]):
        selected = [bar for bar in bars_by_day[past_day]
                    if slot(int(bar["open_timestamp_ms"])) in wanted]
        if len(selected) != len(wanted):
            continue
        history.append(sum(float(bar["buy_volume"]) + float(bar["sell_volume"])
                           for bar in selected))
        if len(history) == 20:
            break
    if len(history) < 10:
        return None
    baseline = statistics.median(history)
    current = sum(float(bar["buy_volume"]) + float(bar["sell_volume"])
                  for bar in recent)
    return current / baseline if baseline > 0 else None


def candidate_features(
    row: dict, snapshot: dict, profile: dict, bars_by_day: dict[str, list[dict]],
    available_days: list[str], fee_bps: float,
) -> dict[str, float | None]:
    day = row["session_day"]
    direction = row["actual_direction"]
    sign = 1 if direction == "long" else -1
    signal_ms = int(snapshot["feature_as_of_ms"])
    if signal_ms > int(row["actual_entry_ms"]):
        raise ValueError(f"Future signal feature on {day}")
    bars = [bar for bar in bars_by_day.get(day, [])
            if 9 <= datetime.fromtimestamp(int(bar["open_timestamp_ms"]) / 1000, NY).hour < 12
            and int(bar["close_timestamp_ms"]) <= signal_ms]
    if not bars or int(bars[-1]["close_timestamp_ms"]) != signal_ms:
        raise ValueError(f"Missing completed signal bar on {day}, {profile['profile_window']}")
    recent = bars[-4:]
    atr = finite(snapshot.get("atr14_1h"))
    if atr is not None and atr <= 0:
        atr = None
    edge = float(profile["vah"] if direction == "long" else profile["val"])
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    vwap_margins = []
    for bar in bars:
        volume = float(bar["buy_volume"]) + float(bar["sell_volume"])
        typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3
        cumulative_pv += typical * volume
        cumulative_volume += volume
        if bar in recent and cumulative_volume > 0 and atr is not None:
            vwap = cumulative_pv / cumulative_volume
            vwap_margins.append(sign * (float(bar["close"]) - vwap) / atr)
    edge_margins = ([sign * (float(bar["close"]) - edge) / atr for bar in recent]
                    if atr is not None else [])
    first_open = float(recent[0]["open"])
    result_atr = (sign * (float(recent[-1]["close"]) - first_open) / atr
                  if atr is not None else None)
    volume = sum(float(bar["buy_volume"]) + float(bar["sell_volume"])
                 for bar in recent)
    delta = sign * sum(float(bar["buy_volume"]) - float(bar["sell_volume"])
                       for bar in recent)
    entry = float(row["actual_entry_price"])
    stop_risk = abs(entry - float(row["actual_stop"])) / entry
    if sign * (entry - float(row["actual_stop"])) <= 0:
        raise ValueError(f"Invalid executed stop geometry on {day}")
    return {
        "vwap_persistence": min(vwap_margins) if vwap_margins else None,
        "value_edge_acceptance": min(edge_margins) if edge_margins else None,
        "same_clock_relative_volume": _same_clock_relative_volume(
            day, recent, bars_by_day, available_days),
        "directional_result_atr": result_atr,
        "directional_delta_imbalance": delta / volume if volume > 0 else None,
        "atr_activation": finite(snapshot.get("atr14_to_20h_median")),
        "fee_stop_burden": 2 * fee_bps / 10_000 / stop_risk if stop_risk > 0 else None,
    }


def _thresholds(train: list[dict], plug: str) -> dict[str, float] | None:
    keys = {
        "vwap_persistence": ("vwap_persistence",),
        "value_edge_acceptance": ("value_edge_acceptance",),
        "volume_without_result": ("same_clock_relative_volume", "directional_result_atr"),
        "delta_without_result": ("directional_delta_imbalance", "directional_result_atr"),
        "atr_activation": ("atr_activation",),
        "fee_stop_burden": ("fee_stop_burden",),
    }[plug]
    available = [row for row in train if all(row["features"][key] is not None for key in keys)]
    if len(available) < 25:
        return None
    return {key: statistics.median(row["features"][key] for row in available) for key in keys}


from .runtime.gates import _gate


def load_dataset(symbol: str, observation_dir: Path, audit_dir: Path,
                 fee_bps: float) -> dict[str, list[dict]]:
    manifest = json.loads((audit_dir / "summary.json").read_text(encoding="utf-8"))
    if manifest.get("audit_schema_version") != 2 or manifest.get("hourly_timestamp_unit") != "epoch_ms_explicit":
        raise ValueError(f"{symbol}: v02 causal audit required")
    bars_by_day = _bars_by_day(observation_dir)
    available_days = sorted(bars_by_day)
    profiles = {(p["session_day"], p["profile_window"]): p for p in
                read_jsonl(observation_dir / "prepared_profiles.jsonl")}
    signals = {(r["session_day"], r["profile_window"]): r for window in PROFILE_WINDOWS
               for r in read_jsonl(observation_dir / window / "signals.jsonl")}
    snapshots = {(f["session_day"], f["profile_window"]): f for f in
                 read_jsonl(audit_dir / "feature_snapshots.jsonl")
                 if f["snapshot"] == "at_actual_signal"}
    result: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for row in read_jsonl(audit_dir / "setup_audit.jsonl"):
        if row["profile_window"] not in PROFILE_WINDOWS or row["actual_status"] != "executed":
            continue
        key = (row["session_day"], row["profile_window"])
        if key in seen:
            raise ValueError(f"{symbol}: duplicate executed candidate {key}")
        seen.add(key)
        if key not in snapshots or key not in profiles or key not in signals:
            raise ValueError(f"{symbol}: missing causal inputs for {key}")
        signal = signals[key]
        if signal["symbol"].upper() != symbol.upper():
            raise ValueError(f"{symbol}: observation symbol mismatch")
        if int(signal["feature_as_of_ms"]) != int(snapshots[key]["feature_as_of_ms"]):
            raise ValueError(f"{symbol}: signal snapshot mismatch {key}")
        if signal["direction"] != row["actual_direction"]:
            raise ValueError(f"{symbol}: direction mismatch {key}")
        features = candidate_features(row, snapshots[key], profiles[key],
                                      bars_by_day, available_days, fee_bps)
        path = row.get("actual_path") or {}
        result[row["profile_window"]].append({
            "symbol": symbol, "profile_window": row["profile_window"],
            "session_day": row["session_day"], "net_r": float(row["actual_net_r"]),
            "mfe_lower_pct": finite(path.get("mfe_before_stop_lower_pct")),
            "mfe_lower_atr": finite(path.get("mfe_before_stop_lower_atr")),
            "features": features,
            "sample_id": row.get("actual_sample_id"),
            "direction": row["actual_direction"],
            "feature_as_of_ms": int(snapshots[key]["feature_as_of_ms"]),
            "entry_ms": int(row["actual_entry_ms"]),
            "entry_price": float(row["actual_entry_price"]),
            "stop": float(row["actual_stop"]),
            "audit_context": snapshots[key],
            "profile": profiles[key],
            "path_outcome": path,
        })
    for rows in result.values():
        rows.sort(key=lambda item: item["session_day"])
    return result


def evaluate(rows: list[dict], *, train_days: int, test_days: int,
             embargo_days: int, min_train_trades: int) -> tuple[list[dict], list[dict]]:
    if not rows:
        return [], []
    first = date.fromisoformat(rows[0]["session_day"])
    last = date.fromisoformat(rows[-1]["session_day"])
    cursor = first + timedelta(days=train_days)
    predictions, folds = [], []
    fold_id = 0
    while cursor + timedelta(days=embargo_days) <= last:
        train_start = cursor - timedelta(days=train_days)
        test_start = cursor + timedelta(days=embargo_days)
        test_end = test_start + timedelta(days=test_days)
        train = [r for r in rows if train_start <= date.fromisoformat(r["session_day"]) < cursor]
        test = [r for r in rows if test_start <= date.fromisoformat(r["session_day"]) < test_end]
        if len(train) >= min_train_trades and test:
            fold_id += 1
            for plug in PLUGS:
                limits = _thresholds(train, plug)
                if limits is None:
                    continue
                fold_rows = []
                for row in test:
                    gate = _gate(row["features"], plug, limits)
                    # Missing features preserve the baseline trade, never silently reject it.
                    take = gate is not False
                    record = {key: row[key] for key in ("symbol", "profile_window", "session_day")}
                    record.update(fold=fold_id, plug=plug, gate=gate,
                                  baseline_net_r=row["net_r"],
                                  gated_net_r=row["net_r"] if take else 0.0,
                                  mfe_lower_pct=row["mfe_lower_pct"],
                                  mfe_lower_atr=row["mfe_lower_atr"],
                                  features=row["features"], thresholds=limits)
                    predictions.append(record)
                    fold_rows.append(record)
                folds.append({
                    "symbol": test[0]["symbol"], "profile_window": test[0]["profile_window"],
                    "plug": plug, "fold": fold_id,
                    "train_start": str(train_start), "train_end_exclusive": str(cursor),
                    "test_start": str(test_start), "test_end_exclusive": str(test_end),
                    "train_trades": len(train), "test_trades": len(test),
                    "thresholds": limits,
                    "baseline_sum_r": sum(r["baseline_net_r"] for r in fold_rows),
                    "gated_sum_r": sum(r["gated_net_r"] for r in fold_rows),
                    "rejected": sum(r["gate"] is False for r in fold_rows),
                    "unknown": sum(r["gate"] is None for r in fold_rows),
                })
        cursor += timedelta(days=test_days)
    return predictions, folds


def summarize(predictions: list[dict], folds: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in predictions:
        grouped[(row["symbol"], row["profile_window"], row["plug"])].append(row)
    results = []
    for (symbol, profile, plug), rows in sorted(grouped.items()):
        rejected = [r for r in rows if r["gate"] is False]
        retained = [r for r in rows if r["gate"] is not False]
        baseline = sum(r["baseline_net_r"] for r in rows)
        gated = sum(r["gated_net_r"] for r in rows)
        related_folds = [f for f in folds if (f["symbol"], f["profile_window"], f["plug"])
                         == (symbol, profile, plug)]
        wins = [r["baseline_net_r"] for r in retained if r["baseline_net_r"] > 0]
        losses = [r["baseline_net_r"] for r in retained if r["baseline_net_r"] < 0]
        tail = lambda row: row["mfe_lower_pct"] is not None and row["mfe_lower_pct"] >= 2
        baseline_tails = sum(tail(r) for r in rows)
        results.append({
            "symbol": symbol, "profile_window": profile, "plug": plug,
            "oos_folds": len(related_folds), "oos_baseline_trades": len(rows),
            "rejected": len(rejected),
            "unknown_preserved": sum(r["gate"] is None for r in rows),
            "rejected_losers": sum(r["baseline_net_r"] < 0 for r in rejected),
            "rejected_winners": sum(r["baseline_net_r"] > 0 for r in rejected),
            "baseline_tails_2pct": baseline_tails,
            "rejected_tails_2pct": sum(tail(r) for r in rejected),
            "tail_retention_fraction": ((baseline_tails - sum(tail(r) for r in rejected))
                                        / baseline_tails if baseline_tails else None),
            "baseline_sum_r": baseline, "gated_sum_r": gated,
            "delta_r_per_baseline_opportunity": (gated - baseline) / len(rows),
            "retained_win_rate": len(wins) / len(retained) if retained else None,
            "retained_profit_factor": (sum(wins) / -sum(losses) if losses else None),
            "positive_delta_folds": sum(f["gated_sum_r"] > f["baseline_sum_r"]
                                        for f in related_folds),
            "negative_delta_folds": sum(f["gated_sum_r"] < f["baseline_sum_r"]
                                        for f in related_folds),
        })
    return results


def cross_asset_stability(summary: list[dict]) -> list[dict]:
    """Equal-weight pair diagnostics; never pool correlated asset-days as IID."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in summary:
        grouped[(row["profile_window"], row["plug"])].append(row)
    output = []
    for (profile, plug), assets in sorted(grouped.items()):
        deltas = [float(row["delta_r_per_baseline_opportunity"]) for row in assets]
        retained = [row["tail_retention_fraction"] for row in assets
                    if row["tail_retention_fraction"] is not None]
        output.append({
            "profile_window": profile, "plug": plug,
            "measured_assets": len(assets),
            "assets_with_positive_delta": sum(delta > 0 for delta in deltas),
            "assets_with_negative_delta": sum(delta < 0 for delta in deltas),
            "median_asset_delta_r_per_opportunity": statistics.median(deltas),
            "worst_asset_delta_r_per_opportunity": min(deltas),
            "lowest_asset_tail_retention_fraction": min(retained) if retained else None,
            "assets": ",".join(row["symbol"] for row in assets),
        })
    return output


def export_context(study_manifest: Path, caches: list[list[str]], output_dir: Path) -> dict:
    """Enrich existing candidates, never refit gates or replay raw trades."""
    import pandas as pd
    from .audit_opportunity import (
        _finite, _write_jsonl, load_minutes, trend_indicators_15m, trend_snapshot_15m,
    )

    if (output_dir / "manifest.json").exists():
        raise FileExistsError(f"Use a new output directory: {output_dir}")
    study = json.loads(study_manifest.read_text(encoding="utf-8"))
    datasets = study["datasets"]
    symbols = [item["symbol"].upper() for item in datasets]
    if len(set(symbols)) != len(symbols):
        raise ValueError("Duplicate study dataset symbols")
    cache_map = defaultdict(list)
    for symbol, path in caches:
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        cache_map[symbol.upper()].append(Path(path))
    if set(cache_map) != set(symbols):
        raise ValueError(f"Cache symbols must match study symbols: {symbols}")

    def source_path(value: str) -> Path:
        # One explicit migration only. Never choose an approximate run by name.
        return Path(value.replace("\\", "/").replace(
            "models/daily_profile_1h/", "models/trend_following_preny_profile_15m/"))

    historical = defaultdict(dict)
    for row in read_jsonl(study_manifest.parent / "predictions.jsonl"):
        if row["profile_window"] not in PROFILE_WINDOWS:
            continue
        key = (row["symbol"].upper(), row["profile_window"], row["session_day"])
        if row["plug"] in historical[key]:
            raise ValueError(f"Duplicate historical gate: {key}, {row['plug']}")
        historical[key][row["plug"]] = row

    combined, coverage, sources = [], [], []
    for item in datasets:
        symbol = item["symbol"].upper()
        obs, audit = source_path(item["observation_dir"]), source_path(item["audit_dir"])
        print(f"{symbol}: reading existing audit and minute cache", flush=True)
        candidates = load_dataset(symbol, obs, audit, float(item["fee_bps"]))
        minutes = load_minutes(cache_map[symbol])
        indicators = trend_indicators_15m(minutes)
        del minutes
        asset_rows = []
        for rows in candidates.values():
            for row in rows:
                key = (symbol, row["profile_window"], row["session_day"])
                gates = {}
                for plug, previous in historical.get(key, {}).items():
                    if previous["features"] != row["features"] or previous["baseline_net_r"] != row["net_r"]:
                        raise ValueError(f"Existing study inputs changed: {key}; refusing to mix versions")
                    gates[plug] = {name: previous[name] for name in ("gate", "fold", "thresholds")}
                context = trend_snapshot_15m(indicators, row["feature_as_of_ms"], row["direction"])
                record = {name: row[name] for name in (
                    "symbol", "profile_window", "session_day", "sample_id", "direction",
                    "feature_as_of_ms", "entry_ms", "entry_price", "stop", "features", "audit_context", "profile")}
                record.update(trend_15m=context, research_gates=gates,
                              outcomes={"net_r": row["net_r"], "path": row["path_outcome"]})
                asset_rows.append(record)
        combined.extend(asset_rows)
        coverage.append({"symbol": symbol, "candidates": len(asset_rows),
                         **{status: sum(r["trend_15m"]["status"] == status for r in asset_rows)
                            for status in ("ready", "warmup", "missing_completed_bar")}})
        sources.append({"symbol": symbol, "observation_dir": str(obs), "audit_dir": str(audit),
                        "minute_caches": [{"path": str(p.resolve()), "bytes": p.stat().st_size,
                                           "mtime_ns": p.stat().st_mtime_ns} for p in cache_map[symbol]]})
    combined.sort(key=lambda r: (r["session_day"], r["symbol"]))
    manifest = {
        "schema_version": 1, "purpose": "observation_only_no_entry_changes",
        "source_study_manifest": str(study_manifest.resolve()), "sources": sources,
        "scope": "All executed Pre-NY baseline candidates in source audits, including initial training dates; not no-trade sessions",
        "trend_timeframe": "15m all-hours completed bars only",
        "indicators": {"ema": 200, "sma": 20, "adx_wilder": 14, "slope_bars": 4},
        "initialization": "SMA seed for EMA/Wilder; no future backfill; full missing 15m bar resets warmup",
        "coverage_policy": "Partial bars use observed prices; coverage exported; missing context never rejects entries",
        "original_plugs": PLUGS,
        "legacy_units": "Existing audit_context EMA/ADX/ATR remain 1h; original six-plug features unchanged",
        "feature_timing": "trend_15m and audit_context use completed signal-time bars; fee_stop_burden uses actual fill geometry, not a pre-fill feature",
        "outcomes": "net_r from existing audit; path MFE bounds from minute candles before original stop, not raw-fill target sweep",
        "research_gates": "Copied from prior study only; absent means no historical test-fold gate, not rejection",
        "validation_warning": "Descriptive enrichment only; prior-inspected data is not untouched OOS; no threshold selected",
        "coverage": coverage,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "candidate_context.jsonl", combined)
    pd.json_normalize(_finite(combined), sep="__").to_csv(output_dir / "candidate_context.csv", index=False)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"output_dir": str(output_dir), "coverage": coverage}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", nargs=4, action="append", metavar=("SYMBOL", "OBS_DIR", "AUDIT_DIR", "FEE_BPS"),
                        help="Repeat for each pair; profile windows stay separate")
    parser.add_argument("--context-only", action="store_true", help="Enrich existing candidates; no gate fitting or replay")
    parser.add_argument("--effort-result-context", type=Path,
                        help="Annotate an existing context directory with stateful effort/result; no entry changes")
    parser.add_argument("--study-manifest", type=Path)
    parser.add_argument("--context-cache", nargs=2, action="append", metavar=("SYMBOL", "MINUTE_PARQUET"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--embargo-days", type=int, default=1)
    parser.add_argument("--min-train-trades", type=int, default=25)
    args = parser.parse_args()
    if args.effort_result_context:
        if args.context_only or args.study_manifest or args.context_cache or args.dataset:
            parser.error("--effort-result-context is a separate annotation mode; use it with --output-dir only")
        from .effort_result import export_effort_result
        print(json.dumps(export_effort_result(args.effort_result_context, args.output_dir), indent=2))
        return
    if args.context_only:
        if not args.study_manifest or not args.context_cache or args.dataset:
            parser.error("context-only requires --study-manifest and --context-cache, without --dataset")
        print(json.dumps(export_context(args.study_manifest, args.context_cache, args.output_dir), indent=2))
        return
    if not args.dataset or args.study_manifest or args.context_cache:
        parser.error("normal plug study requires --dataset; context options require --context-only")
    if min(args.train_days, args.test_days, args.min_train_trades) <= 0 or args.embargo_days < 1:
        parser.error("train/test/min-trades must be positive and embargo must be >= 1 day")
    predictions, folds = [], []
    seen_symbols = set()
    for symbol, obs, audit, fee_text in args.dataset:
        if symbol.upper() in seen_symbols:
            parser.error(f"Duplicate dataset symbol: {symbol}")
        seen_symbols.add(symbol.upper())
        fee = float(fee_text)
        if not math.isfinite(fee) or fee < 0:
            parser.error("FEE_BPS must be a finite nonnegative number")
        dataset = load_dataset(symbol.upper(), Path(obs), Path(audit), fee)
        for profile in PROFILE_WINDOWS:
            part, part_folds = evaluate(dataset.get(profile, []),
                                        train_days=args.train_days, test_days=args.test_days,
                                        embargo_days=args.embargo_days,
                                        min_train_trades=args.min_train_trades)
            predictions.extend(part)
            folds.extend(part_folds)
    summary = summarize(predictions, folds)
    cross_asset = cross_asset_stability(summary)
    symbol_days: dict[str, list[str]] = defaultdict(list)
    for row in predictions:
        symbol_days[row["symbol"]].append(row["session_day"])
    common_start = (max(min(days) for days in symbol_days.values())
                    if len(symbol_days) == len(seen_symbols) and len(symbol_days) > 1 else None)
    common_end = (min(max(days) for days in symbol_days.values())
                  if common_start is not None else None)
    common_predictions = ([row for row in predictions
                           if common_start <= row["session_day"] <= common_end]
                          if common_start is not None and common_start <= common_end else [])
    common_summary = summarize(common_predictions, [])
    common_stability = cross_asset_stability(common_summary)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("predictions.jsonl", predictions), ("folds.jsonl", folds)):
        with (args.output_dir / name).open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, allow_nan=False, sort_keys=True) + "\n")
    if summary:
        with (args.output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
    if cross_asset:
        with (args.output_dir / "cross_asset_stability.csv").open(
                "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(cross_asset[0]))
            writer.writeheader()
            writer.writerows(cross_asset)
    if common_summary:
        with (args.output_dir / "common_period_summary.csv").open(
                "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(common_summary[0]))
            writer.writeheader()
            writer.writerows(common_summary)
        with (args.output_dir / "common_period_stability.csv").open(
                "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(common_stability[0]))
            writer.writeheader()
            writer.writerows(common_stability)
    manifest = {
        "datasets": [{"symbol": row[0], "observation_dir": row[1],
                      "audit_dir": row[2], "fee_bps": float(row[3])} for row in args.dataset],
        "train_days": args.train_days, "test_days": args.test_days,
        "embargo_days": args.embargo_days, "min_train_trades": args.min_train_trades,
        "plugs": PLUGS, "threshold_policy": "train-fold median only; no grid search",
        "missing_feature_policy": "preserve baseline trade",
        "execution_policy": "baseline actual net R or zero R if research gate rejects",
        "not_an_equity_backtest": True,
        "comparability_note": "Equal-weight asset diagnostics; profile portfolios never combined.",
        "common_oos_start": common_start,
        "common_oos_end": common_end,
        "common_period_note": "Report-only overlapping test dates; thresholds still use each pair's earlier training data.",
        "validation_warning": "Previously inspected data is not untouched out-of-sample evidence.",
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(predictions), "folds": len(folds),
                      "summary_rows": len(summary), "output_dir": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
