"""Conditional sizing readout: account returns, risk, scenario equivalence and equity curves."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_conditional_sizing as model

LABELS = {1:'Gabungan; boost lokasi', 2:'Irisan; boost lokasi (semua trade)',
    3:'Irisan; boost ATR tinggi', 4:'Gabungan; boost ATR tinggi',
    5:'Gabungan; boost lokasi DAN ATR tinggi', 6:'Irisan; boost lokasi DAN ATR tinggi (=3)'}


def plot(out, trades):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True, constrained_layout=True)
    fig.suptitle('LTCUSDC RR 1:1: conditional sizing, unchanged entries and exits', fontsize=14)
    start, end = pd.Timestamp('2025-01-01', tz=model.base.NY), pd.Timestamp('2026-09-01', tz=model.base.NY)
    for ax, parent, cases in ((axes[0], 'union', (1, 4, 5)), (axes[1], 'intersection', (2, 3))):
        baseline = trades.loc[trades.scenario.eq('baseline_'+parent)].sort_values('exit_timestamp_ny')
        times = [start]+pd.to_datetime(baseline.exit_timestamp_ny, utc=True).tolist()
        ax.step(times, [0.]+(100*(baseline.equity_after-1)).tolist(), where='post', color='black', lw=1.8, label='Normal risk 0.5%')
        for case, color in zip(cases, ('#16a34a', '#2563eb', '#d97706')):
            for mult, style in ((1.5, '--'), (2., '-')):
                p = trades.loc[trades.scenario.eq(f'case{case}_x{mult:g}')].sort_values('exit_timestamp_ny')
                short = {1:'1 Location', 2:'2 All trades', 3:'3=6 ATR high', 4:'4 ATR high', 5:'5 Location + ATR high'}[case]
                ax.step([start]+pd.to_datetime(p.exit_timestamp_ny, utc=True).tolist(),
                    [0.]+(100*(p.equity_after-1)).tolist(), where='post', color=color, ls=style,
                    lw=1.2, label=f'{short} x{mult:g}')
        ax.axhline(0, color='#64748b', lw=.7)
        ax.axvline(pd.Timestamp('2026-01-01', tz=model.base.NY), color='#64748b', ls=':', lw=.9)
        ax.set_title('Gabungan / OR — 318 trades' if parent == 'union' else 'Irisan / AND — 69 trades')
        ax.set_ylabel('Closed-trade net equity return (%)')
        ax.set_xlim(start, end)
        ax.grid(alpha=.2)
        ax.legend(fontsize=8, ncol=3, loc='upper left')
    axes[1].xaxis.set_major_locator(dates.MonthLocator(bymonth=(1, 4, 7, 10), tz=model.base.NY))
    axes[1].xaxis.set_major_formatter(dates.DateFormatter('%b %Y', tz=model.base.NY))
    fig.savefig(out/'sizing_equity.png', dpi=160)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    years = pd.read_csv(out/'yearly_metrics.csv')
    folds = pd.read_csv(out/'fold_metrics.csv')
    trades = pd.read_parquet(out/'sizing_ledger.parquet')
    plot(out, trades)
    stability = []
    for scenario, group in folds.groupby('scenario', sort=True):
        stability.append(dict(scenario=scenario, test_folds=len(group),
            nonempty_test_folds=int(group.trades.gt(0).sum()), positive_net_account_folds=int(group.net_account_return_pct.gt(0).sum())))
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    manifest = json.loads((out/'config_snapshot.json').read_text())
    image_path = (Path(manifest['checkpoint_run']).parent/out.name/'sizing_equity.png').as_posix()
    lines = ['# Conditional position sizing LTC — v30', '',
        'Semua trade dan exit RR 1:1 checkpoint tetap. Hasil terbesar datang dari menaikkan '
        'SELURUH trade irisan ke 2x sizing (kasus 2), yang secara mekanis menaikkan risiko '
        'semua trade. Pada gabungan, boost lokasi memberi hasil lebih baik daripada boost ATR '
        'saja. ATR-based boost pada irisan menaikkan profit, tetapi return/drawdown sampel '
        'lebih rendah daripada normal sizing.', '',
        '## Definisi', '',
        'LTCUSDC saja, source test 2025-01 sampai 2026-08 (2026 parsial). Normal target '
        'risk 0,5% equity sebelum trade. Kondisi terpenuhi: risk target 0,75% pada 1,5x '
        'atau 1% pada 2x. Desired notional = equity × target risk / fractional stop distance; '
        'notional tetap dibatasi 5x equity. Size tidak selalu mencapai multiplier bila cap '
        'mengikat. Equity berkompon memakai P&L net tiap trade yang ditutup; tidak ada posisi '
        'overlap dalam satu skenario.', '',
        'Lokasi = membership train-selected v25 yang menghasilkan 138 trade LTC, bukan '
        'pemilihan ulang kelompok berdasarkan hasil sizing. ATR tinggi = label ATR-SMA30% '
        'dari v29 yang sudah tersedia sebelum entry; cutoff 8-bulan train dikunci untuk '
        '2-bulan test. Tidak ada refit atau filter entry baru. Seluruh skenario independen '
        'sebagai akun hipotetis; bukan dijalankan bersamaan atau digabung sebagai portfolio.', '',
        'Pada irisan, seluruh 69 trade sudah memenuhi lokasi. Akibatnya kasus 2 adalah '
        'uniform sizing seluruh trade irisan, dan kasus 6 identik dengan kasus 3. '
        'Ini diverifikasi secara numerik, bukan dua bukti independen.', '',
        '## Baseline dan hasil akun', '',
        '| Model / kondisi | Multiplier | Entry | Trade memenuhi kondisi | Trade size aktual naik | Net return akun | Max closed DD | Mean actual risk/trade |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary.itertuples():
        label = ('Baseline gabungan' if r.parent_model == 'union' else 'Baseline irisan') if r.case == 0 else f'{r.case}. {LABELS[r.case]}'
        lines.append(f'| {label} | {r.multiplier:g}x | {r.trades} | {r.boost_condition_trades} | '
            f'{r.actual_increased_trades} | {r.net_account_return_pct:+.2f}% | {r.max_closed_dd_pct:.2f}% | '
            f'{r.mean_actual_risk_pct:.4f}% |')
    lines += ['', 'Return akun adalah kumulatif seluruh test, BUKAN return tahunan. Drawdown '
        'diukur pada equity setelah trade ditutup, belum intratrade. Fee 0,04% per sisi '
        'dihitung pada notional aktual. Funding, spread, latency dan market impact belum '
        'dihitung, sehingga manfaat peningkatan sizing bisa terlalu optimistis.', '',
        f'![Closed-trade equity comparison]({image_path})', '',
        '## R, risiko dan biaya', '',
        'WR dan R terhadap risiko aktual per trade tidak berubah: gabungan WR gross 55,66%, '
        'mean gross +0,1169R dan mean net +0,0330R; irisan WR gross 62,32%, mean gross '
        '+0,3032R dan mean net +0,2563R. Menambah sizing tidak meningkatkan probabilitas '
        'entry atau mengubah TP/SL.', '',
        'R berbobot normal di bawah = sum(net R_i × actual risk fraction_i / 0,005). '
        'Ini mengukur alokasi relatif terhadap unit risiko normal pada equity sebelum '
        'masing-masing trade, bukan R per trade aktual dan bukan uang tetap. '
        'Risk-weighted mean R = sum(net R_i × actual risk fraction_i) / sum(actual risk fraction_i). '
        'Uniform leverage mengubah total R berbobot tetapi tidak mengubah risk-weighted mean R.', '',
        '| Skenario | R net berbobot normal | Risk-weighted mean net R | Return / closed DD | Capped trade |',
        '|---|---:|---:|---:|---:|']
    for r in summary.itertuples():
        lines.append(f'| {r.scenario} | {r.capital_weighted_total_net_r:+.4f} | '
            f'{r.risk_weighted_mean_net_r:+.4f} | {r.return_over_closed_dd:.3f} | {r.capped_trades} |')
    lines += ['', 'Kasus 1 memiliki 138 trade pemicu, tetapi dua trade sudah terikat cap pada '
        'normal sizing sehingga size aktualnya tidak bertambah. Beberapa trade lain '
        'hanya naik sebagian akibat cap; jumlah capped menjadi 5 pada 1,5x dan 8 pada 2x. '
        'Semua skenario tetap memenuhi maksimum notional 5x.', '',
        '## Per tahun', '',
        '| Skenario | Tahun | Entry | Net return akun | Max closed DD | R net berbobot normal |',
        '|---|---:|---:|---:|---:|---:|']
    for r in years.itertuples():
        lines.append(f'| {r.scenario} | {r.year} | {r.trades} | {r.net_account_return_pct:+.2f}% | '
            f'{r.max_closed_dd_pct:.2f}% | {r.capital_weighted_total_net_r:+.4f} |')
    lines += ['', 'Setiap return tahunan adalah product return trade di tahun tersebut relatif '
        'equity awal tahun; 2026 hanya bagian test yang tersedia. Semua skenario gabungan '
        'masih negatif pada 2026. Boost ATR saja pada gabungan memperburuk 2026: baseline '
        '-1,57%, 1,5x -2,05%, 2x -2,53%. Semua skenario irisan tetap positif pada kedua tahun.', '',
        '## Interpretasi', '',
        '- Kasus 2 return tertinggi: 2x memberi +19,01%, DD 2,42%, versus normal irisan '
        '+9,17%, DD 1,21%. Ini terutama penambahan risiko menyeluruh; risk-weighted '
        'mean net R tetap +0,2563R.',
        '- Kasus 3/6, 2x: +13,37%, DD 2,36%, return/DD 5,67 versus normal 7,59. '
        'Hasil profit naik, tetapi ukuran return relatif drawdown yang diamati turun.',
        '- Kasus 1 memberi peningkatan historis paling besar di antara conditional sizing '
        'pada gabungan: 2x +13,68%, DD 6,99%. Kasus 4, 2x hanya +8,73% dengan DD 6,61%.',
        '- Kasus 5 lebih sedikit trade dinaikkan (32), sehingga perubahan exposure dan '
        'return lebih kecil. Bandingkan juga mean risk pada tabel, bukan profit saja.', '',
        'Peningkatan return dengan risiko lebih besar bukan bukti edge entry baru. '
        'Tidak ada normalisasi total budget risiko antar skenario: frekuensi trade boost '
        'berbeda sehingga total exposure berbeda. Return/DD hanya ukuran deskriptif '
        'sampel, bukan estimasi risiko masa depan atau Calmar tahunan. Seluruh hasil masih '
        'menggunakan riwayat development yang sudah dipelajari; tidak ada holdout baru '
        'atau pemilihan multiplier teroptimasi. Checkpoint v27/v29 tidak diubah.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    files = ('FINDINGS.md', 'sizing_equity.png', 'stability.csv', 'VALIDATION.json')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in files}), indent=2))
    print('Sizing findings and equity chart written; all six requested cases retained.')


if __name__ == '__main__':
    main()
