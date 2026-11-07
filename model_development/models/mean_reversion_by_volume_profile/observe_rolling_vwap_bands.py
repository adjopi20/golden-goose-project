"""Compare rolling 24h VWAP bands to frozen 24h and frozen daily bands."""
import argparse
import csv
import json
from contextlib import ExitStack
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from observe import load_window, metadata
from observe_four_profiles import clocks, quality
from observe_frozen_vwap_bands import EVENT_COLUMNS, measure, summarize

METHODS = {
    "frozen_daily": "Frozen daily 00:00 UTC",
    "frozen_24h": "Frozen 24h before 13:00",
    "rolling_24h": "Rolling 24h (completed 1m candles)",
}
KEYS = ["symbol", "profile", "session_date"]


def rolling_states(c, window_minutes=1440):
    """State becomes available at candle close; no current-candle lookahead."""
    ts = c.timestamp_ms.to_numpy()
    if len(np.unique(ts)) != len(ts) or (np.diff(ts) <= 0).any() or (ts % 60000 != 0).any():
        raise ValueError("Candles must be unique, sorted and aligned to minute opens")
    valid = np.isfinite(c[["open", "high", "low", "close", "volume"]].to_numpy()).all(axis=1)
    valid &= (c.volume.ge(0) & c[["open", "high", "low", "close"]].gt(0).all(axis=1)
              & c.high.ge(c[["open", "close", "low"]].max(axis=1))
              & c.low.le(c[["open", "close", "high"]].min(axis=1))).to_numpy()
    p = (c.high + c.low + c.close) / 3
    reference = float(c.close.iloc[0])
    shifted = p - reference
    q = c.volume
    volume = q.rolling(window_minutes, min_periods=window_minutes).sum()
    first_moment = (shifted * q).rolling(window_minutes, min_periods=window_minutes).sum() / volume.replace(0, np.nan)
    second_moment = (shifted.pow(2) * q).rolling(window_minutes, min_periods=window_minutes).sum() / volume.replace(0, np.nan)
    mean = first_moment + reference
    sigma = (second_moment - first_moment.pow(2)).clip(lower=0).pow(.5)
    start_ts = pd.Series(ts, index=c.index).shift(window_minutes - 1)
    complete = pd.Series(valid, index=c.index).rolling(window_minutes).sum().eq(window_minutes)
    complete &= (pd.Series(ts, index=c.index) - start_ts).eq((window_minutes - 1) * 60000)
    complete &= np.isfinite(mean) & np.isfinite(sigma) & sigma.gt(0)
    return pd.DataFrame(dict(timestamp_ms=ts + 60000, vwap=mean.to_numpy(),
        sigma=sigma.to_numpy(), valid=complete.to_numpy(), last_close=c.close.to_numpy()))


def summary_tables(frames, common):
    summary, matched, years, hours = [], [], [], []
    for method, events in frames.items():
        summary.append(summarize(events).assign(method=method))
        matched.append(summarize(events.loc[events.session_date.isin(common)]).assign(method=method))
        years.append(summarize(events.assign(year=events.session_date.str[:4]), ["year"]).assign(method=method))
        reached = events.loc[events.event_status.ne("not_reached")].copy()
        reached["event_hour_ny"] = 13 + np.floor(reached.minutes_after_1300 / 60).astype(int)
        hours.append(summarize(reached, ["event_hour_ny"]).assign(method=method))
    return [pd.concat(t, ignore_index=True) for t in [summary, matched, years, hours]]


def report(out, summary, common_summary, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    profile_labels = {"ny_0930_1300": "POC today 09:30-13:00 NY",
        "pre_ny_0130_0930": "POC pre-NY 01:30-09:30",
        "previous_day_0930_0930": "POC previous 09:30-today 09:30",
        "previous_0930_today_1300": "POC previous 09:30-today 13:00"}
    profiles = list(profile_labels)
    symbols = list(common_summary.symbol.unique())
    fig, axes = plt.subplots(len(profiles), len(symbols), figsize=(15, 15), sharex=True, sharey=True, squeeze=False)
    for row, profile in enumerate(profiles):
        for col, symbol in enumerate(symbols):
            ax = axes[row, col]
            g = common_summary.loc[common_summary.symbol.eq(symbol) & common_summary.profile.eq(profile)
                & common_summary.direction.eq("both") & common_summary.event_status.eq("first_reach")]
            for method, label in METHODS.items():
                m = g.loc[g.method.eq(method)].sort_values("level_sigma")
                line, = ax.plot(m.level_sigma, m.returned_pct, label=label)
                enough = m.inward_target_events.ge(20)
                ax.scatter(m.loc[enough, "level_sigma"], m.loc[enough, "returned_pct"], s=16, color=line.get_color())
                ax.scatter(m.loc[~enough, "level_sigma"], m.loc[~enough, "returned_pct"], s=28,
                           facecolors="white", edgecolors=line.get_color())
            ax.set_title(symbol + "\n" + profile_labels[profile], fontsize=10)
            ax.set_ylim(0, 100)
            ax.grid(alpha=.25)
            if row == len(profiles) - 1:
                ax.set_xlabel("VWAP distance (sigma)")
            if col == 0:
                ax.set_ylabel("Return to inward POC by 17:00 (%)")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.5, .985), ncol=3, fontsize=9)
    fig.suptitle("Rolling 24h versus frozen bands: same eligible dates, first reaches 13:00-17:00 NY", y=.999, fontsize=12)
    fig.text(.5, .004, "Open markers: fewer than 20 inward events; counts and event times differ across methods.", ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .015, 1, .96))
    fig.savefig(out / "rolling_vs_frozen_common_dates.png", dpi=150)
    plt.close(fig)
    dates = cfg["common_dates"]
    lines = ["# Observasi VWAP rolling 24 jam", "",
        "VWAP rolling dihitung dari HLC3 candle 1 menit berbobot volume pada jendela 24 jam yang bergerak. Pada transaksi waktu t, awal menitnya adalah m; jendela data yang tersedia adalah [m minus 24 jam, m). VWAP dan sigma diperbarui setiap awal menit dari candle yang sudah selesai. Candle yang masih berjalan tidak digunakan. Durasi 24 jam berarti 1440 menit waktu UTC, termasuk saat pergantian DST NY.", "",
        "Sigma dihitung ulang sebagai RMS deviasi berbobot volume dari VWAP pada jendela yang sama. POC tetap berasal dari empat profile sebelumnya. Observasi tetap [13:00,17:00) NY, tanpa entry, SL, TP atau PnL.", "",
        "Tiga pembanding: frozen daily 00:00 UTC (hasil lama), frozen 24h sebelum 13:00, dan rolling 24h. Frozen 24h memisahkan efek panjang anchor dari efek pergerakan jendela. Semua metode memakai profile-session yang lolos kualitas rolling dan tersedia pada baseline lama; tabel common dates membatasi lagi kalender yang sama antar asset/profile.", "",
        "Band rolling bisa tercapai karena harga bergerak, band bergeser, atau keduanya. First-reach berarti transaksi pertama memenuhi jarak >=k sigma pada band yang saat itu tersedia. Kelompok already_outside_1300 memakai band 24h tepat sebelum 13:00 dan tetap dipisahkan; tidak berubah menjadi first-reach jika kemudian masuk dan keluar lagi.", "",
        "Kembali = transaksi setelah event menyentuh/crossing POC tetap, sebelum 17:00. Persentase utama hanya event dengan POC pada arah balik (inward). POC outward dan event tepat di POC dicatat terpisah. Sisi upper/lower digabung pada tabel ini, tetapi tersedia terpisah dalam CSV. Satu hari dapat muncul di beberapa tingkat dan kedua arah, jadi N adalah event, bukan jumlah hari unik.", "",
        "Gerak lanjutan dan end move dalam sigma dinormalisasi memakai sigma pada event, sehingga retracement harga tidak tercampur perubahan sigma sesudahnya. end_z_signed memakai VWAP/sigma yang tersedia pada transaksi terakhir. Tidak kembali ke POC bukan otomatis continuation.", "",
        "Metode berbeda dapat menghasilkan event dan jumlah event berbeda meskipun kalendernya sama. Waktu event dan jarak ke POC ikut berubah; tabel menyertakan horizon 30/60 menit dengan waktu observasi cukup, kelompok jam, dan tahun. Interval Wilson deskriptif belum mengoreksi dependensi antar event/sesi. Titik grafik berongga menunjukkan N kurang dari 20. Banyak tingkat dibandingkan; tidak memilih optimum atau menyimpulkan edge trading.", "",
        "Venue/produk arsip lokal belum terverifikasi. Kualitas dan sumber tercatat pada quality.csv dan config_snapshot.json.", "",
        f"Tanggal valid bersama: {len(dates)}, {min(dates)} sampai {max(dates)}.", "",
        "## POC hari ini 09:30–13:00: tanggal bersama, first-reach, kedua arah", "",
        "Angka = jumlah kembali / event inward — persentase. Harga yang sudah di luar band sebelum 13:00 berada di kelompok terpisah.", "",
        "| Asset | Sigma | Frozen daily | Frozen 24h | Rolling 24h |", "|---|---:|---:|---:|---:|"]
    chosen = common_summary.loc[common_summary.profile.eq("ny_0930_1300")
        & common_summary.direction.eq("both") & common_summary.event_status.eq("first_reach")
        & common_summary.level_sigma.isin([.5, 1., 1.5, 2., 3.])]
    for (symbol, level), g in chosen.groupby(["symbol", "level_sigma"]):
        cells = []
        for method in METHODS:
            r = g.loc[g.method.eq(method)].iloc[0]
            cells.append(f"{r.returned_events}/{r.inward_target_events} — {r.returned_pct:.1f}%" if r.inward_target_events else "0/0 — N/A")
        lines.append(f"| {symbol} | {level:g} | " + " | ".join(cells) + " |")
    lines += ["", "summary.csv: seluruh periode dengan kalender per asset yang sama antar metode. common_dates_summary.csv: kalender bersama semua asset/profile. yearly_summary.csv dan event_hour_summary.csv: tahun dan jam event. rolling_24h/events.csv serta frozen_24h/events.csv: detail event. frozen_daily_matched_events.csv: baseline lama yang disesuaikan kalender. anchors.csv: nilai 24h sebelum 13:00 dan sebelum menit terakhir.", "",
        "![Comparison on common dates](rolling_vs_frozen_common_dates.png)", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--profiles-run", type=Path, required=True)
    parser.add_argument("--frozen-run", type=Path, default=Path(__file__).resolve().parent / "runs/frozen_vwap_bands_v07")
    parser.add_argument("--candles-config", type=Path, default=Path(__file__).with_name("vwap_candles_config.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    source = args.profiles_run.resolve()
    cfg = json.loads((source / "config_snapshot.json").read_text())
    frozen_cfg = json.loads((args.frozen_run / "config_snapshot.json").read_text())
    if Path(frozen_cfg["source_profiles"]).resolve() != source:
        raise ValueError("Frozen baseline must reference the same POC profile run")
    levels = frozen_cfg["levels_sigma"]
    frozen = pd.read_csv(args.frozen_run / "events.csv")
    old_keys = frozen[KEYS].drop_duplicates()
    observations = pd.read_csv(source / "daily.csv")
    valid = observations.loc[observations.status.eq("valid")].merge(old_keys, on=KEYS, validate="one_to_one")
    if args.start:
        valid = valid.loc[valid.session_date.ge(args.start)]
    if args.end:
        valid = valid.loc[valid.session_date.le(args.end)]
    if valid.empty:
        raise ValueError("No eligible profile sessions")
    caches = json.loads(args.candles_config.read_text())
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    anchors, qualities, manifests = [], [], {}
    raw_manifest = json.loads((source / "source_manifest.json").read_text())
    with ExitStack() as stack:
        writers = {}
        for method in ["rolling_24h", "frozen_24h"]:
            folder = out / method
            folder.mkdir(exist_ok=True)
            stream = stack.enter_context((folder / "events.csv").open("w", newline="", encoding="utf-8"))
            writers[method] = csv.DictWriter(stream, fieldnames=EVENT_COLUMNS)
            writers[method].writeheader()
        for symbol, group in valid.groupby("symbol", sort=False):
            cols = ["timestamp_ms", "open", "high", "low", "close", "volume"]
            c = pd.concat([pd.read_parquet(args.root / p, columns=cols) for p in caches[symbol]], ignore_index=True).sort_values("timestamp_ms").reset_index(drop=True)
            states = rolling_states(c)
            stamps = states.timestamp_ms.to_numpy()
            raw_path = args.root / cfg["assets"][symbol]
            stat = raw_path.stat()
            if stat.st_size != raw_manifest[symbol]["bytes"] or stat.st_mtime_ns != raw_manifest[symbol]["mtime_ns"]:
                raise ValueError(f"Raw archive changed since POC construction: {symbol}")
            raw_file, mins, maxs = metadata(raw_path)
            paths = [raw_path, *[args.root / p for p in caches[symbol]]]
            manifests[symbol] = [dict(path=str(p.resolve()), bytes=p.stat().st_size, mtime_ns=p.stat().st_mtime_ns) for p in paths]
            days = list(group.groupby("session_date", sort=True))
            for counter, (day, profiles) in enumerate(days, start=1):
                _, _, _, pivot, end = clocks(date.fromisoformat(day))
                wanted = np.arange(pivot, end, 60000)
                indexes = np.searchsorted(stamps, wanted)
                q = dict(symbol=symbol, session_date=day, status="valid", valid_profiles=len(profiles))
                if (indexes >= len(states)).any() or not np.array_equal(stamps[indexes], wanted):
                    q["status"] = "missing_rolling_states"
                elif not states.valid.iloc[indexes].all():
                    q["status"] = "incomplete_or_invalid_24h_windows"
                elif not np.allclose(profiles.price_before_1300, states.last_close.iloc[indexes[0]], rtol=1e-10, atol=1e-8):
                    q["status"] = "cache_price_mismatch"
                if q["status"] != "valid":
                    qualities.append(q)
                    continue
                mu = states.vwap.iloc[indexes].to_numpy()
                sigma = states.sigma.iloc[indexes].to_numpy()
                arrays = load_window(raw_file, mins, maxs, pivot, end)
                after_quality = quality(*arrays, pivot, end, cfg)
                if after_quality != "valid":
                    q["status"] = "return_" + after_quality
                    qualities.append(q)
                    continue
                ts, prices = arrays[:2]
                buckets = (ts - pivot) // 60000
                before = float(profiles.price_before_1300.iloc[0])
                pocs = dict(zip(profiles.profile, profiles.poc))
                for method, mean_arg, sigma_arg, initial in [
                    ("rolling_24h", mu[buckets], sigma[buckets], (float(mu[0]), float(sigma[0]))),
                    ("frozen_24h", float(mu[0]), float(sigma[0]), None)]:
                    for row in measure(ts, prices, before, mean_arg, sigma_arg, levels, pocs, pivot, end, initial):
                        writers[method].writerow(dict(symbol=symbol, session_date=day, **row))
                anchors.append(dict(symbol=symbol, session_date=day, vwap_1300=mu[0], sigma_1300=sigma[0],
                    vwap_1659=mu[-1], sigma_1659=sigma[-1], price_before_1300=before))
                qualities.append(q)
                if counter % 100 == 0:
                    print(f"{symbol}: {counter}/{len(days)} sessions ({day})", flush=True)
            print(f"{symbol}: completed", flush=True)
    pd.DataFrame(qualities).to_csv(out / "quality.csv", index=False)
    pd.DataFrame(anchors).to_csv(out / "anchors.csv", index=False)
    frames = {method: pd.read_csv(out / method / "events.csv") for method in writers}
    if frames["rolling_24h"].empty:
        raise ValueError("No valid rolling observations; inspect quality.csv")
    eligible = frames["rolling_24h"][KEYS].drop_duplicates()
    frames["frozen_daily"] = frozen.merge(eligible, on=KEYS, validate="many_to_one")
    if len(frames["frozen_daily"]) != len(frames["rolling_24h"]) or len(frames["frozen_24h"]) != len(frames["rolling_24h"]):
        raise ValueError("Mismatched candidate counts across methods")
    frames["frozen_daily"].to_csv(out / "frozen_daily_matched_events.csv", index=False)
    combinations = eligible[["symbol", "profile"]].drop_duplicates().itertuples(index=False)
    common = set.intersection(*[set(eligible.loc[eligible.symbol.eq(s) & eligible.profile.eq(p), "session_date"]) for s, p in combinations])
    if not common:
        raise ValueError("No common calendar across assets/profiles")
    tables = summary_tables(frames, common)
    for name, table in zip(["summary.csv", "common_dates_summary.csv", "yearly_summary.csv", "event_hour_summary.csv"], tables):
        table.to_csv(out / name, index=False)
    snapshot = dict(source_profiles=str(source), source_frozen=str(args.frozen_run.resolve()), methods=METHODS,
        window_minutes=1440, price_source="HLC3 of completed 1m candles, qty volume", update="each minute, never active candle",
        sigma="volume-weighted RMS around VWAP of same window", event_window="13:00 inclusive to 17:00 exclusive NY",
        event_excursion_sigma="sigma frozen at event", levels_sigma=levels, common_dates=sorted(common),
        source_manifest=manifests, start=args.start, end=args.end)
    (out / "config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    report(out, tables[0], tables[1], snapshot)
    print(f"Saved rolling observation: {out}", flush=True)


if __name__ == "__main__":
    main()
