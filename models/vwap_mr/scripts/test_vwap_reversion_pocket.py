from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from observe_vwap_reversion_edge import DEFAULT_CANDLES, load_1m, parse_hhmm, to_bars


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Test one XRP session VWAP long capitulation reversion pocket.")
    p.add_argument("--candles", default=str(DEFAULT_CANDLES))
    p.add_argument("--start-date")
    p.add_argument("--end-date")
    p.add_argument("--session-tz", default="UTC")
    p.add_argument("--session-start", default="08:00")
    p.add_argument("--session-end", default="16:00")
    p.add_argument("--bar-minutes", type=int, default=5)
    p.add_argument("--output-dir")
    p.add_argument("--entry-z-min", type=float, default=2.5)
    p.add_argument("--entry-z-max", type=float, default=3.0)
    p.add_argument("--target-frac", type=float, default=0.5)
    p.add_argument("--stop-frac", type=float, default=0.5)
    p.add_argument("--max-hold-minutes", type=int, default=90)
    p.add_argument("--cost-r", type=float, default=0.0)
    p.add_argument("--reset-z", type=float, default=0.5)
    p.add_argument("--ema-span", type=int, default=20)
    p.add_argument("--ema-slope-bars", type=int, default=3)
    p.add_argument("--atr-span", type=int, default=14)
    p.add_argument("--session-trend-atr-max", type=float, default=1.5)
    p.add_argument("--ema-slope-atr-max", type=float, default=0.35)
    p.add_argument("--weekly-slope-bars", type=int, default=12)
    p.add_argument("--warmup-days", type=int, default=7)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def outcome(future: pd.DataFrame, entry: float, target: float, stop: float, cost_r: float) -> tuple[str, float, int, float]:
    risk = entry - stop
    for i, row in future.iterrows():
        # ponytail: 5m candle path unknown; count stop before target if both hit in one candle.
        if float(row["low"]) <= stop:
            return "stop", -1.0 - cost_r, int(i + 1), stop
        if float(row["high"]) >= target:
            return "target", ((target - entry) / risk) - cost_r, int(i + 1), target
    if future.empty:
        return "no_future", -cost_r, 0, entry
    exit_price = float(future.iloc[-1]["close"])
    return "timeout", ((exit_price - entry) / risk) - cost_r, len(future), exit_price


def observe(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    events: list[dict] = []
    max_bars = math.ceil(args.max_hold_minutes / args.bar_minutes)
    for _, rows in bars.dropna(subset=["session_vwap_z", "session_vwap"]).groupby("session_date", sort=True):
        rows = rows.reset_index(drop=True)
        armed = True
        i = 0
        while i < len(rows) - 1:
            row = rows.iloc[i]
            z = float(row["session_vwap_z"])
            if z > -args.reset_z:
                armed = True
            in_pocket = -args.entry_z_max <= z <= -args.entry_z_min
            if not armed or not in_pocket:
                i += 1
                continue
            future = rows.iloc[i + 1 : i + 1 + max_bars].reset_index(drop=True)
            if future.empty:
                break
            entry = float(future.iloc[0]["open"])
            vwap = float(row["session_vwap"])
            distance = max(vwap - entry, 0.0)
            if distance <= 0:
                i += 1
                continue
            target = entry + args.target_frac * distance
            stop = entry - args.stop_frac * distance
            reason, net_r, bars_held, exit_price = outcome(future, entry, target, stop, args.cost_r)
            events.append(
                {
                    "session_date": row["session_date"],
                    "month": row["month"],
                    "signal_timestamp_ms": int(row["timestamp_ms"]),
                    "entry": entry,
                    "target": target,
                    "stop": stop,
                    "exit_price": exit_price,
                    "signal_close": float(row["close"]),
                    "session_vwap": vwap,
                    "session_vwap_z": z,
                    "target_frac": args.target_frac,
                    "stop_frac": args.stop_frac,
                    "risk_pct": (entry - stop) / entry,
                    "session_return_pct": float(row["session_return_pct"]),
                    "session_move_atr": float(row["session_move_atr"]),
                    "ema_slope_atr": float(row["ema_slope_atr"]),
                    "exit_reason": reason,
                    "net_R": net_r,
                    "bars_held": bars_held,
                    "minutes_held": bars_held * args.bar_minutes,
                }
            )
            armed = False
            i += max(1, bars_held)
    return pd.DataFrame(events)


def summarize(events: pd.DataFrame) -> dict:
    if events.empty:
        return {"events": 0}
    wins = events[events["net_R"] > 0]
    losses = events[events["net_R"] <= 0]
    loss_sum = losses["net_R"].sum()
    return {
        "events": int(len(events)),
        "avg_R": float(events["net_R"].mean()),
        "median_R": float(events["net_R"].median()),
        "win_rate": float((events["net_R"] > 0).mean()),
        "profit_factor": float(wins["net_R"].sum() / abs(loss_sum)) if loss_sum else None,
        "total_R": float(events["net_R"].sum()),
        "avg_minutes_held": float(events["minutes_held"].mean()),
        "median_minutes_held": float(events["minutes_held"].median()),
        "by_exit": events.groupby("exit_reason")["net_R"].agg(["count", "mean"]).to_dict("index"),
    }


def write_outputs(output_dir: Path, events: pd.DataFrame, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    events.to_csv(output_dir / "events.csv", index=False)
    by_month = events.groupby("month")["net_R"].agg(["count", "mean", "median", "sum"]).reset_index() if not events.empty else pd.DataFrame()
    by_month.to_csv(output_dir / "by_month.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summarize(events), indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "config.json").write_text(json.dumps(vars(args), indent=2, sort_keys=True), encoding="utf-8")


def self_check() -> None:
    future = pd.DataFrame([{"open": 10, "high": 11, "low": 9.8, "close": 10.8}])
    assert outcome(future, 10.0, 11.0, 9.0, 0.05) == ("target", 0.95, 1, 11.0)
    assert parse_hhmm("08:00").hour == 8
    print("self_check_ok")


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        return
    if not args.start_date or not args.end_date or not args.output_dir:
        raise SystemExit("--start-date, --end-date, and --output-dir are required unless --self-check is used")
    tz = ZoneInfo(args.session_tz)
    candles = load_1m(Path(args.candles), date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), tz, args.warmup_days)
    bars = to_bars(candles, args)
    events = observe(bars, args)
    write_outputs(Path(args.output_dir), events, args)
    print(json.dumps({"bars": len(bars), "events": len(events), "output_dir": args.output_dir}, indent=2))


if __name__ == "__main__":
    main()
