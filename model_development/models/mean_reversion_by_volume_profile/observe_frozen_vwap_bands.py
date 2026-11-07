"""Observe first frozen-VWAP band reaches and subsequent POC touches; no trades."""
import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from observe import iso, load_window, metadata
from observe_four_profiles import clocks, quality


EVENT_COLUMNS = [
    "symbol", "profile", "session_date", "direction", "level_sigma", "event_status",
    "vwap", "sigma", "band_price", "price_before_1300", "poc", "event_ny", "event_price",
    "event_z_signed", "minutes_after_1300", "minutes_remaining", "target_position",
    "returned_to_poc", "first_return_ny", "minutes_to_poc", "further_away_before_poc_sigma",
    "further_away_before_poc_pct", "further_away_to_1700_sigma", "end_move_sigma",
    "end_move_pct", "end_price", "end_z_signed"]


def first_reaches(prices, before, mean, sigma, levels, initial_anchor=None):
    result = []
    initial_mean, initial_sigma = initial_anchor if initial_anchor is not None else (mean, sigma)
    for sign, direction in [(1, "upper"), (-1, "lower")]:
        distances = sign * (prices - mean) / sigma
        initial = sign * (before - initial_mean) / initial_sigma
        for level in levels:
            if initial >= level:
                status, index = "already_outside_1300", -1
            else:
                reaches = np.flatnonzero(distances >= level)
                status, index = ("first_reach", int(reaches[0])) if len(reaches) else ("not_reached", None)
            result.append(dict(direction=direction, sign=sign, level_sigma=level,
                               event_status=status, index=index))
    return result


def measure(ts, prices, before, mean, sigma, levels, profiles, pivot, end, initial_anchor=None):
    events = first_reaches(prices, before, mean, sigma, levels, initial_anchor)
    initial_mean, initial_sigma = initial_anchor if initial_anchor is not None else (mean, sigma)
    end_mean = float(mean[-1]) if np.ndim(mean) else mean
    end_sigma = float(sigma[-1]) if np.ndim(sigma) else sigma
    suffix_max = np.maximum.accumulate(prices[::-1])[::-1]
    suffix_min = np.minimum.accumulate(prices[::-1])[::-1]
    output = []
    for profile, poc in profiles.items():
        below = np.flatnonzero(prices <= poc)
        above = np.flatnonzero(prices >= poc)
        for event in events:
            level, sign, index = event["level_sigma"], event["sign"], event["index"]
            event_mean = float(mean[index]) if index is not None and index >= 0 and np.ndim(mean) else initial_mean
            event_sigma = float(sigma[index]) if index is not None and index >= 0 and np.ndim(sigma) else initial_sigma
            row = dict(profile=profile, direction=event["direction"], level_sigma=level,
                       event_status=event["event_status"], vwap=event_mean, sigma=event_sigma,
                       band_price=event_mean + sign * level * event_sigma, price_before_1300=before, poc=poc)
            if index is None:
                output.append(row)
                continue
            event_price = before if index == -1 else float(prices[index])
            event_time = pivot if index == -1 else int(ts[index])
            future_start = index + 1
            target_direction = sign * (event_price - poc)
            position = "inward" if target_direction > 0 else "outward" if target_direction < 0 else "at_poc"
            hits = below if event_price > poc else above
            hit_slot = np.searchsorted(hits, future_start)
            hit = int(hits[hit_slot]) if hit_slot < len(hits) and position != "at_poc" else None
            last_before = hit if hit is not None else len(prices)
            path = prices[future_start:last_before]
            extreme_before = float(path.max() if sign == 1 else path.min()) if len(path) else event_price
            away_before = max(0., sign * (extreme_before - event_price))
            if future_start < len(prices):
                extreme_all = float(suffix_max[future_start] if sign == 1 else suffix_min[future_start])
            else:
                extreme_all = event_price
            row.update(event_ny=iso(event_time), event_price=event_price,
                event_z_signed=(event_price - event_mean) / event_sigma,
                minutes_after_1300=(event_time - pivot) / 60000,
                minutes_remaining=(end - event_time) / 60000,
                target_position=position, returned_to_poc=hit is not None,
                first_return_ny=iso(int(ts[hit])) if hit is not None else None,
                minutes_to_poc=(int(ts[hit]) - event_time) / 60000 if hit is not None else None,
                further_away_before_poc_sigma=away_before / event_sigma,
                further_away_before_poc_pct=100 * away_before / event_price,
                further_away_to_1700_sigma=max(0., sign * (extreme_all - event_price)) / event_sigma,
                end_move_sigma=sign * (float(prices[-1]) - event_price) / event_sigma,
                end_move_pct=100 * sign * (float(prices[-1]) - event_price) / event_price,
                end_price=float(prices[-1]), end_z_signed=(float(prices[-1]) - end_mean) / end_sigma)
            output.append(row)
    return output


def summarize(events, extra_keys=()):
    rows = []
    groups = [events, events.assign(direction="both")]
    keys = ["symbol", "profile", "direction", "level_sigma", *extra_keys]
    for frame in groups:
        for values, group in frame.groupby(keys, sort=True):
            for status in ["first_reach", "already_outside_1300"]:
                reached = group.loc[group.event_status.eq(status)]
                inward = reached.loc[reached.target_position.eq("inward")]
                returned = inward.loc[inward.returned_to_poc.eq(True)]
                n, hits = len(inward), len(returned)
                rate = hits / n if n else np.nan
                if n:
                    z = 1.959963984540054
                    divisor = 1 + z * z / n
                    center = (rate + z * z / (2 * n)) / divisor
                    half = z * np.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / divisor
                else:
                    center = half = np.nan
                row = dict(zip(keys, values), event_status=status, candidate_observations=len(group),
                    reached_events=len(reached), inward_target_events=n,
                    outward_target_events=int(reached.target_position.eq("outward").sum()),
                    at_poc_events=int(reached.target_position.eq("at_poc").sum()),
                    returned_events=hits, returned_pct=100 * rate,
                    wilson_low_pct=100 * (center - half), wilson_high_pct=100 * (center + half),
                    median_minutes_after_1300=inward.minutes_after_1300.median(),
                    median_minutes_to_poc=returned.minutes_to_poc.median(),
                    median_further_away_before_poc_sigma=inward.further_away_before_poc_sigma.median(),
                    p90_further_away_before_poc_sigma=inward.further_away_before_poc_sigma.quantile(.9),
                    median_further_away_before_poc_pct=inward.further_away_before_poc_pct.median(),
                    median_end_move_sigma=inward.end_move_sigma.median(),
                    median_distance_to_poc_sigma=((inward.event_price - inward.poc).abs() / inward.sigma).median(),
                    continued_05sigma_before_poc_pct=100 * inward.further_away_before_poc_sigma.ge(.5).mean(),
                    continued_1sigma_before_poc_pct=100 * inward.further_away_before_poc_sigma.ge(1).mean(),
                    end_further_than_event_pct=100 * inward.end_move_sigma.gt(0).mean())
                for horizon in [30, 60]:
                    complete = inward.loc[inward.minutes_remaining.ge(horizon)]
                    h_hits = int(complete.minutes_to_poc.le(horizon).sum())
                    row.update({f"eligible_{horizon}m": len(complete), f"returned_{horizon}m": h_hits,
                        f"returned_{horizon}m_pct": 100 * h_hits / len(complete) if len(complete) else np.nan})
                rows.append(row)
    return pd.DataFrame(rows)


def write_report(out, events, summary, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator
    profiles = list(events.profile.unique())
    plot_inputs = [(summary, "Each asset's available history", "poc_return_by_sigma.png")]
    common_path = out / "common_dates_summary.csv"
    if common_path.exists() and common_path.stat().st_size > 1:
        common_summary = pd.read_csv(common_path)
        if not common_summary.empty:
            plot_inputs.append((common_summary, "Same valid dates across all assets/profiles", "poc_return_by_sigma_common_dates.png"))
    names = {"ny_0930_1300": "POC: today 09:30-13:00 NY",
             "pre_ny_0130_0930": "POC: pre-NY 01:30-09:30",
             "previous_day_0930_0930": "POC: previous 09:30-today 09:30",
             "previous_0930_today_1300": "POC: previous 09:30-today 13:00"}
    for plot_summary, subtitle, filename in plot_inputs:
        fig, axes = plt.subplots(2, 2, figsize=(13, 9), sharex=True, sharey=True)
        for ax, profile in zip(axes.flat, profiles):
            for symbol, g in plot_summary.loc[plot_summary.profile.eq(profile) & plot_summary.direction.eq("both")
                                         & plot_summary.event_status.eq("first_reach")].groupby("symbol"):
                g = g.sort_values("level_sigma")
                ax.plot(g.level_sigma, g.returned_pct, marker="o", label=symbol)
            ax.set_title(names.get(profile, profile))
            ax.set_ylim(0, 100)
            ax.xaxis.set_major_locator(MultipleLocator(.5))
            ax.grid(alpha=.25)
            ax.set_xlabel("Frozen VWAP distance (sigma)")
            ax.set_ylabel("Subsequent inward POC touch by 17:00 (%)")
            ax.legend()
        fig.suptitle("First band reaches 13:00-17:00 NY; upper and lower combined\n" + subtitle)
        fig.tight_layout()
        fig.savefig(out / filename, dpi=150)
        plt.close(fig)
    lines = ["# Observasi band VWAP beku dan kembalinya harga ke POC", "",
        "VWAP harian berjangkar 00:00 UTC sampai tepat sebelum 13:00 NY, HLC3 candle 1 menit berbobot volume. Sigma adalah RMS deviasi HLC3 terhadap VWAP final berbobot volume. VWAP dan sigma kemudian dibekukan sampai 17:00.", "",
        "Ini observasi first-reach band, tanpa entry, SL, TP, atau PnL. POC memakai empat profile lama dan tidak dibangun ulang. Semua profile-session valid digunakan, tanpa filter bias pagi atau pernah-menjauhi.", "",
        "Transaksi aggregate pertama pada [13:00,17:00) yang mencapai/melewati +k sigma atau -k sigma menjadi event. Satu event per sesi, tingkat, arah, dan profile. Level bersarang: satu sesi dapat muncul di beberapa tingkat; angka lintas tingkat bukan sampel independen.", "",
        "Harga terakhir sebelum 13:00 yang sudah berada di luar/tepat pada band masuk already_outside_1300, bukan first_reach. Kelompok ini memakai harga sebelum 13:00 sebagai reference pada 13:00; sentuh POC pada transaksi pertama setelah 13:00 ikut dihitung. Kedua kelompok dilaporkan terpisah.", "",
        "POC inward = POC di sisi menuju pusat VWAP dari harga event: di bawah harga untuk upper, di atas untuk lower. POC di sisi outward dan harga event tepat di POC dicatat terpisah. Persentase utama memakai event inward saja, sehingga sentuh POC melalui continuation outward tidak disebut kembali.", "",
        "Kembali = transaksi setelah event menyentuh atau melintasi POC; event itu sendiri tidak dihitung sebagai kembali. Urutan memakai aggregate trades, bukan urutan high/low candle yang tidak diketahui. Crossing lompatan harga dihitung sebagai crossing, tanpa asumsi fill order.", "",
        "Further-away adalah tambahan gerak mengikuti arah band dari harga event, hingga sebelum sentuh POC pertama (atau sampai sebelum 17:00 bila tidak kembali), dalam sigma beku dan persen harga event. End-move positif berarti harga akhir lebih jauh mengikuti arah band; tidak kembali sendiri bukan bukti continuation.", "",
        "Hitungan sampai 17:00 bergantung waktu event. Tabel mencatat median waktu event; peluang dalam 30/60 menit hanya memakai event dengan sisa waktu sekurangnya horizon tersebut. Tanggal bersama dan kelompok jam event dilaporkan terpisah.", "",
        "Unit hitungan adalah event sesi × arah × tingkat × profile. Pada tabel kedua arah, satu sesi dapat menyumbang event upper dan lower; persentase ini bukan persentase hari unik. Wilson interval bersifat deskriptif dan belum mengoreksi dependensi antar sesi/arah/band. Sampel kecil, perbedaan periode asset, dan banyak tingkat yang dibandingkan membatasi kesimpulan. Tidak ada tingkat optimum yang dipilih otomatis.", "",
        "Venue/produk belum terverifikasi dari arsip lokal. Cakupan tanggal antar asset berbeda; grafik dan tabel common dates tersedia untuk kalender bersama.", "",
        f"Source profiles: {cfg['source_profiles']}", "",
        "## Cakupan sesi yang lolos kualitas VWAP dan transaksi", "",
        "| Asset | Sesi | Awal | Akhir |", "|---|---:|---|---|"]
    for symbol, g in events.groupby("symbol"):
        lines.append(f"| {symbol} | {g.session_date.nunique()} | {g.session_date.min()} | {g.session_date.max()} |")
    common = cfg.get("common_dates", [])
    lines += ["", f"Tanggal valid bersama seluruh asset dan profile: {len(common)}"
              + (f", {min(common)} sampai {max(common)}." if common else "."), "",
        "## Profile hari ini 09:30–13:00, first-reach, kedua arah", "",
        "| Asset | Level sigma | Event inward | Kembali ke POC | Persentase | Median menit setelah 13:00 | Return ≤60m | N horizon 60m |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.loc[summary.profile.eq("ny_0930_1300") & summary.direction.eq("both")
                          & summary.event_status.eq("first_reach")].itertuples():
        pct = f"{r.returned_pct:.1f}%" if pd.notna(r.returned_pct) else "N/A"
        minute = f"{r.median_minutes_after_1300:.1f}" if pd.notna(r.median_minutes_after_1300) else "N/A"
        h_pct = f"{r.returned_60m_pct:.1f}%" if pd.notna(r.returned_60m_pct) else "N/A"
        lines.append(f"| {r.symbol} | {r.level_sigma:g} | {r.inward_target_events} | {r.returned_events} | {pct} | {minute} | {h_pct} | {r.eligible_60m} |")
    lines += ["", "## Gerak lanjutan sebelum kembali: contoh 1 dan 3 sigma", "",
        "Profile hari ini, first-reach, target inward, kedua arah. Lanjut ≥0.5 sigma diukur dari harga event sampai sebelum sentuh POC (atau sampai 17:00 bila tidak kembali). Median end move negatif menunjukkan retracement relatif harga event, meskipun POC belum tentu tercapai.", "",
        "| Asset | Band | N | Lanjut ≥0.5 sigma sebelum POC | Median gerak akhir (sigma) | Akhir lebih jauh dari event |",
        "|---|---:|---:|---:|---:|---:|"]
    for r in summary.loc[summary.profile.eq("ny_0930_1300") & summary.direction.eq("both")
                          & summary.event_status.eq("first_reach") & summary.level_sigma.isin([1., 3.])].itertuples():
        lines.append(f"| {r.symbol} | {r.level_sigma:g} | {r.inward_target_events} | {r.continued_05sigma_before_poc_pct:.1f}% | {r.median_end_move_sigma:+.3f} | {r.end_further_than_event_pct:.1f}% |")
    lines += ["", "Seluruh profile dan sisi: summary.csv. Tanggal valid bersama: common_dates_summary.csv. Kelompok jam: event_hour_summary.csv. Tahun: yearly_summary.csv. Detail event: events.csv. Kualitas: quality.csv. Nilai beku: anchors.csv.", "",
              "![POC return versus frozen VWAP distance](poc_return_by_sigma.png)", "",
              "![POC return on common dates](poc_return_by_sigma_common_dates.png)", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--profiles-run", type=Path, required=True)
    parser.add_argument("--candles-config", type=Path, default=Path(__file__).with_name("vwap_candles_config.json"))
    parser.add_argument("--levels", default="0.5,1,1.5,2,2.5,3,3.5,4,4.5,5")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    levels = sorted(set(float(k) for k in args.levels.split(",")))
    if not levels or not np.isfinite(levels).all() or min(levels) <= 0:
        raise ValueError("Levels must be positive finite sigma multiples")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.root))
    from indicator.vwap import vwap
    source = args.profiles_run.resolve()
    cfg = json.loads((source / "config_snapshot.json").read_text())
    caches = json.loads(args.candles_config.read_text())
    observations = pd.read_csv(source / "daily.csv")
    if observations.duplicated(["symbol", "profile", "session_date"]).any():
        raise ValueError("Duplicate profile sessions")
    valid = observations.loc[observations.status.eq("valid")].copy()
    if args.start:
        valid = valid.loc[valid.session_date.ge(args.start)]
    if args.end:
        valid = valid.loc[valid.session_date.le(args.end)]
    if valid.empty:
        raise ValueError("No valid profile sessions in requested period")
    cols = ["timestamp_ms", "open", "high", "low", "close", "volume"]
    anchors, qualities, manifests = [], [], {}
    with (out / "events.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_COLUMNS)
        writer.writeheader()
        for symbol, group in valid.groupby("symbol", sort=False):
            if symbol not in caches:
                raise ValueError(f"No configured 1m candle archive for {symbol}")
            parts = [pd.read_parquet(args.root / p, columns=cols) for p in caches[symbol]]
            candles = pd.concat(parts, ignore_index=True).sort_values("timestamp_ms")
            if candles.timestamp_ms.duplicated().any():
                raise ValueError(f"Duplicate candle timestamps: {symbol}")
            stamps = candles.timestamp_ms.to_numpy()
            raw_path = args.root / cfg["assets"][symbol]
            raw_file, mins, maxs = metadata(raw_path)
            paths = [raw_path, *[args.root / p for p in caches[symbol]]]
            manifests[symbol] = [dict(path=str(p.resolve()), bytes=p.stat().st_size,
                mtime_ns=p.stat().st_mtime_ns) for p in paths]
            days = list(group.groupby("session_date", sort=True))
            if args.max_sessions is not None:
                days = days[:args.max_sessions]
            for counter, (day, day_profiles) in enumerate(days, start=1):
                _, _, _, pivot, end = clocks(date.fromisoformat(day))
                start = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp() * 1000)
                c = candles.iloc[np.searchsorted(stamps, start):np.searchsorted(stamps, pivot)]
                q = dict(symbol=symbol, session_date=day, status="valid", valid_profiles=len(day_profiles))
                expected = (pivot - start) // 60000
                if (len(c) != expected or c.empty or c.timestamp_ms.iloc[0] != start
                        or c.timestamp_ms.iloc[-1] != pivot - 60000
                        or not c.timestamp_ms.diff().dropna().eq(60000).all()):
                    q["status"] = "incomplete_daily_anchor"
                elif (not np.isfinite(c[cols].to_numpy()).all() or c.volume.lt(0).any()
                        or c[["open", "high", "low", "close"]].le(0).any().any()
                        or c.high.lt(c[["open", "close", "low"]].max(axis=1)).any()
                        or c.low.gt(c[["open", "close", "high"]].min(axis=1)).any()):
                    q["status"] = "invalid_candles"
                elif not np.allclose(day_profiles.price_before_1300.to_numpy(), c.close.iloc[-1], rtol=1e-10, atol=1e-8):
                    q["status"] = "cache_price_mismatch"
                if q["status"] != "valid":
                    qualities.append(q)
                    continue
                if c.volume.sum() <= 0:
                    q["status"] = "zero_anchor_volume"
                    qualities.append(q)
                    continue
                price = (c.high + c.low + c.close) / 3
                mean = float(vwap(price, c.volume).iloc[-1])
                sigma = float(np.sqrt(np.average((price.to_numpy() - mean) ** 2, weights=c.volume)))
                if not np.isfinite([mean, sigma]).all() or sigma <= 0:
                    q["status"] = "invalid_anchor_dispersion"
                    qualities.append(q)
                    continue
                arrays = load_window(raw_file, mins, maxs, pivot, end)
                after_quality = quality(*arrays, pivot, end, cfg)
                if after_quality != "valid":
                    q["status"] = "return_" + after_quality
                    qualities.append(q)
                    continue
                before = float(c.close.iloc[-1])
                profiles = dict(zip(day_profiles.profile, day_profiles.poc))
                for row in measure(arrays[0], arrays[1], before, mean, sigma, levels, profiles, pivot, end):
                    writer.writerow(dict(symbol=symbol, session_date=day, **row))
                anchors.append(dict(symbol=symbol, session_date=day, vwap=mean, sigma=sigma,
                    price_before_1300=before, initial_z=(before - mean) / sigma,
                    zero_volume_minutes=int(c.volume.eq(0).sum())))
                qualities.append(q)
                if counter % 100 == 0:
                    print(f"{symbol}: {counter}/{len(days)} sessions checked ({day})", flush=True)
            print(f"{symbol}: completed", flush=True)
    pd.DataFrame(qualities).to_csv(out / "quality.csv", index=False)
    pd.DataFrame(anchors).to_csv(out / "anchors.csv", index=False)
    events = pd.read_csv(out / "events.csv")
    if events.empty:
        raise ValueError("No valid band observations; inspect quality.csv")
    summary = summarize(events)
    summary.to_csv(out / "summary.csv", index=False)
    summary_year = summarize(events.assign(year=events.session_date.str[:4]), ["year"])
    summary_year.to_csv(out / "yearly_summary.csv", index=False)
    reached = events.loc[events.event_status.ne("not_reached")].copy()
    reached["event_hour_ny"] = 13 + np.floor(reached.minutes_after_1300 / 60).astype(int)
    summarize(reached, ["event_hour_ny"]).to_csv(out / "event_hour_summary.csv", index=False)
    valid_dates = [set(events.loc[events.symbol.eq(s) & events.profile.eq(p), "session_date"])
                   for s in events.symbol.unique() for p in observations.profile.unique()]
    common = set.intersection(*valid_dates)
    summarize(events.loc[events.session_date.isin(common)]).to_csv(out / "common_dates_summary.csv", index=False)
    snapshot = dict(source_profiles=str(source), anchor="00:00 UTC daily, frozen before 13:00 NY",
        sigma="volume-weighted RMS HLC3 deviation from frozen VWAP", timeframe="1m for anchor; aggregate trades for events",
        event_window="13:00 inclusive to 17:00 exclusive America/New_York", levels_sigma=levels,
        source_profile_config=cfg, common_dates=sorted(common), start=args.start, end=args.end,
        max_sessions=args.max_sessions, source_manifest=manifests)
    (out / "config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    write_report(out, events, summary, snapshot)
    print(f"Saved observation: {out}", flush=True)


if __name__ == "__main__":
    main()
