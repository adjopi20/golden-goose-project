from __future__ import annotations

import argparse
import bisect
import json
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from indicator.ohlcv import aggregate_trades_to_ohlcv

from .execution_engine import execution_price, fee
from .strategies.fabio_ivb_orb import FabioIvbConfig, FabioIvbState, decide


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def _ms(day: date, value: time, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, value, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)


def _load_trades(path: Path, start_ms: int, end_ms: int) -> pd.DataFrame:
    df = pd.read_parquet(path, filters=[("timestamp", ">=", start_ms), ("timestamp", "<", end_ms)])
    required = {"timestamp", "price", "qty", "is_buyer_maker"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Input parquet missing columns: {missing}")
    return df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _fmt_ms(timestamp_ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()


def _exit_event(position: dict[str, Any], requested_exit: float, timestamp_ms: int, reason: str, fee_bps: float, slippage_bps: float, tz: ZoneInfo) -> dict[str, Any]:
    qty = float(position["qty"])
    fill = execution_price(requested_exit, "long", "exit", slippage_bps)
    exit_fee = fee(fill, qty, fee_bps)
    gross = (fill - float(position["entry_fill"])) * qty
    risk_dollars = float(position["risk_dollars"])
    pnl = gross - exit_fee
    return {
        "event": "paper_close",
        "strategy": "fabio_ivb_orb",
        "reason": reason,
        "timestamp_ms": int(timestamp_ms),
        "time": _fmt_ms(timestamp_ms, tz),
        "exit_time": _fmt_ms(timestamp_ms, tz),
        "exit_timestamp_ms": int(timestamp_ms),
        "entry_time": position["entry_time"],
        "entry_timestamp_ms": position["entry_timestamp_ms"],
        "entry": position["entry_requested"],
        "entry_fill": position["entry_fill"],
        "stop_loss": position["stop_loss"],
        "take_profit": position["take_profit"],
        "exit": requested_exit,
        "exit_fill": fill,
        "qty": qty,
        "entry_fee": position["entry_fee"],
        "exit_fee": exit_fee,
        "gross_pnl": gross,
        "pnl": pnl,
        "r": pnl / risk_dollars if risk_dollars else None,
    }


def _open_position(decision: dict[str, Any], fee_bps: float, slippage_bps: float, tz: ZoneInfo) -> dict[str, Any]:
    qty = float(decision["qty"])
    entry = float(decision["entry"])
    fill = execution_price(entry, "long", "entry", slippage_bps)
    entry_fee = fee(fill, qty, fee_bps)
    timestamp_ms = int(decision["snapshot_timestamp_ms"]) + 5 * 60_000
    return {
        "entry_requested": entry,
        "entry_fill": fill,
        "entry_fee": entry_fee,
        "entry_timestamp_ms": timestamp_ms,
        "entry_time": _fmt_ms(timestamp_ms, tz),
        "stop_loss": float(decision["stop_loss"]),
        "take_profit": float(decision["take_profit"]),
        "qty": qty,
        "risk_dollars": (fill - float(decision["stop_loss"])) * qty,
    }


def _scan_exit(
    position: dict[str, Any],
    raw_timestamps: list[int],
    raw_prices: list[float],
    start_index: int,
    end_ms: int,
    fee_bps: float,
    slippage_bps: float,
    tz: ZoneInfo,
) -> tuple[dict[str, Any] | None, int]:
    end_index = bisect.bisect_right(raw_timestamps, end_ms)
    stop = float(position["stop_loss"])
    tp = float(position["take_profit"])
    for index in range(start_index, end_index):
        price = raw_prices[index]
        if price <= stop:
            return _exit_event(position, stop, raw_timestamps[index], "initial_stop", fee_bps, slippage_bps, tz), index + 1
        if price >= tp:
            return _exit_event(position, tp, raw_timestamps[index], "take_profit_1r", fee_bps, slippage_bps, tz), index + 1
    return None, end_index


def run(args: argparse.Namespace) -> dict[str, Any]:
    tz = ZoneInfo(args.timezone)
    start_day = _parse_date(args.start_date)
    end_day = _parse_date(args.end_date)
    strategy_config = FabioIvbConfig(
        orb_start=_parse_time(args.orb_start),
        orb_minutes=args.orb_minutes,
        trade_end=_parse_time(args.trade_end),
        delta_threshold=args.delta_threshold,
        use_cumulative_delta=args.use_cumulative_delta,
        cumulative_delta_threshold=args.cumulative_delta_threshold,
        tp_rr=args.tp_rr,
        qty=args.qty,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    raw_index = 0
    rows_loaded = 0
    equity = float(args.initial_equity)
    state = FabioIvbState()

    for chunk_start in _chunks(start_day, end_day, args.chunk_days):
        chunk_end = min(end_day, chunk_start + timedelta(days=args.chunk_days - 1))
        df = _load_trades(Path(args.input), _ms(chunk_start, strategy_config.orb_start, tz), _ms(chunk_end + timedelta(days=1), strategy_config.trade_end, tz))
        rows_loaded += len(df)
        if df.empty:
            continue
        raw_timestamps = [int(v) for v in df["timestamp"].to_list()]
        raw_prices = [float(v) for v in df["price"].to_list()]
        candles = aggregate_trades_to_ohlcv(df, args.symbol, "5m")
        raw_index = 0

        for candle in candles:
            candle_ms = int(candle["timestamp_ms"])
            close_ms = candle_ms + 5 * 60_000
            local_day = datetime.fromtimestamp(close_ms / 1000.0, timezone.utc).astimezone(tz).date()
            if local_day < chunk_start or local_day > chunk_end:
                continue

            if position is not None:
                exit_row, raw_index = _scan_exit(position, raw_timestamps, raw_prices, raw_index, close_ms, args.fee_bps, args.slippage_bps, tz)
                if exit_row is not None:
                    equity += float(exit_row["pnl"])
                    exit_row["close_equity"] = equity
                    trades.append(exit_row)
                    orders.append(exit_row)
                    position = None
                elif datetime.fromtimestamp(close_ms / 1000.0, timezone.utc).astimezone(tz).time() >= strategy_config.trade_end:
                    exit_row = _exit_event(position, float(candle["close"]), close_ms, "time_exit", args.fee_bps, args.slippage_bps, tz)
                    equity += float(exit_row["pnl"])
                    exit_row["close_equity"] = equity
                    trades.append(exit_row)
                    orders.append(exit_row)
                    position = None

            if position is None:
                decision = decide(candle, state, strategy_config, tz)
                if decision["decision"] == "TAKE":
                    position = _open_position(decision, args.fee_bps, args.slippage_bps, tz)
                    equity -= float(position["entry_fee"])
                    raw_index = bisect.bisect_right(raw_timestamps, int(position["entry_timestamp_ms"]))
                    orders.append({"event": "paper_open", "strategy": "fabio_ivb_orb", **position, "equity": equity})
                decisions.append(decision)

    summary = {
        "event": "fast_fabio_ivb_backtest_finished",
        "strategy": "fabio_ivb_orb",
        "config": asdict(strategy_config),
        "rows_loaded": rows_loaded,
        "trades_taken": len(trades),
        "wins": sum(1 for trade in trades if float(trade["pnl"]) > 0),
        "losses": sum(1 for trade in trades if float(trade["pnl"]) <= 0),
        "net_pnl": sum(float(trade["pnl"]) for trade in trades),
        "final_equity": equity,
        "output_dir": str(output_dir),
    }
    _write_jsonl(output_dir / "decisions.jsonl", decisions)
    _write_jsonl(output_dir / "paper_orders.jsonl", orders)
    _write_jsonl(output_dir / "trades.jsonl", trades)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return summary


def _chunks(start_day: date, end_day: date, days: int) -> list[date]:
    out = []
    current = start_day
    while current <= end_day:
        out.append(current)
        current += timedelta(days=days)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Fabio IVB long-only ORB as a detachable strategy.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--symbol", default="AVAXUSDC")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--orb-start", default="08:30")
    parser.add_argument("--orb-minutes", type=int, default=30)
    parser.add_argument("--trade-end", default="14:00")
    parser.add_argument("--delta-threshold", type=float, default=200.0)
    parser.add_argument("--use-cumulative-delta", action="store_true")
    parser.add_argument("--cumulative-delta-threshold", type=float, default=500.0)
    parser.add_argument("--tp-rr", type=float, default=1.0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--fee-bps", type=float, default=0.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--chunk-days", type=int, default=31)
    print(json.dumps(run(parser.parse_args()), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
