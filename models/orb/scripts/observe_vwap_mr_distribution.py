from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.vwap import vwap, vwap_std


DEFAULT_BINS = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, math.inf)
HORIZONS = (5, 15, 30, 60, 120)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Observe VWAP z-score mean-reversion distribution.")
    p.add_argument("--aggtrades", required=True)
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--anchor-period", choices=["session", "week"], default="session")
    p.add_argument("--session-tz", default="America/New_York")
    p.add_argument("--session-start", default="09:30")
    p.add_argument("--session-end", default="17:30")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--reset-z", type=float, default=0.5)
    p.add_argument("--adverse-z", type=float, default=1.0)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def load_trades(path: Path, start: date, end: date, tz: ZoneInfo) -> pd.DataFrame:
    start_ms = ms(datetime.combine(start, time.min, tz))
    end_ms = ms(datetime.combine(end + timedelta(days=1), time.min, tz))
    df = pd.read_parquet(
        path,
        columns=["timestamp", "price", "qty", "is_buyer_maker"],
        filters=[("timestamp", ">=", start_ms), ("timestamp", "<", end_ms)],
    )
    return df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)


def build_session_bars(
    trades: pd.DataFrame,
    symbol: str,
    tz: ZoneInfo,
    start_time: time,
    end_time: time,
    anchor_period: str,
) -> pd.DataFrame:
    bars = pd.DataFrame(aggregate_trades_to_ohlcv(trades, symbol=symbol, timeframe="1m"))
    if bars.empty:
        return bars
    local = pd.to_datetime(bars["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(tz)
    bars["session_date"] = local.dt.date.astype(str)
    bars["session_time"] = local.dt.time
    bars["session_phase"] = pd.cut(
        local.dt.hour * 60 + local.dt.minute,
        bins=[0, 10 * 60 + 30, 14 * 60, 24 * 60],
        labels=["open", "mid", "late"],
        right=False,
    )
    if anchor_period == "session":
        bars = bars[(bars["session_time"] >= start_time) & (bars["session_time"] < end_time)].copy()
        bars["anchor_key"] = bars["session_date"]
    else:
        iso = local.dt.isocalendar()
        bars["anchor_key"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
        bars["vwap"] = vwap(bars["close"], bars["volume"], bars["anchor_key"])
        bars["vwap_std"] = vwap_std(bars["close"], bars["volume"], bars["vwap"], bars["anchor_key"])
        bars = bars[(bars["session_time"] >= start_time) & (bars["session_time"] < end_time)].copy()
    if anchor_period == "session":
        bars["vwap"] = vwap(bars["close"], bars["volume"], bars["anchor_key"])
        bars["vwap_std"] = vwap_std(bars["close"], bars["volume"], bars["vwap"], bars["anchor_key"])
    bars["vwap_z"] = (bars["close"] - bars["vwap"]) / bars["vwap_std"].replace(0, pd.NA)
    return bars.dropna(subset=["vwap_z"]).reset_index(drop=True)


def z_bin(abs_z: float) -> str:
    prev = DEFAULT_BINS[0]
    for edge in DEFAULT_BINS[1:]:
        if math.isinf(edge):
            return f">={prev:g}"
        if abs_z < edge:
            return f"{prev:g}-{edge:g}"
        prev = edge
    return f">={prev:g}"


def first_index(mask: pd.Series) -> int | None:
    hits = mask[mask].index
    return int(hits[0]) if len(hits) else None


def observe_event(rows: pd.DataFrame, i: int, side: str, args: argparse.Namespace) -> dict:
    event = rows.iloc[i]
    future = rows.iloc[i + 1 :].reset_index(drop=True)
    event_z = float(event["vwap_z"])
    event_std = float(event["vwap_std"])
    event_vwap = float(event["vwap"])
    if side == "long":
        vwap_hit = first_index(future["high"] >= event_vwap)
        adverse_hit = first_index(future["low"] <= event_vwap + (event_z - args.adverse_z) * event_std)
        mfe = ((future["high"].max() if not future.empty else event["close"]) - event["close"]) / event["close"]
        mae = (event["close"] - (future["low"].min() if not future.empty else event["close"])) / event["close"]
    else:
        vwap_hit = first_index(future["low"] <= event_vwap)
        adverse_hit = first_index(future["high"] >= event_vwap + (event_z + args.adverse_z) * event_std)
        mfe = (event["close"] - (future["low"].min() if not future.empty else event["close"])) / event["close"]
        mae = ((future["high"].max() if not future.empty else event["close"]) - event["close"]) / event["close"]
    success = vwap_hit is not None and (adverse_hit is None or vwap_hit < adverse_hit)
    out = {
        "session_date": event["session_date"],
        "anchor_key": event["anchor_key"],
        "session_phase": str(event["session_phase"]),
        "side": side,
        "z_bin": z_bin(abs(event_z)),
        "timestamp_ms": int(event["timestamp_ms"]),
        "close": float(event["close"]),
        "vwap": event_vwap,
        "vwap_std": event_std,
        "vwap_z": event_z,
        "success_to_vwap_before_adverse": bool(success),
        "minutes_to_vwap": vwap_hit + 1 if vwap_hit is not None else math.nan,
        "mfe_pct": float(mfe),
        "mae_pct": float(mae),
    }
    for horizon in HORIZONS:
        window = future.head(horizon)
        out[f"return_to_vwap_within_{horizon}m"] = bool(
            ((window["high"] >= event_vwap).any() if side == "long" else (window["low"] <= event_vwap).any())
        )
        out[f"forward_return_{horizon}m"] = (
            float(window.iloc[-1]["close"] / event["close"] - 1.0) if side == "long" and not window.empty
            else float(event["close"] / window.iloc[-1]["close"] - 1.0) if not window.empty
            else math.nan
        )
    return out


def observe_events(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    events: list[dict] = []
    for _, rows in bars.groupby("session_date", sort=True):
        rows = rows.reset_index(drop=True)
        active = {"long": False, "short": False}
        for i, row in rows.iterrows():
            z = float(row["vwap_z"])
            if abs(z) <= args.reset_z:
                active = {"long": False, "short": False}
            side = "long" if z <= -1.0 else "short" if z >= 1.0 else None
            if side and not active[side]:
                events.append(observe_event(rows, int(i), side, args))
                active[side] = True
    return pd.DataFrame(events)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    grouped = events.groupby(["z_bin", "side", "session_phase"], observed=True)
    out = grouped.agg(
        events=("timestamp_ms", "size"),
        success_rate_to_vwap=("success_to_vwap_before_adverse", "mean"),
        median_minutes_to_vwap=("minutes_to_vwap", "median"),
        p75_minutes_to_vwap=("minutes_to_vwap", lambda s: s.quantile(0.75)),
        avg_forward_return_15m=("forward_return_15m", "mean"),
        avg_forward_return_30m=("forward_return_30m", "mean"),
        avg_forward_return_60m=("forward_return_60m", "mean"),
        median_mfe_pct=("mfe_pct", "median"),
        median_mae_pct=("mae_pct", "median"),
    ).reset_index()
    out["failed_continuation_rate"] = 1.0 - out["success_rate_to_vwap"]
    return out.sort_values(["side", "session_phase", "z_bin"]).reset_index(drop=True)


def write_outputs(output_dir: Path, events: pd.DataFrame, summary: pd.DataFrame, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    events.to_csv(output_dir / "events.csv", index=False)
    summary.to_csv(output_dir / "distribution.csv", index=False)
    (output_dir / "config.json").write_text(json.dumps(vars(args), indent=2, sort_keys=True), encoding="utf-8")


def self_check() -> None:
    assert z_bin(1.2) == "1-1.5"
    assert z_bin(4.2) == ">=4"
    s = pd.Series([False, True, False])
    assert first_index(s) == 1
    print("self_check_ok")


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        return
    tz = ZoneInfo(args.session_tz)
    trades = load_trades(Path(args.aggtrades), date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), tz)
    bars = build_session_bars(
        trades,
        Path(args.aggtrades).stem.split("-")[0],
        tz,
        parse_hhmm(args.session_start),
        parse_hhmm(args.session_end),
        args.anchor_period,
    )
    events = observe_events(bars, args)
    summary = summarize(events)
    write_outputs(Path(args.output_dir), events, summary, args)
    print(json.dumps({"bars": len(bars), "events": len(events), "output_dir": args.output_dir}, indent=2))


if __name__ == "__main__":
    main()
