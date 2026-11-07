"""Source-backed v25 findings, selected-policy yearly readout and limitations."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_simple_base_filters as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    grouped = pd.read_csv(out/'group_summary.csv')
    confidence = pd.read_csv(out/'absolute_confidence.csv')
    controls = pd.read_csv(out/'controls.csv')
    stability = pd.read_csv(out/'stability.csv')
    cuts = pd.read_csv(out/'train_selections.csv')
    predictions = pd.read_parquet(out/'test_predictions.parquet')
    yearly = []
    cfg = json.loads((out/'config_snapshot.json').read_text())['config']
    for (symbol, year), group in predictions.groupby(['symbol', predictions.session_date.str[:4]]):
        for policy in model.POLICIES:
            part = group if policy == 'base_all' else group.loc[group[policy.split('_')[0]+'_selected']]
            yearly.append(dict(symbol=symbol, year=year, policy=policy, **model.base.stats(part.sort_values('session_date'), len(group), cfg)))
    pd.DataFrame(yearly).to_csv(out/'yearly_policy_metrics.csv', index=False)
    choice_counts = []
    for symbol, group in cuts.groupby('symbol'):
        for family in ('distance', 'location'):
            for choice, count in group[family+'_selected'].fillna('skip_no_supported_positive_group').value_counts().items():
                choice_counts.append(dict(symbol=symbol, family=family, choice=choice, folds=int(count)))
    pd.DataFrame(choice_counts).to_csv(out/'selection_counts.csv', index=False)
    labels = {'base_all':'Simple base v21', 'distance_train_selected':'Filter jarak, dipilih train',
        'location_train_selected':'Filter lokasi open, dipilih train'}
    lines = ['# Simple base ETH/LTC: jarak dan lokasi open — v25', '',
        'Kandidat utama adalah LTC ketika NY open berada DI LUAR value area sebelumnya dan '
        'harga kemudian kembali menyentuh VAL/VAH menuju POC. Pemilihan kelompok lewat train '
        'menghasilkan expectancy net positif pada test, tetapi hasil 2026 melemah dan '
        'interval ketidakpastian expectancy absolut masih mencakup nol. ETH belum menghasilkan '
        'filter train-selected yang net positif.', '',
        '## Model dan desain', '',
        'Simple base v21 dipertahankan: frozen profile previous 09:30 NY → current 09:30 NY; '
        'open di bawah POC mengunci long di VAL, open di atas POC mengunci short di VAH. '
        'Open boleh berada di dalam atau luar VA. Entry langsung pada sentuhan batas terpilih; '
        'TP POC, SL 1:1, satu kandidat per periode; time exit sebelum NY open berikutnya. '
        'Tidak memakai aturan A first-edge, second push, SL ujung profile, delta, CVD, atau ATR.', '',
        'Dua filter diuji TERPISAH, tidak digabung. Jarak entry–POC dalam persen dibagi tercile '
        'near/middle/far dari trade train yang outcome-nya sudah diketahui sebelum test. '
        'Beban fee estimasi saat entry = 2 × 0,0004 × entry / jarak entry–POC, tanpa harga exit. '
        'Lokasi open dibagi inside_va (inklusif batas) dan outside_va; long/short dilaporkan '
        'terpisah secara deskriptif, tidak dipilih sebagai filter tambahan.', '',
        'Untuk setiap keluarga, train memilih satu kelompok dengan mean net R tertinggi '
        'yang positif dan minimal 25 trade. Jika tidak ada yang memenuhi, fold test dilewati. '
        'Minimum 25 adalah batas bukti awal yang ditetapkan sebelum evaluasi, bukan angka hasil optimasi. '
        'Kalender train 8 bulan → test 2 bulan, maju dua bulan; outcome train yang melewati '
        'midnight NY awal test dipurge. Batas dan pilihan dikunci sebelum melihat test.', '',
        '## Hasil test dari pemilihan train', '',
        '| Aset | Model | Entry | WR gross | WR net | Mean gross R | Mean net R | Total net R | Net account |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary.itertuples():
        lines.append(f'| {r.symbol} | {labels[r.policy]} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | {r.net_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} | {r.net_account_return_pct:+.2f}% |')
    lines += ['', 'Fee 0,04% tiap sisi. WR termasuk posisi profit saat time exit, bukan hanya TP. '
        'Net account memakai ilustrasi risiko 0,5% equity dengan batas notional 5x, kumulatif '
        'selama sampel test masing-masing dan bukan return tahunan. Train overlap tidak dipool.', '',
        '## Kelompok test: observasi, bukan hasil pemilihan kategori setelah melihat test', '',
        '| Aset | Keluarga | Kelompok | Entry | WR gross | Mean gross R | Mean net R |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for r in grouped.itertuples():
        lines.append(f'| {r.symbol} | {r.family} | {r.category} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} |')
    lines += ['', 'Pada LTC, seluruh trade test outside_va berjumlah 152 dengan WR gross 61,18% '
        'dan mean net +0,1275R. Ini berbeda dari 138 trade train-selected: fold pertama '
        'dilewati karena tidak ada kelompok train net positif, kemudian outside_va dipilih pada '
        '9 fold berikutnya. Kelompok long outside dan short outside sama-sama positif secara '
        'agregat, tetapi itu bukan dua konfirmasi independen.', '',
        'Pada ETH, kelompok middle terlihat lebih baik secara deskriptif (+0,0732R net), '
        'namun train tidak memilih middle secara konsisten: middle hanya 2/11 fold. '
        'Mengunci middle setelah melihat hasil ini akan menjadi eksperimen development baru. '
        'Jarak lebih jauh menurunkan biaya dalam R, tetapi tidak otomatis meningkatkan '
        'peluang/hasil mencapai POC; far ETH justru net negatif.', '',
        '## Kestabilan dan ketidakpastian', '',
        '| Aset | Model | Fold test positif / total | Train dan test positif / comparable | Bootstrap 95% mean net R |',
        '|---|---|---:|---:|---:|']
    for r in stability.itertuples():
        ci = confidence.loc[confidence.symbol.eq(r.symbol) & confidence.policy.eq(r.policy)].iloc[0]
        lines.append(f'| {r.symbol} | {labels[r.policy]} | {r.positive_net_test_folds}/{r.test_folds} | '
            f'{r.train_test_both_positive}/{r.comparable_folds} | [{ci.net_mean_ci025:+.4f}, {ci.net_mean_ci975:+.4f}] |')
    lines += ['', 'LTC filter lokasi memiliki 6 fold positif dari 9 fold bertransaksi (10 fold total). '
        'Train memilih outside pada 9/10 fold, tetapi train windows tumpang tindih sehingga '
        'bukan 9 bukti independen. Tahun 2025: 79 trade, +15,94R net. Tahun 2026 sampai '
        'akhir test: 59 trade, -1,64R net. Edge agregat terutama datang dari 2025.', '',
        'Kontrol acak memakai frekuensi sama pada setiap fold dengan 2.000 pengulangan. '
        'LTC filter lokasi memiliki unadjusted upper-tail fraction sekitar 0,024; '
        '95% interval lift terhadap baseline sekitar [+0,018R, +0,260R]. '
        'Ini mendukung perbaikan relatif terhadap baseline yang merugi; tidak membuktikan '
        'expectancy absolut sudah positif, karena interval absolut sekitar [-0,048R, +0,244R]. '
        'Bootstrap memakai blok kalender tujuh hari: 2.000 pengulangan untuk lift, '
        '4.000 untuk expectancy absolut. Tidak dikoreksi terhadap banyak eksperimen historis.', '',
        'Semua trade dan outcome v21 dipakai ulang; nol profile dibangun dan nol data raw dibaca ulang. '
        'Riwayat ini sudah pernah dipelajari dan tetap development evidence, bukan holdout baru. '
        'Funding, spread, latency dan dampak order belum dihitung. Hasil 2026 yang melemah '
        'perlu dipertahankan dalam penilaian, bukan ditutupi rata-rata gabungan.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    files = ('FINDINGS.md', 'yearly_policy_metrics.csv', 'selection_counts.csv', 'VALIDATION.json', 'absolute_confidence.csv')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        source_core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in files}), indent=2))
    print(summary[['symbol', 'policy', 'trades', 'gross_wr_pct', 'net_mean_r', 'net_total_r', 'net_account_return_pct']].to_string(index=False))
    print(pd.DataFrame(yearly).loc[lambda d:d.symbol.eq('LTCUSDC') & d.policy.eq('location_train_selected'), ['year', 'trades', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    main()
