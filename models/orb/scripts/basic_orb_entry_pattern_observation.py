from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _read(observation_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    samples_path = observation_dir / "samples.jsonl"
    path_path = observation_dir / "orderflow_path.parquet"
    if not samples_path.exists() or not path_path.exists():
        raise FileNotFoundError("Observation directory must contain samples.jsonl and orderflow_path.parquet")

    samples = pd.DataFrame(
        json.loads(line)
        for line in samples_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    samples = samples[samples.get("sample", False).eq(True)].copy()
    required_samples = {
        "session_day",
        "breakout_time",
        "breakout_level",
        "stop_loss",
        "risk_abs",
        "direction",
        "result",
        "path_end_time",
        "max_favorable_r_before_invalidation",
    }
    required_path = {
        "session_day",
        "breakout_time",
        "minutes_from_breakout_candle",
        "orderflow_candle_start_time",
        "orderflow_candle_complete_time",
        "open",
        "high",
        "low",
        "close",
        "volume_expansion_ratio",
        "directional_delta_ratio",
        "directional_cvd_ratio_30",
        "directional_bubble_qty_imbalance",
    }
    missing_samples = required_samples - set(samples.columns)
    path = pd.read_parquet(path_path)
    missing_path = required_path - set(path.columns)
    if missing_samples or missing_path:
        raise ValueError(
            f"Missing observation columns: samples={sorted(missing_samples)}, path={sorted(missing_path)}"
        )

    samples = samples[list(required_samples)].copy()
    for frame, column in (
        (samples, "breakout_time"),
        (samples, "path_end_time"),
        (path, "breakout_time"),
        (path, "orderflow_candle_start_time"),
        (path, "orderflow_candle_complete_time"),
    ):
        frame[column] = pd.to_datetime(frame[column], format="mixed", utc=True, errors="coerce")
    return samples, path


def _outside(row: pd.Series, level: float, sign: float) -> bool:
    return sign * (float(row["close"]) - level) > 0


def _touches_level(row: pd.Series, level: float, direction: str) -> bool:
    if direction == "long":
        return float(row["low"]) <= level
    return float(row["high"]) >= level


def _away_extreme(row: pd.Series, direction: str) -> float:
    return float(row["high"] if direction == "long" else row["low"])


def _toward_extreme(row: pd.Series, direction: str) -> float:
    return float(row["low"] if direction == "long" else row["high"])


def _before(timestamp: pd.Timestamp, path_end: pd.Timestamp) -> bool:
    return pd.isna(path_end) or timestamp < path_end


def _candidate(
    *,
    sample: pd.Series,
    breakout: pd.Series,
    reference: pd.Series,
    confirmation: pd.Series,
    entry: pd.Series,
    pattern: str,
    threshold_status: str,
) -> dict[str, Any] | None:
    direction = str(sample["direction"])
    sign = 1.0 if direction == "long" else -1.0
    level = float(sample["breakout_level"])
    risk = float(sample["risk_abs"])
    entry_price = float(entry["open"])
    entry_risk = sign * (entry_price - float(sample["stop_loss"]))
    if risk <= 0 or entry_risk <= 0:
        return None
    return {
        "session_day": sample["session_day"],
        "breakout_time": sample["breakout_time"],
        "direction": direction,
        "pattern": pattern,
        "threshold_status": threshold_status,
        "breakout_level": level,
        "poc_stop": float(sample["stop_loss"]),
        "reference_minutes_from_breakout": int(reference["minutes_from_breakout_candle"]),
        "confirmation_minutes_from_breakout": int(confirmation["minutes_from_breakout_candle"]),
        "bars_reference_to_confirmation": int(
            confirmation["minutes_from_breakout_candle"] - reference["minutes_from_breakout_candle"]
        ),
        "reference_candle_start_time": reference["orderflow_candle_start_time"],
        "confirmation_candle_complete_time": confirmation["orderflow_candle_complete_time"],
        "entry_time": entry["orderflow_candle_start_time"],
        "entry_price": entry_price,
        "entry_risk_abs": entry_risk,
        "breakout_close_expansion_r": sign * (float(breakout["close"]) - level) / risk,
        "breakout_volume_expansion": breakout["volume_expansion_ratio"],
        "breakout_directional_delta": breakout["directional_delta_ratio"],
        "breakout_directional_bubble_imbalance": breakout["directional_bubble_qty_imbalance"],
        "reference_level_distance_r": sign * (_toward_extreme(reference, direction) - level) / risk,
        "confirmation_close_distance_r": sign * (float(confirmation["close"]) - level) / risk,
        "confirmation_volume_expansion": confirmation["volume_expansion_ratio"],
        "confirmation_directional_delta": confirmation["directional_delta_ratio"],
        "confirmation_directional_cvd": confirmation["directional_cvd_ratio_30"],
        "confirmation_directional_bubble_imbalance": confirmation["directional_bubble_qty_imbalance"],
        "benchmark_result_from_raw_breakout": sample["result"],
        "benchmark_max_favorable_r_from_raw_breakout": float(sample["max_favorable_r_before_invalidation"]),
    }


def _observe_sample(sample: pd.Series, candles: pd.DataFrame) -> list[dict[str, Any]]:
    candles = candles.sort_values("minutes_from_breakout_candle", kind="mergesort").reset_index(drop=True)
    if len(candles) < 2 or int(candles.iloc[0]["minutes_from_breakout_candle"]) != 0:
        return []
    direction = str(sample["direction"])
    sign = 1.0 if direction == "long" else -1.0
    level = float(sample["breakout_level"])
    path_end = sample["path_end_time"]
    breakout = candles.iloc[0]
    out: list[dict[str, Any]] = []

    if (
        _before(breakout["orderflow_candle_complete_time"], path_end)
        and _before(candles.iloc[1]["orderflow_candle_start_time"], path_end)
        and _outside(breakout, level, sign)
        and sign * (float(breakout["close"]) - float(breakout["open"])) > 0
    ):
        row = _candidate(
            sample=sample,
            breakout=breakout,
            reference=breakout,
            confirmation=breakout,
            entry=candles.iloc[1],
            pattern="immediate_expansion_observation",
            threshold_status="train_price_and_volume_thresholds_required",
        )
        if row:
            out.append(row)

    for i in range(1, len(candles) - 1):
        reference = candles.iloc[i]
        if not _before(reference["orderflow_candle_complete_time"], path_end):
            break
        if not _touches_level(reference, level, direction):
            continue
        trigger = _away_extreme(reference, direction)
        for j in range(i + 1, len(candles) - 1):
            confirmation = candles.iloc[j]
            entry = candles.iloc[j + 1]
            if not _before(confirmation["orderflow_candle_complete_time"], path_end):
                break
            if (
                _before(entry["orderflow_candle_start_time"], path_end)
                and _outside(confirmation, level, sign)
                and sign * (float(confirmation["close"]) - trigger) > 0
            ):
                row = _candidate(
                    sample=sample,
                    breakout=breakout,
                    reference=reference,
                    confirmation=confirmation,
                    entry=entry,
                    pattern="retest_extreme_then_continuation",
                    threshold_status="structural_confirmation_complete",
                )
                if row:
                    out.append(row)
                break
        if any(row["pattern"] == "retest_extreme_then_continuation" for row in out):
            break

    if _outside(breakout, level, sign):
        for i in range(1, len(candles) - 1):
            reference = candles.iloc[i]
            if not _before(reference["orderflow_candle_complete_time"], path_end) or not _outside(reference, level, sign):
                break
            previous = candles.iloc[i - 1]
            held_outside = sign * (_toward_extreme(reference, direction) - level) > 0
            pulled_back = sign * (float(reference["close"]) - float(previous["close"])) < 0
            if not (held_outside and pulled_back):
                continue
            trigger = _away_extreme(reference, direction)
            for j in range(i + 1, len(candles) - 1):
                confirmation = candles.iloc[j]
                entry = candles.iloc[j + 1]
                if not _before(confirmation["orderflow_candle_complete_time"], path_end) or not _outside(
                    confirmation, level, sign
                ):
                    break
                if _before(entry["orderflow_candle_start_time"], path_end) and sign * (
                    float(confirmation["close"]) - trigger
                ) > 0:
                    row = _candidate(
                        sample=sample,
                        breakout=breakout,
                        reference=reference,
                        confirmation=confirmation,
                        entry=entry,
                        pattern="held_outside_pullback_then_continuation",
                        threshold_status="structural_confirmation_complete",
                    )
                    if row:
                        out.append(row)
                    break
            if any(row["pattern"] == "held_outside_pullback_then_continuation" for row in out):
                break
    return out


def _markdown(summary: dict[str, Any], observation_dir: Path) -> str:
    lines = [
        "# Basic ORB Entry-Pattern Observation",
        "",
        f"Source: `{observation_dir}`",
        "",
        "Observer only. Every signal uses completed candles and the candidate entry is the next candle open.",
        "Original breakout outcomes are benchmark labels, not delayed-entry outcomes.",
        "",
        f"- Samples scanned: `{summary['samples_scanned']}`",
        f"- Pattern rows: `{summary['pattern_rows']}`",
        "",
        "| pattern | rows | benchmark WR | median benchmark MFE R |",
        "| --- | ---: | ---: | ---: |",
    ]
    for pattern, values in summary["by_pattern"].items():
        lines.append(
            f"| {pattern} | {values['rows']} | {values['benchmark_win_rate']:.4f} | "
            f"{values['median_benchmark_mfe_r']:.4f} |"
        )
    lines += [
        "",
        "`immediate_expansion_observation` is not an entry rule. Its price/volume fields require train-only thresholds.",
        "Retest and held-outside rows are structural confirmations, but their entry outcomes must still be replayed from the next open.",
    ]
    return "\n".join(lines) + "\n"


def run(observation_dir: Path, output_dir: Path) -> dict[str, Any]:
    samples, path = _read(observation_dir)
    sample_lookup = samples.set_index(["session_day", "breakout_time"])
    candidates: list[dict[str, Any]] = []
    for key, candles in path.groupby(["session_day", "breakout_time"], sort=False):
        if key not in sample_lookup.index:
            continue
        sample = sample_lookup.loc[key].copy()
        sample["session_day"], sample["breakout_time"] = key
        candidates.extend(_observe_sample(sample, candles))
    result = pd.DataFrame(candidates)
    if result.empty:
        raise ValueError("No entry-pattern observations found")

    by_pattern: dict[str, Any] = {}
    for pattern, rows in result.groupby("pattern"):
        by_pattern[str(pattern)] = {
            "rows": len(rows),
            "benchmark_win_rate": float(rows["benchmark_result_from_raw_breakout"].eq("win").mean()),
            "median_benchmark_mfe_r": float(rows["benchmark_max_favorable_r_from_raw_breakout"].median()),
        }
    summary = {
        "event": "basic_orb_entry_pattern_observation_finished",
        "samples_scanned": len(samples),
        "pattern_rows": len(result),
        "by_pattern": by_pattern,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    result.to_parquet(output_dir / "entry_pattern_candidates.parquet", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "findings.md").write_text(_markdown(summary, observation_dir), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe no-lookahead ORB entry-pattern state transitions.")
    parser.add_argument("--observation-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    print(json.dumps(run(Path(args.observation_dir), Path(args.output_dir)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
