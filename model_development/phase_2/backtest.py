"""Phase 2 public runner and shared replay, metrics, exit-policy and path helpers.

Model decisions remain in the selected strategy. Shared fill/accounting primitives
remain in utils.engine.adapters.paper_execution; this file does not duplicate that engine.
"""
from __future__ import annotations
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.modules.setdefault('model_development.phase_2.backtest', sys.modules[__name__])


# Consolidated from exit_policy.py; calculations preserved.
import math
from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable, Literal, Sequence


ExitMode = Literal["fixed", "trailing_only", "partial_trailing"]
TrailUnit = Literal["r", "atr", "percent"]


def _positive(name: str, value: float | None) -> None:
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _number(value: float) -> str:
    return f"{value:g}".replace("-", "m").replace(".", "p")


@dataclass(frozen=True)
class ExitPolicy:
    """Strategy-independent instructions for managing an open position."""

    label: str
    mode: ExitMode
    target_r: float | None = None
    tp1_r: float | None = None
    tp1_fraction: float | None = None
    trail_activation_r: float | None = None
    trail_unit: TrailUnit | None = None
    trail_distance: float | None = None
    protection_floor_r: float | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("label must not be empty")
        if self.mode not in {"fixed", "trailing_only", "partial_trailing"}:
            raise ValueError(f"unknown exit mode: {self.mode}")
        if self.mode == "fixed":
            _positive("target_r", self.target_r)
            forbidden = (
                self.tp1_r,
                self.tp1_fraction,
                self.trail_activation_r,
                self.trail_unit,
                self.trail_distance,
                self.protection_floor_r,
            )
            if any(value is not None for value in forbidden):
                raise ValueError("fixed policy cannot contain partial or trailing fields")
            return

        _positive("trail_activation_r", self.trail_activation_r)
        _positive("trail_distance", self.trail_distance)
        if self.trail_unit not in {"r", "atr", "percent"}:
            raise ValueError("trailing policy requires trail_unit r, atr, or percent")
        if self.trail_unit == "percent" and float(self.trail_distance) > 100:
            raise ValueError("percentage trail distance cannot exceed 100")
        if self.target_r is not None:
            raise ValueError("trailing policy cannot contain a fixed target")
        if self.protection_floor_r is not None:
            if not math.isfinite(self.protection_floor_r):
                raise ValueError("protection_floor_r must be finite")
            if self.protection_floor_r < -1:
                raise ValueError("protection_floor_r cannot widen beyond the original -1R stop")
            if self.protection_floor_r > float(self.trail_activation_r):
                raise ValueError("protection_floor_r cannot exceed trail activation")

        if self.mode == "trailing_only":
            if self.tp1_r is not None or self.tp1_fraction is not None:
                raise ValueError("trailing_only policy cannot contain TP1 fields")
            return

        _positive("tp1_r", self.tp1_r)
        if (
            self.tp1_fraction is None
            or not math.isfinite(self.tp1_fraction)
            or not 0 < self.tp1_fraction < 1
        ):
            raise ValueError("tp1_fraction must be between zero and one")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def fixed_policy(target_r: float) -> ExitPolicy:
    return ExitPolicy(
        label=f"fixed_{_number(target_r)}r_full",
        mode="fixed",
        target_r=float(target_r),
    )


def trailing_policy(
    *,
    activation_r: float,
    unit: TrailUnit,
    distance: float,
    protection_floor_r: float | None,
) -> ExitPolicy:
    floor = "none" if protection_floor_r is None else f"{_number(protection_floor_r)}r"
    distance_label = (
        f"{_number(distance)}r" if unit == "r"
        else f"{_number(distance)}pct" if unit == "percent"
        else f"{_number(distance)}x"
    )
    return ExitPolicy(
        label=(
            f"trail_only_act_{_number(activation_r)}r_"
            f"{unit}_{distance_label}_floor_{floor}"
        ),
        mode="trailing_only",
        trail_activation_r=float(activation_r),
        trail_unit=unit,
        trail_distance=float(distance),
        protection_floor_r=protection_floor_r,
    )


def partial_trailing_policy(
    *,
    tp1_r: float,
    tp1_fraction: float,
    activation_r: float,
    unit: TrailUnit,
    distance: float,
    protection_floor_r: float | None,
) -> ExitPolicy:
    floor = "none" if protection_floor_r is None else f"{_number(protection_floor_r)}r"
    distance_label = (
        f"{_number(distance)}r" if unit == "r"
        else f"{_number(distance)}pct" if unit == "percent"
        else f"{_number(distance)}x"
    )
    return ExitPolicy(
        label=(
            f"tp1_{_number(tp1_r)}r_frac_{_number(tp1_fraction)}_"
            f"trail_act_{_number(activation_r)}r_{unit}_{distance_label}_floor_{floor}"
        ),
        mode="partial_trailing",
        tp1_r=float(tp1_r),
        tp1_fraction=float(tp1_fraction),
        trail_activation_r=float(activation_r),
        trail_unit=unit,
        trail_distance=float(distance),
        protection_floor_r=protection_floor_r,
    )


def build_exit_policy_grid(
    *,
    modes: Iterable[ExitMode],
    fixed_targets_r: Sequence[float] = (),
    tp1_r_values: Sequence[float] = (),
    tp1_fractions: Sequence[float] = (),
    trail_activation_r_values: Sequence[float] = (),
    risk_trail_distances_r: Sequence[float] = (),
    atr_trail_multipliers: Sequence[float] = (),
    percentage_trail_callbacks_pct: Sequence[float] = (),
    protection_floor_r_values: Sequence[float | None] = (None,),
) -> list[ExitPolicy]:
    """Build a deterministic Cartesian grid without evaluating market outcomes."""
    requested = tuple(dict.fromkeys(modes))
    unknown = set(requested) - {"fixed", "trailing_only", "partial_trailing"}
    if unknown:
        raise ValueError(f"Unknown exit modes: {sorted(unknown)}")

    trail_shapes: list[tuple[TrailUnit, float]] = [
        *(('r', float(value)) for value in risk_trail_distances_r),
        *(('atr', float(value)) for value in atr_trail_multipliers),
        *(('percent', float(value)) for value in percentage_trail_callbacks_pct),
    ]
    policies: list[ExitPolicy] = []
    if "fixed" in requested:
        policies.extend(fixed_policy(float(target)) for target in fixed_targets_r)
    if "trailing_only" in requested:
        policies.extend(
            trailing_policy(
                activation_r=float(activation),
                unit=unit,
                distance=distance,
                protection_floor_r=floor,
            )
            for activation, (unit, distance), floor in product(
                trail_activation_r_values,
                trail_shapes,
                protection_floor_r_values,
            )
            if floor is None or float(floor) <= float(activation)
        )
    if "partial_trailing" in requested:
        policies.extend(
            partial_trailing_policy(
                tp1_r=float(tp1),
                tp1_fraction=float(fraction),
                activation_r=float(activation),
                unit=unit,
                distance=distance,
                protection_floor_r=floor,
            )
            for tp1, fraction, activation, (unit, distance), floor in product(
                tp1_r_values,
                tp1_fractions,
                trail_activation_r_values,
                trail_shapes,
                protection_floor_r_values,
            )
            if floor is None or float(floor) <= float(activation)
        )

    labels = [policy.label for policy in policies]
    if not policies:
        raise ValueError("Exit grid is empty")
    if len(labels) != len(set(labels)):
        raise ValueError("Exit grid produced duplicate labels")
    return policies


# Consolidated from results.py; calculations preserved.
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                + "\n"
            )


def _backtest_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value) / 1000.0, dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _trade_timestamp(trade: dict[str, Any], prefix: str) -> dt.datetime | None:
    return _backtest_timestamp(
        trade.get(f"{prefix}_time") or trade.get(f"{prefix}_timestamp_ms")
    )


def _positive_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def calculate_shared_backtest_metrics(
    trades: list[dict[str, Any]],
    initial_equity: float,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> dict[str, Any]:
    """Calculate cost-aware metrics for any standard trade ledger."""
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")

    ordered = sorted(
        trades,
        key=lambda trade: _trade_timestamp(trade, "exit")
        or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
    )
    net = [float(trade.get("pnl", 0.0)) for trade in ordered]
    rs = [
        float(trade["r"])
        for trade in ordered
        if trade.get("r") is not None and math.isfinite(float(trade["r"]))
    ]
    gross = [
        float(trade.get("gross_pnl_before_costs", trade.get("pnl", 0.0)))
        for trade in ordered
    ]
    fees = [
        float(
            trade.get(
                "fees",
                float(trade.get("entry_fee", 0.0))
                + float(trade.get("exit_fees", 0.0)),
            )
        )
        for trade in ordered
    ]
    slippage = [float(trade.get("slippage", 0.0)) for trade in ordered]

    equity = peak = float(initial_equity)
    max_drawdown = 0.0
    daily_pnl: dict[dt.date, float] = defaultdict(float)
    quarters: dict[str, float] = defaultdict(float)
    longest_loss_streak = loss_streak = 0
    for trade, pnl in zip(ordered, net):
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown,
            (peak - equity) / peak if peak else 0.0,
        )
        exited = _trade_timestamp(trade, "exit") or _trade_timestamp(trade, "entry")
        entered = _trade_timestamp(trade, "entry")
        if exited:
            daily_pnl[exited.date()] += pnl
        if entered:
            quarters[f"{entered.year}-Q{(entered.month - 1) // 3 + 1}"] += float(
                trade.get("r") or 0.0
            )
        if pnl < 0:
            loss_streak += 1
            longest_loss_streak = max(longest_loss_streak, loss_streak)
        else:
            loss_streak = 0

    dates = [
        value.date()
        for trade in ordered
        for value in (
            _trade_timestamp(trade, "entry"),
            _trade_timestamp(trade, "exit"),
        )
        if value
    ]
    first_day = start_date or (min(dates) if dates else None)
    last_day = end_date or (max(dates) if dates else first_day)
    day_count = (
        (last_day - first_day).days + 1
        if first_day and last_day and last_day >= first_day
        else 0
    )
    daily_returns: list[float] = []
    running_equity = float(initial_equity)
    if first_day and last_day:
        current = first_day
        while current <= last_day:
            pnl = daily_pnl.get(current, 0.0)
            daily_returns.append(pnl / running_equity if running_equity else 0.0)
            running_equity += pnl
            current += dt.timedelta(days=1)
    daily_mean = mean(daily_returns) if daily_returns else 0.0
    daily_std = stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    downside = (
        math.sqrt(mean(min(value, 0.0) ** 2 for value in daily_returns))
        if daily_returns
        else 0.0
    )

    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    positive_rs = [value for value in rs if value > 0]
    gross_profit = sum(value for value in gross if value > 0)
    quarter_values = list(quarters.values())
    holds = [
        (exit_time - entry_time).total_seconds() / 60.0
        for trade in ordered
        if (entry_time := _trade_timestamp(trade, "entry"))
        and (exit_time := _trade_timestamp(trade, "exit"))
    ]
    trade_count = len(ordered)
    total_fees, total_slippage = sum(fees), sum(slippage)
    return {
        "trades": trade_count,
        "trade_count": trade_count,
        "wins": len(wins),
        "losses": len(losses),
        "expectancy": mean(net) if net else 0.0,
        "expectancy_r": mean(rs) if rs else 0.0,
        "sharpe": daily_mean / daily_std * math.sqrt(365) if daily_std else None,
        "sortino": daily_mean / downside * math.sqrt(365) if downside else None,
        "max_drawdown": max_drawdown,
        "win_rate": len(wins) / trade_count if trade_count else 0.0,
        "average_r_per_trade": mean(rs) if rs else 0.0,
        "avg_r": mean(rs) if rs else 0.0,
        "median_r": median(rs) if rs else 0.0,
        "total_r": sum(rs),
        "trade_frequency_per_day": trade_count / day_count if day_count else 0.0,
        "trade_frequency_per_week": (
            trade_count / day_count * 7 if day_count else 0.0
        ),
        "profit_factor": _positive_ratio(sum(wins), abs(sum(losses))),
        "profit_factor_r": _positive_ratio(
            sum(positive_rs), abs(sum(value for value in rs if value < 0))
        ),
        "average_holding_minutes": mean(holds) if holds else 0.0,
        "gross_edge_before_fees_slippage": mean(gross) if gross else 0.0,
        "net_edge_after_fees_slippage": mean(net) if net else 0.0,
        "gross_pnl_before_fees_slippage": sum(gross),
        "net_pnl_after_fees_slippage": sum(net),
        "total_fees": total_fees,
        "total_slippage": total_slippage,
        "fee_to_gross_pnl_ratio": _positive_ratio(total_fees, gross_profit),
        "cost_to_gross_pnl_ratio": _positive_ratio(
            total_fees + total_slippage, gross_profit
        ),
        "final_equity": initial_equity + sum(net),
        "longest_loss_streak": longest_loss_streak,
        "positive_quarters": sum(value > 0 for value in quarter_values),
        "quarters": len(quarter_values),
        "positive_quarter_ratio": (
            sum(value > 0 for value in quarter_values) / len(quarter_values)
            if quarter_values
            else 0.0
        ),
        "median_quarter_r": median(quarter_values) if quarter_values else 0.0,
        "worst_quarter_r": min(quarter_values) if quarter_values else 0.0,
        "top3_winner_share": _positive_ratio(
            sum(sorted(positive_rs, reverse=True)[:3]), sum(positive_rs)
        )
        or 0.0,
    }


def write_shared_backtest_result(
    output_dir: Path,
    trades: list[dict[str, Any]],
    initial_equity: float,
    start_date: dt.date,
    end_date: dt.date,
    summary: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write a standard trade ledger and its cost-aware summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        **(summary or {}),
        "metrics": calculate_shared_backtest_metrics(
            trades,
            initial_equity,
            start_date,
            end_date,
        ),
    }
    _write_jsonl(output_dir / "trades.jsonl", trades)
    if orders is not None:
        _write_jsonl(output_dir / "paper_orders.jsonl", orders)
    if decisions is not None:
        _write_jsonl(output_dir / "decisions.jsonl", decisions)
    (output_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


# Consolidated from replay_aggtrades.py; calculations preserved.
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



from utils.engine.adapters.paper_execution import Config, Portfolio, close_position, take_partial, on_tick, validate_config


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




def replay(portfolios: list[Portfolio], ticks: Iterator[dict], config: Config,
           *, retain_open: bool = False) -> None:
    for tick in ticks:
        for portfolio in portfolios:
            if not portfolio.done():
                if retain_open and portfolio.position is not None:
                    while (portfolio.next_signal < len(portfolio.signals)
                           and portfolio.signals[portfolio.next_signal]["entry_eligible_timestamp_ms"] <= tick["timestamp_ms"]):
                        signal = portfolio.signals[portfolio.next_signal]
                        portfolio.decisions.append(dict(record_type="position_block", signal=signal,
                            blocking_setup_id=portfolio.position["sample_id"],
                            blocking_entry_timestamp_ms=portfolio.position["entry_timestamp_ms"]))
                        portfolio.next_signal += 1
                on_tick(portfolio, tick, portfolio.config or config)
        if all(portfolio.done() for portfolio in portfolios):
            break
    if not retain_open and not all(portfolio.done() for portfolio in portfolios):
        raise ValueError("Raw replay ended with an open position or unprocessed signal")
    for portfolio in portfolios:
        peak = (portfolio.config or config).initial_equity
        for point in portfolio.equity_curve:
            peak = max(peak, point["equity"])
            point["drawdown_from_peak"] = (peak-point["equity"])/peak


def run_phase2(input_path: Path, signals: list[dict], output: Path, config: Config,
               start_day: date, end_day: date, metadata: dict, *, timing_validator) -> dict:
    """Compact geometric replay; existing fills/fees, explicit open censoring.

    The model supplies causal signals and owns calibration. This function never
    imports a strategy or refits a selector. Legacy run() artifacts are unchanged.
    """
    import pyarrow as pa
    import matplotlib
    matplotlib.use("Agg")

    validate_config(config)
    if end_day < start_day:
        raise ValueError("Invalid Phase 2 date range")
    if any((output / name).exists() for name in ("trades.parquet", "equity_curve.png")):
        raise FileExistsError("Phase 2 artifacts already exist; use a new output folder")
    output.mkdir(parents=True, exist_ok=True)
    metadata = {**metadata, "phase": "backtest", "status": "running", "config": asdict(config)}
    def save():
        (output / "run.json").write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    save()
    try:
        original = (input_path.stat().st_size, input_path.stat().st_mtime_ns)
        source = pq.ParquetFile(input_path)
        minimum, maximum = source_range(source)
        ordered = sorted(signals, key=lambda s: (s["entry_eligible_timestamp_ms"], s["sample_id"]))
        if len({s["sample_id"] for s in ordered}) != len(ordered):
            raise ValueError("Duplicate signal IDs")
        for signal in ordered:
            timing_validator(signal)
            eligible, deadline, force = (signal[k] for k in
                ("entry_eligible_timestamp_ms", "entry_deadline_timestamp_ms", "force_exit_timestamp_ms"))
            if not minimum <= eligible <= deadline < force or eligible > maximum:
                raise ValueError("Signal outside source or invalid timing")
            if signal["direction"] not in ("long", "short") or not start_day <= date.fromisoformat(signal["session_day"]) <= end_day:
                raise ValueError("Invalid signal direction/session")
            if any(not math.isfinite(float(signal[k])) or float(signal[k]) <= 0 for k in ("stop", "entry_reference")):
                raise ValueError("Invalid signal price/stop")
        label = f"{metadata.get('instrument', 'phase2')}__{metadata.get('submodel_id', 'strategy')}__geometric"
        p = Portfolio(label, ordered, config.initial_equity, config=config)
        p.equity_curve.append(dict(timestamp_ms=None, equity=config.initial_equity))
        if ordered:
            replay([p], raw_ticks(source, ordered[0]["entry_eligible_timestamp_ms"]), config, retain_open=True)
        source.close()
        if original != (input_path.stat().st_size, input_path.stat().st_mtime_ns):
            raise ValueError("Raw source changed during replay")
        rows, by_id = [], {s["sample_id"]: s for s in ordered}
        for trade in p.trades:
            signal = by_id[trade["sample_id"]]
            risk = trade["actual_initial_risk"]
            rows.append({**trade, "record_type": "closed_trade", "trade_id": trade["sample_id"],
                "setup_id": trade["sample_id"], "feature_as_of_ms": signal["feature_as_of_ms"],
                "feature_available_at_ms": signal["feature_available_at_ms"],
                "decision_timestamp_ms": signal["decision_timestamp_ms"],
                "entry_eligible_timestamp_ms": signal["entry_eligible_timestamp_ms"],
                "calibration_fold": signal.get("c1_fold"),
                "gross_R": trade["gross_pnl_before_costs"] / risk, "net_R": trade["r"],
                "gross_pnl": trade["gross_pnl_before_costs"], "net_pnl": trade["pnl"],
                "initial_risk": risk, "blocking_setup_id": None, "blocking_entry_timestamp_ms": None})
        for block in p.decisions:
            if block.get("record_type") == "position_block":
                signal = block["signal"]
                rows.append(dict(record_type="position_block", trade_id=None, setup_id=signal["sample_id"],
                    session_day=signal["session_day"], direction=signal["direction"],
                    feature_as_of_ms=signal["feature_as_of_ms"],
                    feature_available_at_ms=signal["feature_available_at_ms"],
                    decision_timestamp_ms=signal["decision_timestamp_ms"],
                    entry_eligible_timestamp_ms=signal["entry_eligible_timestamp_ms"],
                    blocking_setup_id=block["blocking_setup_id"],
                    blocking_entry_timestamp_ms=block["blocking_entry_timestamp_ms"]))
        # Reuse the declared research master schema for typed zero-trade output.
        from model_development.phase_1.observe import SCHEMA
        identity = {k: metadata.get(k) for k in ("run_id", "model_id", "model_version", "submodel_id",
                                               "configuration_id", "iteration_id", "checkpoint_id")}
        rows = [{**r, **identity} for r in rows]
        fields = list(SCHEMA)
        extra = dict(session_day=pa.string(), actual_initial_risk=pa.float64(),
                     calibration_fold=pa.int64(),
                     equity_before_entry=pa.float64(), equity_after_exit=pa.float64(),
                     tp1_filled=pa.bool_(), tp1_price=pa.float64(), tp1_timestamp_ms=pa.int64(),
                     tp1_fraction=pa.float64(), tp1_r=pa.float64(),
                     partial_gross=pa.float64(), partial_exit_fee=pa.float64(), final_exit_fee=pa.float64())
        fields += [pa.field(k, v) for k, v in extra.items() if k not in SCHEMA.names]
        pq.write_table(pa.Table.from_pylist(rows, schema=pa.schema(fields)), output / "trades.parquet", compression="zstd")
        metrics = calculate_shared_backtest_metrics(p.trades, config.initial_equity, start_day, end_day)
        if not p.trades:
            for key in ("win_rate", "expectancy", "expectancy_r", "average_r_per_trade", "avg_r", "median_r"):
                metrics[key] = None
        gross = [{**t, "pnl": t["gross_pnl_before_costs"],
                  "r": t["gross_pnl_before_costs"] / t["actual_initial_risk"]} for t in p.trades]
        gross_metrics = calculate_shared_backtest_metrics(gross, config.initial_equity, start_day, end_day)
        metrics.update(return_geometric=metrics["final_equity"] / config.initial_equity - 1,
            gross_win_rate=gross_metrics["win_rate"] if gross else None,
            gross_expectancy_r=gross_metrics["expectancy_r"] if gross else None,
            gross_profit_factor_money=gross_metrics["profit_factor"],
            gross_comparison_basis="same actual net-sized quantities; not a second gross-compounded portfolio")
        save_equity_chart(output, p, start_day, end_timestamp_ms=maximum)
        metadata.update(status="complete", metrics=metrics, source_min_ms=minimum, source_max_ms=maximum,
            closed_trades=len(p.trades), position_blocks=sum(r["record_type"] == "position_block" for r in rows),
            open_positions_omitted=int(p.position is not None), pending_signals=len(ordered)-p.next_signal,
            skipped_entries=sum(d.get("decision") == "SKIP" for d in p.decisions),
            censoring="No forced terminal close; unresolved position and its partial PnL excluded from closed metrics",
            equity_basis="Net closed-complete-position equity; partial cashflows consolidated at final exit; not MTM",
            execution="First eligible raw print; stop/TP1 fill next print after crossing; time exit first print at/after deadline",
            cost_limits="Fee-only next-print proxy; no funding, bid/ask, impact, liquidation or venue quantity rounding")
        save()
        return metadata
    except Exception as exc:
        metadata.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        save()
        raise


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
        summary = dict(execution_engine="model_development.phase_2.backtest",
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


def save_equity_chart(path: Path, portfolio: Portfolio, start_day: date,
                      *, end_timestamp_ms: int | None = None) -> None:
    """Closed-trade compounded equity, never presented as intratrade MTM."""
    import matplotlib.pyplot as plt
    times = [datetime.fromtimestamp(p["timestamp_ms"]/1000, timezone.utc)
             if p["timestamp_ms"] is not None else datetime.combine(start_day, datetime.min.time(), timezone.utc)
             for p in portfolio.equity_curve]
    equity = [p["equity"] for p in portfolio.equity_curve]
    if end_timestamp_ms is not None:
        end = datetime.fromtimestamp(end_timestamp_ms/1000, timezone.utc)
        if end > times[-1]:
            times.append(end)
            equity.append(equity[-1])
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


def signal_cli_main(argv=None) -> None:
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
    args = parser.parse_args(argv)
    config = Config(args.initial_equity, args.risk_fraction, args.fee_bps,
                    args.max_leverage, args.max_entry_delay_seconds*1000)
    grid = (json.loads(args.grid.read_text(encoding="utf-8-sig")) if args.grid else
            {"target_r": args.target_r} if args.target_r else None)
    results = run(args.input, signal_sources(args.observation_dir, args.signals), args.output_dir,
                  config, args.start_date, args.end_date,
                  variants=parameter_grid(config, grid) if grid is not None else None,
                  plot_equity=args.plot_equity)
    print(json.dumps({label: result["metrics"] for label, result in results.items()}, indent=2))


# Consolidated from path_audit.py; calculations preserved.
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def read_rows(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False), encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_signal(s):
    if s['direction'] not in ('long', 'short'):
        raise ValueError('Invalid direction')
    sign = 1 if s['direction'] == 'long' else -1
    if not np.isfinite(s['entry']) or s['entry'] <= 0:
        raise ValueError('Invalid entry')
    if s['force_exit_timestamp_ms'] <= s['entry_timestamp_ms']:
        raise ValueError('Invalid horizon')
    if s['feature_as_of_ms'] > s['entry_timestamp_ms']:
        raise ValueError('Future feature snapshot')
    if 'baseline' not in s['stops']:
        raise ValueError('Missing baseline stop')
    for stop in s['stops'].values():
        if not np.isfinite(stop) or stop <= 0 or sign*(s['entry']-stop) <= 0:
            raise ValueError('Stop is not on risk side')


def audit_arrays(s, timestamp, price, ids, targets, fee_bps):
    """Touch metrics end at trigger; fills use next print, never post-stop MFE."""
    validate_signal(s)
    timestamp, price, ids = map(np.asarray, (timestamp, price, ids))
    if len(price) < 2 or not (len(price) == len(timestamp) == len(ids)):
        raise ValueError('Incomplete path')
    if (np.diff(timestamp) < 0).any() or (np.diff(ids) <= 0).any():
        raise ValueError('Unordered or duplicate ticks')
    if not np.isfinite(price).all() or (price <= 0).any():
        raise ValueError('Bad prices')
    if int(ids[0]) != s['entry_fill_agg_trade_id'] or int(timestamp[0]) != s['entry_timestamp_ms'] or abs(price[0]-s['entry']) > 1e-8:
        raise ValueError('Cached entry differs from actual fill')
    targets = sorted(set(float(t) for t in targets))
    if not targets or any(not np.isfinite(t) or t <= 0 for t in targets) or not 0 <= fee_bps < 10000:
        raise ValueError('Invalid targets/fees')
    cutoff = int(np.searchsorted(timestamp, s['force_exit_timestamp_ms']))
    if cutoff+1 >= len(price):
        raise ValueError('Missing next-print force exit')
    sign = 1 if s['direction'] == 'long' else -1
    change = sign*(price-s['entry'])
    result = []
    for label, stop in s['stops'].items():
        risk = sign*(s['entry']-stop)
        crossed = np.flatnonzero(sign*(price[:cutoff]-stop) <= 0)
        end = int(crossed[0]) if len(crossed) else cutoff
        reason = 'initial_stop' if len(crossed) else 'next_day_0929'
        # No milestone at or after the time cutoff. Stop crossing cannot be positive R.
        eligible = change[:end]/risk
        path = change[:end+1]/risk
        mfe = max(0., float(eligible.max())) if len(eligible) else 0.
        base = {k:s[k] for k in ('sample_id','session_day','route','direction')}
        base.update(s.get('context', {}))
        base.update(stop_variant=label, stop=stop, entry=s['entry'], risk_pct=risk/s['entry'],
                    mfe_r=mfe, mfe_pct=mfe*risk/s['entry'], mae_r=min(0.,float(path.min())),
                    terminal_reason=reason, terminal_trigger_ms=int(timestamp[end]),
                    terminal_fill_ms=int(timestamp[end+1]), terminal_fill_price=float(price[end+1]),
                    terminal_gross_r=float(change[end+1]/risk),
                    terminal_net_r=float((change[end+1]-(s['entry']+price[end+1])*fee_bps/10000)/risk),
                    nominal_roundtrip_fee_r=2*s['entry']*fee_bps/10000/risk)
        for target in targets:
            hits = np.flatnonzero(eligible >= target)
            hit = int(hits[0]) if len(hits) else None
            trigger = hit if hit is not None else end
            fill = trigger+1
            later_targets = [t for t in targets if t > target]
            next_hits = np.flatnonzero(eligible >= later_targets[0]) if later_targets else []
            segment_end = int(next_hits[0]) if len(next_hits) else end
            segment = path[hit:segment_end+1] if hit is not None else np.array([])
            pullback = float((np.maximum.accumulate(segment)-segment).max()) if len(segment) else None
            result.append({**base, 'target_r':target, 'hit_before_stop_or_cutoff':hit is not None,
                'hit_timestamp_ms':int(timestamp[hit]) if hit is not None else None,
                'minutes_to_hit':float((timestamp[hit]-timestamp[0])/60000) if hit is not None else None,
                'max_pullback_r_until_next_target_or_terminal':pullback,
                'returned_to_initial_stop_after_hit':hit is not None and reason == 'initial_stop',
                'exit_reason':'target' if hit is not None else reason,
                'exit_fill_timestamp_ms':int(timestamp[fill]), 'exit_fill_price':float(price[fill]),
                'net_r':float((change[fill]-(s['entry']+price[fill])*fee_bps/10000)/risk)})
    return result


def cached_path(raw, signal, cache_dir):
    # Raw schema reader is shared with the existing replay, not reimplemented here.
    from model_development.models.orb.scripts.pre_ny_submodels.audit_pre_ny_early_immediate_tail_geometry_cp002 import _iter_ticks
    raw = Path(raw).resolve()
    stat = raw.stat()
    identity = dict(schema=1, raw=str(raw), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    entry_ms=signal['entry_timestamp_ms'], entry_id=signal['entry_fill_agg_trade_id'],
                    cutoff=signal['force_exit_timestamp_ms'])
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    cache_dir.mkdir(parents=True,exist_ok=True)
    path, meta = cache_dir/(key+'.parquet'), cache_dir/(key+'.json')
    if path.exists() and meta.exists():
        saved = json.loads(meta.read_text())
        if saved['identity'] != identity or digest(path) != saved['sha256']:
            raise ValueError('Corrupt path cache; use a new cache directory')
        return path, True
    tmp = cache_dir/(key+'.partial')
    schema = pa.schema([('timestamp_ms',pa.int64()),('price',pa.float64()),('agg_trade_id',pa.int64())])
    buffer=[]; found=False; cutoff_seen=False; complete=False; count=0; last=None
    with pq.ParquetWriter(tmp,schema,compression='zstd') as writer:
        for tick in _iter_ticks(pq.ParquetFile(raw), SimpleNamespace(**signal)):
            t, p, i = tick['timestamp_ms'], tick['price'], tick['agg_trade_id']
            if not found:
                if i != signal['entry_fill_agg_trade_id']:
                    continue
                if t != signal['entry_timestamp_ms'] or abs(p-signal['entry'])>1e-8:
                    raise ValueError('Raw entry mismatch')
                found=True
            if last and (t<last[0] or i<=last[1]):
                raise ValueError('Raw path ordering failure')
            last=(t,i)
            buffer.append(dict(timestamp_ms=t,price=p,agg_trade_id=i)); count+=1
            if len(buffer)>=100000:
                writer.write_table(pa.Table.from_pylist(buffer,schema=schema)); buffer=[]
            if cutoff_seen:
                complete=True
                break
            cutoff_seen=t>=signal['force_exit_timestamp_ms']
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer,schema=schema))
    if not complete:
        raise ValueError(f"Incomplete raw horizon for {signal['sample_id']}; no fabricated exit")
    tmp.replace(path)
    write_json(meta,dict(identity=identity,sha256=digest(path),ticks=count))
    return path, False


def summarize(rows):
    groups=defaultdict(list)
    for r in rows:
        for dimension in ('all','macro_direction','volatility_state','alignment','historical_period'):
            for route in ('all',r['route']):
                groups[(route,r['stop_variant'],r['target_r'],dimension,r.get(dimension,'all'))].append(r)
    output=[]
    for key, rs in groups.items():
        values=[r['net_r'] for r in rs]; losses=-sum(min(x,0) for x in values)
        output.append(dict(zip(('route','stop_variant','target_r','dimension','state'),key))|
            dict(n=len(rs),wins=sum(v>0 for v in values),wr=sum(v>0 for v in values)/len(rs),
                 target_hit_rate=sum(r['hit_before_stop_or_cutoff'] for r in rs)/len(rs),
                 expectancy=sum(values)/len(rs),pf=sum(max(v,0) for v in values)/losses if losses else None,
                 median_mfe_r=float(np.median([r['mfe_r'] for r in rs])),
                 median_mfe_pct=float(np.median([r['mfe_pct'] for r in rs]))))
    return output


def paired_comparison(rows):
    baseline={(r['sample_id'],r['target_r']):r for r in rows if r['stop_variant']=='baseline'}
    return [dict(sample_id=r['sample_id'],route=r['route'],target_r=r['target_r'],
        stop_variant=r['stop_variant'],stop_changed=r['stop']!=baseline[r['sample_id'],r['target_r']]['stop'],
        baseline_net_r=baseline[r['sample_id'],r['target_r']]['net_r'],challenger_net_r=r['net_r'],
        baseline_winner_to_loser=baseline[r['sample_id'],r['target_r']]['net_r']>0 and r['net_r']<=0,
        baseline_loser_to_winner=baseline[r['sample_id'],r['target_r']]['net_r']<=0 and r['net_r']>0)
        for r in rows if r['stop_variant']!='baseline']


def path_audit_cli_main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args(argv); cfg=json.loads(a.experiment.read_text())
    signals=read_rows(cfg['signals'])
    if not signals or len({s['sample_id'] for s in signals})!=len(signals):
        raise ValueError('Empty/duplicate signal universe')
    for s in signals: validate_signal(s)
    a.output_dir.mkdir(parents=True,exist_ok=False)
    all_rows=[]
    for idx,s in enumerate(signals,1):
        path,reused=cached_path(cfg['input'],s,Path(cfg['path_cache_dir']))
        table=pq.read_table(path)
        rows=audit_arrays(s,*(table[c].to_numpy() for c in ('timestamp_ms','price','agg_trade_id')),
                          cfg['targets_r'],cfg['fee_bps'])
        all_rows.extend(rows)
        print(json.dumps(dict(event='path_audited',sample_id=s['sample_id'],completed=idx,total=len(signals),cache_reused=reused)),flush=True)
    with (a.output_dir/'trade_target_audit.jsonl').open('w') as f:
        for r in all_rows: f.write(json.dumps(r,allow_nan=False)+'\n')
    write_json(a.output_dir/'regime_summary.json',summarize(all_rows))
    write_json(a.output_dir/'matched_stop_comparison.json',paired_comparison(all_rows))
    write_json(a.output_dir/'manifest.json',dict(experiment=cfg,signals_sha256=digest(cfg['signals']),
        engine_sha256=digest(__file__),trades=len(signals),status='complete',
        warning='Historical descriptive audit, not untouched OOS, portfolio equity, or order-book simulation.'))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if any(a == '--model' or a.startswith('--model=') for a in argv) or argv in (['--help'], ['-h']):
        from model_development.phase_2.model import parser, run_model
        args = parser().parse_args(argv)
        if args.batch:
            from model_development.phase_2.batch import run_batch
            result = run_batch(args)
        else:
            result = run_model(args)
        print(f"Complete: {result['run_id']}; closed trades={result['closed_trades']}")
        return 0
    if any(a == '--experiment' or a.startswith('--experiment=') for a in argv):
        path_audit_cli_main(argv)
    else:
        signal_cli_main(argv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
