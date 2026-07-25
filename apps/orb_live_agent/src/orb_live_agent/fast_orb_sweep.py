from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from models.orb.btc_opening_range_breakout import BTCOpeningRangeBreakoutConfig

from .config import load_config
from .fast_orb_backtest import _btc_runtime_config, _input_symbol, run_btc_orb_with_features, run_with_features
from .feature_cache import build_or_load_feature_set, parse_date


def _bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


PARAMETERS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "ORB_FOLLOW_FAILURE_FILTER": ("__failure_filter", _bool),
    "ORB_ENTRY_WINDOW_MINUTES": ("orb_entry_window_minutes", int),
    "ORB_SESSION_START_TIME": ("orb_session_start_time", str),
    "ORB_ENTRY_START_TIME": ("orb_entry_start_time", str),
    "ORB_MIN_SUPPORTIVE_BUBBLE_QTY_RATIO": ("orb_min_supportive_bubble_qty_ratio", float),
    "ORB_MIN_VOLUME_EXPANSION_RATIO": ("orb_min_volume_expansion_ratio", float),
    "ORB_MIN_CANDIDATE_BODY_RATIO": ("orb_min_candidate_body_ratio", float),
    "ORB_SHORT_MAX_CLOSE_POSITION": ("orb_short_max_close_position", float),
    "ORB_LONG_MIN_CLOSE_POSITION": ("orb_long_min_close_position", float),
    "ORB_REQUIRE_DIRECTIONAL_DELTA": ("orb_require_directional_delta", _bool),
    "ORB_MIN_PREENTRY_DELTA_RATIO": ("orb_min_preentry_delta_ratio", float),
    "ORB_PREENTRY_DELTA_LOOKBACK_MINUTES": ("orb_preentry_delta_lookback_minutes", int),
    "ORB_OPPOSITE_TOUCH_POLICY": ("orb_opposite_touch_policy", str),
    "ORB_DIRECT_MIN_BODY_RATIO": ("orb_direct_min_body_ratio", float),
    "ORB_DIRECT_SHORT_MAX_CLOSE_POSITION": ("orb_direct_short_max_close_position", float),
    "ORB_DIRECT_LONG_MIN_CLOSE_POSITION": ("orb_direct_long_min_close_position", float),
    "ORB_DIRECT_MIN_RANGE_RATIO": ("orb_direct_min_range_ratio", float),
    "ORB_DIRECT_MIN_DELTA_RATIO": ("orb_direct_min_delta_ratio", float),
    "ORB_STOP_MODEL": ("orb_stop_model", str),
    "PAPER_MIN_STOP_RISK_PCT": ("paper_min_stop_risk_pct", float),
    "PAPER_MAX_STOP_RISK_PCT": ("paper_max_stop_risk_pct", float),
    "PAPER_EXIT_MODE": ("paper_exit_mode", str),
    "PAPER_TP1_R": ("paper_tp1_r", float),
    "PAPER_TP1_FRACTION": ("paper_tp1_fraction", float),
    "PAPER_TRAIL_ACTIVATION_R": ("paper_trail_activation_r", float),
    "PAPER_TRAIL_DISTANCE_R": ("paper_trail_distance_r", float),
    "PAPER_PROTECTION_ENABLED": ("paper_protection_enabled", _bool),
    "PAPER_PROTECTION_ACTIVATION_R": ("paper_protection_activation_r", float),
    "PAPER_PROTECTION_STOP_R": ("paper_protection_stop_r", float),
    "PAPER_PROTECTION_FRACTION": ("paper_protection_fraction", float),
    "PAPER_MAX_HOLD_EXIT_TIME": ("paper_max_hold_exit_time", str),
    "BTC_ORB_SESSION_START": ("btc.session_start", str),
    "BTC_ORB_ENTRY_END": ("btc.entry_end", str),
    "BTC_ORB_RANGE_MINUTES": ("btc.range_minutes", int),
    "BTC_ORB_VOLUME_LOOKBACK": ("btc.volume_lookback", int),
    "BTC_ORB_VOLUME_MULTIPLIER": ("btc.volume_multiplier", float),
    "BTC_ORB_ATR_PERIOD": ("btc.atr_period", int),
    "BTC_ORB_ATR_MULTIPLIER": ("btc.atr_multiplier", float),
    "BTC_ORB_EMA_PERIOD": ("btc.ema_period", int),
    "BTC_ORB_ASSUMED_SPREAD_BPS": ("btc.assumed_spread_bps", float),
    "BTC_ORB_MAX_SPREAD_BPS": ("btc.max_spread_bps", float),
    "BTC_ORB_RISK_FRACTION": ("btc.risk_fraction", float),
    "BTC_ORB_MAX_DAILY_LOSS_FRACTION": ("btc.max_daily_loss_fraction", float),
    "BTC_ORB_MAX_CONSECUTIVE_LOSSES": ("btc.max_consecutive_losses", int),
    "BTC_ORB_TP1_R": ("btc.tp1_r", float),
    "BTC_ORB_TP1_FRACTION": ("btc.tp1_fraction", float),
}


def parse_grid(specs: list[str]) -> list[tuple[str, str, list[Any]]]:
    grid: list[tuple[str, str, list[Any]]] = []
    for spec in specs:
        name, separator, raw_values = spec.partition("=")
        name = name.strip().upper()
        if not separator or name not in PARAMETERS:
            raise ValueError(f"Unsupported --param {spec!r}. Supported: {', '.join(PARAMETERS)}")
        field, parser = PARAMETERS[name]
        values = [parser(value.strip()) for value in raw_values.split(",") if value.strip()]
        if not values:
            raise ValueError(f"No values supplied for {name}")
        grid.append((name, field, values))
    return grid


def combinations(grid: list[tuple[str, str, list[Any]]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (
            {grid[i][0]: values[i] for i in range(len(grid))},
            {grid[i][1]: values[i] for i in range(len(grid))},
        )
        for values in product(*(item[2] for item in grid))
    ]


def _coverage_minutes(features: Any, timezone: str, entry_start_time: str) -> int:
    tz = ZoneInfo(timezone)
    hour, minute = entry_start_time.split(":", maxsplit=1)
    start_minutes = int(hour) * 60 + int(minute)
    offsets = []
    for timestamp_ms in features.triggers:
        dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=ZoneInfo("UTC")).astimezone(tz)
        offsets.append(dt.hour * 60 + dt.minute - start_minutes + 1)
    return max(offsets, default=0)


def _markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# ORB Parameter Sweep",
        "",
        "Sorted by eligible sample, positive-quarter ratio, worst quarter, drawdown, then total R.",
        "",
        "| rank | variant | trades | wins | total R | positive quarters | worst quarter R | max DD | loss streak | top-3 winner share |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(rows, 1):
        m = row["metrics"]
        lines.append(
            f"| {rank} | {row['variant']} | {m['trades']} | {m['wins']} | {m['total_r']:.2f} | "
            f"{m['positive_quarters']}/{m['quarters']} | {m['worst_quarter_r']:.2f} | "
            f"{m['max_drawdown']:.1%} | {m['longest_loss_streak']} | {m['top3_winner_share']:.1%} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a cache-once Cartesian ORB parameter sweep.")
    parser.add_argument(
        "--strategy",
        choices=("btc_opening_range_breakout", "legacy_orb"),
        default="btc_opening_range_breakout",
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--param", action="append", default=[], help="NAME=value1,value2; repeat for each parameter")
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--keep-decisions", action="store_true")
    parser.add_argument(
        "--entry-mode",
        choices=("strategy", "follow_candle", "persistent_acceptance", "first_breakout_retest"),
        default="strategy",
    )
    parser.add_argument("--failure-filter", action="store_true")
    args = parser.parse_args()

    grid = parse_grid(args.param)
    variants = combinations(grid)
    if not variants:
        raise SystemExit("At least one --param is required")
    base = load_config()
    if args.strategy == "btc_opening_range_breakout":
        base = replace(base, symbol=_input_symbol(Path(args.input), base.symbol))
    max_window = max(
        (int(values["ORB_ENTRY_WINDOW_MINUTES"]) for values, _ in variants if "ORB_ENTRY_WINDOW_MINUTES" in values),
        default=base.orb_entry_window_minutes,
    )
    btc_base = BTCOpeningRangeBreakoutConfig()
    cache_config = (
        _btc_runtime_config(base, btc_base) if args.strategy == "btc_opening_range_breakout"
        else replace(base, orb_entry_window_minutes=max_window)
    )
    start_day = parse_date(args.start_date)
    end_day = parse_date(args.end_date)
    features = build_or_load_feature_set(
        Path(args.input),
        start_day,
        end_day,
        cache_config,
        Path(args.cache_dir),
        use_cache=True,
    )
    coverage = _coverage_minutes(features, base.session_timezone, getattr(cache_config, "orb_entry_start_time", "09:45"))
    if args.strategy == "legacy_orb" and max_window > coverage:
        raise SystemExit(
            f"Cache covers approximately {coverage} entry-window minutes, but sweep requests {max_window}. "
            "Rebuild the cache with ORB_ENTRY_WINDOW_MINUTES set to the requested maximum."
        )

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, (display_values, field_values) in enumerate(variants, 1):
        config_values = dict(field_values)
        variant_failure_filter = bool(config_values.pop("__failure_filter", args.failure_filter))
        btc_values = {
            key.removeprefix("btc."): config_values.pop(key)
            for key in list(config_values)
            if key.startswith("btc.")
        }
        config = replace(base, **config_values)
        variant = f"variant_{index:03d}"
        variant_dir = output_root / variant
        btc_config = replace(btc_base, **btc_values)
        if args.strategy == "btc_opening_range_breakout":
            summary = run_btc_orb_with_features(
                config,
                features,
                start_day,
                end_day,
                variant_dir,
                btc_config,
                write_decisions=args.keep_decisions,
            )
        else:
            summary = run_with_features(
                config,
                features,
                start_day,
                end_day,
                variant_dir,
                write_decisions=args.keep_decisions,
                entry_mode=args.entry_mode,
                failure_filter=variant_failure_filter,
            )
        metrics = summary["metrics"]
        run_values = {**display_values, "STRATEGY": args.strategy, "ENTRY_MODE": args.entry_mode, "FAILURE_FILTER": variant_failure_filter}
        (variant_dir / "variant_config.json").write_text(
            json.dumps(run_values, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        results.append({"variant": variant, "parameters": run_values, "summary": summary, "metrics": metrics})
        print(json.dumps({"event": "variant_finished", "variant": variant, **metrics}, sort_keys=True))

    results.sort(
        key=lambda row: (
            row["metrics"]["trades"] >= args.min_trades,
            row["metrics"]["positive_quarter_ratio"],
            row["metrics"]["worst_quarter_r"],
            -row["metrics"]["max_drawdown"],
            row["metrics"]["total_r"],
        ),
        reverse=True,
    )
    (output_root / "sweep_summary.json").write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    (output_root / "sweep_results.md").write_text(_markdown(results), encoding="utf-8")
    print(json.dumps({"event": "sweep_finished", "variants": len(results), "output_dir": str(output_root)}, indent=2))


if __name__ == "__main__":
    main()
