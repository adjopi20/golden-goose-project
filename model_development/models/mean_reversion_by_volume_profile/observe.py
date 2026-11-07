"""Descriptive historical observation. No orders, execution, or PnL simulation."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

NY = ZoneInfo("America/New_York")
WIB = ZoneInfo("Asia/Jakarta")
UTC = timezone.utc


def bounds(day, cfg):
    # On the autumn repeated 01:00 use fold=0 (first occurrence) explicitly.
    def stamp(d, clock):
        return int(datetime.combine(d, time.fromisoformat(clock), NY).replace(fold=0).timestamp() * 1000)
    return (stamp(day - timedelta(days=1), cfg["orb_start_previous_day"]),
            stamp(day, cfg["orb_end_observation_start"]),
            stamp(day, cfg["observation_end"]))


def iso(ts, zone=NY):
    return datetime.fromtimestamp(int(ts) / 1000, UTC).astimezone(zone).isoformat()


def profile(prices, qty, bins, fraction):
    low, high = float(prices.min()), float(prices.max())
    width = (high - low) / bins
    if width == 0:
        return dict(poc=low, val=low, vah=high, profile_volume=float(qty.sum()))
    indices = np.minimum(((prices - low) / width).astype(int), bins - 1)
    weights = np.bincount(indices, weights=qty, minlength=bins)
    poc = int(weights.argmax())
    left = right = poc
    held = weights[poc]
    target = weights.sum() * fraction
    while held < target and (left > 0 or right < bins - 1):
        if left == 0 or (right < bins - 1 and weights[right + 1] > weights[left - 1]):
            right += 1
            held += weights[right]
        else:
            left -= 1
            held += weights[left]
    return dict(poc=low + (poc + .5) * width, val=low + left * width,
                vah=low + (right + 1) * width, profile_volume=float(weights.sum()))


def first_index(mask):
    indices = np.flatnonzero(mask)
    return int(indices[0]) if len(indices) else None


def analyse(day, ts, prices, qty, ids, cfg):
    start, pivot, end = bounds(day, cfg)
    row = dict(session_date=str(day), status="valid", orb_start_ny=iso(start),
               observation_start_ny=iso(pivot), observation_start_wib=iso(pivot, WIB),
               observation_end_ny=iso(end), raw_rows=len(ts))
    if not len(ts):
        row["status"] = "no_data"
        return row
    if (not np.isfinite(prices).all() or not np.isfinite(qty).all()
            or (prices <= 0).any() or (qty <= 0).any()):
        row["status"] = "invalid_prices_or_volume"
        return row
    if (np.diff(ts) < 0).any() or (np.diff(ids) <= 0).any():
        row["status"] = "unordered_or_duplicate_trades"
        return row
    row["missing_aggregate_ids"] = int(np.maximum(np.diff(ids) - 1, 0).sum())
    row["max_trade_gap_seconds"] = float(np.diff(ts).max() / 1000) if len(ts) > 1 else 0
    split = int(np.searchsorted(ts, pivot))
    if split == 0 or split == len(ts):
        row["status"] = "incomplete_session"
        return row
    boundary_gaps = [ts[0] - start, pivot - ts[split - 1], ts[split] - pivot, end - ts[-1]]
    row["max_boundary_gap_seconds"] = float(max(boundary_gaps) / 1000)
    if row["missing_aggregate_ids"]:
        row["status"] = "aggregate_id_gaps"
    elif row["max_trade_gap_seconds"] > cfg["max_internal_gap_seconds"]:
        row["status"] = "large_trade_gap"
    elif row["max_boundary_gap_seconds"] > cfg["max_boundary_gap_seconds"]:
        row["status"] = "boundary_gap"
    if row["status"] != "valid":
        return row
    op, oq, ot = prices[:split], qty[:split], ts[:split]
    future, ft = prices[split:], ts[split:]
    high, low = float(op.max()), float(op.min())
    width = high - low
    if width <= 0:
        row["status"] = "zero_range"
        return row
    reference = float(op[-1])  # Last known trade strictly before 01:00 NY.
    direction = int(np.sign(reference - op[0]))
    vp = profile(op, oq, cfg["profile_bins"], cfg["value_area_fraction"])
    row.update(vp)
    row.update(orb_high=high, orb_low=low, orb_midpoint=(high + low) / 2,
               orb_open=float(op[0]), reference_price=reference, orb_width=width,
               prior_session_direction=direction, reference_timestamp_ny=iso(ot[-1]),
               close_17=float(future[-1]), close_17_timestamp_ny=iso(ft[-1]),
               return_to_17_pct=(future[-1] / reference - 1) * 100,
               position_in_orb_at_01=(reference - low) / width,
               poc_distance_in_ranges=(reference - vp["poc"]) / width,
               initial_value_area_position="above" if reference > vp["vah"] else
                   "below" if reference < vp["val"] else "inside")
    if direction:
        signed = direction * (future - reference) / width
        row["opposite_direction_at_17"] = bool(signed[-1] < 0)
        row["signed_return_to_17_ranges"] = float(signed[-1])
        row["max_counter_move_ranges"] = float(max(0, -signed.min()))
        row["max_continuation_ranges"] = float(max(0, signed.max()))
        hit = first_index(signed <= -cfg["counter_move_range_fraction"])
        row["counter_move_025r"] = hit is not None
        if hit is not None:
            row["first_counter_move_ny"] = iso(ft[hit])
            row["first_counter_move_wib"] = iso(ft[hit], WIB)
            row["first_counter_move_hour_ny"] = datetime.fromtimestamp(int(ft[hit]) / 1000, NY).hour
            row["first_counter_move_minutes"] = float((ft[hit] - pivot) / 60000)
    else:
        row["opposite_direction_at_17"] = None
        row["counter_move_025r"] = None
    # Touch means crossing the level from the known pre-01 price, including a price jump.
    # Starting exactly at a level is excluded from that level's eligible denominator.
    for label, level in [("poc", vp["poc"]), ("midpoint", (high + low) / 2)]:
        side = int(np.sign(reference - level))
        row[f"{label}_eligible"] = bool(side)
        hit = first_index(side * (future - level) <= 0) if side else None
        row[f"{label}_reached"] = (hit is not None) if side else None
        if hit is not None:
            row[f"first_{label}_ny"] = iso(ft[hit])
            row[f"first_{label}_wib"] = iso(ft[hit], WIB)
    breakout = first_index((future > high) | (future < low))
    row["breakout_occurred"] = breakout is not None
    row["returned_after_breakout"] = None
    if breakout is not None:
        bside = 1 if future[breakout] > high else -1
        row["breakout_side"] = bside
        row["first_breakout_ny"] = iso(ft[breakout])
        boundary = high if bside == 1 else low
        back = first_index(bside * (future[breakout + 1:] - boundary) <= 0)
        row["returned_after_breakout"] = back is not None
        if back is not None:
            row["first_reentry_ny"] = iso(ft[breakout + 1 + back])
        opposite = low if bside == 1 else high
        other = first_index(bside * (future[breakout + 1:] - opposite) <= 0)
        row["opposite_boundary_after_breakout"] = other is not None
    return row


def metadata(path):
    f = pq.ParquetFile(path)
    idx = f.schema_arrow.get_field_index("timestamp")
    if idx < 0:
        raise ValueError(f"No timestamp column in {path}")
    stats = [f.metadata.row_group(i).column(idx).statistics for i in range(f.num_row_groups)]
    if any(s is None or not s.has_min_max for s in stats):
        raise ValueError("Timestamp row-group statistics required")
    mins, maxs = np.array([s.min for s in stats]), np.array([s.max for s in stats])
    return f, mins, maxs


def load_window(f, mins, maxs, start, end):
    groups = np.flatnonzero((maxs >= start) & (mins < end)).tolist()
    if not groups:
        return [np.array([], dtype=d) for d in [np.int64, float, float, np.int64]]
    columns = ["timestamp", "price", "qty", "agg_trade_id"]
    table = f.read_row_groups(groups, columns=columns)
    arrays = [table[c].combine_chunks().to_numpy() for c in columns]
    keep = (arrays[0] >= start) & (arrays[0] < end)
    return [a[keep] for a in arrays]


def rate(df, column, eligible=None):
    selected = df if eligible is None else df.loc[eligible]
    values = selected[column].dropna() if column in selected else pd.Series(dtype=bool)
    n, hits = len(values), int(values.sum())
    return {"count": hits, "eligible_days": n, "pct": 100 * hits / n if n else None}


def summarize(df):
    valid = df.loc[df.status == "valid"]
    summary = {"requested_days": len(df), "valid_days": len(valid),
               "status_counts": {str(k): int(v) for k, v in df.status.value_counts().items()}}
    if valid.empty:
        return summary
    summary.update(first_valid_session=valid.session_date.min(), last_valid_session=valid.session_date.max(),
                   opposite_direction_at_17=rate(valid, "opposite_direction_at_17"),
                   counter_move_025r=rate(valid, "counter_move_025r"),
                   poc_reached=rate(valid, "poc_reached", valid.poc_eligible),
                   midpoint_reached=rate(valid, "midpoint_reached", valid.midpoint_eligible),
                   breakout_occurred=rate(valid, "breakout_occurred"),
                   returned_after_breakout=rate(valid, "returned_after_breakout"),
                   median_return_to_17_pct=float(valid.return_to_17_pct.median()))
    return summary


def write_report(out, summaries, cfg):
    lines = ["# Observasi mean reversion by volume profile", "",
        f"Periode diminta: {cfg['requested_start']} sampai {cfg['requested_end']} (tanggal observasi NY).",
        "ORB: 09:30 hari sebelumnya sampai 01:00 NY; observasi: 01:00 sampai 17:00 NY.",
        "Sumber: arsip transaksi agregat lokal; provenance exchange dan spot/futures belum terverifikasi.", "",
        "| Market | Sesi valid | Cakupan valid | Berlawanan arah pada 17:00 | Counter-move ≥0.25 range | Mencapai POC |",
        "|---|---:|---|---:|---:|---:|"]
    def fmt(r):
        return f"{r['pct']:.1f}% ({r['count']}/{r['eligible_days']})" if r.get("pct") is not None else "N/A"
    for symbol, s in summaries.items():
        if s["valid_days"]:
            lines.append(f"| {symbol} | {s['valid_days']} | {s['first_valid_session']}–{s['last_valid_session']} | "
                         f"{fmt(s['opposite_direction_at_17'])} | {fmt(s['counter_move_025r'])} | {fmt(s['poc_reached'])} |")
        else:
            lines.append(f"| {symbol} | 0 | N/A | N/A | N/A | N/A |")
    common_path = out / "common_period_summary.json"
    if common_path.exists():
        common = json.loads(common_path.read_text(encoding="utf-8"))
        if common and all(s["valid_days"] for s in common.values()):
            sample = next(iter(common.values()))
            lines += ["", "## Perbandingan pada tanggal valid yang sama", "",
                f"{sample['valid_days']} tanggal valid bersama dalam {sample['first_valid_session']}–{sample['last_valid_session']}.", "",
                "| Market | Berlawanan arah pada 17:00 | Counter-move ≥0.25 range | Mencapai POC |",
                "|---|---:|---:|---:|"]
            for symbol, s in common.items():
                lines.append(f"| {symbol} | {fmt(s['opposite_direction_at_17'])} | {fmt(s['counter_move_025r'])} | {fmt(s['poc_reached'])} |")
            close_rates = [s["opposite_direction_at_17"]["pct"] for s in common.values()]
            poc_rates = [s["poc_reached"]["pct"] for s in common.values()]
            lines += ["", f"Pada sampel bersama, arah berlawanan pada 17:00 terjadi {min(close_rates):.1f}–{max(close_rates):.1f}% sesi, sementara crossing POC terjadi {min(poc_rates):.1f}–{max(poc_rates):.1f}%. Harga sering mencapai level profil selama sesi, tetapi hasil arah pada akhir sesi dekat 50%. Angka ini belum menunjukkan keunggulan reversal khusus pada 01:00 tanpa pembanding waktu dan aturan reversal yang lebih spesifik."]
    lines += ["", "## Cara membaca", "",
        "Arah sesi sebelumnya = tanda perubahan harga transaksi pertama ORB ke transaksi terakhir sebelum 01:00.",
        "Berbalik pada 17:00 = perubahan dari harga terakhir sebelum 01:00 ke harga terakhir sebelum 17:00 berlawanan dengan arah sesi sebelumnya. Sesi arah nol dikecualikan dari denominator.",
        "Counter-move = harga pernah bergerak berlawanan arah setidaknya 25% lebar ORB. Ambang 0.25 dipilih untuk observasi awal, bukan hasil optimasi.",
        "POC = pusat bin bervolume terbesar dari 50 bin harga berlebar sama pada ORB. Value area 70% dibangun secara kontigu dari POC (tie memilih kiri). Ini memakai volume transaksi, bukan alokasi candle.",
        "Mencapai POC/midpoint berarti harga melintasi level dari sisi harga pada awal observasi; sudah tepat pada level dikecualikan. Price jump juga dihitung sebagai crossing; bukan bukti fill order.",
        "Breakout = transaksi pertama di luar ORB setelah 01:00. Reentry = transaksi berikutnya kembali melintasi boundary breakout. Denominator reentry hanya hari dengan breakout.",
        "Timestamp event memakai transaksi aktual; tidak ada ambiguity urutan high/low candle. Semua level dibekukan sebelum observasi.", "",
        "## Kualitas dan batas interpretasi", "",
        "Hari dengan ID transaksi hilang/duplikat, harga/volume tidak valid, timestamp tidak terurut, jeda transaksi >300 detik, atau jeda boundary >60 detik dikecualikan. Jeda dapat berarti tidak ada perdagangan, sehingga filter ini konservatif dan bisa bias terhadap sesi likuid.",
        "Kontinuitas ID dan gap transaksi tidak membuktikan kelengkapan absolut arsip. Tidak ada forward fill atau penggantian simbol untuk tahun yang tidak tersedia.",
        "Waktu memakai America/New_York dengan DST. Pada pengulangan 01:00 di musim gugur, dipilih 01:00 pertama (fold=0); durasi nyata sesi dapat berubah.",
        "Hipotesis yang diuji sesuai konfirmasi terbaru adalah setelah 01:00 NY. Angka ini tidak menguji reversal sekitar 00:00 WIB secara khusus.",
        "Hari antar simbol dan sesi berdekatan dapat berkorelasi; proporsi deskriptif bukan bukti edge atau profit. Cakupan market berbeda; lihat yearly_summary.csv dan common_period_summary.json sebelum membandingkan.",
        "Tidak ada simulasi fee, slippage, entry, sizing, atau PnL.", "",
        "Reentry boundary setelah breakout sekecil satu tick adalah definisi yang lemah; angka reentry tinggi tidak berarti reversal besar. Histogram first-hit juga cenderung menumpuk pada jam awal karena event berikutnya hanya berasal dari sesi yang belum pernah mencapai ambang, sehingga tidak membuktikan keistimewaan 01:00 NY.", "",
        "## File hasil", "", "- daily_observations.csv: semua tanggal diminta, termasuk hari yang dikecualikan.",
        "- summary.json dan yearly_summary.csv: proporsi dengan numerator/denominator.",
        "- common_period_summary.json: perbandingan hanya tanggal valid yang sama untuk ketiga market.",
        "- first_counter_move_hour.csv: distribusi jam pertama counter-move; event bukan reversal yang pasti bertahan.",
        "- source_manifest.json dan config_snapshot.json: sumber, cakupan, serta parameter pengamatan."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if args.start:
        cfg["requested_start"] = args.start
    if args.end:
        cfg["requested_end"] = args.end
    begin, finish = date.fromisoformat(cfg["requested_start"]), date.fromisoformat(cfg["requested_end"])
    if finish < begin:
        raise ValueError("End before start")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "config_snapshot.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    frames, sources, summaries, years = [], {}, {}, []
    for symbol, relative in cfg["assets"].items():
        path = (args.root / relative).resolve()
        f, mins, maxs = metadata(path)
        sources[symbol] = dict(path=str(path), bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns,
            rows=f.metadata.num_rows, min_timestamp_utc=iso(mins.min(), UTC),
            max_timestamp_utc=iso(maxs.max(), UTC), venue_product=cfg["venue_product"])
        print(f"{symbol}: {sources[symbol]['min_timestamp_utc']} to {sources[symbol]['max_timestamp_utc']}", flush=True)
        rows = []
        for count in range((finish - begin).days + 1):
            day = begin + timedelta(days=count)
            start, _, end = bounds(day, cfg)
            if end <= mins.min() or start > maxs.max():
                row = dict(session_date=str(day), status="outside_archive", raw_rows=0)
            else:
                row = analyse(day, *load_window(f, mins, maxs, start, end), cfg)
            row["symbol"] = symbol
            rows.append(row)
            if count % 100 == 0:
                print(f"{symbol} {day}: {row['status']}", flush=True)
        df = pd.DataFrame(rows)
        df.to_csv(out / f"{symbol}_daily.csv", index=False)
        frames.append(df)
        summaries[symbol] = summarize(df)
        for year, group in df.groupby(df.session_date.str[:4]):
            s = summarize(group)
            flat = {"symbol": symbol, "year": year, "valid_days": s["valid_days"], "requested_days": len(group)}
            for key in ["opposite_direction_at_17", "counter_move_025r", "poc_reached"]:
                for k, v in s.get(key, {}).items():
                    flat[f"{key}_{k}"] = v
            years.append(flat)
        (out / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        (out / "source_manifest.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
        print(f"{symbol} done: {summaries[symbol]['valid_days']} valid days", flush=True)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(out / "daily_observations.csv", index=False)
    pd.DataFrame(years).to_csv(out / "yearly_summary.csv", index=False)
    date_sets = [set(df.loc[df.status == "valid", "session_date"]) for df in frames]
    common = set.intersection(*date_sets)
    common_summary = {symbol: summarize(df.loc[df.session_date.isin(common)])
                      for symbol, df in zip(cfg["assets"], frames)}
    (out / "common_period_summary.json").write_text(json.dumps(common_summary, indent=2), encoding="utf-8")
    hits = combined.loc[(combined.status == "valid") & combined.get("first_counter_move_hour_ny", pd.Series(index=combined.index, dtype=float)).notna()]
    hourly = hits.groupby(["symbol", "first_counter_move_hour_ny"]).size().rename("first_hits").reset_index() if not hits.empty else pd.DataFrame(columns=["symbol", "first_counter_move_hour_ny", "first_hits"])
    hourly.to_csv(out / "first_counter_move_hour.csv", index=False)
    write_report(out, summaries, cfg)
    print(f"Finished: {out / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()
