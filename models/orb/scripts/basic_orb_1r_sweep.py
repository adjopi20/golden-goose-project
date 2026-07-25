from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from basic_orb_1r_observation import (
    _fmt,
    _markdown,
    _ms,
    _parse_date,
    _parse_time,
    _read,
    _load_orderflow_features,
    _sample_day,
    _summary,
    _write_jsonl,
)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _variant_name(index: int, orb_start: str, breakout_minutes: int, risk_model: str) -> str:
    return f"variant_{index:03d}_{orb_start.replace(':', '')}_{breakout_minutes}m_{risk_model}"


def _write_variant(output_dir: Path, rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"event": "basic_orb_1r_variant_finished", **config, **_summary(rows)}
    _write_jsonl(output_dir / "samples.jsonl", rows)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "findings.md").write_text(_markdown(summary, argparse.Namespace(**config)), encoding="utf-8")
    return summary


def _table(results: list[dict[str, Any]]) -> str:
    rows = sorted(results, key=lambda row: (row["summary"]["expectancy_r_all_unresolved_0r"] or -999, row["summary"]["samples"]), reverse=True)
    lines = [
        "# Basic ORB 1R Sweep",
        "",
        "| rank | variant | ORB start | breakout window | risk model | samples | wins | losses | resolved WR | exp R | reach 2R | reach 4R | reach 8R | >8R | avg MFE R |",
        "| ---: | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(rows, 1):
        summary = row["summary"]
        lines.append(
            f"| {rank} | {row['variant']} | {summary['orb_start']} | {summary['breakout_window_minutes']} | "
            f"{summary['risk_model']} | {summary['samples']} | {summary['wins']} | {summary['losses']} | "
            f"{_fmt(summary['win_rate_resolved'])} | {_fmt(summary['expectancy_r_all_unresolved_0r'])} | "
            f"{_fmt(summary['reach_2r_rate'])} | {_fmt(summary['reach_4r_rate'])} | {_fmt(summary['reach_8r_rate'])} | "
            f"{_fmt(summary['reach_gt_8r_rate'])} | {_fmt(summary['avg_max_favorable_r_before_invalidation'])} |"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    tz = ZoneInfo(args.timezone)
    start_day = _parse_date(args.start_date)
    end_day = _parse_date(args.end_date)
    orb_starts = _csv(args.orb_starts)
    breakout_windows = [int(value) for value in _csv(args.breakout_window_minutes)]
    risk_models = _csv(args.risk_models)
    outcome_end = _parse_time(args.outcome_end)
    feature_cache_dir = getattr(args, "feature_cache_dir", None)
    orderflow_features = _load_orderflow_features(Path(feature_cache_dir)) if feature_cache_dir else None
    variants = list(product(orb_starts, breakout_windows, risk_models))
    rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    configs: dict[str, dict[str, Any]] = {}
    for index, (orb_start, breakout_minutes, risk_model) in enumerate(variants, 1):
        variant = _variant_name(index, orb_start, breakout_minutes, risk_model)
        rows_by_variant[variant] = []
        configs[variant] = {
            "input": str(args.input),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "timezone": args.timezone,
            "orb_start": orb_start,
            "orb_minutes": args.orb_minutes,
            "breakout_window_minutes": breakout_minutes,
            "outcome_end": args.outcome_end,
            "risk_model": risk_model,
            "feature_cache_dir": feature_cache_dir,
        }

    day = start_day
    while day <= end_day:
        chunk_end = min(end_day, day + timedelta(days=args.chunk_days - 1))
        read_start = min(_ms(day, _parse_time(value), tz) for value in orb_starts)
        read_end = _ms(chunk_end, outcome_end, tz, next_day_if_before=min(_parse_time(value) for value in orb_starts))
        df = _read(Path(args.input), read_start, read_end)
        ts = df["timestamp"].to_numpy(dtype=np.int64)
        current = day
        while current <= chunk_end:
            for variant, config in configs.items():
                rows_by_variant[variant].append(
                    _sample_day(
                        day=current,
                        df=df,
                        ts=ts,
                        tz=tz,
                        orb_start=_parse_time(config["orb_start"]),
                        orb_minutes=args.orb_minutes,
                        breakout_window_minutes=int(config["breakout_window_minutes"]),
                        outcome_end=outcome_end,
                        bins=args.bins,
                        risk_model=str(config["risk_model"]),
                        orderflow_features=orderflow_features,
                    )
                )
            current += timedelta(days=1)
        day = chunk_end + timedelta(days=1)

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    results = [
        {"variant": variant, "summary": _write_variant(output_root / variant, rows, configs[variant])}
        for variant, rows in rows_by_variant.items()
    ]
    (output_root / "sweep_summary.json").write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    (output_root / "sweep_results.md").write_text(_table(results), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all basic ORB 1R observation variants in one parquet pass per chunk.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--feature-cache-dir")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--orb-starts", default="08:30,09:00,09:30")
    parser.add_argument("--orb-minutes", type=int, default=15)
    parser.add_argument("--breakout-window-minutes", default="30,45,60")
    parser.add_argument("--risk-models", default="opposite_extreme,poc,opposite_value_area")
    parser.add_argument("--outcome-end", default="04:30")
    parser.add_argument("--bins", type=int, default=50)
    parser.add_argument("--chunk-days", type=int, default=31)
    results = run(parser.parse_args())
    print(json.dumps({"event": "basic_orb_1r_sweep_finished", "variants": len(results)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
