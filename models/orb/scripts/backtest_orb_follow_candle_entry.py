from __future__ import annotations

import argparse
import json
from dataclasses import replace
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from orb_live_agent.config import load_config
from orb_live_agent.fast_orb_backtest import _stamp, _trades_from_orders, _write_jsonl
from orb_live_agent.feature_cache import build_or_load_feature_set, parse_date
from orb_live_agent.paper_broker import PaperBroker
from orb_live_agent.risk_gate import RiskGate

RULE = (
    "reject only when the first completed follow candle has an adverse/flat body, "
    "closes inside the ORB, and has adverse/flat directional delta"
)
ENTRY_FAMILIES = {
    "immediate_expansion": "immediate_expansion_observation",
    "retest_continuation": "retest_extreme_then_continuation",
    "held_outside_pullback": "held_outside_pullback_then_continuation",
}


def _reject_follow(direction: str, open_: float, close: float, breakout_level: float, delta: float) -> bool:
    sign = 1.0 if direction == "long" else -1.0
    return sign * (close - open_) <= 0 and sign * (close - breakout_level) <= 0 and sign * delta <= 0


def _timestamp_ms(values: pd.Series) -> pd.Series:
    return values.map(lambda value: int(value.timestamp() * 1000) if pd.notna(value) else pd.NA).astype("Int64")


def _stop_price(direction: str, stop_model: str, poc: float, profile_low: float, profile_high: float) -> float:
    if stop_model == "poc":
        return poc
    if stop_model == "opposite_extreme":
        return profile_low if direction == "long" else profile_high
    raise ValueError(f"Unsupported stop model: {stop_model}")


def _failure_rejects(entry_family: str, enabled: bool, follow_reject: bool) -> bool:
    return enabled and entry_family != "immediate_expansion" and follow_reject


def _observe_family_patterns(sample: Any, candles: pd.DataFrame) -> list[dict[str, Any]]:
    rows = candles.sort_values("minutes_from_breakout_candle", kind="mergesort").to_dict("records")
    if len(rows) < 2 or int(rows[0]["minutes_from_breakout_candle"]) != 0:
        return []
    direction = str(sample.direction)
    sign = 1.0 if direction == "long" else -1.0
    level = float(sample.breakout_level)
    risk = float(sample.risk_abs)
    breakout = rows[0]

    def outside(row: dict[str, Any]) -> bool:
        return sign * (float(row["close"]) - level) > 0

    def touches(row: dict[str, Any]) -> bool:
        return float(row["low"]) <= level if direction == "long" else float(row["high"]) >= level

    def away(row: dict[str, Any]) -> float:
        return float(row["high"] if direction == "long" else row["low"])

    def toward(row: dict[str, Any]) -> float:
        return float(row["low"] if direction == "long" else row["high"])

    def candidate(family: str, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_day": sample.session_day,
            "breakout_time": sample.breakout_time,
            "entry_family": family,
            "entry_time": entry["orderflow_candle_start_time"],
            "breakout_close_expansion_r": sign * (float(breakout["close"]) - level) / risk,
            "breakout_volume_expansion": breakout["volume_expansion_ratio"],
        }

    found: list[dict[str, Any]] = []
    if outside(breakout) and sign * (float(breakout["close"]) - float(breakout["open"])) > 0:
        found.append(candidate("immediate_expansion", rows[1]))

    for i in range(1, len(rows) - 1):
        reference = rows[i]
        if not touches(reference):
            continue
        trigger = away(reference)
        for j in range(i + 1, len(rows) - 1):
            confirmation = rows[j]
            if outside(confirmation) and sign * (float(confirmation["close"]) - trigger) > 0:
                found.append(candidate("retest_continuation", rows[j + 1]))
                break
        if any(row["entry_family"] == "retest_continuation" for row in found):
            break

    if outside(breakout):
        for i in range(1, len(rows) - 1):
            reference = rows[i]
            if not outside(reference):
                break
            previous = rows[i - 1]
            held_outside = sign * (toward(reference) - level) > 0
            pulled_back = sign * (float(reference["close"]) - float(previous["close"])) < 0
            if not (held_outside and pulled_back):
                continue
            trigger = away(reference)
            for j in range(i + 1, len(rows) - 1):
                confirmation = rows[j]
                if not outside(confirmation):
                    break
                if sign * (float(confirmation["close"]) - trigger) > 0:
                    found.append(candidate("held_outside_pullback", rows[j + 1]))
                    break
            break
    return found


@lru_cache(maxsize=2)
def _family_source(observation_dir_text: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    observation_dir = Path(observation_dir_text)
    metadata = pd.DataFrame(
        json.loads(line)
        for line in (observation_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    metadata = metadata[metadata.get("sample", False).eq(True)][
        [
            "session_day",
            "breakout_time",
            "direction",
            "breakout_level",
            "risk_abs",
            "result",
            "outcome_time",
            "poc_price",
            "profile_low",
            "profile_high",
        ]
    ].copy()
    path = pd.read_parquet(observation_dir / "orderflow_path.parquet")
    for frame in (metadata, path):
        frame["breakout_time"] = pd.to_datetime(frame["breakout_time"], format="mixed", utc=True, errors="coerce")
    metadata["outcome_time"] = pd.to_datetime(metadata["outcome_time"], format="mixed", utc=True, errors="coerce")
    lookup = metadata.set_index(["session_day", "breakout_time"])
    observed: list[dict[str, Any]] = []
    for key, candles in path.groupby(["session_day", "breakout_time"], sort=False):
        if key in lookup.index:
            sample = lookup.loc[key].copy()
            sample["session_day"], sample["breakout_time"] = key
            observed.extend(_observe_family_patterns(sample, candles))
    return metadata, path, pd.DataFrame(observed)


def _family_candidates(
    observation_dir: Path,
    stop_model: str,
    entry_families: list[str],
    use_failure_filter: bool,
    immediate_min_price_expansion_r: float,
    immediate_min_volume_expansion: float,
) -> pd.DataFrame:
    metadata, path, patterns = (frame.copy() for frame in _family_source(str(observation_dir.resolve())))
    metadata["stop_loss"] = [
        _stop_price(row.direction, stop_model, row.poc_price, row.profile_low, row.profile_high)
        for row in metadata.itertuples()
    ]

    patterns = patterns[patterns["entry_family"].isin(entry_families)].copy()
    patterns["entry_time"] = pd.to_datetime(patterns["entry_time"], format="mixed", utc=True, errors="coerce")
    patterns["immediate_threshold_pass"] = True
    immediate = patterns["entry_family"].eq("immediate_expansion")
    patterns.loc[immediate, "immediate_threshold_pass"] = (
        patterns.loc[immediate, "breakout_close_expansion_r"].ge(immediate_min_price_expansion_r)
        & patterns.loc[immediate, "breakout_volume_expansion"].ge(immediate_min_volume_expansion)
    )

    follow = path[path["minutes_from_breakout_candle"].eq(1)][
        ["session_day", "breakout_time", "open", "high", "low", "close", "directional_delta_ratio"]
    ].rename(
        columns={
            "open": "follow_open",
            "high": "follow_high",
            "low": "follow_low",
            "close": "follow_close",
            "directional_delta_ratio": "follow_directional_delta",
        }
    )
    patterns = patterns.merge(metadata, on=["session_day", "breakout_time"], how="left", validate="many_to_one")
    patterns = patterns.merge(follow, on=["session_day", "breakout_time"], how="left", validate="many_to_one")
    patterns["follow_reject"] = [
        _reject_follow(row.direction, row.follow_open, row.follow_close, row.breakout_level, row.follow_directional_delta)
        if pd.notna(row.follow_open) and pd.notna(row.follow_directional_delta)
        else False
        for row in patterns.itertuples()
    ]
    patterns["failure_rejected"] = [
        _failure_rejects(row.entry_family, use_failure_filter, row.follow_reject)
        for row in patterns.itertuples()
    ]

    path_groups = {
        key: candles.sort_values("minutes_from_breakout_candle", kind="mergesort")
        for key, candles in path.groupby(["session_day", "breakout_time"], sort=False)
    }
    invalidated: list[bool] = []
    for row in patterns.itertuples():
        candles = path_groups[(row.session_day, row.breakout_time)]
        complete = pd.to_datetime(candles["orderflow_candle_complete_time"], format="mixed", utc=True, errors="coerce")
        prior = candles[candles["minutes_from_breakout_candle"].ge(1) & complete.le(row.entry_time)]
        touched = (
            prior["low"].le(row.stop_loss).any()
            if row.direction == "long"
            else prior["high"].ge(row.stop_loss).any()
        )
        if stop_model == "poc":
            touched = touched or (row.result == "loss" and pd.notna(row.outcome_time) and row.outcome_time <= row.entry_time)
        invalidated.append(bool(touched))
    patterns["invalidated_before_entry"] = invalidated
    patterns["candidate_eligible"] = (
        patterns["immediate_threshold_pass"]
        & ~patterns["failure_rejected"]
        & ~patterns["invalidated_before_entry"]
        & patterns["entry_time"].notna()
        & patterns["stop_loss"].notna()
    )
    patterns["post_original_1r_entry"] = (
        patterns["result"].eq("win") & patterns["outcome_time"].le(patterns["entry_time"])
    )

    priority = {"immediate_expansion": 0, "retest_continuation": 1, "held_outside_pullback": 2}
    eligible = patterns[patterns["candidate_eligible"]].copy()
    eligible["family_priority"] = eligible["entry_family"].map(priority)
    selected = eligible.sort_values(
        ["session_day", "breakout_time", "entry_time", "family_priority"], kind="mergesort"
    ).drop_duplicates(["session_day", "breakout_time"], keep="first")

    base = metadata[["session_day", "breakout_time", "direction", "stop_loss"]].copy()
    selected = selected[
        ["session_day", "breakout_time", "entry_time", "entry_family", "post_original_1r_entry"]
    ]
    rows = base.merge(selected, on=["session_day", "breakout_time"], how="left", validate="one_to_one")
    blocked = patterns.groupby(["session_day", "breakout_time"], sort=False).agg(
        had_pattern=("entry_family", "size"),
        failure_rejected=("failure_rejected", "max"),
        stop_invalidated=("invalidated_before_entry", "max"),
    )
    rows = rows.merge(blocked, on=["session_day", "breakout_time"], how="left")
    rows["eligible"] = rows["entry_time"].notna()
    rows["missing_follow_or_entry"] = rows["had_pattern"].fillna(0).eq(0)
    rows["reject_candidate"] = rows["failure_rejected"].fillna(False) & ~rows["eligible"]
    rows["invalidated_before_entry"] = rows["stop_invalidated"].fillna(False) & ~rows["eligible"]
    rows["post_original_1r_entry"] = rows["post_original_1r_entry"].fillna(False)
    rows["entry_timestamp_ms"] = _timestamp_ms(rows["entry_time"])
    return rows.sort_values(["session_day", "breakout_time"], kind="mergesort").reset_index(drop=True)


def _candidates(observation_dir: Path, stop_model: str = "poc") -> pd.DataFrame:
    samples = pd.DataFrame(
        json.loads(line)
        for line in (observation_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    samples = samples[samples.get("sample", False).eq(True)][
        [
            "session_day",
            "breakout_time",
            "direction",
            "breakout_level",
            "poc_price",
            "profile_low",
            "profile_high",
            "result",
            "outcome_time",
        ]
    ]
    path = pd.read_parquet(observation_dir / "orderflow_path.parquet")
    follow = path[path["minutes_from_breakout_candle"].eq(1)][
        [
            "session_day",
            "breakout_time",
            "open",
            "high",
            "low",
            "close",
            "directional_delta_ratio",
            "orderflow_candle_complete_time",
        ]
    ].rename(
        columns={
            "open": "follow_open",
            "high": "follow_high",
            "low": "follow_low",
            "close": "follow_close",
            "directional_delta_ratio": "follow_directional_delta",
            "orderflow_candle_complete_time": "follow_complete_time",
        }
    )
    entry = path[path["minutes_from_breakout_candle"].eq(2)][
        ["session_day", "breakout_time", "open", "orderflow_candle_start_time"]
    ].rename(columns={"open": "entry_open", "orderflow_candle_start_time": "entry_time"})

    rows = samples.merge(follow, on=["session_day", "breakout_time"], how="left", validate="one_to_one")
    rows = rows.merge(entry, on=["session_day", "breakout_time"], how="left", validate="one_to_one")
    rows["stop_loss"] = [
        _stop_price(row.direction, stop_model, row.poc_price, row.profile_low, row.profile_high)
        for row in rows.itertuples()
    ]
    for column in ("outcome_time", "follow_complete_time", "entry_time"):
        rows[column] = pd.to_datetime(rows[column], format="mixed", utc=True, errors="coerce")
    required = [
        "follow_open",
        "follow_close",
        "follow_directional_delta",
        "follow_complete_time",
        "entry_open",
        "entry_time",
        "stop_loss",
    ]
    rows["missing_follow_or_entry"] = rows[required].isna().any(axis=1)
    valid = ~rows["missing_follow_or_entry"]
    rows["reject_candidate"] = False
    rows.loc[valid, "reject_candidate"] = [
        _reject_follow(str(row.direction), row.follow_open, row.follow_close, row.breakout_level, row.follow_directional_delta)
        for row in rows.loc[valid].itertuples()
    ]
    poc_invalidated = rows["result"].eq("loss") & rows["outcome_time"].le(rows["entry_time"])
    if stop_model == "poc":
        rows["invalidated_before_entry"] = poc_invalidated
    else:
        rows["invalidated_before_entry"] = np.where(
            rows["direction"].eq("long"),
            rows["follow_low"].le(rows["stop_loss"]),
            rows["follow_high"].ge(rows["stop_loss"]),
        )
    rows["post_original_1r_entry"] = rows["result"].eq("win") & rows["outcome_time"].le(rows["entry_time"])
    rows["eligible"] = ~(
        rows["missing_follow_or_entry"] | rows["reject_candidate"] | rows["invalidated_before_entry"]
    )
    rows["entry_timestamp_ms"] = _timestamp_ms(rows["entry_time"])
    return rows.sort_values(["session_day", "breakout_time"], kind="mergesort").reset_index(drop=True)


def _run_variant(
    config: Any,
    features: Any,
    candidates: pd.DataFrame,
    output_dir: Path,
) -> dict[str, Any]:
    eligible = candidates[candidates["eligible"]].copy()
    if eligible["entry_timestamp_ms"].duplicated().any():
        raise ValueError("More than one eligible entry candidate shares a candle")
    signals = {int(row.entry_timestamp_ms): row for row in eligible.itertuples()}

    tz = ZoneInfo(config.session_timezone)
    gate = RiskGate(config.paper_min_stop_risk_pct, config.paper_max_stop_risk_pct)
    broker = PaperBroker(config)
    orders: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for candle in features.candles:
        candle_ms = int(candle["timestamp_ms"])
        if broker.position and broker.position.max_hold_exit_ms is not None and candle_ms >= broker.position.max_hold_exit_ms:
            force_trade = features.force_exit_trades.get(int(broker.position.max_hold_exit_ms))
            if force_trade:
                orders.extend(_stamp(broker.on_trade(force_trade), int(force_trade["timestamp"]), tz))

        signal = signals.get(candle_ms)
        if signal is not None:
            decision = {
                "decision": "TAKE",
                "entry_model": "trend",
                "strategy": "orb_follow_candle_reject",
                "direction": str(signal.direction),
                "entry": float(candle["open"]),
                "stop_loss": float(signal.stop_loss),
                "reason": RULE,
                "snapshot_timestamp_ms": candle_ms,
            }
            gate_result = gate.validate(decision, broker.has_open_position())
            decisions.append({"decision": decision, "gate": gate_result})
            event = broker.on_decision(decision, gate_result)
            if event:
                orders.extend(_stamp([event], candle_ms, tz))

        orders.extend(_stamp(broker.on_candle(candle), candle_ms, tz))

    trades = _trades_from_orders(orders)
    r_values = [float(trade["r"]) for trade in trades if trade.get("r") is not None]
    summary = {
        "event": "orb_follow_candle_entry_backtest_finished",
        "rule": RULE,
        "entry_timing": "open of the candle after the completed follow candle",
        "samples": len(candidates),
        "missing_follow_or_entry": int(candidates["missing_follow_or_entry"].sum()),
        "rejected_by_three_condition_rule": int(candidates["reject_candidate"].sum()),
        "invalidated_before_entry": int(candidates["invalidated_before_entry"].sum()),
        "eligible_candidates": len(eligible),
        "post_original_1r_candidates": int(eligible["post_original_1r_entry"].sum()),
        "trades_taken": len(trades),
        "wins": sum(float(trade["pnl"]) > 0 for trade in trades),
        "losses": sum(float(trade["pnl"]) <= 0 for trade in trades),
        "win_rate": sum(float(trade["pnl"]) > 0 for trade in trades) / len(trades) if trades else None,
        "expectancy_r_net": float(np.mean(r_values)) if r_values else None,
        "net_pnl": float(sum(float(trade["pnl"]) for trade in trades)),
        "final_equity": broker.equity,
        "fee_bps": config.paper_fee_bps,
        "slippage_bps": config.paper_slippage_bps,
        "risk_fraction": config.paper_risk_fraction,
        "stop_model": config.orb_stop_model,
        "tp1_r": config.paper_tp1_r,
        "tp1_fraction": config.paper_tp1_fraction,
        "exit_mode": config.paper_exit_mode,
        "output_dir": str(output_dir),
    }
    if "entry_family" in eligible.columns:
        summary["entry_families"] = {
            str(family): int(count)
            for family, count in eligible["entry_family"].value_counts().items()
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_parquet(output_dir / "entry_candidates.parquet", index=False)
    _write_jsonl(output_dir / "decisions.jsonl", decisions)
    _write_jsonl(output_dir / "paper_orders.jsonl", orders)
    _write_jsonl(output_dir / "trades.jsonl", trades)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _load_features(cache_dir: Path, config: Any) -> Any:
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    return build_or_load_feature_set(
        Path(manifest["input"]),
        parse_date(manifest["start_date"]),
        parse_date(manifest["end_date"]),
        config,
        cache_dir,
        use_cache=True,
    )


def run(observation_dir: Path, cache_dir: Path, output_dir: Path) -> dict[str, Any]:
    config = load_config()
    return _run_variant(config, _load_features(cache_dir, config), _candidates(observation_dir, config.orb_stop_model), output_dir)


def run_sweep(
    observation_dir: Path,
    cache_dir: Path,
    output_dir: Path,
    stop_models: list[str],
    tp1_values: list[float],
    entry_families: list[str] | None = None,
    use_failure_filter: bool = False,
    immediate_min_price_expansion_r: float = 0.0,
    immediate_min_volume_expansion: float = 0.0,
) -> list[dict[str, Any]]:
    base = load_config()
    features = _load_features(cache_dir, base)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    candidates_by_stop = {
        model: (
            _family_candidates(
                observation_dir,
                model,
                entry_families,
                use_failure_filter,
                immediate_min_price_expansion_r,
                immediate_min_volume_expansion,
            )
            if entry_families
            else _candidates(observation_dir, model)
        )
        for model in stop_models
    }
    for stop_model, tp1_r in product(stop_models, tp1_values):
        config = replace(base, orb_stop_model=stop_model, paper_tp1_r=tp1_r)
        label = f"{stop_model}_tp1_{tp1_r:g}r"
        summary = _run_variant(config, features, candidates_by_stop[stop_model], output_dir / label)
        results.append(
            {
                "variant": label,
                "enabled_entry_families": entry_families or ["follow_candle_entry"],
                "failure_filter_enabled": use_failure_filter,
                "immediate_min_price_expansion_r": immediate_min_price_expansion_r,
                "immediate_min_volume_expansion": immediate_min_volume_expansion,
                **summary,
            }
        )
        print(json.dumps({"event": "variant_finished", "variant": label, **summary}, sort_keys=True))
    (output_dir / "sweep_summary.json").write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# ORB Follow-Candle Entry Sweep",
        "",
        "| stop | TP1 | trades | WR | net expectancy R | final equity |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in results:
        wr = "" if row["win_rate"] is None else f"{row['win_rate']:.4f}"
        exp = "" if row["expectancy_r_net"] is None else f"{row['expectancy_r_net']:.4f}"
        lines.append(
            f"| {row['stop_model']} | {row['tp1_r']:g}R | {row['trades_taken']} | {wr} | {exp} | {row['final_equity']:.4f} |"
        )
    (output_dir / "sweep_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the ORB follow-candle three-condition reject rule.")
    parser.add_argument("--observation-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--stop-model", action="append", choices=["poc", "opposite_extreme"])
    parser.add_argument("--tp1-r", action="append", type=float)
    parser.add_argument("--entry-family", action="append", choices=sorted(ENTRY_FAMILIES))
    parser.add_argument("--use-failure-filter", action="store_true")
    parser.add_argument("--immediate-min-price-expansion-r", type=float, default=0.0)
    parser.add_argument("--immediate-min-volume-expansion", type=float, default=0.0)
    args = parser.parse_args()
    if args.stop_model or args.tp1_r or args.entry_family:
        results = run_sweep(
            Path(args.observation_dir),
            Path(args.cache_dir),
            Path(args.output_dir),
            args.stop_model or [load_config().orb_stop_model],
            args.tp1_r or [load_config().paper_tp1_r],
            args.entry_family,
            args.use_failure_filter,
            args.immediate_min_price_expansion_r,
            args.immediate_min_volume_expansion,
        )
        print(json.dumps({"event": "sweep_finished", "variants": len(results), "output_dir": args.output_dir}, indent=2))
    else:
        print(json.dumps(run(Path(args.observation_dir), Path(args.cache_dir), Path(args.output_dir)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
