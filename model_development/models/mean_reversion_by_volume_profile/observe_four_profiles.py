"""Count sessions moving away from each frozen POC and returning after 13:00 NY."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from observe import iso, load_window, metadata

NY = ZoneInfo("America/New_York")
PROFILE_NAMES = ("ny_0930_1300", "pre_ny_0130_0930", "previous_day_0930_0930", "previous_0930_today_1300")


def clocks(day):
    def stamp(d, clock):
        return int(datetime.combine(d, time.fromisoformat(clock), NY).timestamp() * 1000)
    return (stamp(day - timedelta(days=1), "09:30"), stamp(day, "01:30"),
            stamp(day, "09:30"), stamp(day, "13:00"), stamp(day, "17:00"))


def window(ts, start, end):
    return slice(int(np.searchsorted(ts, start)), int(np.searchsorted(ts, end)))


def quality(ts, price, qty, ids, start, end, cfg):
    if not len(ts):
        return "no_data"
    if not np.isfinite(price).all() or not np.isfinite(qty).all() or (price <= 0).any() or (qty <= 0).any():
        return "invalid_price_or_volume"
    if (np.diff(ts) < 0).any() or (np.diff(ids) <= 0).any():
        return "unordered_or_duplicate"
    if (np.diff(ids) > 1).any():
        return "aggregate_id_gaps"
    if len(ts) > 1 and np.diff(ts).max() > cfg["max_internal_gap_seconds"] * 1000:
        return "large_trade_gap"
    if max(ts[0] - start, end - ts[-1]) > cfg["max_boundary_gap_seconds"] * 1000:
        return "boundary_gap"
    return "valid"


def departure_and_return(prices, future, poc, mode):
    initial_distance = abs(float(prices[0]) - poc)
    final_distance = abs(float(prices[-1]) - poc)
    distance = final_distance if mode == "endpoint" else float(np.abs(prices - poc).max())
    eligible = bool(distance > initial_distance)
    signs = np.sign(future - poc)
    previous_signs = np.r_[np.sign(float(prices[-1]) - poc), signs[:-1]]
    hits = np.flatnonzero((signs == 0) | (signs * previous_signs < 0)) if eligible else np.array([], dtype=int)
    return eligible, (int(hits[0]) if len(hits) else None), initial_distance, final_distance


def analyse(day, arrays, cfg, mode, builder):
    ts, price, qty, ids = arrays
    previous, pre_ny, opening, pivot, end = clocks(day)
    # searchsorted requires order; fail rather than silently sorting or deduplicating.
    if (np.diff(ts) < 0).any():
        return [dict(session_date=str(day), profile=name, status="unordered_timestamps") for name in PROFILE_NAMES]
    move = window(ts, opening, pivot)
    after = window(ts, pivot, end)
    move_quality = quality(*(a[move] for a in arrays), opening, pivot, cfg)
    after_quality = quality(*(a[after] for a in arrays), pivot, end, cfg)
    ranges = [(opening, pivot), (pre_ny, opening), (previous, opening), (previous, pivot)]
    rows = []
    for name, (start, finish) in zip(PROFILE_NAMES, ranges):
        row = dict(session_date=str(day), profile=name, profile_start_ny=iso(start),
                   profile_end_ny=iso(finish), return_start_ny=iso(pivot), return_end_ny=iso(end))
        part = window(ts, start, finish)
        status = quality(*(a[part] for a in arrays), start, finish, cfg)
        for prefix, q in [("departure", move_quality), ("return", after_quality)]:
            if status == "valid" and q != "valid":
                status = f"{prefix}_{q}"
        row["status"] = status
        if status != "valid":
            rows.append(row)
            continue
        vp = builder(pd.DataFrame({"price": price[part], "qty": qty[part]}), n_bins=cfg["profile_bins"])
        poc = float(vp["poc_price"])
        eligible, hit, d0, d1 = departure_and_return(price[move], price[after], poc, mode)
        row.update(poc=poc, poc_volume=float(vp["poc_volume"]), profile_volume=float(vp["total_volume"]),
                   price_0930=float(price[move][0]), price_before_1300=float(price[move][-1]),
                   distance_0930=d0, distance_before_1300=d1,
                   max_distance_0930_1300=float(np.abs(price[move] - poc).max()),
                   already_at_poc_before_1300=bool(price[move][-1] == poc),
                   moved_away=eligible, returned_to_poc=(hit is not None) if eligible else None)
        if hit is not None:
            row["first_return_ny"] = iso(ts[after][hit])
            row["first_return_wib"] = iso(ts[after][hit], ZoneInfo("Asia/Jakarta"))
            row["minutes_after_1300"] = float((ts[after][hit] - pivot) / 60000)
        rows.append(row)
    return rows


def summarize(df):
    result = []
    for (symbol, name), group in df.groupby(["symbol", "profile"], sort=False):
        valid = group.loc[group.status == "valid"]
        eligible = valid.loc[valid.moved_away.eq(True)] if "moved_away" in valid else valid.iloc[:0]
        hits = int(eligible.returned_to_poc.eq(True).sum()) if "returned_to_poc" in eligible else 0
        result.append(dict(symbol=symbol, profile=name, valid_sessions=len(valid),
                           moved_away_sessions=len(eligible), returned_sessions=hits,
                           returned_pct=100 * hits / len(eligible) if len(eligible) else None,
                           excluded_sessions=len(group) - len(valid)))
    return pd.DataFrame(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("four_profiles_config.json"))
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-10-05")
    parser.add_argument("--departure-mode", required=True, choices=["endpoint", "ever"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    from indicator.volume_profile import build_basic_volume_profile
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    begin, finish = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if finish < begin:
        raise ValueError("End before start")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    snapshot = dict(cfg, requested_start=args.start, requested_end=args.end,
                    departure_mode=args.departure_mode, return_window="13:00–17:00 America/New_York",
                    profiles=PROFILE_NAMES, builder="indicator.volume_profile.build_basic_volume_profile")
    (out / "config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    frames, sources = [], {}
    for symbol, relative in cfg["assets"].items():
        path = (args.root / relative).resolve()
        f, mins, maxs = metadata(path)
        sources[symbol] = dict(path=str(path), rows=f.metadata.num_rows,
                               min_timestamp=iso(mins.min()), max_timestamp=iso(maxs.max()),
                               bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns)
        rows = []
        for count in range((finish - begin).days + 1):
            day = begin + timedelta(days=count)
            previous, _, _, _, end = clocks(day)
            if end <= mins.min() or previous > maxs.max():
                daily = [dict(session_date=str(day), profile=p, status="outside_archive") for p in PROFILE_NAMES]
            else:
                daily = analyse(day, load_window(f, mins, maxs, previous, end), cfg,
                                args.departure_mode, build_basic_volume_profile)
            for row in daily:
                row["symbol"] = symbol
            rows.extend(daily)
            if count % 100 == 0:
                print(f"{symbol}: {day}", flush=True)
        df = pd.DataFrame(rows)
        df.to_csv(out / f"{symbol}_daily.csv", index=False)
        frames.append(df)
        summarize(pd.concat(frames, ignore_index=True)).to_csv(out / "summary.csv", index=False)
        (out / "source_manifest.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
        print(f"{symbol}: completed", flush=True)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(out / "daily.csv", index=False)
    summary = summarize(combined)
    summary.to_csv(out / "summary.csv", index=False)
    years = [summarize(g).assign(year=y) for y, g in combined.groupby(combined.session_date.str[:4])]
    pd.concat(years, ignore_index=True).to_csv(out / "yearly_summary.csv", index=False)
    statuses = combined.groupby(["symbol", "profile", "status"]).size().rename("sessions").reset_index()
    statuses.to_csv(out / "quality.csv", index=False)
    # All 12 market/profile combinations share the same dates for this comparison.
    valid = combined.loc[combined.status == "valid"]
    sets = [set(valid.loc[(valid.symbol == s) & (valid.profile == p), "session_date"])
            for s in cfg["assets"] for p in PROFILE_NAMES]
    common = set.intersection(*sets)
    if common:
        summarize(combined.loc[combined.session_date.isin(common)]).to_csv(out / "common_dates_summary.csv", index=False)
    else:
        pd.DataFrame([dict(symbol=s, profile=p, valid_sessions=0, moved_away_sessions=0,
                           returned_sessions=0, returned_pct=None, excluded_sessions=0)
                      for s in cfg["assets"] for p in PROFILE_NAMES]).to_csv(out / "common_dates_summary.csv", index=False)
    description = "distance just before 13:00 > distance at first trade >=09:30" if args.departure_mode == "endpoint" else "maximum distance during 09:30–13:00 > distance at first trade >=09:30"
    (out / "DEFINITIONS.md").write_text(
        f"Departure: {description}, measured against each fixed POC. No minimum-distance threshold.\n"
        "Return: at least one trade at POC or a crossing of POC during [13:00,17:00) NY. Compare the first post-13:00 trade with the last pre-13:00 trade, then consecutive trades. A price jump across POC counts.\n"
        "Eligible sessions already exactly at POC just before 13:00 remain in the denominator; they require a new touch/cross during the return interval.\n"
        "POC is the midpoint of the highest-volume bin; 50 equal-width bins by default. Built with the existing basic profile indicator.\n"
        "All four profile intervals are half-open. Profiles ending 13:00 are retrospective references for the departure interval; no evolving POC is tested.\n"
        "Endpoint distance can increase after crossing to the opposite side of POC. No same-side or minimum-distance condition is imposed.\n"
        "Denominator: valid sessions that meet departure criterion for each individual POC; numerator: those eligible sessions that return. Every calendar date is considered, including weekends.\n"
        "13:00 NY is fixed: midnight WIB during NY daylight saving, 01:00 WIB during NY standard time.\n"
        "Missing/duplicate IDs, invalid prices/volume, >300-second internal gaps, >60-second boundary gaps are excluded. Missing archive years stay missing.\n"
        "Archive exchange/product provenance is unverified. Output is descriptive; not a trading performance estimate.\n",
        encoding="utf-8")
    print(summary.to_string(index=False), flush=True)
    print(f"Saved: {out}", flush=True)


if __name__ == "__main__":
    main()
