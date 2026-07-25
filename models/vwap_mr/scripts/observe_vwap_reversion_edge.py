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
DEFAULT_Z_EDGES = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Observe raw VWAP z-score mean-reversion edge.")
    p.add_argument("--candles", default=str(DEFAULT_CANDLES))
    p.add_argument("--start-date")
    p.add_argument("--end-date")
    p.add_argument("--session-tz", default="UTC")
    p.add_argument("--session-start", default="08:00")
    p.add_argument("--session-end", default="16:00")
    p.add_argument("--bar-minutes", type=int, default=5)
    p.add_argument("--anchor-period", choices=["session", "week", "both"], default="session")
    p.add_argument("--output-dir")
    p.add_argument("--min-abs-z", type=float, default=1.0)
    p.add_argument("--reset-z", type=float, default=0.5)
    p.add_argument("--adverse-frac", type=float, default=0.5)
    p.add_argument("--horizons", default="15,30,60,120")
    p.add_argument("--ema-span", type=int, default=20)
    p.add_argument("--ema-slope-bars", type=int, default=3)
    p.add_argument("--atr-span", type=int, default=14)
    p.add_argument("--session-trend-atr-max", type=float, default=1.5)
    p.add_argument("--ema-slope-atr-max", type=float, default=0.35)
    p.add_argument("--weekly-slope-bars", type=int, default=12)
    p.add_argument("--warmup-days", type=int, default=7)
    p.add_argument("--self-check", action="store_true")
    return p.parse_args()


def parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def load_1m(path: Path, start: date, end: date, tz: ZoneInfo, warmup_days: int) -> pd.DataFrame:
    start_ms = ms(datetime.combine(start - timedelta(days=warmup_days), time.min, tz))
    end_ms = ms(datetime.combine(end + timedelta(days=1), time.min, tz))
    return pd.read_parquet(
        path,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume"],
        filters=[("timestamp_ms", ">=", start_ms), ("timestamp_ms", "<", end_ms)],
    ).sort_values("timestamp_ms", kind="mergesort")


def weighted_anchor(df: pd.DataFrame, key: pd.Series, name: str) -> pd.DataFrame:
    out = df.copy()
    pv = out["close"] * out["volume"]
    p2v = out["close"] * out["close"] * out["volume"]
    vol_sum = out.groupby(key)["volume"].cumsum()
    vwap = pv.groupby(key).cumsum() / vol_sum.replace(0, pd.NA)
    variance = p2v.groupby(key).cumsum() / vol_sum.replace(0, pd.NA) - vwap * vwap
    out[f"{name}_vwap"] = vwap
    out[f"{name}_vwap_std"] = variance.clip(lower=0).pow(0.5)
    out[f"{name}_vwap_z"] = (out["close"] - out[f"{name}_vwap"]) / out[f"{name}_vwap_std"].replace(0, pd.NA)
    return out


def to_bars(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    tz = ZoneInfo(args.session_tz)
    dt = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    bars = (
        df.assign(dt=dt)
        .set_index("dt")
        .resample(f"{args.bar_minutes}min")
        .agg({"timestamp_ms": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["timestamp_ms", "open", "high", "low", "close"])
        .reset_index(drop=True)
    )
    local = pd.to_datetime(bars["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(tz)
    bars["session_date"] = local.dt.date.astype(str)
    bars["session_time"] = local.dt.time
    bars["month"] = local.dt.strftime("%Y-%m")
    iso = local.dt.isocalendar()
    bars["week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    bars = weighted_anchor(bars, bars["session_date"], "session")
    bars = weighted_anchor(bars, bars["week"], "week")
    bars["week_vwap_prev"] = bars.groupby("week")["week_vwap"].shift(args.weekly_slope_bars)
    bars["week_vwap_slope_pct"] = bars["week_vwap"] / bars["week_vwap_prev"] - 1.0
    bars["ema"] = bars.groupby("session_date")["close"].transform(lambda s: s.ewm(span=args.ema_span, adjust=False).mean())
    bars["ema_prev"] = bars.groupby("session_date")["ema"].shift(args.ema_slope_bars)
    prev_close = bars.groupby("session_date")["close"].shift(1)
    tr = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    bars["atr"] = tr.groupby(bars["session_date"]).transform(lambda s: s.ewm(span=args.atr_span, adjust=False).mean())
    bars["session_open"] = bars.groupby("session_date")["open"].transform("first")
    bars["session_return_pct"] = bars["close"] / bars["session_open"] - 1.0
    bars["session_move_atr"] = (bars["close"] - bars["session_open"]).abs() / bars["atr"]
    bars["ema_slope_atr"] = (bars["ema"] - bars["ema_prev"]).abs() / bars["atr"]
    start_time = parse_hhmm(args.session_start)
    end_time = parse_hhmm(args.session_end)
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    in_date = (local.dt.date >= start) & (local.dt.date <= end)
    in_session = (bars["session_time"] >= start_time) & (bars["session_time"] < end_time)
    return bars[in_date & in_session].reset_index(drop=True)


def z_bin(abs_z: float) -> str:
    prev = DEFAULT_Z_EDGES[0]
    for edge in DEFAULT_Z_EDGES[1:]:
        if abs_z < edge:
            return f"{prev:g}-{edge:g}"
        prev = edge
    return f">={prev:g}"


def first_hit(mask: pd.Series) -> int | None:
    hits = mask[mask].index
    return int(hits[0]) if len(hits) else None


def hit_minutes(hit: int | None, bar_minutes: int) -> float:
    return math.nan if hit is None else float((hit + 1) * bar_minutes)


def observe_one(rows: pd.DataFrame, i: int, anchor: str, args: argparse.Namespace, horizons: list[int]) -> dict:
    row = rows.iloc[i]
    future = rows.iloc[i + 1 :].reset_index(drop=True)
    close = float(row["close"])
    vwap = float(row[f"{anchor}_vwap"])
    z = float(row[f"{anchor}_vwap_z"])
    distance = abs(close - vwap)
    side = "short" if z > 0 else "long"
    if side == "short":
        target_25 = close - 0.25 * distance
        target_50 = close - 0.50 * distance
        adverse = close + args.adverse_frac * distance
        hit_25 = first_hit(future["low"] <= target_25)
        hit_50 = first_hit(future["low"] <= target_50)
        hit_vwap = first_hit(future["low"] <= vwap)
        hit_adverse = first_hit(future["high"] >= adverse)
        mfe = (close - future["low"].min()) / close if not future.empty else 0.0
        mae = (future["high"].max() - close) / close if not future.empty else 0.0
    else:
        target_25 = close + 0.25 * distance
        target_50 = close + 0.50 * distance
        adverse = close - args.adverse_frac * distance
        hit_25 = first_hit(future["high"] >= target_25)
        hit_50 = first_hit(future["high"] >= target_50)
        hit_vwap = first_hit(future["high"] >= vwap)
        hit_adverse = first_hit(future["low"] <= adverse)
        mfe = (future["high"].max() - close) / close if not future.empty else 0.0
        mae = (close - future["low"].min()) / close if not future.empty else 0.0
    strong_trend = float(row["session_move_atr"]) > args.session_trend_atr_max or float(row["ema_slope_atr"]) > args.ema_slope_atr_max
    weekly_agreement = (side == "short" and close > float(row["week_vwap"]) and float(row["week_vwap_slope_pct"]) > 0) or (
        side == "long" and close < float(row["week_vwap"]) and float(row["week_vwap_slope_pct"]) < 0
    )
    out = {
        "anchor_period": anchor,
        "session_date": row["session_date"],
        "month": row["month"],
        "side": side,
        "z_bin": z_bin(abs(z)),
        "timestamp_ms": int(row["timestamp_ms"]),
        "close": close,
        "vwap": vwap,
        "vwap_z": z,
        "abs_z": abs(z),
        "session_return_pct": float(row["session_return_pct"]),
        "session_move_atr": float(row["session_move_atr"]),
        "ema_slope_atr": float(row["ema_slope_atr"]),
        "strong_session_trend": bool(strong_trend),
        "week_vwap_slope_pct": float(row["week_vwap_slope_pct"]),
        "weekly_vwap_agreement": bool(weekly_agreement),
        "revert_25_before_adverse": hit_25 is not None and (hit_adverse is None or hit_25 < hit_adverse),
        "revert_50_before_adverse": hit_50 is not None and (hit_adverse is None or hit_50 < hit_adverse),
        "vwap_before_adverse": hit_vwap is not None and (hit_adverse is None or hit_vwap < hit_adverse),
        "minutes_to_25": hit_minutes(hit_25, args.bar_minutes),
        "minutes_to_50": hit_minutes(hit_50, args.bar_minutes),
        "minutes_to_vwap": hit_minutes(hit_vwap, args.bar_minutes),
        "mfe_pct": float(mfe),
        "mae_pct": float(mae),
    }
    for horizon in horizons:
        bars = math.ceil(horizon / args.bar_minutes)
        window = future.head(bars)
        if window.empty:
            out[f"fade_return_{horizon}m"] = math.nan
            out[f"vwap_touch_{horizon}m"] = False
            continue
        end_close = float(window.iloc[-1]["close"])
        out[f"fade_return_{horizon}m"] = (close / end_close - 1.0) if side == "short" else (end_close / close - 1.0)
        out[f"vwap_touch_{horizon}m"] = bool((window["low"] <= vwap).any() if side == "short" else (window["high"] >= vwap).any())
    return out


def observe_events(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    anchors = ["session", "week"] if args.anchor_period == "both" else [args.anchor_period]
    events: list[dict] = []
    for anchor in anchors:
        needed = [f"{anchor}_vwap", f"{anchor}_vwap_z", "week_vwap", "week_vwap_slope_pct", "ema_prev", "atr"]
        anchor_bars = bars.dropna(subset=needed).reset_index(drop=True)
        for _, rows in anchor_bars.groupby("session_date", sort=True):
            rows = rows.reset_index(drop=True)
            seen = {"long": set(), "short": set()}
            for i, row in rows.iloc[:-1].iterrows():
                z = float(row[f"{anchor}_vwap_z"])
                if abs(z) <= args.reset_z:
                    seen = {"long": set(), "short": set()}
                    continue
                if abs(z) < args.min_abs_z:
                    continue
                side = "short" if z > 0 else "long"
                bucket = z_bin(abs(z))
                if bucket in seen[side]:
                    continue
                events.append(observe_one(rows, int(i), anchor, args, horizons))
                seen[side].add(bucket)
    return pd.DataFrame(events)


def summarize(events: pd.DataFrame, group_cols: list[str], horizons: list[int]) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    aggs = {
        "events": ("timestamp_ms", "size"),
        "revert_25_before_adverse_rate": ("revert_25_before_adverse", "mean"),
        "revert_50_before_adverse_rate": ("revert_50_before_adverse", "mean"),
        "vwap_before_adverse_rate": ("vwap_before_adverse", "mean"),
        "median_minutes_to_vwap": ("minutes_to_vwap", "median"),
        "median_mfe_pct": ("mfe_pct", "median"),
        "median_mae_pct": ("mae_pct", "median"),
    }
    for horizon in horizons:
        aggs[f"vwap_touch_{horizon}m_rate"] = (f"vwap_touch_{horizon}m", "mean")
        aggs[f"avg_fade_return_{horizon}m"] = (f"fade_return_{horizon}m", "mean")
    return events.groupby(group_cols, dropna=False).agg(**aggs).reset_index()


def write_outputs(output_dir: Path, events: pd.DataFrame, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    distribution = summarize(events, ["anchor_period", "side", "z_bin"], horizons)
    by_regime = summarize(events, ["anchor_period", "side", "z_bin", "strong_session_trend", "weekly_vwap_agreement"], horizons)
    by_month = summarize(events, ["month", "anchor_period", "side", "z_bin"], horizons)
    events.to_csv(output_dir / "events.csv", index=False)
    distribution.to_csv(output_dir / "distribution.csv", index=False)
    by_regime.to_csv(output_dir / "by_regime.csv", index=False)
    by_month.to_csv(output_dir / "by_month.csv", index=False)
    summary = {
        "events": int(len(events)),
        "anchors": sorted(events["anchor_period"].unique().tolist()) if not events.empty else [],
        "outputs": ["events.csv", "distribution.csv", "by_regime.csv", "by_month.csv"],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "config.json").write_text(json.dumps(vars(args), indent=2, sort_keys=True), encoding="utf-8")


def self_check() -> None:
    assert parse_hhmm("08:00") == time(8, 0)
    assert z_bin(1.2) == "1-1.5"
    assert z_bin(4.1) == ">=4"
    assert first_hit(pd.Series([False, True])) == 1
    assert math.isnan(hit_minutes(None, 5))
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
    events = observe_events(bars, args)
    write_outputs(Path(args.output_dir), events, args)
    print(json.dumps({"bars": len(bars), "events": len(events), "output_dir": args.output_dir}, indent=2))


if __name__ == "__main__":
    main()
