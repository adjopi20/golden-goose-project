"""Read-only validation plus figures and Indonesian findings for sizing v12."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

KEYS = ["symbol", "profile", "session_date"]
GROUP = ["symbol", "profile", "model", "target_cost_bps"]
TODAY = "ny_0930_1300"
MODELS = ["adx_4h", "ema50_slope_4h", "poc_distance_volatility", "va_pre_ny", "va_previous",
          "vwap_daily", "vwap_24h", "adx_plus_vwap", "adx_plus_poc_distance"]
LABELS = ["ADX 4h", "EMA50 slope 4h", "POC distance / volatility", "Pre-NY VA outside closes",
          "Previous VA outside closes", "Daily VWAP displacement", "24h VWAP displacement",
          "ADX + daily VWAP", "ADX + POC distance"]


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def validate(out):
    cfg = json.loads((out/"config_snapshot.json").read_text())
    assert cfg["status"] == "complete"
    assert cfg["code_sha256"] == hash_file(Path(__file__).with_name("observe_edge_sizing.py"))
    audit = json.loads((out/"audit.json").read_text())
    assert audit["status"] == "passed"
    for source in audit["sources"]:
        assert hash_file(source["path"]) == source["sha256"], source["path"]
    original_path = next(s["path"] for s in audit["sources"] if s["role"] == "original")
    original = pd.read_csv(original_path)
    original = original.loc[original.symbol.isin(cfg["symbols"])]
    base = pd.read_csv(out/"feature_trade_ledger.csv")
    pred = pd.read_csv(out/"sizing_predictions.csv")
    summary = pd.read_csv(out/"summary.csv")
    coverage = pd.read_csv(out/"coverage.csv")
    assert set(base.symbol) == set(pred.symbol) == {"BTCUSDC", "XRPUSDT"}
    assert not pred.duplicated(KEYS+["model", "target_cost_bps"]).any()
    checked = base.merge(original, on=KEYS, suffixes=("", "_old"), validate="one_to_one")
    assert len(checked) == len(base)
    for c in ["entry", "tp", "sl", "side", "risk_price", "gross_r"]:
        np.testing.assert_allclose(checked[c], checked[c+"_old"])
    for c in ["status", "entry_timestamp_ny", "exit_timestamp_ny"]:
        assert checked[c].eq(checked[c+"_old"]).all()
    snapshots = pd.read_csv(out/"vwap_24h_entry_snapshots.csv")
    pivot = pd.to_datetime(snapshots.session_date+" 13:00").dt.tz_localize("America/New_York").dt.tz_convert("UTC")
    assert pd.to_datetime(snapshots.snapshot_ms, unit="ms", utc=True).eq(pivot).all()
    # Reconcile every policy/cost against the persisted trade-level allocations.
    groups = {key: g.sort_values("session_date") for key, g in pred.groupby(GROUP)}
    for r in summary.itertuples():
        g = groups[(r.symbol, r.profile, r.model, r.target_cost_bps)]
        assert len(g) == r.trades
        risk = g[{"fixed_base": "base_risk", "scored_sizing": "sized_risk", "fixed_train_matched": "matched_risk"}[r.policy]].to_numpy()
        net = g.gross_r.to_numpy()-r.cost_bps*g.cost_r_per_bps.to_numpy()
        np.testing.assert_allclose(np.sum(risk/.005*net), r.capital_net_base_r, atol=1e-9)
        np.testing.assert_allclose(100*(np.prod(1+risk*net)-1), r.compounded_return_pct, atol=1e-9)
        m = (g.multiplier if r.policy == "scored_sizing" else
             g.matched_requested_multiplier if r.policy == "fixed_train_matched" else np.ones(len(g)))
        np.testing.assert_allclose(np.sum(m*g.gross_r), r.uncapped_weighted_gross_r, atol=1e-9)
    expected = coverage.groupby(GROUP).test_trades.sum()
    actual = pred.groupby(GROUP).size()
    pd.testing.assert_series_equal(expected.sort_index(), actual.sort_index(), check_names=False)
    # Every candidate must keep the complete identical base test population.
    for _, g in pred.groupby(["symbol", "profile", "session_date"]):
        assert len(g) == len(cfg["models"])*len(cfg["calibration_cost_bps"])
        assert g.gross_r.nunique() == 1
    population = []
    for (symbol, profile), source in original.groupby(["symbol", "profile"]):
        active = source.loc[source.status.isin(["tp_first", "sl_first", "time_exit"])]
        q = pred.loc[pred.symbol.eq(symbol) & pred.profile.eq(profile) & pred.model.eq("adx_4h") & pred.target_cost_bps.eq(0)]
        population.append(dict(symbol=symbol, profile=profile, source_sessions=len(source),
            valid_source_sessions=int(source.profile_status.eq("valid").sum()), source_entries=len(active),
            complete_test_entries=len(q), test_first=q.session_date.min(), test_last=q.session_date.max(),
            initial_or_trailing_entries=len(active)-len(q),
            note="Initial training and incomplete trailing periods are not shortened into tests."))
    pd.DataFrame(population).to_csv(out/"population.csv", index=False)
    checks = dict(status="passed", original_entries=len(base), prediction_rows=len(pred), summary_rows=len(summary),
        checks=["source hashes unchanged", "engine code hash", "entry/TP/SL/side/exit/R identity",
                "pre-entry 24h snapshot timestamps", "every candidate retains identical entries",
                "all policy/cost capital and equity reconciliation", "uncapped matched-control units"],
        report_code_sha256=hash_file(__file__))
    (out/"validation.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    return summary, coverage, pred


def plot(out, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    robust = pd.read_csv(out/"robustness.csv")
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), constrained_layout=True)
    for col, symbol in enumerate(["BTCUSDC", "XRPUSDT"]):
        for row, cost in enumerate([0, 2]):
            ax = axes[row, col]
            g = summary.loc[summary.symbol.eq(symbol) & summary.profile.eq(TODAY)
                & summary.target_cost_bps.eq(cost) & summary.cost_bps.eq(cost)]
            table = g.pivot(index="model", columns="policy", values="capital_net_base_r").reindex(MODELS)
            delta = table.scored_sizing-table.fixed_train_matched
            u = robust.loc[robust.symbol.eq(symbol) & robust.profile.eq(TODAY)
                & robust.target_cost_bps.eq(cost) & robust.cost_bps.eq(cost)].set_index("model").reindex(MODELS)
            n = g.loc[g.policy.eq("scored_sizing")].set_index("model").reindex(MODELS).trades.to_numpy()
            low = u.bootstrap_delta_per_opportunity_ci025.to_numpy()*n
            high = u.bootstrap_delta_per_opportunity_ci975.to_numpy()*n
            y = np.arange(len(MODELS))
            ax.hlines(y, low, high, color="#7a8794", lw=2)
            ax.scatter(delta, y, c=np.where(delta >= 0, "#087f8c", "#b34e33"), s=38, zorder=3)
            ax.axvline(0, color="#36434f", lw=1)
            ax.set_yticks(y, LABELS if col == 0 else [])
            ax.invert_yaxis()
            ax.grid(axis="x", alpha=.15)
            ax.set_title(f"{symbol} — {'gross' if cost == 0 else '2 bps per side scenario'}", fontsize=12)
            ax.set_xlabel("Sized minus TRAIN-matched fixed: capital base-R units")
    fig.suptitle("Same entries; learned position weights versus fixed risk\nToday's 09:30–13:00 POC · 8+2-month retrospective walk-forward", fontsize=15)
    fig.supxlabel("Intervals: descriptive 95% seven-day block bootstrap; uncorrected for many trials. One base-R unit = 0.5% pre-trade equity.", fontsize=10)
    fig.savefig(out/"sizing_comparison.png", dpi=150)
    plt.close(fig)


def findings(out, summary, coverage):
    stable = pd.read_csv(out/"stability.csv")
    robust = pd.read_csv(out/"robustness.csv")
    cohorts = pd.read_csv(out/"sizing_cohorts.csv")
    lines = ["# Ringkasan sizing mean reversion — v12", "",
        "Semua entry dipertahankan: BTC 444 dan XRP 900 pada POC pagi 09:30–13:00 NY. Jumlah ini lebih besar daripada baseline fitur-lengkap studi lama karena fitur hilang sekarang mendapat bobot netral, bukan dikeluarkan. HYPE tidak diteliti.",
        "Train 8 bulan, test 2 bulan, maju 2 bulan. Bobot 0,5–1,5× dipelajari dari train; kombinasi merata-ratakan kontribusi, tanpa filter AND. Risiko acuan 0,5% equity dan batas notional 5×. Pembanding fixed di bawah dicocokkan pada rata-rata risiko TRAIN setelah cap, bukan disetarakan memakai masa depan.",
        "Hasil gross dan biaya 2 bps/sisi merupakan dua kalibrasi terpisah yang telah ditetapkan sebelum pengujian. Angka R modal berbobot bukan R asli trade; satu unit setara alokasi risiko acuan 0,5% equity sebelum trade.", "",
        "## Gross: semua kandidat pada POC pagi", "",
        "| Asset | Indikator sizing | R modal fixed → sized | Return akun fixed → sized | Fold membaik | Interval 95% tambahan R/opportunity |",
        "|---|---|---:|---:|---:|---:|"]
    for symbol in ["BTCUSDC", "XRPUSDT"]:
        for model, label in zip(MODELS, LABELS):
            g = summary.loc[summary.symbol.eq(symbol) & summary.profile.eq(TODAY)
                & summary.model.eq(model) & summary.target_cost_bps.eq(0) & summary.cost_bps.eq(0)]
            b, s = [g.loc[g.policy.eq(p)].iloc[0] for p in ["fixed_train_matched", "scored_sizing"]]
            st = stable.loc[stable.symbol.eq(symbol) & stable.profile.eq(TODAY) & stable.model.eq(model)
                & stable.target_cost_bps.eq(0) & stable.cost_bps.eq(0)].iloc[0]
            u = robust.loc[robust.symbol.eq(symbol) & robust.profile.eq(TODAY) & robust.model.eq(model)
                & robust.target_cost_bps.eq(0) & robust.cost_bps.eq(0)].iloc[0]
            lines.append(f"| {symbol} | {label} | {b.capital_net_base_r:+.2f} → {s.capital_net_base_r:+.2f} | {b.compounded_return_pct:+.2f}% → {s.compounded_return_pct:+.2f}% | {int(st.improved_folds)}/{int(st.folds)} | [{u.bootstrap_delta_per_opportunity_ci025:+.4f}, {u.bootstrap_delta_per_opportunity_ci975:+.4f}] |")
    lines += ["", "## Apakah ukuran lebih besar diberikan ke trade yang lebih berkualitas?", "",
        "Kelompok bobot dipilih dari train, dilaporkan pada test. Rendah ≤0,9×, sedang di antaranya, tinggi ≥1,1×. WR di bawah tetap WR asli trade, bukan WR yang dapat dinaikkan oleh ukuran posisi.", "",
        "| Asset | Sizing | Kelompok | Trade | Mean gross R asli | WR gross asli |", "|---|---|---|---:|---:|---:|"]
    for symbol, model in [("BTCUSDC", "adx_4h"), ("BTCUSDC", "ema50_slope_4h"), ("XRPUSDT", "adx_4h")]:
        for cohort in ["lower", "middle", "higher"]:
            q = cohorts.loc[cohorts.symbol.eq(symbol) & cohorts.profile.eq(TODAY) & cohorts.model.eq(model)
                & cohorts.target_cost_bps.eq(0) & cohorts.cost_bps.eq(0) & cohorts.cohort.eq(cohort)].iloc[0]
            lines.append(f"| {symbol} | {model} | {cohort} | {int(q.trades)} | {q.original_mean_gross_r:+.4f} | {q.original_gross_wr_pct:.1f}% |")
    lines += ["", "## Biaya dan batas kesimpulan", "",
        "Pada skenario biaya 2 bps/sisi, semua sembilan kandidat sizing untuk POC pagi masih menghasilkan total net negatif pada kedua asset. Beberapa mengurangi rugi dibanding fixed, tetapi mengurangi rugi belum membuat model profitable. Biaya ini hipotetis; venue/produk, funding dan fill aktual belum terverifikasi.",
        "Gross ADX XRP dan EMA slope BTC layak sebagai kandidat lanjutan, namun interval tambahan hasil keduanya masih melintasi nol dan kontrol pembobotan acak belum kuat. Tidak ada kesimpulan bahwa indikator tersebut memprediksi probabilitas yang stabil. ADX juga tidak menghasilkan urutan rendah–sedang–tinggi yang sepenuhnya mulus.",
        "VWAP daily maupun 24h tidak meningkatkan total hasil pada kalibrasi gross untuk BTC atau XRP. Pada kalibrasi biaya hasilnya campuran; belum ada dasar menaikkan sizing karena VWAP. Rolling 24h saat entry 13:00 sama dengan snapshot frozen 24h; tidak ada perubahan posisi setelah entry.",
        "Kombinasi tidak otomatis lebih baik: pada gross, ADX + daily VWAP dan ADX + jarak POC menghasilkan tambahan yang lebih kecil daripada ADX sendiri pada XRP; penambahan VWAP juga tidak mengalahkan ADX sendiri pada BTC.",
        "Sembilan kandidat × empat POC × dua asset × dua target kalibrasi telah dicoba; skenario biaya menambah pembacaan. Interval dan kontrol acak belum mengoreksi pencarian tersebut. Seluruh hasil per POC/fold/kategori tetap dilaporkan, tanpa memilih rule terbaik otomatis.",
        "Lihat REPORT.md untuk tabel net, summary.csv untuk semua statistik, sizing_cohorts.csv untuk semua kelompok bobot, population.csv untuk cakupan, calibration.json untuk keputusan train, dan validation.json untuk rekonsiliasi. DD hanya pada penutupan trade; tidak ada klaim eksekusi live, likuidasi atau Sharpe tahunan.", "",
        "![Perbandingan sizing](sizing_comparison.png)", ""]
    (out/"FINDINGS.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    summary, coverage, pred = validate(args.run)
    plot(args.run, summary)
    findings(args.run, summary, coverage)
    print(json.dumps(dict(status="passed", summary_rows=len(summary), prediction_rows=len(pred))))


if __name__ == "__main__":
    main()
