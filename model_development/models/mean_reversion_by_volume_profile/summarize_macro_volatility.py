"""Full macro-volatility readout and timeline chart; annotation only, no filter selection."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_macro_volatility as model

NAMES = {'A_first_touch':'A', 'distance':'Jarak >=1%', 'location':'Lokasi open',
         'intersection':'Irisan', 'union':'Gabungan'}
ORDER = {name:i for i,name in enumerate(NAMES)}


def plot(out, states, cuts, ledger):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    first = pd.Timestamp(cuts.test_start.min(), tz=model.base.NY)
    last = pd.Timestamp(cuts.test_end_exclusive.max(), tz=model.base.NY)
    t = pd.to_datetime(states.available_ms, unit='ms', utc=True)
    active = (t >= first.tz_convert('UTC')) & (t <= last.tz_convert('UTC'))
    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True, constrained_layout=True)
    fig.suptitle('LTCUSDC: 30-day macro volatility and unchanged RR 1:1 models', fontsize=14)
    for ax, feature, title in zip(axes[:2], model.FEATURES, ('ATR30 SMA / latest daily close (%)', 'Std of 30 daily log returns (%)')):
        ax.plot(t[active], states.loc[active, feature], color='#2563eb', lw=1.5, label='Known completed-day value')
        c = cuts.loc[cuts.feature.eq(feature)].sort_values('fold')
        boundaries = [pd.Timestamp(v, tz=model.base.NY) for v in c.test_start]+[last]
        for column, color, label in (('low_cut', '#059669', 'Train 33rd percentile'), ('high_cut', '#d97706', 'Train 67th percentile')):
            values = c[column].tolist()+[c[column].iloc[-1]]
            ax.step(boundaries, values, where='post', color=color, lw=1.2, ls='--', label=label)
        ax.set_ylabel(title)
        ax.grid(alpha=.2)
        ax.legend(loc='upper right', fontsize=8, ncol=3)
    colors = {'A_first_touch':'#b91c1c', 'distance':'#2563eb', 'location':'#16a34a', 'intersection':'#7c3aed', 'union':'#ea580c'}
    for parent in NAMES:
        p = ledger.loc[ledger.model.eq(parent)].sort_values('exit_timestamp_ny')
        axes[2].step(pd.to_datetime(p.exit_timestamp_ny, utc=True), p.net_r.cumsum(), where='post',
            color=colors[parent], label=NAMES[parent], lw=1.3)
    axes[2].axhline(0, color='black', lw=.7)
    axes[2].set_ylabel('Closed-trade cumulative net R\n(no compounding)')
    axes[2].legend(loc='lower left', ncol=5, fontsize=8)
    axes[2].grid(alpha=.2)
    for ax in axes:
        ax.axvline(pd.Timestamp('2026-01-01', tz=model.base.NY), color='#475569', lw=.8, ls=':')
        ax.set_xlim(first, last)
    axes[2].xaxis.set_major_locator(dates.MonthLocator(interval=2))
    axes[2].xaxis.set_major_formatter(dates.DateFormatter('%b %Y'))
    axes[2].tick_params(axis='x', rotation=25)
    fig.savefig(out/'macro_regimes.png', dpi=160)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'regime_summary.csv')
    baseline = pd.read_csv(out/'baseline_summary.csv')
    years = pd.read_csv(out/'yearly_metrics.csv')
    cuts = pd.read_csv(out/'train_cutpoints.csv', float_precision='round_trip')
    states = pd.read_parquet(out/'daily_macro_states.parquet')
    ledger = pd.read_parquet(out/'feature_ledger.parquet')
    confidence7 = pd.read_csv(out/'absolute_confidence.csv')
    confidence30 = pd.read_csv(out/'confidence_block30.csv')
    stability = pd.read_csv(out/'stability.csv')
    summary['mean_fee_r'] = summary.gross_mean_r-summary.net_mean_r
    summary.to_csv(out/'regime_summary_with_costs.csv', index=False)
    plot(out, states, cuts, ledger)
    for feature in model.FEATURES:
        matrix = summary.loc[summary.feature.eq(feature)].pivot(index='model', columns='regime', values='net_mean_r')
        matrix.reindex(NAMES).to_csv(out/(feature+'_mean_net_matrix.csv'))
    manifest = json.loads((out/'config_snapshot.json').read_text())
    installed_run = Path(manifest['source_trade_run']).parent/out.name
    image_path = (installed_run/'macro_regimes.png').as_posix()
    lines = ['# LTC: macro ATR 30 hari dan standar deviasi return 30 hari — v29', '',
        'Hipotesis bahwa model hanya bekerja saat volatilitas tinggi tidak didukung hasil ini. '
        'Irisan checkpoint masih net positif pada ketiga regime untuk kedua indikator. '
        'ATR tinggi memiliki kualitas net lebih besar pada beberapa model filter, '
        'sedangkan standar deviasi return sedang sering terlihat paling baik. '
        'Efeknya berbeda antar submodel dan sebagian kelompok berisi sedikit trade.', '',
        '## Definisi yang dikunci', '',
        'Hanya LTCUSDC, lima submodel A / jarak >=1% / lokasi open / irisan / gabungan. '
        'Semua memakai RR 1:1 dari checkpoint untuk memisahkan efek regime dari perubahan '
        'SL. Entry, TP POC, SL, biaya dan membership trade tidak diubah. '
        'A adalah baseline first-edge inside VA; empat model filter tetap memakai '
        'entry simple base serta membership train-selected yang telah disimpan.', '',
        'ATR30% = 100 × mean(30 daily True Range) / close harian terakhir. '
        'True Range memakai fungsi proyek indicator/trend.py, tetapi smoothing memakai '
        'SMA tepat 30 hari, bukan ATR Wilder yang memiliki memori lebih panjang. '
        'Std30 = 100 × sample standard deviation (ddof=1) dari 30 log return harian. '
        'Tidak dianualisasi dan tidak memakai standar deviasi level harga. '
        'Keduanya membutuhkan 31 candle harian UTC valid berturut-turut.', '',
        'Untuk setiap entry, gunakan daily state terakhir yang sudah tersedia pada atau '
        'sebelum entry; candle hari berjalan tidak dipakai. '
        'Rendah <= persentil 33,3%; sedang hingga persentil 66,7%; tinggi di atasnya. '
        'Batas berasal dari daily feature states yang sudah tersedia dalam delapan bulan '
        'train sebelum midnight NY awal test. Batas sama bagi semua model pada setiap fold, '
        'dikunci selama dua bulan test; tidak memakai P&L untuk memilih batas atau kategori. '
        'Karena relatif terhadap train, label tinggi tidak memiliki satu batas persen '
        'yang tetap sepanjang 2025–2026. Ini klasifikasi, belum filter entry baru.', '',
        'Candle harian OHLC diaudit dan dipakai ulang dari cache v18 yang dibangun dari '
        'archive LTC yang sama. Kolom ADX, EMA dan fitur arah lama tidak digunakan. '
        'Semua 1.123 model-entry memperoleh kedua fitur pada test (coverage 100%). '
        'Baseline kelima model dan semua outcome tetap identik.', '',
        f'![Macro volatility and unchanged-model results]({image_path})', '']
    for feature, title in (('atr30_pct', 'ATR% rolling 30 hari'), ('std30_return_pct', 'Standar deviasi return rolling 30 hari')):
        lines += [f'## {title}', '',
            '| Model | Regime | Entry | WR gross | Mean gross R | Mean fee R | Mean net R | Total net R |',
            '|---|---|---:|---:|---:|---:|---:|---:|']
        for parent in NAMES:
            for regime in model.REGIMES:
                r = summary.loc[summary.model.eq(parent) & summary.feature.eq(feature) & summary.regime.eq(regime)].iloc[0]
                lines.append(f'| {NAMES[parent]} | {regime} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
                    f'{r.gross_mean_r:+.4f} | {r.mean_fee_r:.4f} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
        lines += ['']
    lines += ['Fee 0,04% setiap sisi. WR mencakup posisi profit saat time exit, bukan hanya '
        'TP POC. Setiap kelompok pada satu indikator merupakan partisi; total R ketiganya '
        'menjumlah ke baseline model, bukan tiga eksperimen independen.', '',
        '## Interpretasi utama', '',
        '- A tetap net negatif pada seluruh regime. ATR rendah hanya membuatnya hampir impas (-0,0066R/trade).',
        '- Jarak >=1% dan gabungan: ATR tinggi lebih baik daripada ATR rendah, tetapi kelompok rendah juga net positif.',
        '- Lokasi open: mean net lebih tinggi pada ATR tinggi, tetapi gross mean R justru '
        'sedikit lebih rendah daripada ATR rendah. Fee relatif terhadap risiko jauh lebih kecil '
        'pada ATR tinggi. Jadi ini tidak sama dengan bukti probabilitas reversal meningkat.',
        '- Irisan: ATR rendah 37 trade, +0,1763R/trade; tinggi 21 trade, +0,3652R/trade. '
        'Mengambil high saja mempertahankan 30,4% trade dan +7,67R dari baseline +17,68R.',
        '- Std return sedang: irisan 18 trade, +0,4187R/trade. '
        'Namun hanya 10 trade pada 2025 (semuanya gross profit) dan 8 pada 2026 '
        '(WR gross 37,5%, net -0,1596R/trade). Agregat yang tinggi tidak stabil antar tahun.', '',
        '## Irisan per tahun', '',
        '| Indikator | Regime | Tahun | Entry | WR gross | Mean net R | Total net R |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in years.loc[years.model.eq('intersection')].itertuples():
        lines.append(f'| {r.feature} | {r.regime} | {r.year} | {int(r.trades)} | '
            f'{r.gross_wr_pct:.2f}% | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'ATR tinggi irisan pada 2026 hanya berisi tiga trade. '
        'Sebaliknya ATR rendah memiliki 18 trade pada 2025 dan 19 pada 2026, '
        'dan kedua tahun tetap net positif. Std rendah juga net positif pada kedua tahun. '
        'Dengan demikian, hasil ini tidak mendukung membuang seluruh regime rendah.', '',
        '## Ketidakpastian irisan', '',
        '| Indikator | Regime | Fold net positif / aktif | CI95 mean net R, blok 7 hari | CI95 mean net R, blok 30 hari |',
        '|---|---|---:|---:|---:|']
    for r in stability.loc[stability.model.eq('intersection')].itertuples():
        a = confidence7.loc[confidence7.model.eq(r.model) & confidence7.feature.eq(r.feature) & confidence7.regime.eq(r.regime)].iloc[0]
        b = confidence30.loc[confidence30.model.eq(r.model) & confidence30.feature.eq(r.feature) & confidence30.regime.eq(r.regime)].iloc[0]
        lines.append(f'| {r.feature} | {r.regime} | {r.positive_net_test_folds}/{r.nonempty_test_folds} | '
            f'[{a.net_mean_ci025:+.4f}, {a.net_mean_ci975:+.4f}] | [{b.net_mean_ci025:+.4f}, {b.net_mean_ci975:+.4f}] |')
    lines += ['', 'Kedua bootstrap memakai 4.000 resample blok kalender dalam fold test yang '
        'sama, dengan cutoff dan membership tetap. Sensitivitas blok 30 hari disertakan '
        'karena fitur rolling satu bulan berkelompok dan sangat berautokorelasi. '
        'Blok lebih panjang tidak harus menghasilkan interval lebih lebar pada sampel '
        'stratifikasi fold yang tetap. Tidak ada koreksi banyak perbandingan dan seluruh '
        'kelompok masih menggunakan riwayat development yang sudah dipelajari.', '',
        'CI agregat positif tidak berarti hasil stabil antar tahun: std sedang irisan '
        'tetap negatif pada 2026 meskipun CI mean agregat positif. Beberapa kelompok '
        'hanya memiliki 11–18 trade. Empat model filter memiliki overlap trade besar '
        'dan ATR/std juga berkorelasi kuat; jangan menganggapnya konfirmasi independen. '
        'Volatilitas tidak memakai arah, tetapi dapat berkorelasi dengan tren, lokasi '
        'open, jarak target serta biaya. Ini hubungan deskriptif, bukan sebab-akibat.', '',
        '## Cutoff train yang dipakai bersama', '',
        '| Fold | Test mulai | ATR% 33,3 / 66,7 | Std% 33,3 / 66,7 | Daily train observations |',
        '|---|---|---:|---:|---:|']
    for fold, g in cuts.groupby('fold'):
        a = g.loc[g.feature.eq('atr30_pct')].iloc[0]
        b = g.loc[g.feature.eq('std30_return_pct')].iloc[0]
        lines.append(f'| {fold} | {a.test_start} | {a.low_cut:.3f} / {a.high_cut:.3f} | '
            f'{b.low_cut:.3f} / {b.high_cut:.3f} | {int(a.train_daily_observations)} |')
    lines += ['', 'Tidak ada perubahan checkpoint v27/v28, filter regime baru, sizing atau '
        'pemilihan kategori terbaik yang diterapkan. Nol profile dibangun ulang dan '
        'nol archive raw dibaca ulang pada iterasi ini. Spread, funding, latency dan '
        'dampak order belum dihitung. Tahun 2026 hanya bagian test yang tersedia.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    names = ('FINDINGS.md', 'macro_regimes.png', 'regime_summary_with_costs.csv', 'atr30_pct_mean_net_matrix.csv',
        'std30_return_pct_mean_net_matrix.csv', 'VALIDATION.json', 'confidence_block30.csv')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in names}), indent=2))
    print('Macro findings and chart written; no regimes selected as new entry filters.')


if __name__ == '__main__':
    main()
