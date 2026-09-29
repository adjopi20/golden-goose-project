from __future__ import annotations

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
