from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.performance_metrics import calculate_performance_metrics
from analytics.trade_log import trades_to_dataframe
from loader.trade_loader import load_trades_window
from utils.session_utils import (
    compute_profile_overlay_window,
    parse_iso8601_series,
    parse_session_date,
    previous_session_date,
    session_window_from_date,
)
from indicator.deep_trade import build_order_bubbles
from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.volume_profile import build_volume_profile
from utils.export import export_trade_report

try:
    from utils.renderer import render_snapshot
    from utils.snapshot_context import SnapshotContext
    from evaluator.interpreter import evaluate_candle_against_previous_value
    from executor.trend_following_model import SimulatedTrade, simulate_trend_following, TrendFollowingExecutor
except ModuleNotFoundError as exc:
    _LEGACY_IMPORT_ERROR: ModuleNotFoundError | None = exc
    SimulatedTrade = Any
else:
    _LEGACY_IMPORT_ERROR = None


SUPPORTED_SPANS = {
    "1d",
    "3d",
    "1w",
    "2w",
    "1m",
    "2m",
    "3m",
    "6m",
    "9m",
    "1y",
    "2y",
    "3y",
    "5y",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _backtest_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value) / 1000.0, dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _trade_timestamp(trade: dict[str, Any], prefix: str) -> dt.datetime | None:
    return _backtest_timestamp(trade.get(f"{prefix}_time") or trade.get(f"{prefix}_timestamp_ms"))


def _positive_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def calculate_shared_backtest_metrics(
    trades: list[dict[str, Any]],
    initial_equity: float,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> dict[str, Any]:
    """Cost-aware metrics shared by any strategy emitting the standard trade log."""
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    ordered = sorted(trades, key=lambda trade: _trade_timestamp(trade, "exit") or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
    net = [float(trade.get("pnl", 0.0)) for trade in ordered]
    rs = [float(trade["r"]) for trade in ordered if trade.get("r") is not None and math.isfinite(float(trade["r"]))]
    gross = [float(trade.get("gross_pnl_before_costs", trade.get("pnl", 0.0))) for trade in ordered]
    fees = [float(trade.get("fees", float(trade.get("entry_fee", 0.0)) + float(trade.get("exit_fees", 0.0)))) for trade in ordered]
    slippage = [float(trade.get("slippage", 0.0)) for trade in ordered]

    equity = peak = float(initial_equity)
    max_drawdown = 0.0
    daily_pnl: dict[dt.date, float] = defaultdict(float)
    quarters: dict[str, float] = defaultdict(float)
    longest_loss_streak = loss_streak = 0
    for trade, pnl in zip(ordered, net):
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
        exited = _trade_timestamp(trade, "exit") or _trade_timestamp(trade, "entry")
        entered = _trade_timestamp(trade, "entry")
        if exited:
            daily_pnl[exited.date()] += pnl
        if entered:
            quarters[f"{entered.year}-Q{(entered.month - 1) // 3 + 1}"] += float(trade.get("r") or 0.0)
        if pnl < 0:
            loss_streak += 1
            longest_loss_streak = max(longest_loss_streak, loss_streak)
        else:
            loss_streak = 0

    dates = [value.date() for trade in ordered for value in (_trade_timestamp(trade, "entry"), _trade_timestamp(trade, "exit")) if value]
    first_day = start_date or (min(dates) if dates else None)
    last_day = end_date or (max(dates) if dates else first_day)
    day_count = (last_day - first_day).days + 1 if first_day and last_day and last_day >= first_day else 0
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
    downside = math.sqrt(mean(min(value, 0.0) ** 2 for value in daily_returns)) if daily_returns else 0.0

    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    positive_rs = [value for value in rs if value > 0]
    gross_profit = sum(value for value in gross if value > 0)
    quarter_values = list(quarters.values())
    holds = [
        (exit_time - entry_time).total_seconds() / 60.0
        for trade in ordered
        if (entry_time := _trade_timestamp(trade, "entry")) and (exit_time := _trade_timestamp(trade, "exit"))
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
        "trade_frequency_per_week": trade_count / day_count * 7 if day_count else 0.0,
        "profit_factor": _positive_ratio(sum(wins), abs(sum(losses))),
        "profit_factor_r": _positive_ratio(sum(positive_rs), abs(sum(value for value in rs if value < 0))),
        "average_holding_minutes": mean(holds) if holds else 0.0,
        "gross_edge_before_fees_slippage": mean(gross) if gross else 0.0,
        "net_edge_after_fees_slippage": mean(net) if net else 0.0,
        "gross_pnl_before_fees_slippage": sum(gross),
        "net_pnl_after_fees_slippage": sum(net),
        "total_fees": total_fees,
        "total_slippage": total_slippage,
        "fee_to_gross_pnl_ratio": _positive_ratio(total_fees, gross_profit),
        "cost_to_gross_pnl_ratio": _positive_ratio(total_fees + total_slippage, gross_profit),
        "final_equity": initial_equity + sum(net),
        "longest_loss_streak": longest_loss_streak,
        "positive_quarters": sum(value > 0 for value in quarter_values),
        "quarters": len(quarter_values),
        "positive_quarter_ratio": sum(value > 0 for value in quarter_values) / len(quarter_values) if quarter_values else 0.0,
        "median_quarter_r": median(quarter_values) if quarter_values else 0.0,
        "worst_quarter_r": min(quarter_values) if quarter_values else 0.0,
        "top3_winner_share": _positive_ratio(sum(sorted(positive_rs, reverse=True)[:3]), sum(positive_rs)) or 0.0,
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
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {**(summary or {}), "metrics": calculate_shared_backtest_metrics(trades, initial_equity, start_date, end_date)}
    write_jsonl(output_dir / "trades.jsonl", trades)
    if orders is not None:
        write_jsonl(output_dir / "paper_orders.jsonl", orders)
    if decisions is not None:
        write_jsonl(output_dir / "decisions.jsonl", decisions)
    (output_dir / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    unit = timeframe[-1]
    value = int(timeframe[:-1])

    if unit == "m":
        return pd.Timedelta(minutes=value)

    if unit == "h":
        return pd.Timedelta(hours=value)

    raise ValueError(f"Unsupported timeframe: {timeframe}")

def parse_span_to_timedelta_or_dateoffset(span: str) -> pd.Timedelta | pd.DateOffset:
    span = str(span).lower().strip()
    if span == "1d":
        return pd.Timedelta(days=1)
    if span == "3d":
        return pd.Timedelta(days=3)
    if span == "1w":
        return pd.Timedelta(weeks=1)
    if span == "2w":
        return pd.Timedelta(weeks=2)
    if span == "1m":
        return pd.DateOffset(months=1)
    if span == "2m":
        return pd.DateOffset(months=2)
    if span == "3m":
        return pd.DateOffset(months=3)
    if span == "6m":
        return pd.DateOffset(months=6)
    if span == "9m":
        return pd.DateOffset(months=9)
    if span == "1y":
        return pd.DateOffset(years=1)
    if span == "2y":
        return pd.DateOffset(years=2)
    if span == "3y":
        return pd.DateOffset(years=3)
    if span == "5y":
        return pd.DateOffset(years=5)
    raise ValueError(f"Unsupported --span '{span}'. Supported: {sorted(SUPPORTED_SPANS)}")


def parse_chart_replay_dates(raw: str | None) -> list[dt.date]:
    if raw is None or not raw.strip():
        return []
    dates: list[dt.date] = []
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        dates.append(parse_session_date(value))
    return dates


def force_close_open_trades(trades: list[SimulatedTrade], session_last_candle: pd.Series) -> int:
    forced = 0
    exit_timestamp = pd.Timestamp(session_last_candle["timestamp"])
    exit_price = float(session_last_candle["close"])
    for trade in trades:
        if getattr(trade, "result", "open") == "open":
            trade.exit_timestamp = exit_timestamp
            trade.exit_price = exit_price
            trade.result = "closed_force"
            forced += 1
    return forced


def _safe_profit_factor_str(profit_factor: Any) -> str:
    if profit_factor is None:
        return "None"
    if profit_factor == float("inf"):
        return "inf"
    return f"{float(profit_factor):.4f}"


def _print_trade_summary(
    trades_df: pd.DataFrame,
    metrics: dict[str, Any],
    requested_start: pd.Timestamp,
    requested_end: pd.Timestamp,
    loaded_start: pd.Timestamp,
    loaded_end: pd.Timestamp,
    raw_trade_count: int,
    candle_count: int,
    bubble_count: int,
    report_output_path: str,
) -> None:
    closed_count = int(trades_df["is_closed"].sum()) if not trades_df.empty else 0
    open_count = int((~trades_df["is_closed"]).sum()) if not trades_df.empty else 0

    print("\nTrade Summary:")
    print(f"requested start: {requested_start.isoformat()}")
    print(f"requested end: {requested_end.isoformat()}")
    print(f"actual loaded start: {loaded_start.isoformat()}")
    print(f"actual loaded end: {loaded_end.isoformat()}")
    print(f"raw trade count: {raw_trade_count}")
    print(f"candle count: {candle_count}")
    print(f"bubble count: {bubble_count}")
    print(f"total trades: {len(trades_df)}")
    print(f"closed trades: {closed_count}")
    print(f"open trades: {open_count}")
    print(f"win rate: {metrics['win_rate']:.2%}")
    print(f"TP1 hit rate: {metrics['tp1_hit_rate']:.2%}")
    print(f"total R: {metrics['total_r']:.4f}")
    print(f"avg R: {metrics['avg_r']:.4f}")
    print(f"median R: {metrics['median_r']:.4f}")
    print(f"max drawdown R: {metrics['max_drawdown_r']:.4f}")
    print(f"expectancy R: {metrics['expectancy_r']:.4f}")
    print(f"longest losing streak: {metrics['longest_losing_streak']}")
    print(f"profit factor: {_safe_profit_factor_str(metrics['profit_factor'])}")
    print(f"report output path: {report_output_path}")


def _build_session_profile_map(
    profile_by_session_id: dict[str, dict[str, Any]],
    session_id: str,
    previous_session_start: pd.Timestamp,
    previous_session_end: pd.Timestamp,
    input_path: str,
) -> dict[str, dict[str, Any]]:
    if session_id in profile_by_session_id:
        return profile_by_session_id

    prev_trades = load_trades_window(input_path=input_path, start=previous_session_start, end=previous_session_end)
    if prev_trades.empty:
        return profile_by_session_id

    profile_by_session_id[session_id] = {"session_id": session_id, **build_volume_profile(prev_trades)}
    return profile_by_session_id


def _generate_replay_html(
    replay_date: dt.date,
    args: argparse.Namespace,
    backtest_start: pd.Timestamp,
    backtest_end: pd.Timestamp,
) -> None:
    current_session_start, current_session_end = session_window_from_date(
        replay_date,
        args.session_start_hour,
        args.session_start_minute,
    )

    if current_session_start < backtest_start or current_session_end > backtest_end:
        print(
            f"Warning: --chart-replay date {replay_date.isoformat()} is outside requested span "
            f"[{backtest_start.isoformat()} -> {backtest_end.isoformat()}). Skipping."
        )
        return

    reference_session_date = previous_session_date(replay_date)
    previous_session_start, previous_session_end = session_window_from_date(
        reference_session_date,
        args.session_start_hour,
        args.session_start_minute,
    )

    previous_session_trades = load_trades_window(args.input, start=previous_session_start, end=previous_session_end)
    if previous_session_trades.empty:
        print(
            f"Warning: No reference-session trade data for replay date {replay_date.isoformat()} "
            f"(reference={reference_session_date.isoformat()}). Skipping HTML replay."
        )
        return

    previous_session_candles = aggregate_trades_to_ohlcv(
        trades_df=previous_session_trades,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )
    if not previous_session_candles:
        print(
            f"Warning: No reference-session candles generated for replay date {replay_date.isoformat()} "
            f"(reference={reference_session_date.isoformat()}). Skipping HTML replay."
        )
        return

    previous_session_profile = {
        "session_id": reference_session_date.isoformat(),
        **build_volume_profile(previous_session_trades),
    }

    current_session_trades = load_trades_window(args.input, start=current_session_start, end=current_session_end)
    if current_session_trades.empty:
        print(f"Warning: No current-session trades for replay date {replay_date.isoformat()}. Skipping HTML replay.")
        return

    current_session_candles = aggregate_trades_to_ohlcv(
        trades_df=current_session_trades,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )
    if not current_session_candles:
        print(f"Warning: No current-session candles generated for replay date {replay_date.isoformat()}. Skipping HTML replay.")
        return

    current_session_bubbles = build_order_bubbles(
        trades_df=current_session_trades,
        symbol=args.symbol,
        min_qty=args.min_qty,
        min_notional=args.min_notional,
    )

    previous_session_ohlcv_df = pd.DataFrame(previous_session_candles)
    previous_session_ohlcv_df["timestamp"] = parse_iso8601_series(previous_session_ohlcv_df["timestamp"])
    previous_session_ohlcv_df = previous_session_ohlcv_df.sort_values("timestamp").reset_index(drop=True)

    current_session_ohlcv_df = pd.DataFrame(current_session_candles)
    current_session_ohlcv_df["timestamp"] = parse_iso8601_series(current_session_ohlcv_df["timestamp"])
    current_session_ohlcv_df = current_session_ohlcv_df.sort_values("timestamp").reset_index(drop=True)

    current_session_bubbles_df = pd.DataFrame(current_session_bubbles)
    if not current_session_bubbles_df.empty:
        current_session_bubbles_df["timestamp"] = parse_iso8601_series(current_session_bubbles_df["timestamp"])
        current_session_bubbles_df["aggressive_side"] = current_session_bubbles_df["aggressive_side"].astype(str).str.lower()
        current_session_bubbles_df = current_session_bubbles_df.sort_values("timestamp").reset_index(drop=True)

    # Use the same session execution path as backtest loop.
    profile_by_session_id: dict[str, dict[str, Any]] = {
        reference_session_date.isoformat(): previous_session_profile,
    }

    current_session_ohlcv_df["location"] = "unknown"
    current_session_ohlcv_df["balance_state"] = "unknown"

    for idx, row in current_session_ohlcv_df.iterrows():
        candle_payload = {
            "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
            "close": float(row["close"]),
        }
        try:
            evaluation = evaluate_candle_against_previous_value(
                candle=candle_payload,
                profile_by_session_id=profile_by_session_id,
                session_start_hour=args.session_start_hour,
                session_start_minute=args.session_start_minute,
            )
            current_session_ohlcv_df.at[idx, "location"] = str(evaluation["location"])
            current_session_ohlcv_df.at[idx, "balance_state"] = str(evaluation["balance_state"])
        except ValueError:
            current_session_ohlcv_df.at[idx, "location"] = "unknown"
            current_session_ohlcv_df.at[idx, "balance_state"] = "unknown"

    interpreter_df = current_session_ohlcv_df[["location", "balance_state"]].reset_index(drop=True)
    executed_trades = simulate_trend_following(
        candles_df=current_session_ohlcv_df.reset_index(drop=True),
        bubbles_df=current_session_bubbles_df.reset_index(drop=True),
        interpreter_df=interpreter_df,
        tick_size=float(args.tick_size),
    )
    if executed_trades:
        force_close_open_trades(executed_trades, current_session_ohlcv_df.iloc[-1])

    profile_overlay_start, profile_overlay_end = compute_profile_overlay_window(
        previous_session_start,
        previous_session_end,
        0.30,
    )

    profile_clamp_low = float(previous_session_ohlcv_df["low"].min())
    profile_clamp_high = float(previous_session_ohlcv_df["high"].max())

    snapshot_context = SnapshotContext(
        symbol=args.symbol,
        timeframe=args.timeframe,
        session_date=replay_date.isoformat(),
        previous_session_start=previous_session_start,
        previous_session_end=previous_session_end,
        previous_session_candles=previous_session_ohlcv_df,
        previous_session_profile=previous_session_profile,
        current_session_start=current_session_start,
        current_session_end=current_session_end,
        current_session_candles=current_session_ohlcv_df,
        current_session_bubbles=current_session_bubbles_df,
        executed_trades=executed_trades,
        interpreter_states=current_session_ohlcv_df[["timestamp", "location", "balance_state"]].copy(),
        profile_overlay_start=profile_overlay_start,
        profile_overlay_end=profile_overlay_end,
        profile_clamp_low=profile_clamp_low,
        profile_clamp_high=profile_clamp_high,
    )

    print("========== SNAPSHOT CONTEXT ==========")
    print("reference_start:", snapshot_context.previous_session_start)
    print("reference_end:", snapshot_context.previous_session_end)
    print("trading_start:", snapshot_context.current_session_start)
    print("trading_end:", snapshot_context.current_session_end)
    print("prev candles:", len(snapshot_context.previous_session_candles))
    print("prev profile:", snapshot_context.previous_session_profile is not None)
    print("curr candles:", len(snapshot_context.current_session_candles))
    print("curr bubbles:", len(snapshot_context.current_session_bubbles))
    print("trades:", len(snapshot_context.executed_trades))
    print("======================================")

    fig = render_snapshot(snapshot_context)

    out_dir = Path(args.chart_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.symbol}_{replay_date.isoformat()}_replay.html"
    fig.write_html(out_file, include_plotlyjs="cdn", full_html=True)
    print(f"chart replay output: {out_file}")


def _shared_metrics_cli() -> None:
    parser = argparse.ArgumentParser(description="Calculate shared, cost-aware metrics from a JSONL trade log.")
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--initial-equity", type=float, required=True)
    parser.add_argument("--start-date", type=dt.date.fromisoformat)
    parser.add_argument("--end-date", type=dt.date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(
        calculate_shared_backtest_metrics(read_jsonl(args.trades), args.initial_equity, args.start_date, args.end_date),
        indent=2,
        sort_keys=True,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> None:
    if "--trades" in sys.argv:
        _shared_metrics_cli()
        return
    if _LEGACY_IMPORT_ERROR is not None:
        raise RuntimeError("Legacy backtest dependencies are unavailable; use --trades for the shared service") from _LEGACY_IMPORT_ERROR
    parser = argparse.ArgumentParser(description="Long-horizon walk-forward backtest (no full-span HTML rendering).")
    parser.add_argument("--input", required=True, help="Input aggTrades file (.jsonl or .parquet)")
    parser.add_argument("--symbol", required=True, help="Symbol label (e.g., BTCUSDT)")
    parser.add_argument("--session-date", required=True, help="Start session date (YYYY-MM-DD or DDMMYYYY)")
    parser.add_argument("--timeframe", required=True, choices=["1m", "5m", "15m"], help="OHLCV timeframe")
    parser.add_argument("--trade-report-output", required=True, help="Output .xlsx path for full-span report")

    parser.add_argument(
        "--span",
        default="1d",
        choices=sorted(SUPPORTED_SPANS),
        help="Backtest span. IMPORTANT: --span 1m means one calendar month; --timeframe 1m means one-minute candles.",
    )
    parser.add_argument("--session-start-hour", type=int, default=13)
    parser.add_argument("--session-start-minute", type=int, default=30)
    parser.add_argument("--min-qty", type=float, default=None)
    parser.add_argument("--min-notional", type=float, default=None)
    parser.add_argument("--chart-replay", default=None, help="Optional comma-separated replay dates (DDMMYYYY or YYYY-MM-DD)")
    parser.add_argument("--chart-output-dir", default="charts/backtest_replays")
    parser.add_argument("--tick-size", type=float, default=0.1, help="Strategy tick size. Default follows strategy module default.")
    parser.add_argument("--min-bubble-tier", choices=["medium", "large", "extreme"], default="medium",
                        help="Minimum bubble tier to consider for execution")
    parser.add_argument("--continuation-condition", choices=["mfe_gt_mae", "mfe_gt_2x_mae"], default="mfe_gt_mae",
                        help="Continuation confirmation condition")
    parser.add_argument("--min-mfe-usd", type=float, default=10.0,
                        help="Minimum MFE threshold in USD for continuation confirmation")
    parser.add_argument("--bubble-sl-offset-usd", type=float, default=10.0,
                        help="Stop loss offset from bubble price in USD")
    parser.add_argument(
        "--tp1-r",
        type=float,
        default=4.0,
        help="TP1 multiple of risk"
    )
    args = parser.parse_args()

    if args.min_qty is None and args.min_notional is None:
        raise ValueError("At least one threshold must be provided: --min-qty and/or --min-notional")
    if not (0 <= args.session_start_hour <= 23):
        raise ValueError("--session-start-hour must be in [0, 23]")
    if not (0 <= args.session_start_minute <= 59):
        raise ValueError("--session-start-minute must be in [0, 59]")

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    start_session_date = parse_session_date(args.session_date)
    backtest_start, _ = session_window_from_date(start_session_date, args.session_start_hour, args.session_start_minute)
    backtest_end = backtest_start + parse_span_to_timedelta_or_dateoffset(args.span)
    if backtest_end <= backtest_start:
        raise ValueError("Invalid backtest span: computed end must be greater than start")

    first_prev_date = previous_session_date(start_session_date)
    full_trades_window_start, _ = session_window_from_date(first_prev_date, args.session_start_hour, args.session_start_minute)
    full_trades_window_end = backtest_end

    validation_window_trades = load_trades_window(args.input, start=full_trades_window_start, end=full_trades_window_end)
    if validation_window_trades.empty:
        raise ValueError(
            "No trades available for requested backtest+lookback window "
            f"[start={full_trades_window_start.isoformat()}, end={full_trades_window_end.isoformat()})."
        )

    loaded_start_ms = int(validation_window_trades["timestamp"].min())
    loaded_end_ms = int(validation_window_trades["timestamp"].max())
    loaded_start = pd.Timestamp(loaded_start_ms, unit="ms", tz="UTC")
    loaded_end_inclusive = pd.Timestamp(loaded_end_ms, unit="ms", tz="UTC")

    # Data sufficiency check with practical tolerance:
    # some datasets end a few milliseconds before exact boundary.
    # We still fail if availability clearly does not reach requested span.
    start_tolerance = pd.Timedelta(seconds=1)
    end_tolerance = pd.Timedelta(seconds=1)
    if loaded_start > (full_trades_window_start + start_tolerance) or loaded_end_inclusive < (backtest_end - end_tolerance):
        raise ValueError(
            "Insufficient data for requested full span. "
            f"requested_start={full_trades_window_start.isoformat()}, requested_end={backtest_end.isoformat()}, "
            f"available_start={loaded_start.isoformat()}, available_end={loaded_end_inclusive.isoformat()}"
        )

    profile_by_session_id: dict[str, dict[str, Any]] = {}

    all_trades: list[SimulatedTrade] = []
    total_raw_trade_count = 0
    total_candle_count = 0
    total_bubble_count = 0
    total_forced_close = 0

    total_balance = 0
    total_imbalance = 0
    total_unknown_balance = 0
    total_inside = 0
    total_above = 0
    total_below = 0
    total_unknown_location = 0

    current_session_start = backtest_start
    while current_session_start < backtest_end:
        print("SESSION START", current_session_start)
        current_session_end = min(current_session_start + pd.Timedelta(days=1), backtest_end)
        session_date = current_session_start.date()

        prev_session_date = previous_session_date(session_date)
        previous_start, previous_end = session_window_from_date(
            prev_session_date,
            args.session_start_hour,
            args.session_start_minute,
        )
        prev_session_id = prev_session_date.isoformat()

        profile_by_session_id = _build_session_profile_map(
            profile_by_session_id=profile_by_session_id,
            session_id=prev_session_id,
            previous_session_start=previous_start,
            previous_session_end=previous_end,
            input_path=args.input,
        )

        session_trades = load_trades_window(args.input, start=current_session_start, end=current_session_end)
        
        if session_trades.empty:
            raise ValueError(
                "No trades in session window. "
                f"session_date={session_date.isoformat()}, start={current_session_start.isoformat()}, end={current_session_end.isoformat()}"
            )

        total_raw_trade_count += len(session_trades)

        session_candles = aggregate_trades_to_ohlcv(
            trades_df=session_trades,
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
        if not session_candles:
            raise ValueError(
                "No candles generated for session window. "
                f"session_date={session_date.isoformat()}, start={current_session_start.isoformat()}, end={current_session_end.isoformat()}"
            )

        session_bubbles = build_order_bubbles(
            trades_df=session_trades,
            symbol=args.symbol,
            min_qty=args.min_qty,
            min_notional=args.min_notional,
        )

        session_ohlcv_df = pd.DataFrame(session_candles)
        session_ohlcv_df["timestamp"] = parse_iso8601_series(session_ohlcv_df["timestamp"])
        session_ohlcv_df = session_ohlcv_df.sort_values("timestamp").reset_index(drop=True)

        session_bubbles_df = pd.DataFrame(session_bubbles)
        if not session_bubbles_df.empty:
            session_bubbles_df["timestamp"] = parse_iso8601_series(session_bubbles_df["timestamp"])
            session_bubbles_df["aggressive_side"] = session_bubbles_df["aggressive_side"].astype(str).str.lower()
            session_bubbles_df = session_bubbles_df.sort_values("timestamp").reset_index(drop=True)

        total_candle_count += len(session_ohlcv_df)
        total_bubble_count += len(session_bubbles_df)

        session_ohlcv_df["location"] = "unknown"
        session_ohlcv_df["balance_state"] = "unknown"

        for idx, row in session_ohlcv_df.iterrows():
            candle_payload = {
                "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
                "close": float(row["close"]),
            }
            try:
                evaluation = evaluate_candle_against_previous_value(
                    candle=candle_payload,
                    profile_by_session_id=profile_by_session_id,
                    session_start_hour=args.session_start_hour,
                    session_start_minute=args.session_start_minute,
                )
                session_ohlcv_df.at[idx, "location"] = str(evaluation["location"])
                session_ohlcv_df.at[idx, "balance_state"] = str(evaluation["balance_state"])
            except ValueError:
                session_ohlcv_df.at[idx, "location"] = "unknown"
                session_ohlcv_df.at[idx, "balance_state"] = "unknown"

        balance_counts = session_ohlcv_df["balance_state"].value_counts(dropna=False).to_dict()
        location_counts = session_ohlcv_df["location"].value_counts(dropna=False).to_dict()
        total_balance += int(balance_counts.get("balance", 0))
        total_imbalance += int(balance_counts.get("imbalance", 0))
        total_unknown_balance += int(balance_counts.get("unknown", 0))
        total_above += int(location_counts.get("above_vah", 0))
        total_below += int(location_counts.get("below_val", 0))
        total_inside += int(location_counts.get("inside_value", 0))
        total_unknown_location += int(location_counts.get("unknown", 0))

        interpreter_df = session_ohlcv_df[["location", "balance_state"]].reset_index(drop=True)
        candles_for_sim = session_ohlcv_df.reset_index(drop=True)
        bubbles_for_sim = session_bubbles_df.reset_index(drop=True)

        print("SESSION START", current_session_start)
        print("CANDLES", len(candles_for_sim))
        print("BUBBLES", len(bubbles_for_sim))

        # Create executor with new parameters
        executor = TrendFollowingExecutor(
            tick_size=float(args.tick_size),
            min_bubble_tier=args.min_bubble_tier,
            continuation_condition=args.continuation_condition,
            min_mfe_usd=args.min_mfe_usd,
            bubble_sl_offset_usd=args.bubble_sl_offset_usd,
            tp1_r=float(args.tp1_r)
        )
        
        # Process each candle with raw trades
        session_simulated_trades = []
        for i, candle in candles_for_sim.iterrows():
            # Get bubbles in current candle
            candle_start = candle["timestamp"]
            candle_duration = timeframe_to_timedelta(args.timeframe)
            candle_end = candle_start + candle_duration
            
            # Get bubbles and trades in current candle
            bubbles_in_candle = bubbles_for_sim[
                (bubbles_for_sim["timestamp"] >= candle_start) & 
                (bubbles_for_sim["timestamp"] < candle_end)
            ]
            
            session_trades_for_execution = session_trades.copy()

            session_trades_for_execution["timestamp"] = pd.to_datetime(
                session_trades_for_execution["timestamp"],
                unit="ms",
                utc=True,
            )

            trades_in_candle = session_trades_for_execution[
                (session_trades_for_execution["timestamp"] >= candle_start) & 
                (session_trades_for_execution["timestamp"] < candle_end)
            ]
            
            # Process candle through executor
            closed_trade = executor.process_candle(
                candle=candle,
                candle_index=i,
                bubbles_in_candle=bubbles_in_candle,
                trades_in_candle=trades_in_candle,
                interpreter_state=interpreter_df.iloc[i].to_dict()
            )
            
            if closed_trade:
                session_simulated_trades.append(closed_trade)
        
        # Handle any remaining open trade
        if executor.active_trade:
            executor.active_trade.result = "open"
            session_simulated_trades.append(executor.active_trade)

            forced_close_count = force_close_open_trades(session_simulated_trades, candles_for_sim.iloc[-1])
            total_forced_close += forced_close_count
        all_trades.extend(session_simulated_trades)

            # Build completed current session profile for subsequent sessions.
        profile_by_session_id[session_date.isoformat()] = {"session_id": session_date.isoformat(), **build_volume_profile(session_trades)}
            
        print("SESSION COMPLETE", current_session_start)
        current_session_start = current_session_start + pd.Timedelta(days=1)

        print("TRADES GENERATED", len(session_simulated_trades))

    if not all_trades:
        raise ValueError("No trades generated in requested backtest span.")

    print("ALL TRADES", len(all_trades))

    trades_df = trades_to_dataframe(all_trades)
    metrics = calculate_performance_metrics(trades_df)
    export_trade_report(trades_df=trades_df, metrics=metrics, output_path=args.trade_report_output)

    print("\nBacktest Metadata:")
    print(f"symbol: {args.symbol}")
    print(f"timeframe: {args.timeframe}")
    print(f"span: {args.span}")
    print(f"backtest_start: {backtest_start.isoformat()}")
    print(f"backtest_end: {backtest_end.isoformat()}")
    print(f"lookback_start: {full_trades_window_start.isoformat()}")
    print(f"tick_size: {args.tick_size}")
    print(f"forced closed trades at session end: {total_forced_close}")
    print(f"imbalance candles: {total_imbalance}")
    print(f"balance candles: {total_balance}")
    print(f"unknown candles: {total_unknown_balance}")
    print(f"above_vah candles: {total_above}")
    print(f"below_val candles: {total_below}")
    print(f"inside_value candles: {total_inside}")
    print(f"unknown location candles: {total_unknown_location}")

    _print_trade_summary(
        trades_df=trades_df,
        metrics=metrics,
        requested_start=backtest_start,
        requested_end=backtest_end,
        loaded_start=loaded_start,
        loaded_end=loaded_end_inclusive,
        raw_trade_count=total_raw_trade_count,
        candle_count=total_candle_count,
        bubble_count=total_bubble_count,
        report_output_path=args.trade_report_output,
    )

    replay_dates = parse_chart_replay_dates(args.chart_replay)
    if replay_dates:
        print("\nReplay Snapshot Generation:")
        for replay_date in replay_dates:
            try:
                _generate_replay_html(
                    replay_date=replay_date,
                    args=args,
                    backtest_start=backtest_start,
                    backtest_end=backtest_end,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: Failed replay generation for {replay_date.isoformat()}: {exc}")


if __name__ == "__main__":
    main()
