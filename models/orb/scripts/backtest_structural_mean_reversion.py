from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicator.ohlcv import aggregate_trades_to_ohlcv  # noqa: E402
from indicator.volume_profile import build_volume_profile  # noqa: E402
from models.orb.execution_engine import ExecutionConfig, force_exit, on_price, open_position  # noqa: E402


NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DEFAULT_INPUT = ROOT / "storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2024-06_to_2026-05.parquet"


@dataclass(frozen=True)
class Candidate:
    ny_date: str
    direction: str
    signal_ms: int
    entry_ms: int
    entry: float
    stop_loss: float
    tp1_price: float
    tp2_price: float
    swept_level: str
    swept_price: float
    signal_delta: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest structural mean reversion around prior-session/profile levels.")
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--symbol", default="AVAXUSDC")
    p.add_argument("--bars-cache")
    p.add_argument("--direction", choices=["both", "long", "short"], default="both")
    p.add_argument("--fee-bps", type=float, default=4.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--risk-fraction", type=float, default=0.01)
    p.add_argument("--initial-equity", type=float, default=1000.0)
    p.add_argument("--stop-buffer-pct", type=float, default=0.001)
    p.add_argument("--min-stop-risk-pct", type=float, default=0.0015)
    p.add_argument("--max-stop-risk-pct", type=float, default=0.025)
    p.add_argument("--profile-bins", type=int, default=50)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def ms(day: date, hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return int(datetime.combine(day, time(hour, minute), NY).astimezone(UTC).timestamp() * 1000)


def local_date(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).astimezone(NY).date()


def load_raw(path: Path, start: date, end: date) -> pd.DataFrame:
    preload = ms(start - timedelta(days=1), "09:30")
    stop = ms(end, "17:31")
    return pd.read_parquet(
        path,
        columns=["timestamp", "price", "qty", "is_buyer_maker"],
        filters=[("timestamp", ">=", preload), ("timestamp", "<", stop)],
    ).sort_values("timestamp", kind="mergesort").reset_index(drop=True)


def candles_by_ms(raw: pd.DataFrame, symbol: str) -> dict[int, dict[str, Any]]:
    return {int(c["timestamp_ms"]): c for c in aggregate_trades_to_ohlcv(raw, symbol, "1m")}


def cached_candles_by_ms(path: Path) -> dict[int, dict[str, Any]]:
    df = pd.read_parquet(path)
    if "timestamp_ms" not in df.columns:
        raise ValueError("bars cache must include timestamp_ms")
    return {
        int(row.timestamp_ms): {
            "timestamp_ms": int(row.timestamp_ms),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "delta": float(row.delta),
        }
        for row in df.itertuples()
    }


def raw_window(raw: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    start = raw["timestamp"].searchsorted(start_ms, side="left")
    end = raw["timestamp"].searchsorted(end_ms, side="left")
    return raw.iloc[int(start) : int(end)]


def candle_window(candles: dict[int, dict[str, Any]], keys: list[int], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    return [candles[t] for t in keys[bisect_left(keys, start_ms) : bisect_left(keys, end_ms)]]


def levels_for_day(raw: pd.DataFrame, candles: dict[int, dict[str, Any]], keys: list[int], day: date, bins: int) -> dict[str, Any] | None:
    sessions = {
        "previous_ny": (ms(day - timedelta(days=1), "09:30"), ms(day - timedelta(days=1), "17:30")),
        "overnight": (ms(day - timedelta(days=1), "17:30"), ms(day, "01:30")),
        "pre_ny": (ms(day, "01:30"), ms(day, "09:30")),
    }
    out: dict[str, Any] = {"highs": {}, "lows": {}}
    for name, (start, end) in sessions.items():
        rows = candle_window(candles, keys, start, end)
        if not rows:
            return None
        out["highs"][name] = max(float(r["high"]) for r in rows)
        out["lows"][name] = min(float(r["low"]) for r in rows)

    profile_raw = raw_window(raw, ms(day - timedelta(days=1), "09:30"), ms(day, "09:30"))
    if len(profile_raw) < 2:
        return None
    profile = build_volume_profile(profile_raw[["timestamp", "price", "qty", "is_buyer_maker"]], n_bins=bins)
    if profile.get("poc_price") is None or profile.get("val") is None or profile.get("vah") is None:
        return None
    out["profile"] = profile
    return out


def build_candidates(raw: pd.DataFrame, candles: dict[int, dict[str, Any]], keys: list[int], args: argparse.Namespace) -> list[Candidate]:
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    candidates: list[Candidate] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        levels = levels_for_day(raw, candles, keys, day, args.profile_bins)
        if not levels:
            continue
        for candle in candle_window(candles, keys, ms(day, "09:45"), ms(day, "17:30")):
            t = int(candle["timestamp_ms"])
            next_candle = candles.get(t + 60_000)
            if not next_candle or not finite_prices(next_candle, "open"):
                continue
            if args.direction in {"both", "short"}:
                candidates.extend(short_candidates(day, candle, next_candle, levels, args.stop_buffer_pct))
            if args.direction in {"both", "long"}:
                candidates.extend(long_candidates(day, candle, next_candle, levels, args.stop_buffer_pct))
    return candidates


def short_candidates(day: date, candle: dict[str, Any], next_candle: dict[str, Any], levels: dict[str, Any], buffer_pct: float) -> list[Candidate]:
    if not finite_prices(candle, "high", "close", "delta"):
        return []
    touched = [(name, float(price)) for name, price in levels["highs"].items() if float(candle["high"]) >= float(price)]
    if not touched or float(candle["close"]) >= min(price for _, price in touched) or float(candle["delta"]) >= 0:
        return []
    return [
        Candidate(
            ny_date=day.isoformat(),
            direction="short",
            signal_ms=int(candle["timestamp_ms"]),
            entry_ms=int(next_candle["timestamp_ms"]),
            entry=float(next_candle["open"]),
            stop_loss=float(candle["high"]) * (1.0 + buffer_pct),
            tp1_price=float(levels["profile"]["poc_price"]),
            tp2_price=float(levels["profile"]["val"]),
            swept_level=",".join(f"{name}_high" for name, _ in touched),
            swept_price=max(price for _, price in touched),
            signal_delta=float(candle["delta"]),
        )
    ]


def long_candidates(day: date, candle: dict[str, Any], next_candle: dict[str, Any], levels: dict[str, Any], buffer_pct: float) -> list[Candidate]:
    if not finite_prices(candle, "low", "close", "delta"):
        return []
    touched = [(name, float(price)) for name, price in levels["lows"].items() if float(candle["low"]) <= float(price)]
    if not touched or float(candle["close"]) <= max(price for _, price in touched) or float(candle["delta"]) <= 0:
        return []
    return [
        Candidate(
            ny_date=day.isoformat(),
            direction="long",
            signal_ms=int(candle["timestamp_ms"]),
            entry_ms=int(next_candle["timestamp_ms"]),
            entry=float(next_candle["open"]),
            stop_loss=float(candle["low"]) * (1.0 - buffer_pct),
            tp1_price=float(levels["profile"]["poc_price"]),
            tp2_price=float(levels["profile"]["vah"]),
            swept_level=",".join(f"{name}_low" for name, _ in touched),
            swept_price=min(price for _, price in touched),
            signal_delta=float(candle["delta"]),
        )
    ]


def run_trade(candidate: Candidate, candles: dict[int, dict[str, Any]], keys: list[int], args: argparse.Namespace, cost_mult: float) -> dict[str, Any]:
    equity = float(args.initial_equity)
    risk_capital = equity * float(args.risk_fraction)
    config = ExecutionConfig(fee_bps=args.fee_bps * cost_mult, slippage_bps=args.slippage_bps * cost_mult)
    pos, event = open_position(
        direction=candidate.direction,
        entry_model="mean_reversion",
        requested_entry=candidate.entry,
        stop_loss=candidate.stop_loss,
        equity=equity,
        risk_fraction=args.risk_fraction,
        config=config,
        tp1_price=candidate.tp1_price,
        tp2_price=candidate.tp2_price,
    )
    row = asdict(candidate) | {"cost_mult": cost_mult, "status": "rejected", "reject_reason": event.get("reason")}
    if pos is None:
        return row
    risk_pct = abs(pos.entry - pos.stop_loss) / abs(pos.entry)
    if risk_pct < args.min_stop_risk_pct or risk_pct > args.max_stop_risk_pct:
        row["reject_reason"] = "stop_risk_outside_limits"
        row["risk_pct"] = risk_pct
        return row

    pnl = -float(pos.fee_paid)
    close_event = None
    end_ms = ms(date.fromisoformat(candidate.ny_date), "17:30")
    management_times = keys[bisect_left(keys, candidate.entry_ms) : bisect_left(keys, end_ms)]
    if not management_times:
        row["reject_reason"] = "no_management_candles"
        row["risk_pct"] = risk_pct
        return row
    for t in management_times:
        candle = candles[t]
        if not finite_prices(candle, "high", "low"):
            continue
        trigger_price = first_touch(pos.direction, float(candle["high"]), float(candle["low"]), pos.stop_loss, pos.tp1_price, pos.tp2_price, pos.tp1_hit)
        if trigger_price is None:
            continue
        event = on_price(pos, trigger_price, config)
        if event is None:
            continue
        if event["event"] in {"paper_tp1", "paper_close"}:
            pnl += float(event["pnl"])
        if event["event"] == "paper_close":
            close_event = event | {"exit_ms": t}
            break
    if close_event is None:
        finite_close_times = [t for t in management_times if finite_prices(candles[t], "close")]
        if not finite_close_times:
            row["reject_reason"] = "no_finite_close_for_session_exit"
            row["risk_pct"] = risk_pct
            return row
        last = finite_close_times[-1]
        close_event = force_exit(pos, float(candles[last]["close"]), config, "session_end")
        close_event["exit_ms"] = last
        pnl += float(close_event["pnl"])

    row.update(
        {
            "status": "closed",
            "entry_fill": pos.entry,
            "risk_pct": risk_pct,
            "exit_ms": int(close_event["exit_ms"]),
            "exit_fill": close_event.get("exit_fill"),
            "exit_reason": close_event.get("reason"),
            "net_pnl": pnl,
            "net_R": pnl / risk_capital if risk_capital else 0.0,
        }
    )
    return row


def first_touch(direction: str, high: float, low: float, stop: float, tp1: float, tp2: float | None, tp1_hit: bool) -> float | None:
    # ponytail: if stop and target both occur in one 1m candle, assume stop first; use tick replay if this becomes material.
    if direction == "long":
        if low <= stop:
            return stop
        target = tp2 if tp1_hit and tp2 is not None else tp1
        return target if high >= float(target) else None
    if high >= stop:
        return stop
    target = tp2 if tp1_hit and tp2 is not None else tp1
    return target if low <= float(target) else None


def finite_prices(row: dict[str, Any], *fields: str) -> bool:
    return all(math.isfinite(float(row[field])) for field in fields)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for cost_mult in sorted({r["cost_mult"] for r in rows}):
        closed = [r for r in rows if r["cost_mult"] == cost_mult and r["status"] == "closed"]
        for direction in ["all", "long", "short"]:
            subset = closed if direction == "all" else [r for r in closed if r["direction"] == direction]
            pnl = [float(r["net_R"]) for r in subset if math.isfinite(float(r["net_R"]))]
            wins = [x for x in pnl if x > 0]
            losses = [x for x in pnl if x <= 0]
            out.append(
                {
                    "cost_mult": cost_mult,
                    "direction": direction,
                    "trades": len(subset),
                    "expectancy_net_R": sum(pnl) / len(pnl) if pnl else 0.0,
                    "win_rate": len(wins) / len(pnl) if pnl else 0.0,
                    "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
                    "total_net_R": sum(pnl),
                    "max_drawdown_R": max_drawdown(pnl),
                }
            )
    return out


def max_drawdown(values: list[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def write_outputs(output_dir: Path, trades: list[dict[str, Any]], summary: list[dict[str, Any]], args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "trades.jsonl").write_text("\n".join(json.dumps(r, sort_keys=True) for r in trades) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# Structural Mean-Reversion Backtest", "", "Status: research result, not a proven edge.", "", "## Config", ""]
    for key, value in sorted(vars(args).items()):
        if key != "self_check":
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Summary", "", "| cost_mult | direction | trades | expectancy_net_R | win_rate | profit_factor | total_net_R | max_drawdown_R |", "|---:|---|---:|---:|---:|---:|---:|---:|"])
    for row in summary:
        pf = "" if row["profit_factor"] is None else f"{row['profit_factor']:.4f}"
        lines.append(
            f"| {row['cost_mult']:.1f} | {row['direction']} | {row['trades']} | {row['expectancy_net_R']:.4f} | {row['win_rate']:.4f} | {pf} | {row['total_net_R']:.4f} | {row['max_drawdown_R']:.4f} |"
        )
    lines.extend(["", "## Notes", "", "- Uses closed 1m candles and next-candle open entries.", "- Conservative intrabar order: stop before target when both fit inside one candle.", "- Mean reversion is reported separately from trend-following ORB."])
    (output_dir / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    raw = load_raw(Path(args.input), date.fromisoformat(args.start_date), date.fromisoformat(args.end_date))
    candles = cached_candles_by_ms(Path(args.bars_cache)) if args.bars_cache else candles_by_ms(raw, args.symbol)
    keys = sorted(candles)
    candidates = build_candidates(raw, candles, keys, args)
    trades = [run_trade(c, candles, keys, args, mult) for mult in (1.0, 2.0, 3.0) for c in candidates]
    summary = summarize(trades)
    write_outputs(Path(args.output_dir), trades, summary, args)
    return summary


def self_check() -> None:
    assert max_drawdown([1, -2, 3, -1]) == -2.0
    assert first_touch("long", high=11, low=9, stop=9.5, tp1=10.5, tp2=None, tp1_hit=False) == 9.5
    assert first_touch("short", high=11, low=9, stop=10.5, tp1=9.5, tp2=None, tp1_hit=False) == 10.5


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        print("self_check_ok")
        return
    print(json.dumps(run(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
