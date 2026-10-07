"""Shared raw-aggTrades execution replay for any model emitting signal JSONL.

Each signal file is an independent portfolio. The next aggTrade price is a
market-fill proxy; bid/ask spread, impact and funding are not observable here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from .results import write_shared_backtest_result
from .exit_policy import fixed_policy


from trading_core.paper_execution import Config, Portfolio, close_position, take_partial, on_tick, validate_config


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def source_range(source: pq.ParquetFile) -> tuple[int, int]:
    if not {"timestamp", "price", "agg_trade_id"}.issubset(source.schema_arrow.names):
        raise ValueError("Raw aggTrades missing timestamp, price or agg_trade_id")
    column = source.schema_arrow.get_field_index("timestamp")
    values = []
    for i in range(source.num_row_groups):
        stats = source.metadata.row_group(i).column(column).statistics
        if stats is None or not stats.has_min_max:
            raise ValueError("Source timestamp row-group statistics required to check coverage")
        values.append((int(stats.min), int(stats.max)))
    if not values:
        raise ValueError("Empty aggTrades file")
    return min(x[0] for x in values), max(x[1] for x in values)


def raw_ticks(source: pq.ParquetFile, first_timestamp_ms: int) -> Iterator[dict[str, Any]]:
    column = source.schema_arrow.get_field_index("timestamp")
    last_order: tuple[int, int] | None = None
    for i in range(source.num_row_groups):
        stats = source.metadata.row_group(i).column(column).statistics
        if int(stats.max) < first_timestamp_ms:
            continue
        for batch in source.iter_batches(row_groups=[i],
                                         columns=["timestamp", "price", "agg_trade_id"],
                                         batch_size=100_000):
            data = batch.to_pydict()
            for stamp, price, trade_id in zip(data["timestamp"], data["price"], data["agg_trade_id"]):
                if stamp is None or price is None or trade_id is None or stamp < first_timestamp_ms:
                    continue
                order = (int(stamp), int(trade_id))
                if last_order is not None and order <= last_order:
                    raise ValueError(f"AggTrades out of order or duplicated near {order}")
                last_order = order
                price = float(price)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("Invalid raw trade price")
                yield {"timestamp_ms":order[0], "agg_trade_id":order[1], "price":price}


def signal_sources(observation_dir: Path | None, paths: list[str]) -> dict[str, Path]:
    """Discover sibling signal files or accept explicit label=path inputs."""
    if observation_dir and paths:
        raise ValueError("Use either --observation-dir or --signals, not both")
    if observation_dir:
        if (observation_dir / "signals.jsonl").is_file():
            return {observation_dir.name: observation_dir / "signals.jsonl"}
        found = {folder.name: folder / "signals.jsonl"
                 for folder in observation_dir.iterdir()
                 if folder.is_dir() and (folder / "signals.jsonl").is_file()}
        if not found:
            raise ValueError(f"No signals.jsonl found in {observation_dir}")
        return dict(sorted(found.items()))
    result = {}
    for item in paths:
        label, sep, path = item.partition("=")
        if not sep or not label or not path or label in result or not label.replace("_", "").isalnum():
            raise ValueError("--signals must be unique label=path pairs")
        result[label] = Path(path)
    if not result:
        raise ValueError("Provide --observation-dir or at least one --signals label=path")
    return result


def load_portfolios(sources: dict[str, Path], data_min: int, data_max: int,
                    config: Config, start_day: date, end_day: date) -> list[Portfolio]:
    portfolios = []
    for label, path in sources.items():
        candidates = read_jsonl(path)
        summary_path = path.with_name("summary.json")
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if "signals" in summary and len(candidates) != int(summary["signals"]):
                raise ValueError(f"Signal count mismatch for {label}")
        accepted, exclusions, seen_ids = [], [], set()
        required = ("sample_id", "session_day", "direction", "stop", "entry_reference",
                    "entry_eligible_timestamp_ms", "entry_deadline_timestamp_ms",
                    "force_exit_timestamp_ms")
        for signal in candidates:
            missing = [field for field in required if field not in signal]
            if missing:
                raise ValueError(f"{label} signal missing {missing}")
        for signal in sorted(candidates, key=lambda row: int(row["entry_eligible_timestamp_ms"])):
            sample_id = str(signal["sample_id"])
            if sample_id in seen_ids:
                raise ValueError(f"Duplicate {label} sample_id {sample_id}")
            seen_ids.add(sample_id)
            day = date.fromisoformat(signal["session_day"])
            if day < start_day or day > end_day:
                exclusions.append(dict(sample_id=sample_id, decision="EXCLUDE",
                                       reason="outside_requested_dates"))
                continue
            eligible = int(signal["entry_eligible_timestamp_ms"])
            deadline = int(signal["entry_deadline_timestamp_ms"])
            force_exit = int(signal["force_exit_timestamp_ms"])
            if eligible < data_min:
                exclusions.append(dict(sample_id=sample_id, decision="EXCLUDE",
                                       reason="source_starts_after_entry"))
                continue
            if force_exit > data_max:
                exclusions.append(dict(sample_id=sample_id, decision="EXCLUDE",
                                       reason="source_ends_before_force_exit"))
                continue
            if signal["direction"] not in ("long", "short") or not eligible <= deadline < force_exit:
                raise ValueError(f"Invalid signal time/direction for {sample_id}")
            stop, reference = float(signal["stop"]), float(signal["entry_reference"])
            if not (math.isfinite(stop) and math.isfinite(reference) and stop > 0 and reference > 0):
                raise ValueError(f"Invalid stop/reference for {sample_id}")
            accepted.append(signal)
        portfolio = Portfolio(label=label, signals=accepted, equity=config.initial_equity)
        portfolio.decisions.extend(exclusions)
        portfolio.equity_curve.append(dict(timestamp_ms=None, equity=config.initial_equity))
        portfolios.append(portfolio)
    return portfolios




def replay(portfolios: list[Portfolio], ticks: Iterator[dict], config: Config) -> None:
    for tick in ticks:
        for portfolio in portfolios:
            if not portfolio.done():
                on_tick(portfolio, tick, portfolio.config or config)
        if all(portfolio.done() for portfolio in portfolios):
            break
    if not all(portfolio.done() for portfolio in portfolios):
        raise ValueError("Raw replay ended with an open position or unprocessed signal")
    for portfolio in portfolios:
        peak = (portfolio.config or config).initial_equity
        for point in portfolio.equity_curve:
            peak = max(peak, point["equity"])
            point["drawdown_from_peak"] = (peak-point["equity"])/peak


def run(input_path: Path, sources: dict[str, Path], output_dir: Path,
        config: Config, start_day: date, end_day: date,
        *, variants: list[tuple[str, Config, float | None]] | None = None,
        plot_equity: bool = False) -> dict:
    """Replay every signal/config combination in one sequential raw-data pass.

    Omitting variants preserves the original initial-stop/time-exit API.
    """
    if not sources:
        raise ValueError("At least one signal source is required")
    if plot_equity:
        import matplotlib
        matplotlib.use("Agg")
    combinations = variants if variants is not None else [("", config, None)]
    if not combinations or len({label for label, _, _ in combinations}) != len(combinations):
        raise ValueError("Variant labels must be nonempty and unique")
    for label, variant_config, target in combinations:
        if label and not label.replace("_", "").isalnum():
            raise ValueError("Unsafe variant label")
        validate_config(variant_config)
        if target is not None and variant_config.tp1_r is not None:
            raise ValueError("Use full fixed target OR partial TP1/time exit, not both")
        if target is not None:
            fixed_policy(target)
    if end_day < start_day:
        raise ValueError("Invalid date range")
    source = pq.ParquetFile(input_path)
    data_min, data_max = source_range(source)
    portfolios = []
    for label, variant_config, target in combinations:
        for portfolio in load_portfolios(sources, data_min, data_max, variant_config, start_day, end_day):
            portfolio.source_label = portfolio.label
            if label:
                portfolio.label += "__" + label
            portfolio.config, portfolio.target_r = variant_config, target
            portfolios.append(portfolio)
    if len({p.label for p in portfolios}) != len(portfolios):
        raise ValueError("Portfolio label collision")
    output_dir.mkdir(parents=True, exist_ok=True)
    complete_path = output_dir / "complete.json"
    # A failed rerun must not leave a previous success marker valid.
    complete_path.unlink(missing_ok=True)
    manifest = dict(
        input=str(input_path.resolve()), source_size=input_path.stat().st_size,
        source_mtime_ns=input_path.stat().st_mtime_ns,
        start_date=str(start_day), end_date=str(end_day),
        signals={label: {"path": str(path.resolve()),
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for label, path in sources.items()},
        variants=[dict(label=label, config=asdict(cfg), target_r=target)
                  for label, cfg, target in combinations])
    (output_dir / "sweep_manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    eligible = [signal for portfolio in portfolios for signal in portfolio.signals]
    if eligible:
        first = min(int(signal["entry_eligible_timestamp_ms"]) for signal in eligible)
        replay(portfolios, raw_ticks(source, first), config)
    results = {}
    comparison = []
    for portfolio in portfolios:
        variant_config = portfolio.config or config
        path = output_dir / portfolio.label
        summary = dict(execution_engine="backtest_engine.replay_aggtrades",
                       signal_group=portfolio.label, source_group=portfolio.source_label,
                       target_r=portfolio.target_r,
                       signal_count=len(portfolio.signals),
                       skipped_or_excluded=sum(row["decision"] in ("SKIP", "EXCLUDE")
                                                for row in portfolio.decisions),
                       requested_session_count=(end_day-start_day).days+1,
                       raw_price_fill_proxy="next_aggTrade_not_order_book_bid_ask",
                       fee_bps_per_fill=variant_config.fee_bps, risk_fraction=variant_config.risk_fraction,
                       max_leverage=variant_config.max_leverage, max_entry_delay_ms=variant_config.max_entry_delay_ms,
                       time_exit="next_trade_at_or_after_signal_force_exit",
                       target_fill="next_trade_after_target_cross; target_anchored_to_actual_entry_fill",
                       stop_fill="next_trade_after_initial_stop_cross", source_min_ms=data_min,
                       source_max_ms=data_max)
        summary.update(tp1_r=variant_config.tp1_r, tp1_fraction=variant_config.tp1_fraction,
            sizing="risk_fraction times current closed-trade equity; compounds after every complete trade",
            partial_policy="original stop unchanged; no trailing/breakeven; remainder exits at signal time",
            equity_curve_basis="closed_complete_trade; partial cash PnL consolidated at final exit")
        result = write_shared_backtest_result(
            path, portfolio.trades, variant_config.initial_equity, start_day, end_day,
            summary=summary, orders=portfolio.orders, decisions=portfolio.decisions)
        write_jsonl(path / "equity_curve.jsonl", portfolio.equity_curve)
        if plot_equity:
            save_equity_chart(path, portfolio, start_day)
        results[portfolio.label] = result
        comparison.append(dict(label=portfolio.label, source_group=portfolio.source_label,
                               target_r=portfolio.target_r, **asdict(variant_config), **result["metrics"]))
    with (output_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
    complete_path.write_text(json.dumps({"portfolios": list(results), "complete": True}), encoding="utf-8")
    return results


def save_equity_chart(path: Path, portfolio: Portfolio, start_day: date) -> None:
    """Closed-trade compounded equity, never presented as intratrade MTM."""
    import matplotlib.pyplot as plt
    times = [datetime.fromtimestamp(p["timestamp_ms"]/1000, timezone.utc)
             if p["timestamp_ms"] is not None else datetime.combine(start_day, datetime.min.time(), timezone.utc)
             for p in portfolio.equity_curve]
    equity = [p["equity"] for p in portfolio.equity_curve]
    peak = equity[0]
    dd = []
    for value in equity:
        peak = max(peak, value)
        dd.append(100*(value/peak-1))
    with plt.style.context("dark_background"):
        fig, axes = plt.subplots(2, 1, figsize=(12, 6), sharex=True, height_ratios=[2, 1])
        axes[0].step(times, equity, where="post", color="#38cf9b")
        axes[0].set_ylabel("Closed-trade equity")
        axes[0].set_title(portfolio.label.replace("__", "\n"), fontsize=10)
        axes[1].fill_between(times, dd, 0, step="post", color="#e66a6a", alpha=.6)
        axes[1].set_ylabel("Drawdown %")
        axes[1].set_xlabel("UTC exit date; 0.5% sizing only if configured")
        for ax in axes: ax.grid(alpha=.2)
        fig.tight_layout()
        fig.savefig(path/"equity_curve.png", dpi=140)
        plt.close(fig)




def parameter_grid(config: Config, grid: dict) -> list[tuple[str, Config, float | None]]:
    """Cartesian configuration grid; no strategy imports or outcome selection."""
    allowed = set(asdict(config)) | {"target_r"}
    if not isinstance(grid, dict) or not grid or set(grid) - allowed:
        raise ValueError(f"Grid must contain only these parameters: {sorted(allowed)}")
    keys = sorted(grid)
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"Grid {key} needs a nonempty list")
        if len({json.dumps(value) for value in values}) != len(values):
            raise ValueError(f"Duplicate grid values for {key}")
        for value in values:
            if value is None and key in {"target_r", "tp1_r", "tp1_fraction"}:
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"Invalid numeric grid value for {key}")
    variants = []
    for values in itertools.product(*(grid[key] for key in keys)):
        selected = dict(zip(keys, values))
        target = selected.pop("target_r", None)
        label = fixed_policy(target).label if target is not None else "time_exit"
        variant_config = replace(config, **selected)
        validate_config(variant_config)
        if selected:
            label += "__" + "__".join((f"{key}_{value:g}" if value is not None else f"{key}_none").replace(".", "p").replace("-", "m").replace("+", "p")
                                        for key, value in selected.items())
        variants.append((label, variant_config, target))
    if len({label for label, _, _ in variants}) != len(variants):
        raise ValueError("Grid generates duplicate labels")
    return variants


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--observation-dir", type=Path,
                        help="Folder containing signals.jsonl or immediate sibling signal folders")
    parser.add_argument("--signals", action="append", default=[], metavar="LABEL=PATH",
                        help="Explicit independent signal JSONL; repeat for multiple portfolios")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--initial-equity", type=float, default=1000.)
    parser.add_argument("--risk-fraction", type=float, default=.005)
    parser.add_argument("--fee-bps", type=float, default=4.)
    parser.add_argument("--max-leverage", type=float, default=5.)
    parser.add_argument("--max-entry-delay-seconds", type=int, default=300)
    parser.add_argument("--plot-equity", action="store_true", help="Export compounded closed-trade equity/drawdown PNG for each portfolio")
    exits = parser.add_mutually_exclusive_group()
    exits.add_argument("--target-r", type=float, nargs="+",
                       help="Fixed reward grid, e.g. 1 1.5 2; omit for original time-exit control")
    exits.add_argument("--grid", type=Path,
                       help="JSON object of parameter lists; Cartesian product in one raw replay")
    args = parser.parse_args()
    config = Config(args.initial_equity, args.risk_fraction, args.fee_bps,
                    args.max_leverage, args.max_entry_delay_seconds*1000)
    grid = (json.loads(args.grid.read_text(encoding="utf-8-sig")) if args.grid else
            {"target_r": args.target_r} if args.target_r else None)
    results = run(args.input, signal_sources(args.observation_dir, args.signals), args.output_dir,
                  config, args.start_date, args.end_date,
                  variants=parameter_grid(config, grid) if grid is not None else None,
                  plot_equity=args.plot_equity)
    print(json.dumps({label: result["metrics"] for label, result in results.items()}, indent=2))


if __name__ == "__main__":
    main()
