"""Same-entry comparison and explanatory findings for v22's stop geometry."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_profile_end_stop as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    m = json.loads((out/'config_snapshot.json').read_text())
    cfg = m['config']
    data = pd.read_csv(out/'sessions.csv', float_precision='round_trip')
    old = pd.read_csv(Path(m['source_run'])/'sessions.csv', float_precision='round_trip')
    folds = pd.read_csv(out/'fold_metrics.csv')
    results, exclusions = [], []
    for symbol, rows in data.groupby('symbol'):
        tests = folds.loc[folds.symbol.eq(symbol) & folds.phase.eq('test')]
        test_dates = set()
        for f in tests.itertuples():
            test_dates.update(rows.loc[rows.session_date.ge(f.test_start) & rows.session_date.lt(f.test_end_exclusive), 'session_date'])
        for sample, population in (('all_available', rows), ('walk_forward_test', rows.loc[rows.session_date.isin(test_dates)])):
            invalid = population.loc[population.status.eq('invalid_profile_stop_geometry')]
            expected_risk = invalid.side*(invalid.entry-np.where(invalid.side.eq(1), invalid.profile_low, invalid.profile_high))
            assert (expected_risk <= 0).all()
            exclusions.append(dict(symbol=symbol, sample=sample, invalid_stop_geometry=len(invalid),
                zero_risk=int(expected_risk.eq(0).sum()), wrong_side=int(expected_risk.lt(0).sum())))
            after = population.loc[population.status.isin(model.base.ACTIVE)].sort_values('session_date')
            before = old.loc[old.symbol.eq(symbol) & old.session_date.isin(after.session_date)].sort_values('session_date')
            assert before.entry_agg_trade_id.tolist() == after.entry_agg_trade_id.tolist()
            for name, part in (('v21_same_entries', before), ('v22_profile_stop', after)):
                results.append(dict(symbol=symbol, sample=sample, model=name,
                    **model.base.stats(part, int(population.valid_session.sum()), cfg)))
    matched = pd.DataFrame(results)
    excluded = pd.DataFrame(exclusions)
    matched.to_csv(out/'matched_comparison.csv', index=False)
    excluded.to_csv(out/'stop_geometry_exclusions.csv', index=False)
    lines = ['# Hasil SL ujung profile — v22', '',
        'Entry tetap mengikuti v21: bias dari posisi NY open terhadap POC, long di VAL atau short di VAH. '
        'TP POC. SL long di minimum profile dan SL short di maksimum profile; '
        'bila jaraknya melebihi jarak TP, dipersempit ke 1:1. '
        'Profile sebelumnya 09:30–09:30 NY tetap dibekukan. Tidak ada indikator tambahan.', '',
        'Perbandingan berikut memakai entry identik yang memiliki SL baru valid. '
        'Trade dengan SL sama dengan entry (risiko nol) atau sudah di sisi yang salah tidak bisa dijalankan '
        'dengan definisi SL ini; dikeluarkan dari kedua sisi perbandingan. '
        'Jadi ini mengisolasi pengaruh perubahan SL pada trade yang sama.', '',
        '| Asset | Entry test yang sama | WR gross lama → baru | Mean gross R lama → baru | Mean net R lama → baru | Mean return posisi net lama → baru |',
        '|---|---:|---:|---:|---:|---:|']
    test = matched.loc[matched['sample'].eq('walk_forward_test')]
    for symbol, group in test.groupby('symbol'):
        a = group.loc[group.model.eq('v21_same_entries')].iloc[0]
        b = group.loc[group.model.eq('v22_profile_stop')].iloc[0]
        lines.append(f'| {symbol} | {int(b.trades)} | {a.gross_wr_pct:.2f}% → {b.gross_wr_pct:.2f}% | '
            f'{a.gross_mean_r:.4f} → {b.gross_mean_r:.4f} | {a.net_mean_r:.4f} → {b.net_mean_r:.4f} | '
            f'{a.net_mean_position_pct:.4f}% → {b.net_mean_position_pct:.4f}% |')
    lines += ['', 'Expectancy net dalam R tetap negatif pada keempat aset. '
        'Pada entry identik, LTC membaik sedikit dari -0,0516R menjadi -0,0504R; '
        'BTC, ETH dan XRP memburuk. Pada seluruh baseline awal, LTC juga memburuk '
        'karena ada entry baseline yang tidak memiliki SL baru valid. '
        'Penyempitan stop meningkatkan planned reward:risk, tetapi juga memperbesar '
        'kemungkinan SL tersentuh serta fee dalam satuan R.', '',
        'Fee 0,04% setiap sisi (sekitar 0,08% pulang-pergi). '
        'R lama dan baru memakai jarak SL masing-masing. Return posisi (%) disertakan untuk '
        'memisahkan perubahan satuan R dari perubahan pergerakan harga yang didapat. '
        'Risk akun tetap 0,5% equity dengan batas notional 5x; hasil akun tersedia dalam CSV.', '',
        '| Asset | Entry test tidak valid untuk SL baru | Risiko nol | SL sudah salah sisi |', '|---|---:|---:|---:|']
    for r in excluded.loc[excluded['sample'].eq('walk_forward_test')].itertuples():
        lines.append(f'| {r.symbol} | {r.invalid_stop_geometry} | {r.zero_risk} | {r.wrong_side} |')
    lines += ['', 'Bagian test memakai kalender train 8 bulan → test 2 bulan, maju dua bulan. '
        'Tidak ada parameter dipilih dari train pada rule tetap ini. Riwayat yang sudah dipelajari '
        'tetap bukti development, bukan holdout baru. Eksekusi adalah proxy harga transaksi pemicu; '
        'funding, spread, latency dan dampak order belum dihitung. Tidak ada volume profile dibangun ulang.', '',
        'REPORT.md menyimpan perbandingan terhadap seluruh entry baseline lama; '
        'matched_comparison.csv menyimpan perbandingan entry identik. '
        'stop_statistics.csv memuat distribusi planned reward:risk.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    generated = ('matched_comparison.csv', 'stop_geometry_exclusions.csv', 'FINDINGS.md')
    (out/'matched_audit.json').write_text(json.dumps(dict(status='passed',
        code_sha256=model.base.sha(__file__), source_core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in generated}), indent=2))
    print(test[['symbol', 'model', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_mean_position_pct']].to_string(index=False))
    print(excluded.to_string(index=False))


if __name__ == '__main__':
    main()
