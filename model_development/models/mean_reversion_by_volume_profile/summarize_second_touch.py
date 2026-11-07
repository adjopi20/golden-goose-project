"""Compact A-only comparison, trade counts and fold stability for v24."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_second_touch as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    folds = pd.read_csv(out/'fold_metrics.csv')
    compare = pd.read_csv(out/'comparison.csv')
    data = pd.read_parquet(out/'sessions.parquet')
    stability, counts = [], []
    for (symbol, mode), group in folds.groupby(['symbol', 'pullback_mode']):
        train = group.loc[group.phase.eq('train')].set_index('fold')
        test = group.loc[group.phase.eq('test')].set_index('fold')
        paired = test[['trades', 'net_mean_r']].join(train[['trades', 'net_mean_r']], rsuffix='_train')
        available = paired.trades.gt(0) & paired.trades_train.gt(0)
        stability.append(dict(symbol=symbol, pullback_mode=mode, test_folds=len(test),
            nonempty_test_folds=int(test.trades.gt(0).sum()), positive_net_test_folds=int(test.net_mean_r.gt(0).sum()),
            comparable_folds=int(available.sum()),
            train_test_both_positive=int((available & paired.net_mean_r.gt(0) & paired.net_mean_r_train.gt(0)).sum())))
        rows = data.loc[data.symbol.eq(symbol) & data.pullback_mode.eq(mode)]
        dates = set()
        for f in test.itertuples():
            dates.update(rows.loc[rows.session_date.ge(f.test_start) & rows.session_date.lt(f.test_end_exclusive), 'session_date'])
        for sample, population in (('all_available', rows), ('walk_forward_test', rows.loc[rows.session_date.isin(dates)])):
            trades = population.loc[population.status.isin(model.base.ACTIVE)]
            counts.append(dict(symbol=symbol, pullback_mode=mode, sample=sample, entries=len(trades),
                tp=int(trades.status.eq('tp_first').sum()), sl=int(trades.status.eq('sl_first').sum()),
                time_exit=int(trades.status.eq('time_exit').sum()), gross_wins=int(trades.gross_r.gt(0).sum()),
                net_wins=int(trades.net_r.gt(0).sum())))
    stability = pd.DataFrame(stability)
    stability.to_csv(out/'stability.csv', index=False)
    pd.DataFrame(counts).to_csv(out/'entry_win_counts.csv', index=False)
    test = summary.loc[summary['sample'].eq('walk_forward_test')]
    lines = ['# Baseline A + second push tanpa orderflow — v24', '',
        'NY open harus di dalam VA profile 24 jam sebelumnya. Batas pertama menentukan arah: '
        'VAH short, VAL long. Tidak entry pada sentuhan pertama; tunggu pullback minimal '
        'setengah jarak ke POC, lalu entry saat menyentuh batas yang sama lagi.', '',
        '- Wick 50%: cukup harga transaksi pernah mencapai titik tengah; retest setelahnya langsung entry.',
        '- Close 15m 50%: penutupan interval 15 menit harus mencapai titik tengah; baru menunggu retest setelah interval selesai.', '',
        'Tidak ada delta, CVD, body-candle entry atau filter tambahan. TP POC, SL 1:1; '
        'maksimal satu trade per periode per versi. Gerakan sampai/melewati POC sebelum entry '
        'tidak membatalkan setup, seperti observasi sebelumnya. '
        'Penutupan bar yang berisi sentuhan pertama boleh menjadi konfirmasi pullback setelah bar itu selesai. '
        'Ini adalah sentuhan berikutnya SETELAH pullback 50%, bukan sekadar menghitung dua print di batas.', '',
        'Tabel memakai bagian test kalender 8 bulan train → 2 bulan test, langkah dua bulan. '
        'WR menghitung posisi profit termasuk penutupan akhir periode. '
        'Gross sebelum fee; net dengan 0,04% per sisi.', '',
        '| Aset | Versi | Entry | TP | SL | Tutup akhir periode | Posisi profit gross | WR gross | WR net | Mean gross R | Mean net R |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in test.itertuples():
        label = 'Wick 50%' if r.pullback_mode == 'wick_half' else 'Close 15m 50%'
        lines.append(f'| {r.symbol} | {label} | {int(r.trades)} | {int(r.tp_first)} | {int(r.sl_first)} | '
            f'{int(r.time_exit)} | {int(r.gross_wins)} | {r.gross_wr_pct:.2f}% | {r.net_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} |')
    lines += ['', '| Aset | Versi | Entry first → second | WR gross first → second | Mean net R first → second | Fold test net positif |',
        '|---|---|---:|---:|---:|---:|']
    for r in compare.loc[compare['sample'].eq('walk_forward_test')].itertuples():
        s = stability.loc[stability.symbol.eq(r.symbol) & stability.pullback_mode.eq(r.pullback_mode)].iloc[0]
        label = 'Wick 50%' if r.pullback_mode == 'wick_half' else 'Close 15m 50%'
        lines.append(f'| {r.symbol} | {label} | {int(r.trades_first)} → {int(r.trades_second)} | '
            f'{r.gross_wr_pct_first:.2f}% → {r.gross_wr_pct_second:.2f}% | '
            f'{r.net_mean_r_first:+.4f} → {r.net_mean_r_second:+.4f} | '
            f'{int(s.positive_net_test_folds)}/{int(s.test_folds)} |')
    lines += ['', 'Second push mengubah waktu entry dan outcome, jadi hasilnya bukan sekadar subset '
        'P&L dari trade first touch. Kedua versi juga dapat memakai trade yang sama sehingga '
        'tidak boleh dianggap dua bukti independen.', '',
        'Semua aset menggunakan archive exact pair masing-masing. Profile lama dipakai ulang '
        'tanpa rebuild; tidak ada ambang yang dioptimalkan. Riwayat sudah pernah diamati dan '
        'tetap development evidence. Eksekusi memakai harga transaksi pemicu, bukan jaminan '
        'limit fill tepat di batas. Funding, spread, latency dan dampak order belum dihitung.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    names = ('stability.csv', 'entry_win_counts.csv', 'FINDINGS.md', 'VALIDATION.json', 'raw_spotchecks.csv')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed',
        code_sha256=model.base.sha(__file__), core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in names}), indent=2))
    print(test[['symbol', 'pullback_mode', 'trades', 'gross_wr_pct', 'net_wr_pct', 'gross_mean_r', 'net_mean_r']].to_string(index=False))
    print(stability.to_string(index=False))


if __name__ == '__main__':
    main()
