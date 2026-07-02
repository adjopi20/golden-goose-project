from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicator.volume_profile import build_volume_profile  # noqa: E402


RAW_DEFAULT = ROOT / "storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2024-06_to_2026-05.parquet"
BARS_DEFAULT = ROOT / "models/orb/runs/20260626_013_backward_profile_behavior/outputs/rr_check/one_minute_bars_cache.parquet"
WIB = "Asia/Jakarta"
NY = "America/New_York"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Find ORB V2 mean-reversion candidates from prior-session structural levels.")
    p.add_argument("--ny-date", required=True)
    p.add_argument("--bias", required=True, choices=["long", "short"])
    p.add_argument("--out", required=True)
    p.add_argument("--raw", default=str(RAW_DEFAULT))
    p.add_argument("--bars", default=str(BARS_DEFAULT))
    p.add_argument("--sl-buffer", type=float, default=0.001)
    p.add_argument("--min-current-delta", type=float, default=500.0)
    p.add_argument("--cluster-minutes", type=int, default=15)
    p.add_argument("--cooldown-minutes", type=int, default=30)
    return p.parse_args()


def ny_time(day: pd.Timestamp, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day.date()} {hhmm}", tz=NY).tz_convert(WIB)


def ms(value: pd.Timestamp) -> int:
    return int(value.tz_convert("UTC").timestamp() * 1000)


def load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index).tz_convert(WIB)
    return df.sort_index()


def load_raw(path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_parquet(
        path,
        columns=["price", "qty", "timestamp", "is_buyer_maker"],
        filters=[("timestamp", ">=", ms(start)), ("timestamp", "<", ms(end))],
    )
    df["dt_wib"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(WIB)
    return df.sort_values("timestamp").reset_index(drop=True)


def session_windows(ny_date: str) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    day = pd.Timestamp(ny_date)
    prev = day - pd.Timedelta(days=1)
    return [
        ("previous_ny", ny_time(prev, "09:30"), ny_time(prev, "17:30")),
        ("overnight_1730_0130", ny_time(prev, "17:30"), ny_time(day, "01:30")),
        ("pre_ny_0130_0930", ny_time(day, "01:30"), ny_time(day, "09:30")),
    ]


def build_levels(bars: pd.DataFrame, raw_path: Path, ny_date: str) -> tuple[pd.DataFrame, dict]:
    rows = []
    for name, start, end in session_windows(ny_date):
        w = bars.loc[start:end - pd.Timedelta(minutes=1)]
        rows.append({"level_set": name, "window_wib": f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}", "high": float(w["high"].max()), "low": float(w["low"].min())})

    day = pd.Timestamp(ny_date)
    prior_start = ny_time(day - pd.Timedelta(days=1), "09:30")
    prior_end = ny_time(day, "09:30")
    raw = load_raw(raw_path, prior_start, prior_end)
    profile = build_volume_profile(raw[["price", "qty", "is_buyer_maker"]], n_bins=50, value_area_pct=0.70)
    rows.append({"level_set": "prior_24h_profile", "window_wib": f"{prior_start:%Y-%m-%d %H:%M} to {prior_end:%Y-%m-%d %H:%M}", "high": profile["session_high"], "low": profile["session_low"]})
    profile_row = {
        "profile_low": profile["session_low"],
        "val": profile["val"],
        "poc": profile["poc_price"],
        "vah": profile["vah"],
        "profile_high": profile["session_high"],
    }
    return pd.DataFrame(rows), profile_row


def scan_short(bars: pd.DataFrame, levels: dict[str, float], profile: dict, args: argparse.Namespace) -> list[dict]:
    day = pd.Timestamp(args.ny_date)
    start = ny_time(day, "09:45")
    end = ny_time(day, "17:30")
    w = bars.loc[start:end]
    candidates = []
    active = False
    swept: set[str] = set()
    cluster_start = None
    sweep_high = None
    peak_time = None
    cooldown_until = pd.Timestamp.min.tz_localize("UTC").tz_convert(WIB)

    for t, row in w.iterrows():
        if t < cooldown_until:
            continue
        touched = [name for name, price in levels.items() if float(row["high"]) >= price]
        if touched:
            if not active:
                active = True
                cluster_start = t
                swept = set()
                sweep_high = float(row["high"])
                peak_time = t
            swept.update(touched)
            if float(row["high"]) >= float(sweep_high):
                sweep_high = float(row["high"])
                peak_time = t

        if not active:
            continue

        min_swept = min(levels[name] for name in swept)
        expired = t - cluster_start > pd.Timedelta(minutes=args.cluster_minutes)
        current_delta_ok = float(row["delta"]) <= -abs(args.min_current_delta)
        rejected = float(row["close"]) < min_swept and float(row["close"]) < float(row["open"])
        flow_start = peak_time + pd.Timedelta(minutes=1)
        if rejected and current_delta_ok and flow_start <= t:
            flow = bars.loc[flow_start:t]
            if float(flow["delta"].sum()) < 0:
                candidates.append(
                    {
                        "date": args.ny_date,
                        "name": f"{pd.Timestamp(args.ny_date):%b%d}_MR{len(candidates)+1}_STRUCTURAL_HIGH_REJECTION_SHORT".upper(),
                        "entry_model": "mean_reversion",
                        "type": "structural_high_rejection",
                        "direction": "short",
                        "signal_time": f"{t:%Y-%m-%d %H:%M}",
                        "entry_time": f"{t + pd.Timedelta(minutes=1):%Y-%m-%d %H:%M}",
                        "sl": round(float(sweep_high) + args.sl_buffer, 6),
                        "tp1_price": profile["poc"],
                        "tp2_price": profile["val"],
                        "protect_after_tp1": True,
                        "flow_start": f"{flow_start:%Y-%m-%d %H:%M}",
                        "flow_end": f"{t:%Y-%m-%d %H:%M}",
                        "basis": f"Short bias; swept {','.join(sorted(swept))}; closed back below {min_swept:.5f}; sweep high {sweep_high:.5f}.",
                    }
                )
                cooldown_until = t + pd.Timedelta(minutes=args.cooldown_minutes)
            active = False
            swept = set()
        elif expired:
            active = False
            swept = set()
    return candidates


def scan_long(bars: pd.DataFrame, levels: dict[str, float], profile: dict, args: argparse.Namespace) -> list[dict]:
    day = pd.Timestamp(args.ny_date)
    start = ny_time(day, "09:45")
    end = ny_time(day, "17:30")
    w = bars.loc[start:end]
    candidates = []
    active = False
    swept: set[str] = set()
    cluster_start = None
    sweep_low = None
    trough_time = None
    cooldown_until = pd.Timestamp.min.tz_localize("UTC").tz_convert(WIB)

    for t, row in w.iterrows():
        if t < cooldown_until:
            continue
        touched = [name for name, price in levels.items() if float(row["low"]) <= price]
        if touched:
            if not active:
                active = True
                cluster_start = t
                swept = set()
                sweep_low = float(row["low"])
                trough_time = t
            swept.update(touched)
            if float(row["low"]) <= float(sweep_low):
                sweep_low = float(row["low"])
                trough_time = t

        if not active:
            continue

        max_swept = max(levels[name] for name in swept)
        expired = t - cluster_start > pd.Timedelta(minutes=args.cluster_minutes)
        current_delta_ok = float(row["delta"]) >= abs(args.min_current_delta)
        reclaimed = float(row["close"]) > max_swept and float(row["close"]) > float(row["open"])
        flow_start = trough_time + pd.Timedelta(minutes=1)
        if reclaimed and current_delta_ok and flow_start <= t:
            flow = bars.loc[flow_start:t]
            if float(flow["delta"].sum()) > 0:
                candidates.append(
                    {
                        "date": args.ny_date,
                        "name": f"{pd.Timestamp(args.ny_date):%b%d}_MR{len(candidates)+1}_STRUCTURAL_LOW_RECLAIM_LONG".upper(),
                        "entry_model": "mean_reversion",
                        "type": "structural_low_reclaim",
                        "direction": "long",
                        "signal_time": f"{t:%Y-%m-%d %H:%M}",
                        "entry_time": f"{t + pd.Timedelta(minutes=1):%Y-%m-%d %H:%M}",
                        "sl": round(float(sweep_low) - args.sl_buffer, 6),
                        "tp1_price": profile["poc"],
                        "tp2_price": profile["vah"],
                        "protect_after_tp1": True,
                        "flow_start": f"{flow_start:%Y-%m-%d %H:%M}",
                        "flow_end": f"{t:%Y-%m-%d %H:%M}",
                        "basis": f"Long bias; swept {','.join(sorted(swept))}; closed back above {max_swept:.5f}; sweep low {sweep_low:.5f}.",
                    }
                )
                cooldown_until = t + pd.Timedelta(minutes=args.cooldown_minutes)
            active = False
            swept = set()
        elif expired:
            active = False
            swept = set()
    return candidates


def main() -> None:
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bars = load_bars(Path(args.bars))
    levels_df, profile = build_levels(bars, Path(args.raw), args.ny_date)
    levels_df.to_csv(out / "session_levels.csv", index=False)
    pd.DataFrame([profile]).to_csv(out / "prior_24h_profile.csv", index=False)

    if args.bias == "short":
        level_map = {f"{row.level_set}_high": float(row.high) for row in levels_df.itertuples() if row.level_set != "prior_24h_profile"}
        candidates = scan_short(bars, level_map, profile, args)
    else:
        level_map = {f"{row.level_set}_low": float(row.low) for row in levels_df.itertuples() if row.level_set != "prior_24h_profile"}
        candidates = scan_long(bars, level_map, profile, args)
    pd.DataFrame(candidates).to_csv(out / "mean_reversion_candidates.csv", index=False)
    print(out / "mean_reversion_candidates.csv")


if __name__ == "__main__":
    main()
