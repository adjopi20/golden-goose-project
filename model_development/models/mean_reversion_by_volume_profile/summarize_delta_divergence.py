"""Readable v23 comparisons, feature coverage, stability and overlap."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_delta_divergence as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    test = summary.loc[summary['sample'].eq('walk_forward_test')]
    controls = pd.read_csv(out/'controls.csv')
    stability = pd.read_csv(out/'stability.csv')
    rows = []
    for (symbol, direction), group in test.groupby(['symbol', 'direction']):
        base = group.loc[group.policy.eq('base_all')].iloc[0]
        for prefix in ('cvd', 'delta15'):
            a = group.loc[group.policy.eq(prefix+'_matched')].iloc[0]
            b = group.loc[group.policy.eq(prefix+'_divergence')].iloc[0]
            c = controls.loc[controls.symbol.eq(symbol) & controls.direction.eq(direction) & controls.signal.eq(prefix)].iloc[0]
            s = stability.loc[stability.symbol.eq(symbol) & stability.direction.eq(direction) & stability.policy.eq(prefix+'_divergence')].iloc[0]
            rows.append(dict(symbol=symbol, direction=direction, signal=prefix,
                all_base_entries=int(base.trades), matched_entries=int(a.trades), selected_entries=int(b.trades),
                retained_pct_all_base=100*b.trades/base.trades,
                retained_pct_matched=100*b.trades/a.trades,
                matched_gross_wr=a.gross_wr_pct, selected_gross_wr=b.gross_wr_pct,
                selected_gross_wins=int(b.gross_wins), selected_net_wins=int(b.net_wins),
                matched_mean_gross_r=a.gross_mean_r, selected_mean_gross_r=b.gross_mean_r,
                matched_mean_net_r=a.net_mean_r, selected_mean_net_r=b.net_mean_r,
                selected_total_net_r=b.net_total_r, selected_net_account_return_pct=b.net_account_return_pct,
                positive_net_test_folds=int(s.positive_net_test_folds), total_folds=int(s.total_folds),
                nonempty_test_folds=int(s.nonempty_test_folds), comparable_folds=int(s.comparable_folds),
                train_test_both_positive=int(s.train_test_both_positive),
                random_upper_tail=c.net_same_frequency_upper_tail_fraction,
                net_lift_ci025=c.net_lift_block_ci025, net_lift_ci975=c.net_lift_block_ci975,
                limited_evidence=bool(b.trades < 20 or s.nonempty_test_folds < 5)))
    comparisons = pd.DataFrame(rows)
    comparisons.to_csv(out/'signal_comparison.csv', index=False)
    data = pd.read_parquet(out/'sessions.parquet')
    folds = pd.read_csv(out/'fold_metrics.csv')
    overlap = []
    for symbol, group in data.groupby('symbol'):
        fs = folds.loc[folds.symbol.eq(symbol) & folds.direction.eq('first_edge') & folds.policy.eq('base_all') & folds.phase.eq('test')]
        dates = set()
        for f in fs.itertuples():
            dates.update(group.loc[group.session_date.ge(f.test_start) & group.session_date.lt(f.test_end_exclusive), 'session_date'])
        active = group.loc[group.status.isin(model.base.ACTIVE) & group.session_date.isin(dates)]
        for prefix in ('cvd', 'delta15'):
            selected = active.loc[active[prefix+'_observable'].eq(True) & active[prefix+'_signal'].eq(True)]
            a = set(selected.loc[selected.direction.eq('first_edge'), 'entry_agg_trade_id'].astype('int64'))
            b = set(selected.loc[selected.direction.eq('open_poc_bias'), 'entry_agg_trade_id'].astype('int64'))
            overlap.append(dict(symbol=symbol, signal=prefix, first_edge=len(a), open_poc_bias=len(b),
                same_exact_entries=len(a & b), unique_entries=len(a | b)))
    pd.DataFrame(overlap).to_csv(out/'entry_overlap.csv', index=False)
    lines = ['# Observasi divergence delta — v23', '',
        'Ada dua kandidat deskriptif: ETH dengan CVD berlawanan arah sejak NY open, '
        'dan XRP dengan delta 15 menit yang melemah. BTC dan LTC belum menunjukkan '
        'perbaikan yang konsisten. Semua kandidat positif masih memiliki interval '
        'ketidakpastian peningkatan net R yang mencakup nol.', '',
        'Profile 24 jam sebelumnya tetap. NY open harus di dalam VA. '
        'Aturan A: sentuhan pertama VAL → long atau VAH → short. '
        'Aturan B: bias posisi NY open terhadap POC mengunci batas yang ditunggu. '
        'Entry langsung pada sentuhan; TP POC, SL 1:1. Maksimal satu kandidat per periode '
        'per aturan, tidak mencari pengganti bila divergence gagal. '
        'Tidak memakai SL ujung profile v22.', '',
        'CVD dihitung dari volume base buy agresif dikurangi sell agresif sejak NY open, '
        'hanya sampai transaksi sebelum entry. Short di VAH mensyaratkan CVD negatif; '
        'long di VAL CVD positif. Ini berbeda dari pola dua swing harga dan CVD.', '',
        'Delta15 memakai tiga interval 15 menit terakhir yang SUDAH selesai, berpatokan NY open. '
        'Di VAH: delta0 > delta1 > delta2 dan close interval terakhir > close interval pertama. '
        'Di VAL dibalik. Delta boleh tetap positif atau melewati nol; tidak ada syarat '
        'ketiga close harus monoton. Interval berjalan tidak digunakan. '
        'Sentuhan sebelum 45 menit belum memiliki fitur ini; baseline pembanding dibatasi '
        'ke sentuhan dengan fitur tersedia agar pengaruh waktu tidak tercampur.', '',
        'Semua tabel berikut bagian test pada kalender 8 bulan train → 2 bulan test, maju 2 bulan. '
        'WR adalah persentase posisi profit SEBELUM fee, termasuk penutupan akhir periode; '
        'bukan hanya TP POC. Net memasukkan 0,04% fee per sisi.', '']
    for prefix, title in (('cvd', 'CVD sejak NY open berlawanan arah harga'), ('delta15', 'Delta per 15 menit melemah')):
        lines += [f'## {title}', '',
            '| Aset | Aturan | Entry baseline matched → tersisa | WR gross matched → tersisa | Mean net R matched → tersisa | Fold test net positif |',
            '|---|---|---:|---:|---:|---:|']
        for r in comparisons.loc[comparisons.signal.eq(prefix)].itertuples():
            label = 'A: batas pertama' if r.direction == 'first_edge' else 'B: bias open/POC'
            lines.append(f'| {r.symbol} | {label} | {r.matched_entries} → {r.selected_entries} | '
                f'{r.matched_gross_wr:.1f}% → {r.selected_gross_wr:.1f}% | '
                f'{r.matched_mean_net_r:+.4f} → {r.selected_mean_net_r:+.4f} | '
                f'{r.positive_net_test_folds}/{r.total_folds} |')
        lines += ['']
    lines += ['## Batas bukti', '',
        'ETH–CVD: A memiliki 83 trade dan +0,0251R net/trade, B 81 trade dan +0,0348R. '
        'Fold test positif masing-masing 7/11 dan 6/11, tetapi train dan test sama-sama '
        'positif hanya 2/11 dan 1/11. XRP–delta15: A 36 trade +0,0857R, '
        'B 28 trade +0,1596R; fold test positif 9/22 dan 7/22. '
        'Jadi rata-rata positif belum menjadi pola yang stabil antarperiode.', '',
        'Kontrol acak memakai jumlah trade sama per fold, 2.000 pengulangan. '
        'Interval lift memakai bootstrap blok kalender tujuh hari, 2.000 pengulangan. '
        'Tidak dikoreksi untuk banyak perbandingan. Kandidat positif tersebut belum '
        'memberi interval lift yang seluruhnya di atas nol. Kedua aturan dapat memilih '
        'entry identik; entry_overlap.csv mengukur overlap, jadi keduanya bukan konfirmasi independen.', '',
        'Hasil baseline sudah berubah karena open di dalam VA disyaratkan kembali. '
        'Jangan membandingkannya langsung dengan semua entry v21 yang mengizinkan open di luar VA. '
        'Tidak ada parameter yang dipilih lewat optimasi; riwayat ini tetap development '
        'yang sudah berulang diamati, bukan holdout baru.', '',
        'Eksekusi menggunakan harga transaksi yang menyentuh/melintasi batas, bukan jaminan '
        'fill limit tepat pada level. Spread, funding, latency dan dampak order belum dihitung. '
        'Data aggTrades memberi klasifikasi sisi agresor, bukan jumlah buyer unik atau seluruh order book. '
        'Profile lama dipakai ulang tanpa rebuild. REPORT.md, summary.csv dan fold_metrics.csv '
        'menyimpan hasil lengkap termasuk kelompok tanpa divergence.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    names = ('signal_comparison.csv', 'entry_overlap.csv', 'FINDINGS.md')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed',
        code_sha256=model.base.sha(__file__), source_core_audit_sha256=model.base.sha(out/'audit.json'),
        source_diagnostics_sha256=model.base.sha(out/'diagnostics_audit.json'),
        outputs={name:model.base.sha(out/name) for name in names}), indent=2))
    print(comparisons.to_string(index=False))
    print(pd.DataFrame(overlap).to_string(index=False))


if __name__ == '__main__':
    main()
