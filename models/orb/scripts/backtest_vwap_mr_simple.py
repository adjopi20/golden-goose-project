from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.orb.execution_engine import execution_price, fee


NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DEFAULT_BARS = Path("models/orb/runs/20260626_013_backward_profile_behavior/outputs/rr_check/one_minute_bars_cache.parquet")


@dataclass(frozen=True)
class Trade:
    ny_date: str
    direction: str
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry: float
    entry_fill: float
    stop: float
    target_vwap: float
    exit: float
    exit_fill: float
    exit_reason: str
    signal_low: float
    signal_delta: float
    vwap: float
    lower_3std: float
    upper_3std: float
    net_R: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stupid-simple VWAP 3std mean-reversion backtest.")
    p.add_argument("--bars-cache", default=str(DEFAULT_BARS))
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--fee-bps", type=float, default=4.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--stop-buffer-pct", type=float, default=0.001)
    p.add_argument("--min-stop-risk-pct", type=float, default=0.0015)
    p.add_argument("--max-stop-risk-pct", type=float, default=0.025)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def session_ms(day: date, hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return int(datetime.combine(day, time(hour, minute), NY).astimezone(UTC).timestamp() * 1000)


def load_bars(path: Path, start: date, end: date) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df[(df["timestamp_ms"] >= session_ms(start, "09:30")) & (df["timestamp_ms"] < session_ms(end, "17:31"))].copy()
    return df.sort_values("timestamp_ms", kind="mergesort").reset_index(drop=True)


def add_session_vwap(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    dt = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(NY)
    df["ny_date"] = dt.dt.date.astype(str)
    df["ny_time"] = dt.dt.time
    df = df[(df["ny_time"] >= time(9, 30)) & (df["ny_time"] < time(17, 30))].copy()
    df["pv"] = df["close"] * df["volume"]
    df["cum_volume"] = df.groupby("ny_date")["volume"].cumsum()
    df["vwap"] = df.groupby("ny_date")["pv"].cumsum() / df["cum_volume"]
    df["var"] = ((df["close"] - df["vwap"]) ** 2 * df["volume"]).groupby(df["ny_date"]).cumsum() / df["cum_volume"]
    df["std"] = df["var"].clip(lower=0).pow(0.5)
    df["lower_3std"] = df["vwap"] - 3.0 * df["std"]
    df["upper_3std"] = df["vwap"] + 3.0 * df["std"]
    return df.dropna(subset=["open", "high", "low", "close", "volume", "delta", "vwap", "lower_3std"])


def signal_direction(row: pd.Series) -> str | None:
    net_sell = float(row["delta"]) < 0
    if float(row["low"]) <= float(row["lower_3std"]) and net_sell and float(row["close"]) > float(row["open"]):
        return "long"
    if float(row["high"]) >= float(row["upper_3std"]) and net_sell and float(row["close"]) < float(row["open"]):
        return "short"
    return None


def run_trade(rows: pd.DataFrame, i: int, direction: str, args: argparse.Namespace) -> Trade | None:
    sig = rows.iloc[i]
    if i + 1 >= len(rows) or rows.iloc[i + 1]["ny_date"] != sig["ny_date"]:
        return None
    entry_bar = rows.iloc[i + 1]
    entry = float(entry_bar["open"])
    stop = float(sig["low"]) * (1.0 - args.stop_buffer_pct) if direction == "long" else float(sig["high"]) * (1.0 + args.stop_buffer_pct)
    target = float(sig["vwap"])
    entry_fill = execution_price(entry, direction, "entry", args.slippage_bps)
    invalid = stop >= entry_fill or target <= entry_fill if direction == "long" else stop <= entry_fill or target >= entry_fill
    if invalid:
        return None
    risk = entry_fill - stop if direction == "long" else stop - entry_fill
    risk_pct = risk / entry_fill
    if risk_pct < args.min_stop_risk_pct or risk_pct > args.max_stop_risk_pct:
        return None

    qty = 1.0 / risk
    pnl = -fee(entry_fill, qty, args.fee_bps)
    exit_price = float(rows.iloc[-1]["close"])
    exit_ms = int(rows.iloc[-1]["timestamp_ms"])
    reason = "session_end"
    for _, bar in rows.iloc[i + 1 :].iterrows():
        low = float(bar["low"])
        high = float(bar["high"])
        # ponytail: 1m candle has no path; assume stop before VWAP target if both happen.
        stopped = low <= stop if direction == "long" else high >= stop
        targeted = high >= target if direction == "long" else low <= target
        if stopped:
            exit_price = stop
            exit_ms = int(bar["timestamp_ms"])
            reason = "stop"
            break
        if targeted:
            exit_price = target
            exit_ms = int(bar["timestamp_ms"])
            reason = "vwap"
            break

    exit_fill = execution_price(exit_price, direction, "exit", args.slippage_bps)
    gross = (exit_fill - entry_fill) * qty if direction == "long" else (entry_fill - exit_fill) * qty
    pnl += gross - fee(exit_fill, qty, args.fee_bps)
    return Trade(
        ny_date=str(sig["ny_date"]),
        direction=direction,
        signal_ms=int(sig["timestamp_ms"]),
        entry_ms=int(entry_bar["timestamp_ms"]),
        exit_ms=exit_ms,
        entry=entry,
        entry_fill=entry_fill,
        stop=stop,
        target_vwap=target,
        exit=exit_price,
        exit_fill=exit_fill,
        exit_reason=reason,
        signal_low=float(sig["low"]),
        signal_delta=float(sig["delta"]),
        vwap=target,
        lower_3std=float(sig["lower_3std"]),
        upper_3std=float(sig["upper_3std"]),
        net_R=pnl,
    )


def run(args: argparse.Namespace) -> list[Trade]:
    bars = add_session_vwap(load_bars(Path(args.bars_cache), date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)))
    trades: list[Trade] = []
    open_until = -1
    for day, rows in bars.groupby("ny_date", sort=True):
        rows = rows.reset_index(drop=True)
        for i, row in rows.iterrows():
            ts = int(row["timestamp_ms"])
            direction = signal_direction(row)
            if ts <= open_until or direction is None:
                continue
            trade = run_trade(rows, int(i), direction, args)
            if trade:
                trades.append(trade)
                open_until = trade.exit_ms
    return trades


def max_drawdown(values: list[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def summarize(trades: list[Trade]) -> dict[str, float | int | None]:
    values = [t.net_R for t in trades]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    return {
        "trades": len(values),
        "expectancy_net_R": sum(values) / len(values) if values else 0.0,
        "win_rate": len(wins) / len(values) if values else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "total_net_R": sum(values),
        "max_drawdown_R": max_drawdown(values),
    }


def write_outputs(output_dir: Path, trades: list[Trade], args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [asdict(t) for t in trades]
    summary = summarize(trades)
    (output_dir / "trades.jsonl").write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "findings.md").write_text(
        "\n".join(
            [
                "# VWAP 3STD Mean-Reversion Simple Backtest",
                "",
                "Rule: lower 3std net-sell bullish reversal for long, upper 3std net-sell bearish reversal for short, enter next 1m open.",
                "",
                f"- Trades: `{summary['trades']}`",
                f"- Expectancy net R: `{summary['expectancy_net_R']:.4f}`",
                f"- Win rate: `{summary['win_rate']:.4f}`",
                f"- Profit factor: `{'' if summary['profit_factor'] is None else f'{summary['profit_factor']:.4f}'}`",
                f"- Total net R: `{summary['total_net_R']:.4f}`",
                f"- Max drawdown R: `{summary['max_drawdown_R']:.4f}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def self_check() -> None:
    row = pd.Series({"low": 7.0, "lower_3std": 7.1, "delta": -1.0, "close": 7.2, "open": 7.1})
    assert signal_direction(row) == "long"
    assert max_drawdown([1, -2, 3]) == -2.0


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        print("self_check_ok")
        return
    trades = run(args)
    write_outputs(Path(args.output_dir), trades, args)
    print(json.dumps(summarize(trades), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
