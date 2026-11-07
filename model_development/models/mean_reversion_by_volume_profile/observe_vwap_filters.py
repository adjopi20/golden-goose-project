"""Daily UTC VWAP/HLC3 filters and morning directionality, using frozen POC trades."""
import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from observe import load_window, metadata
from observe_four_profiles import clocks, quality
from performance_poc_rr import stats


def feature(candles, opening, pivot, vwap_func):
    c = candles
    tp = (c.high + c.low + c.close) / 3
    vol = c.volume
    weighted = vwap_func(tp, vol)
    final = float(weighted.iloc[-1])
    sigma = float(np.sqrt(np.average((tp.to_numpy() - final) ** 2, weights=vol)))
    before = np.flatnonzero(c.timestamp_ms.to_numpy() < pivot - 1_800_000)
    if not len(before) or not sigma:
        return None
    v1230 = float(weighted.iloc[before[-1]])
    morning = c.loc[c.timestamp_ms >= opening]
    five = morning.groupby(morning.timestamp_ms // 300_000).close.last().to_numpy()
    path = float(np.abs(np.diff(five)).sum())
    er = abs(float(five[-1] - five[0])) / path if path else 0.
    price = float(morning.close.iloc[-1])
    return dict(vwap_1300=final, sigma=sigma, vwap_1230=v1230,
                vwap_slope_30m_sigma=(final - v1230) / sigma,
                distance_vwap_sigma=(price - final) / sigma,
                efficiency_ratio_5m=float(er), morning_open=float(morning.open.iloc[0]),
                morning_close=price)


def filters(frame):
    aligned = (frame.price_before_1300 - frame.vwap_1300) * frame.morning_bias > 0
    stretched = aligned & frame.distance_vwap_sigma.abs().ge(1)
    return {"baseline": pd.Series(True, index=frame.index), "vwap_aligned": aligned,
            "vwap_aligned_1sigma": stretched,
            "vwap_aligned_1sigma_low_slope": stretched & frame.vwap_slope_30m_sigma.abs().le(.05)}


def analyze(frame, cutoff):
    output, regime = [], []
    for (symbol, profile), g in frame.groupby(["symbol", "profile"], sort=False):
        periods = {"all": g, "development": g.loc[g.session_date < cutoff],
                   "validation": g.loc[g.session_date >= cutoff]}
        for period, group in periods.items():
            for name, mask in filters(group).items():
                selected = group.loc[mask].sort_values("session_date")
                for fee in [0, 2]:
                    r = selected.gross_r - fee * selected.fee_r_per_bps_each_side
                    output.append(dict(symbol=symbol, profile=profile, period=period,
                                       filter=name, hypothetical_fee_bps_each_side=fee,
                                       baseline_trades=len(group), **stats(r)))
            for label, mask in [("ER_le_0.3", group.efficiency_ratio_5m.le(.3)),
                                ("ER_between_0.3_0.6", group.efficiency_ratio_5m.gt(.3) & group.efficiency_ratio_5m.lt(.6)),
                                ("ER_ge_0.6", group.efficiency_ratio_5m.ge(.6))]:
                selected = group.loc[mask].sort_values("session_date")
                regime.append(dict(symbol=symbol, profile=profile, period=period,
                                   morning_directionality_bin=label, **stats(selected.gross_r)))
    return pd.DataFrame(output), pd.DataFrame(regime)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--candles-config", type=Path, default=Path(__file__).with_name("vwap_candles_config.json"))
    parser.add_argument("--validation-start", default="2026-01-01")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    from indicator.vwap import vwap
    source_cfg = json.loads((args.run / "config_snapshot.json").read_text())["source_profile_config"]
    cache_cfg = json.loads(args.candles_config.read_text())
    trades = pd.read_csv(args.run / "trades.csv")
    trades = trades.loc[trades.status.isin(["tp_first", "sl_first", "time_exit"])].copy()
    trades["modeled_exit"] = np.where(trades.status.eq("tp_first"), trades.tp,
                                      np.where(trades.status.eq("sl_first"), trades.sl, trades.trigger_trade_price))
    trades["fee_r_per_bps_each_side"] = .0001 * (trades.entry + trades.modeled_exit) / trades.risk_price
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    feature_rows, manifest = [], {}
    cols = ["timestamp_ms", "open", "high", "low", "close", "volume"]
    for symbol, group in trades.groupby("symbol", sort=False):
        cached = cache_cfg.get(symbol, [])
        if cached:
            frames = [pd.read_parquet(args.root / p, columns=cols) for p in cached]
            c = pd.concat(frames, ignore_index=True).sort_values("timestamp_ms")
            if c.timestamp_ms.duplicated().any():
                raise ValueError(f"Duplicate candle cache timestamps: {symbol}")
            stamps = c.timestamp_ms.to_numpy()
            manifest[symbol] = [dict(path=str((args.root / p).resolve()), bytes=(args.root / p).stat().st_size,
                                     mtime_ns=(args.root / p).stat().st_mtime_ns) for p in cached]
        else:
            raw_path = args.root / source_cfg["assets"][symbol]
            f, mins, maxs = metadata(raw_path)
            manifest[symbol] = [dict(path=str(raw_path.resolve()), bytes=raw_path.stat().st_size,
                                     mtime_ns=raw_path.stat().st_mtime_ns)]
        for day, day_trades in group.groupby("session_date", sort=True):
            _, _, opening, pivot, _ = clocks(date.fromisoformat(day))
            start = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp() * 1000)
            row = dict(symbol=symbol, session_date=day, feature_status="valid")
            if cached:
                subset = c.iloc[np.searchsorted(stamps, start):np.searchsorted(stamps, pivot)]
            else:
                arrays = load_window(f, mins, maxs, start, pivot)
                q = quality(*arrays, start, pivot, source_cfg)
                if q != "valid":
                    row["feature_status"] = "raw_" + q
                    feature_rows.append(row)
                    continue
                ts, prices, qty, _ = arrays
                raw = pd.DataFrame(dict(timestamp_ms=ts // 60_000 * 60_000, price=prices, volume=qty))
                subset = raw.groupby("timestamp_ms").agg(open=("price", "first"), high=("price", "max"),
                    low=("price", "min"), close=("price", "last"), volume=("volume", "sum")).reset_index()
                subset = subset.set_index("timestamp_ms").reindex(np.arange(start, pivot, 60_000))
                missing = subset.volume.isna()
                previous_close = subset.close.ffill(limit=5)
                for col in ["open", "high", "low", "close"]:
                    subset.loc[missing, col] = previous_close.loc[missing]
                subset.loc[missing, "volume"] = 0.
                subset = subset.rename_axis("timestamp_ms").reset_index()
            expected = (pivot - start) // 60_000
            if (len(subset) != expected or subset.empty or subset.timestamp_ms.iloc[0] != start
                    or subset.timestamp_ms.iloc[-1] != pivot - 60_000
                    or not subset.timestamp_ms.diff().dropna().eq(60_000).all()):
                row["feature_status"] = "incomplete_daily_anchor"
            elif (not np.isfinite(subset[cols].to_numpy()).all() or subset.volume.lt(0).any()
                  or subset[["open", "high", "low", "close"]].le(0).any().any()
                  or subset.high.lt(subset.low).any()):
                row["feature_status"] = "invalid_candles"
            else:
                feat = feature(subset, opening, pivot, vwap)
                ref = day_trades.iloc[0]
                if feat is None:
                    row["feature_status"] = "zero_dispersion"
                elif not (np.isclose(feat["morning_open"], ref.price_0930, rtol=1e-10, atol=1e-8)
                          and np.isclose(feat["morning_close"], ref.price_before_1300, rtol=1e-10, atol=1e-8)):
                    row["feature_status"] = "cache_price_mismatch"
                else:
                    row.update(feat, zero_volume_minutes=int(subset.volume.eq(0).sum()))
            feature_rows.append(row)
        print(f"{symbol}: features completed", flush=True)
    features = pd.DataFrame(feature_rows)
    features.to_csv(out / "daily_features.csv", index=False)
    joined = trades.merge(features, on=["symbol", "session_date"], validate="many_to_one")
    joined.to_csv(out / "trades_with_features.csv", index=False)
    usable = joined.loc[joined.feature_status.eq("valid")]
    summary, regimes = analyze(usable, args.validation_start)
    summary.to_csv(out / "filter_metrics.csv", index=False)
    regimes.to_csv(out / "directionality_metrics.csv", index=False)
    features.groupby(["symbol", "feature_status"]).size().rename("sessions").reset_index().to_csv(out / "feature_quality.csv", index=False)
    features.loc[features.feature_status.eq("valid")].groupby("symbol").efficiency_ratio_5m.agg(
        sessions="size", median="median", fraction_er_ge_06=lambda x: float(x.ge(.6).mean())).to_csv(out / "morning_directionality.csv")
    common = set.intersection(*[set(features.loc[features.symbol.eq(s) & features.feature_status.eq("valid"), "session_date"])
                               for s in trades.symbol.unique()])
    common_metrics, _ = analyze(usable.loc[usable.session_date.isin(common)], args.validation_start)
    common_metrics.to_csv(out / "common_dates_filter_metrics.csv", index=False)
    cfg = dict(source=str(args.run.resolve()), anchor="00:00 UTC daily", timeframe="1m",
        source_price="HLC3", cutoff="strictly before 13:00 NY", validation_start=args.validation_start,
        stretched_sigma=1., low_slope_30m_sigma=.05, caches=manifest,
        common_feature_dates=sorted(common))
    (out / "analysis_config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    lines = ["# Uji tambahan VWAP harian", "",
        "VWAP reset 00:00 UTC, dihitung dari HLC3 candle 1 menit × volume memakai indicator.vwap.vwap, dan dibekukan sebelum 13:00 NY. POC, entry, SL dan outcome trade berasal dari run sebelumnya.",
        "Sigma = RMS deviasi HLC3 dari VWAP final, berbobot volume. Definisi band ini eksplisit dan tidak menjamin sama dengan band setiap platform chart.",
        "Slope = (VWAP sebelum 13:00 − VWAP sebelum 12:30) / sigma. Efficiency ratio memakai perubahan close candle 5 menit pada 09:30–13:00; ini proxy directedness pagi, bukan label regime multihari.",
        "Filter: baseline; harga selaras dengan VWAP; selaras + jarak ≥1 sigma; kondisi tersebut + |slope| ≤0.05 sigma/30 menit. Ambang dipilih untuk eksperimen awal dan tidak dituning.",
        "Development: sebelum 2026-01-01; validation: sejak 2026-01-01. Bagian validation sudah pernah dilihat pada analisis sebelumnya, jadi bukan holdout yang benar-benar belum tersentuh. Jangan memilih asset/filter hanya dari hasil validation ini.",
        "Baseline dan filter memakai trade dengan fitur valid yang sama. Periksa feature_quality.csv dan jumlah trade; hasil kelompok kecil belum meyakinkan. Hipotesis HYPE mayoritas trending tidak diasumsikan benar.",
        "Candle tanpa transaksi memakai carry-forward close sebelumnya dengan volume nol (maksimal 5 menit pada fallback raw). Candle ini bukan transaksi baru dan tidak menambah bobot VWAP. Cache dicocokkan dengan harga awal 09:30 dan harga akhir sebelum 13:00 pada log sumber.",
        "Angka utama gross; fee 2 bps per sisi hanya skenario. Sumber exchange/produk masih belum terverifikasi. common_dates_filter_metrics.csv memakai tanggal fitur yang tersedia pada semua market, dengan eligibility signal yang bisa berbeda.", "",
        "## Perbandingan tanggal bersama: profile hari ini 09:30–13:00", "",
        "Hanya tanggal fitur valid pada semua asset, sejak 2026-01-01. Filter ketat = selaras VWAP, jarak ≥1 sigma, |slope| ≤0.05. Jumlah entry tetap bisa berbeda karena syarat bias terhadap POC.", "",
        "| Market | Filter | Trade | Avg R gross | Total R gross | Avg R fee 2 bps/sisi |",
        "|---|---|---:|---:|---:|---:|"]
    compact = common_metrics.loc[common_metrics.period.eq("validation")
        & common_metrics.profile.eq("ny_0930_1300")
        & common_metrics["filter"].isin(["baseline", "vwap_aligned_1sigma_low_slope"])]
    for r in compact.loc[compact.hypothetical_fee_bps_each_side.eq(0)].itertuples():
        fee_row = compact.loc[compact.symbol.eq(r.symbol) & compact["filter"].eq(r.filter)
            & compact.hypothetical_fee_bps_each_side.eq(2)].iloc[0]
        label = "baseline" if r.filter == "baseline" else "filter ketat"
        lines.append(f"| {r.symbol} | {label} | {r.trades} | {r.mean_r:+.4f} | {r.total_r:+.2f} | {fee_row.mean_r:+.4f} |")
    lines += ["", "## Hasil validation gross: seluruh profile dan filter", "",
        "| Market | Profile | Filter | Trade | Avg R | Total R | PF | Win rate |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for r in summary.loc[summary.period.eq("validation") & summary.hypothetical_fee_bps_each_side.eq(0)].itertuples():
        avg = f"{r.mean_r:+.4f}" if pd.notna(r.mean_r) else "N/A"
        pf = f"{r.profit_factor:.3f}" if pd.notna(r.profit_factor) else "N/A"
        win = f"{r.win_rate_pct:.1f}%" if pd.notna(r.win_rate_pct) else "N/A"
        lines.append(f"| {r.symbol} | {r.profile} | {r.filter} | {r.trades} | {avg} | {r.total_r:+.2f} | {pf} | {win} |")
    lines += ["", "[Definisi VWAP dan anchor](https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/). Reset 00:00 UTC adalah pilihan eksperimen crypto ini, bukan default universal semua platform."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved VWAP study: {out}", flush=True)


if __name__ == "__main__":
    main()
