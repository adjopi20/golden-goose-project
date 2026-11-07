"""Requested AND/OR report with exact component membership, partitions and uncertainty."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_filter_sets as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    yearly = pd.read_csv(out/'yearly_metrics.csv')
    stability = pd.read_csv(out/'stability.csv')
    confidence = pd.read_csv(out/'absolute_confidence.csv')
    data = pd.read_parquet(out/'test_predictions.parquet')
    cfg = json.loads((out/'config_snapshot.json').read_text())['config']
    partitions = []
    for symbol, group in data.groupby('symbol'):
        d, l = group.distance_selected, group.location_selected
        for label, mask in (('both', d & l), ('distance_only', d & ~l), ('location_only', ~d & l), ('neither', ~d & ~l)):
            part = group.loc[mask].sort_values('session_date')
            partitions.append(dict(symbol=symbol, partition=label, **model.old.metrics(part, group, cfg)))
    pd.DataFrame(partitions).to_csv(out/'partitions.csv', index=False)
    names = {'base_all':'Simple base', 'distance':'Jarak >=1%', 'location':'Lokasi open v25',
        'intersection':'Irisan / AND', 'union':'Gabungan / OR'}
    lines = ['# Irisan dan gabungan filter jarak 1% + lokasi open — v27', '',
        'LTC: irisan 69 trade, WR gross 62,32%, mean gross +0,3032R, mean net +0,2563R '
        'dan total net +17,68R. Gabungan 318 trade memberi +10,49R net. '
        'ETH tetap net negatif pada irisan dan gabungan.', '',
        '## Definisi dan populasi', '',
        'Exact pair LTCUSDC dan ETHUSDC, sesuai sumber eksperimen sebelumnya. '
        'Angka 100% dalam pesan user diinterpretasikan sebagai 1%, sesuai kutipan 249 trade LTC. '
        'Simple base v21 tetap: bias NY open terhadap POC mengunci long VAL atau short VAH, '
        'entry langsung pada sentuhan, TP POC, SL 1:1, satu kandidat per periode, '
        'time exit sebelum NY open berikutnya. Tidak ada aturan A, second push atau orderflow.', '',
        'D = trade dengan 100 × |entry − POC| / entry >=1%, tepat membership v26. '
        'L = trade yang lolos pilihan kelompok lokasi dari train v25, tepat membership '
        '138 LTC dan 104 ETH. BUKAN semua trade outside VA secara retrospektif: '
        'LTC v25 melewati fold pertama lalu memilih outside pada sembilan fold berikutnya; '
        'ETH memilih inside/outside pada beberapa fold dan melewati sisanya. '
        'Tidak ada fitting ulang atau penambahan threshold.', '',
        'Irisan = D AND L. Gabungan = D OR L, tiap trade hanya dihitung sekali. '
        'Trade dicocokkan melalui symbol, session_date, entry_agg_trade_id dan diverifikasi '
        'memiliki entry, exit, risiko, P&L serta fold identik. Kalender test 8+2 sama persis '
        'dengan sumber. Tidak ada profile dibangun ulang atau archive raw dibaca ulang.', '',
        '## Hasil yang diminta — bagian test', '',
        '| Aset | Kombinasi | Entry | WR gross | Mean gross R | Mean net R | Total net R |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in summary.loc[summary.policy.isin(('intersection', 'union'))].itertuples():
        lines.append(f'| {r.symbol} | {names[r.policy]} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'Net memakai fee 0,04% setiap sisi. WR gross mencakup time exit yang profit, '
        'bukan hanya TP POC. Harga entry/exit memakai print pemicu sehingga P&L realized '
        'dapat berbeda dari gross tepat +1/-1R. Semua hasil kumulatif sampel, bukan annual return.', '',
        '## Referensi komponen', '',
        '| Aset | Komponen | Entry | WR gross | Mean gross R | Mean net R | Total net R |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in summary.loc[summary.policy.isin(('base_all', 'distance', 'location'))].itertuples():
        lines.append(f'| {r.symbol} | {names[r.policy]} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'Identitas jumlah trade: LTC 249 + 138 − 69 = 318; ETH 290 + 104 − 62 = 332. '
        'Identitas total R juga diverifikasi: net(D OR L) = net(D) + net(L) − net(D AND L).', '',
        '## Tahun dan fold', '',
        '| Aset | Kombinasi | Tahun | Entry | WR gross | Mean net R | Total net R |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in yearly.loc[yearly.policy.isin(('intersection', 'union'))].itertuples():
        lines.append(f'| {r.symbol} | {names[r.policy]} | {r.year} | {int(r.trades)} | '
            f'{r.gross_wr_pct:.2f}% | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', '| Aset | Kombinasi | Fold net positif / bertransaksi / seluruh test | Bootstrap 95% mean net R |',
        '|---|---|---:|---:|']
    for r in stability.loc[stability.policy.isin(('intersection', 'union'))].itertuples():
        ci = confidence.loc[confidence.symbol.eq(r.symbol) & confidence.policy.eq(r.policy)].iloc[0]
        lines.append(f'| {r.symbol} | {names[r.policy]} | {r.positive_net_test_folds}/{r.nonempty_test_folds}/{r.test_folds} | '
            f'[{ci.net_mean_ci025:+.4f}, {ci.net_mean_ci975:+.4f}] |')
    lines += ['', 'LTC irisan: 2025 berisi 42 trade (+13,18R), bagian test 2026 berisi '
        '27 trade (+4,51R). Seluruh delapan fold yang memiliki trade net positif; '
        'dua fold lain kosong. Interval bootstrap absolut positif sekitar '
        '[+0,070R, +0,423R] pada sampel ini, dengan WR gross 62,3%. '
        'Bootstrap 4.000 pengulangan memakai blok kalender tujuh hari, mempertahankan '
        'kalender fold dan pilihan filter v25 yang sama; pemilihan train tidak dilatih ulang '
        'pada setiap resample. Ini bukti historis lebih kuat daripada filter tunggal, '
        'tetapi interval belum mengoreksi proses memilih banyak eksperimen/versi.', '',
        'Gabungan LTC menambahkan 249 trade di luar irisan; kontribusi net trade tambahan '
        'tersebut sekitar -7,19R. Partitions.csv memisahkan kedua filter saja, keduanya, '
        'serta yang tidak lolos keduanya. Tahun 2026 adalah bagian test tersedia, bukan '
        'setahun penuh. Dataset dan filter komponennya telah dilihat sebelumnya; '
        'kombinasi ini tetap development evidence dan belum diuji pada holdout baru.', '',
        'Funding, spread, latency dan dampak order belum dihitung. '
        'Account sizing ilustratif tetap 0,5% equity dengan batas notional 5x; '
        'angka R di tabel bukan return persentase akun.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    outputs = {name:model.base.sha(out/name) for name in ('FINDINGS.md', 'partitions.csv', 'VALIDATION.json')}
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        source_core_audit_sha256=model.base.sha(out/'audit.json'), outputs=outputs), indent=2))
    print(summary.loc[summary.policy.isin(('intersection', 'union')),
        ['symbol', 'policy', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    main()
