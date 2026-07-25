from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


DEFAULT_CANDLES = Path(
    "apps/orb_live_agent/data/feature_cache/xrpusdt/main_orb_2020-01-06_to_2026-06-30/candles_1m.parquet"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Observe VWAP pullback continuation setups on cached 1m candles.")
    p.add_argument("--candles", default=str(DEFAULT_CANDLES))
    p.add_argument("--start-date")
    p.add_argument("--end-date")
    p.add_argument("--session-tz", default="UTC")
    p.add_argument("--session-start", default="08:00")
    p.add_argument("--session-end", default="16:30")
    p.add_argument("--direction", choices=["long", "short", "both"], default="both")
    p.add_argument("--output-dir")
    p.add_argument("--ema-span", type=int, default=20)
    p.add_argument("--ema-slope-bars", type=int, default=3)
    p.add_argument("--volume-lookback", type=int, default=20)
    p.add_argument("--pullback-volume-max", type=float, default=1.0)
    p.add_argument("--atr-span", type=int, default=14)
    p.add_argument("--atr-stop-mult", type=float, default=0.7)
    p.add_argument("--max-hold-bars", type=int, default=36)
    p.add_argument("--skip-strong-session-trend", action="store_true")
    p.add_argument("--session-trend-atr-max", type=float, default=1.5)
    p.add_argument("--ema-slope-atr-max", type=float, default=0.35)
    p.add_argument("--skip-weekly-vwap-agreement", action="store_true")
    p.add_argument("--weekly-slope-bars", type=int, default=12)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def load_1m(path: Path, start: date, end: date, tz: ZoneInfo) -> pd.DataFrame:
    start_ms = ms(datetime.combine(start, time.min, tz))
    end_ms = ms(datetime.combine(end + timedelta(days=1), time.min, tz))
    return pd.read_parquet(
        path,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume"],
        filters=[("timestamp_ms", ">=", start_ms), ("timestamp_ms", "<", end_ms)],
    ).sort_values("timestamp_ms", kind="mergesort")


def to_5m(df: pd.DataFrame, tz: ZoneInfo, session_start: time, session_end: time, args: argparse.Namespace) -> pd.DataFrame:
    if df.empty:
        return df
    dt = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    bars = (
        df.assign(dt=dt)
        .set_index("dt")
        .resample("5min")
        .agg({"timestamp_ms": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["timestamp_ms", "open", "high", "low", "close"])
        .reset_index(drop=True)
    )
    local = pd.to_datetime(bars["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(tz)
    bars["session_date"] = local.dt.date.astype(str)
    bars["session_time"] = local.dt.time
    bars["week"] = local.dt.strftime("%G-%V")
    bars["pv"] = bars["close"] * bars["volume"]
    bars["weekly_vwap"] = bars.groupby("week")["pv"].cumsum() / bars.groupby("week")["volume"].cumsum().replace(0, pd.NA)
    bars["weekly_vwap_prev"] = bars.groupby("week")["weekly_vwap"].shift(args.weekly_slope_bars)
    bars["weekly_vwap_slope_pct"] = bars["weekly_vwap"] / bars["weekly_vwap_prev"] - 1.0
    bars = bars[(bars["session_time"] >= session_start) & (bars["session_time"] < session_end)].copy()
    bars["vwap"] = bars.groupby("session_date")["pv"].cumsum() / bars.groupby("session_date")["volume"].cumsum().replace(0, pd.NA)
    bars["ema"] = bars.groupby("session_date")["close"].transform(lambda s: s.ewm(span=args.ema_span, adjust=False).mean())
    prev_close = bars.groupby("session_date")["close"].shift(1)
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    bars["atr"] = tr.groupby(bars["session_date"]).transform(lambda s: s.ewm(span=args.atr_span, adjust=False).mean())
    bars["volume_median"] = bars.groupby("session_date")["volume"].transform(
        lambda s: s.rolling(args.volume_lookback, min_periods=5).median()
    )
    bars["ema_prev"] = bars.groupby("session_date")["ema"].shift(args.ema_slope_bars)
    bars["session_open"] = bars.groupby("session_date")["open"].transform("first")
    bars["session_return_pct"] = bars["close"] / bars["session_open"] - 1.0
    bars["session_move_atr"] = (bars["close"] - bars["session_open"]).abs() / bars["atr"]
    bars["ema_slope_atr"] = (bars["ema"] - bars["ema_prev"]).abs() / bars["atr"]
    return bars.dropna(subset=["vwap", "ema", "ema_prev", "atr", "volume_median", "weekly_vwap", "weekly_vwap_slope_pct"]).reset_index(drop=True)


def filters_block(row: pd.Series, direction: str, args: argparse.Namespace) -> tuple[bool, bool, bool]:
    strong_session_trend = (
        float(row["session_move_atr"]) > args.session_trend_atr_max
        or float(row["ema_slope_atr"]) > args.ema_slope_atr_max
    )
    weekly_agreement = (
        direction == "short"
        and float(row["close"]) > float(row["weekly_vwap"])
        and float(row["weekly_vwap_slope_pct"]) > 0
    ) or (
        direction == "long"
        and float(row["close"]) < float(row["weekly_vwap"])
        and float(row["weekly_vwap_slope_pct"]) < 0
    )
    blocked = (args.skip_strong_session_trend and strong_session_trend) or (
        args.skip_weekly_vwap_agreement and weekly_agreement
    )
    return blocked, strong_session_trend, weekly_agreement


def hit_outcome(future: pd.DataFrame, direction: str, entry: float, stop: float, tp1: float, tp2: float) -> tuple[str, float, int]:
    for i, row in future.iterrows():
        low = float(row["low"])
        high = float(row["high"])
        stopped = low <= stop if direction == "long" else high >= stop
        hit1 = high >= tp1 if direction == "long" else low <= tp1
        hit2 = high >= tp2 if direction == "long" else low <= tp2
        vwap_fail = float(row["close"]) < float(row["vwap"]) if direction == "long" else float(row["close"]) > float(row["vwap"])
        # ponytail: 5m candle path unknown; count stop before target if both fit in one candle.
        if stopped:
            return "stop", -1.0, int(i + 1)
        if hit2:
            return "tp2", 1.8, int(i + 1)
        if hit1:
            return "tp1", 1.0, int(i + 1)
        if vwap_fail:
            r = (float(row["close"]) - entry) / abs(entry - stop) if direction == "long" else (entry - float(row["close"])) / abs(entry - stop)
            return "vwap_fail", float(r), int(i + 1)
    last = future.iloc[-1] if not future.empty else None
    if last is None:
        return "no_future", 0.0, 0
    r = (float(last["close"]) - entry) / abs(entry - stop) if direction == "long" else (entry - float(last["close"])) / abs(entry - stop)
    return "timeout", float(r), len(future)


def observe(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    events: list[dict] = []
    allowed = {"long", "short"} if args.direction == "both" else {args.direction}
    for _, rows in bars.groupby("session_date", sort=True):
        rows = rows.reset_index(drop=True)
        for i in range(1, len(rows) - 1):
            pullback = rows.iloc[i - 1]
            recovery = rows.iloc[i]
            future = rows.iloc[i + 1 : i + 1 + args.max_hold_bars].reset_index(drop=True)
            low_touched = float(pullback["low"]) <= max(float(pullback["vwap"]), float(pullback["ema"]))
            high_touched = float(pullback["high"]) >= min(float(pullback["vwap"]), float(pullback["ema"]))
            low_volume = float(pullback["volume"]) <= float(pullback["volume_median"]) * args.pullback_volume_max
            if "long" in allowed:
                trend = float(recovery["ema"]) > float(recovery["ema_prev"]) and float(recovery["close"]) > float(recovery["vwap"])
                confirm = float(recovery["close"]) > float(recovery["open"]) and float(recovery["close"]) > float(pullback["high"])
                if trend and low_touched and low_volume and confirm:
                    blocked, strong_session_trend, weekly_agreement = filters_block(recovery, "long", args)
                    if blocked:
                        continue
                    entry = float(future.iloc[0]["open"]) if not future.empty else float(recovery["close"])
                    stop = min(float(pullback["low"]), entry - args.atr_stop_mult * float(recovery["atr"]))
                    if stop < entry:
                        risk = entry - stop
                        reason, net_r, bars_held = hit_outcome(future, "long", entry, stop, entry + risk, entry + 1.8 * risk)
                        events.append(event_row(recovery, pullback, "long", entry, stop, reason, net_r, bars_held, strong_session_trend, weekly_agreement))
            if "short" in allowed:
                trend = float(recovery["ema"]) < float(recovery["ema_prev"]) and float(recovery["close"]) < float(recovery["vwap"])
                confirm = float(recovery["close"]) < float(recovery["open"]) and float(recovery["close"]) < float(pullback["low"])
                if trend and high_touched and low_volume and confirm:
                    blocked, strong_session_trend, weekly_agreement = filters_block(recovery, "short", args)
                    if blocked:
                        continue
                    entry = float(future.iloc[0]["open"]) if not future.empty else float(recovery["close"])
                    stop = max(float(pullback["high"]), entry + args.atr_stop_mult * float(recovery["atr"]))
                    if stop > entry:
                        risk = stop - entry
                        reason, net_r, bars_held = hit_outcome(future, "short", entry, stop, entry - risk, entry - 1.8 * risk)
                        events.append(event_row(recovery, pullback, "short", entry, stop, reason, net_r, bars_held, strong_session_trend, weekly_agreement))
    return pd.DataFrame(events)


def event_row(
    recovery: pd.Series,
    pullback: pd.Series,
    direction: str,
    entry: float,
    stop: float,
    reason: str,
    net_r: float,
    bars_held: int,
    strong_session_trend: bool,
    weekly_agreement: bool,
) -> dict:
    return {
        "session_date": recovery["session_date"],
        "direction": direction,
        "signal_timestamp_ms": int(recovery["timestamp_ms"]),
        "entry": entry,
        "stop": stop,
        "risk_pct": abs(entry - stop) / entry,
        "pullback_volume_ratio": float(pullback["volume"] / pullback["volume_median"]),
        "close_vs_vwap_pct": float(recovery["close"] / recovery["vwap"] - 1.0),
        "close_vs_ema_pct": float(recovery["close"] / recovery["ema"] - 1.0),
        "session_return_pct": float(recovery["session_return_pct"]),
        "session_move_atr": float(recovery["session_move_atr"]),
        "ema_slope_atr": float(recovery["ema_slope_atr"]),
        "weekly_vwap": float(recovery["weekly_vwap"]),
        "close_vs_weekly_vwap_pct": float(recovery["close"] / recovery["weekly_vwap"] - 1.0),
        "weekly_vwap_slope_pct": float(recovery["weekly_vwap_slope_pct"]),
        "strong_session_trend": bool(strong_session_trend),
        "weekly_vwap_agreement": bool(weekly_agreement),
        "exit_reason": reason,
        "net_R": net_r,
        "bars_held_5m": bars_held,
        "minutes_held": bars_held * 5,
    }


def summarize(events: pd.DataFrame) -> dict:
    if events.empty:
        return {"events": 0}
    wins = events[events["net_R"] > 0]
    losses = events[events["net_R"] <= 0]
    return {
        "events": int(len(events)),
        "avg_R": float(events["net_R"].mean()),
        "median_R": float(events["net_R"].median()),
        "win_rate": float((events["net_R"] > 0).mean()),
        "profit_factor": float(wins["net_R"].sum() / abs(losses["net_R"].sum())) if not losses.empty and losses["net_R"].sum() else None,
        "avg_minutes_held": float(events["minutes_held"].mean()),
        "median_minutes_held": float(events["minutes_held"].median()),
        "by_direction": events.groupby("direction")["net_R"].agg(["count", "mean", "median"]).to_dict("index"),
        "by_exit": events.groupby("exit_reason")["net_R"].agg(["count", "mean"]).to_dict("index"),
    }


def write_outputs(output_dir: Path, events: pd.DataFrame, summary: dict, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    events.to_csv(output_dir / "events.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "config.json").write_text(json.dumps(vars(args), indent=2, sort_keys=True), encoding="utf-8")


def self_check() -> None:
    future = pd.DataFrame([{"open": 10, "high": 11, "low": 9.8, "close": 10.8, "vwap": 10.1}])
    assert hit_outcome(future, "long", 10.0, 9.0, 11.0, 11.8) == ("tp1", 1.0, 1)
    assert parse_hhmm("08:00") == time(8, 0)
    print("self_check_ok")


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        return
    if not args.start_date or not args.end_date or not args.output_dir:
        raise SystemExit("--start-date, --end-date, and --output-dir are required unless --self-check is used")
    tz = ZoneInfo(args.session_tz)
    one_minute = load_1m(Path(args.candles), date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), tz)
    bars = to_5m(one_minute, tz, parse_hhmm(args.session_start), parse_hhmm(args.session_end), args)
    events = observe(bars, args)
    summary = summarize(events)
    write_outputs(Path(args.output_dir), events, summary, args)
    print(json.dumps({"bars_5m": len(bars), "events": len(events), "output_dir": args.output_dir}, indent=2))


if __name__ == "__main__":
    main()
