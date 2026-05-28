from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.performance_metrics import calculate_performance_metrics
from analytics.trade_log import trades_to_dataframe
from evaluator.interpreter import evaluate_candle_against_previous_value, load_all_session_profiles
from loader.trade_loader import load_trades_window
from scripts.chart_replay_snapshot import (
    build_chart,
    parse_iso8601_series,
    parse_session_date,
    previous_session_date,
    session_window_from_date,
)
from executor.trend_following_model import SimulatedTrade, simulate_trend_following
from indicator.deep_trade import build_order_bubbles
from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.volume_profile import build_volume_profile
from utils.export import export_trade_report


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
    full_trades_window_start: pd.Timestamp,
    full_trades_window_end: pd.Timestamp,
    backtest_start: pd.Timestamp,
    backtest_end: pd.Timestamp,
) -> None:
    replay_start, replay_end = session_window_from_date(replay_date, args.session_start_hour, args.session_start_minute)

    if replay_start < backtest_start or replay_end > backtest_end:
        print(
            f"Warning: --chart-replay date {replay_date.isoformat()} is outside requested span "
            f"[{backtest_start.isoformat()} -> {backtest_end.isoformat()}). Skipping."
        )
        return

    # Replay HTML is daily/session-level only; keep it lightweight.
    replay_trades = load_trades_window(args.input, start=replay_start, end=replay_end)
    if replay_trades.empty:
        print(f"Warning: No trade data for replay date {replay_date.isoformat()}. Skipping HTML replay.")
        return

    replay_candles = aggregate_trades_to_ohlcv(trades_df=replay_trades, symbol=args.symbol, timeframe=args.timeframe)
    if not replay_candles:
        print(f"Warning: No candles generated for replay date {replay_date.isoformat()}. Skipping HTML replay.")
        return

    replay_current_trades = load_trades_window(args.input, start=replay_start, end=replay_end)
    if replay_current_trades.empty:
        print(f"Warning: No current-session trades for replay date {replay_date.isoformat()}. Skipping HTML replay.")
        return

    replay_bubbles = build_order_bubbles(
        trades_df=replay_current_trades,
        symbol=args.symbol,
        min_qty=args.min_qty,
        min_notional=args.min_notional,
    )

    replay_ohlcv_df = pd.DataFrame(replay_candles)
    replay_ohlcv_df["timestamp"] = parse_iso8601_series(replay_ohlcv_df["timestamp"])
    replay_ohlcv_df = replay_ohlcv_df.sort_values("timestamp").reset_index(drop=True)

    replay_bubbles_df = pd.DataFrame(replay_bubbles)
    if not replay_bubbles_df.empty:
        replay_bubbles_df["timestamp"] = parse_iso8601_series(replay_bubbles_df["timestamp"])
        replay_bubbles_df["aggressive_side"] = replay_bubbles_df["aggressive_side"].astype(str).str.lower()
        replay_bubbles_df = replay_bubbles_df.sort_values("timestamp").reset_index(drop=True)

    fig = build_chart(
        ohlcv_df=replay_ohlcv_df,
        bubbles_df=replay_bubbles_df,
        timeframe=args.timeframe,
        current_session_start=replay_start,
        current_session_end=replay_end,
        session_label=replay_date.isoformat(),
    )

    out_dir = Path(args.chart_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.symbol}_{replay_date.isoformat()}_replay.html"
    fig.write_html(out_file, include_plotlyjs="cdn", full_html=True)
    print(f"chart replay output: {out_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-horizon walk-forward backtest (no full-span HTML rendering).")
    parser.add_argument("--input", required=True, help="Input aggTrades file (.jsonl or .parquet)")
    parser.add_argument("--symbol", required=True, help="Symbol label (e.g., BTCUSDT)")
    parser.add_argument("--session-date", required=True, help="Start session date (YYYY-MM-DD or DDMMYYYY)")
    parser.add_argument("--profile-input", required=True, help="Session profile JSONL input (required)")
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

    profile_input_path = Path(args.profile_input)
    if not profile_input_path.exists():
        raise FileNotFoundError(f"Profile input file not found: {profile_input_path}")

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

    profile_by_session_id = load_all_session_profiles(profile_input_path)

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

        session_simulated_trades = simulate_trend_following(
            candles_df=candles_for_sim,
            bubbles_df=bubbles_for_sim,
            interpreter_df=interpreter_df,
            tick_size=float(args.tick_size),
        )

        forced_close_count = force_close_open_trades(session_simulated_trades, candles_for_sim.iloc[-1])
        total_forced_close += forced_close_count
        all_trades.extend(session_simulated_trades)

        # Build completed current session profile for subsequent sessions.
        profile_by_session_id[session_date.isoformat()] = {"session_id": session_date.isoformat(), **build_volume_profile(session_trades)}

        current_session_start = current_session_start + pd.Timedelta(days=1)

    if not all_trades:
        raise ValueError("No trades generated in requested backtest span.")

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
                    full_trades_window_start=full_trades_window_start,
                    full_trades_window_end=full_trades_window_end,
                    backtest_start=backtest_start,
                    backtest_end=backtest_end,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: Failed replay generation for {replay_date.isoformat()}: {exc}")


if __name__ == "__main__":
    main()
