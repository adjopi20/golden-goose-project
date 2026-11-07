"""Fixed thresholds: complete results, yearly comparison and interpretation."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_fixed_distance as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    test = summary.loc[summary['sample'].eq('walk_forward_test')].sort_values(['symbol', 'threshold_pct'])
    yearly = pd.read_csv(out/'yearly_metrics.csv')
    years = yearly.loc[yearly['sample'].eq('walk_forward_test')].sort_values(['symbol', 'threshold_pct', 'year'])
    stability = pd.read_csv(out/'stability.csv')
    confidence = pd.read_csv(out/'absolute_confidence.csv')
    lines = ['# Simple base: minimum jarak entry–POC tetap — v26', '',
        'LTC menunjukkan hasil paling menarik pada ambang 1% dalam sampel ini: '
        '249 trade, WR gross 55,02%, mean net +0,0557R dan total net +13,88R. '
        '2025 dan bagian test 2026 sama-sama positif. ETH jauh lebih lemah; '
        'hanya ambang 2% menghasilkan net agregat positif kecil dan hasil tahunan tidak konsisten.', '',
        '## Aturan uji', '',
        'Simple base v21 tetap: profile 24 jam previous 09:30 NY → current 09:30 NY dibekukan; '
        'NY open di bawah POC mengunci long di VAL, di atas POC mengunci short di VAH. '
        'Open boleh di dalam atau luar VA. Entry langsung pada sentuhan batas terpilih, '
        'TP POC, SL 1:1, maksimal satu trade per periode, time exit sebelum NY open berikutnya. '
        'Tidak ada filter outside VA, aturan A, second push, delta, CVD, ATR atau perubahan sizing.', '',
        'Jarak = 100 × |entry − POC| / entry. Trade hanya dipertahankan jika jarak >= '
        '0,3%; 0,5%; 0,7%; 1%; atau 2%, masing-masing TERPISAH. '
        'Ambang ditentukan user sebelum perhitungan, tidak dikalibrasi atau dipilih dari test. '
        'Kita melaporkan semua lima ambang dan baseline tanpa filter. Menyebut 1% terbaik di '
        'sampel ini tidak menjadikannya optimum yang sudah tervalidasi.', '',
        'Kalender 8 bulan train → 2 bulan test, maju 2 bulan sama dengan v21. '
        'Train/readout tetap dipurge bila outcome belum diketahui saat midnight NY awal test. '
        'Tidak ada pemilihan parameter dari train pada sweep ambang tetap ini. '
        'Tabel utama memakai seluruh blok test lengkap; all_available dan setiap fold tetap '
        'tersedia di CSV. ETH test 2024-10-01 sampai 2026-07-31; LTC blok test '
        '2025-01-01 sampai 2026-08-31, hanya sesi valid yang masuk. Tahun 2026 bukan tahun penuh.', '',
        '## Hasil bagian test', '',
        '| Aset | Minimum jarak | Entry | Dipertahankan dari baseline | WR gross | WR net | Mean gross R | Mean net R | Total net R |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in test.itertuples():
        label = 'Tanpa filter' if r.threshold_pct == 0 else f'{r.threshold_pct:g}%'
        lines.append(f'| {r.symbol} | {label} | {int(r.trades)} | {r.retained_pct_base:.1f}% | '
            f'{r.gross_wr_pct:.2f}% | {r.net_wr_pct:.2f}% | {r.gross_mean_r:+.4f} | '
            f'{r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'Fee 0,04% setiap sisi. WR termasuk posisi profit saat time exit, bukan hanya TP POC. '
        'TP dan SL tetap gross 1:1; gross realized R menggunakan harga print pemicu, '
        'sehingga overshoot dan time exit dapat memberi hasil berbeda dari tepat +1/-1R.', '',
        '## Per tahun pada bagian test', '',
        '| Aset | Minimum jarak | Tahun | Entry | Mean net R | Total net R |',
        '|---|---:|---:|---:|---:|---:|']
    for r in years.itertuples():
        label = 'Tanpa filter' if r.threshold_pct == 0 else f'{r.threshold_pct:g}%'
        lines.append(f'| {r.symbol} | {label} | {r.year} | {int(r.trades)} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', '## Kestabilan', '',
        '| Aset | Minimum jarak | Fold net positif / seluruh fold test | Train dan test positif / comparable | Bootstrap 95% mean net R |',
        '|---|---:|---:|---:|---:|']
    for r in stability.sort_values(['symbol', 'threshold_pct']).itertuples():
        ci = confidence.loc[confidence.symbol.eq(r.symbol) & confidence.policy.eq(r.policy)].iloc[0]
        label = 'Tanpa filter' if r.threshold_pct == 0 else f'{r.threshold_pct:g}%'
        lines.append(f'| {r.symbol} | {label} | {r.positive_net_test_folds}/{r.test_folds} | '
            f'{r.train_test_both_positive}/{r.comparable_folds} | [{ci.net_mean_ci025:+.4f}, {ci.net_mean_ci975:+.4f}] |')
    lines += ['', 'LTC 1% mempertahankan 56,3% trade dan membuang 43,7%. '
        '2025: 176 trade, +11,26R net; 2026: 73 trade, +2,62R net. '
        'Fold test positif 6/10, train dan test sama-sama positif 5/10. '
        'Biaya rata-rata turun dari sekitar 0,1015R ke 0,0462R per trade, '
        'sementara mean gross R naik dari +0,0641R ke +0,1019R. '
        'Jadi perbaikan mencakup biaya relatif dan hasil gross, bukan hanya mengurangi frekuensi.', '',
        'Ambang 2% tidak otomatis lebih baik: trade berkurang dan expectancy LTC turun '
        'dibanding 1%. Pada ETH, ambang 2% hanya memberi +1,16R total, dengan '
        '2024 dan 2026 negatif. Jarak target lebih jauh bukan jaminan peluang kembali ke POC lebih besar.', '',
        'Belum ada bukti sebab-akibat bahwa volatilitas adalah regime penentu. '
        'Uji ini tidak mengukur indikator volatilitas; ia mengukur kelayakan jarak target. '
        'Gambar yang diberikan bersifat konteks visual, bukan sumber sinyal/filter. '
        'LTC 1% tetap memiliki interval expectancy net sekitar [-0,041R, +0,155R] yang '
        'mencakup nol. Bootstrap 4.000 pengulangan memakai blok kalender tujuh hari, '
        'tidak dikoreksi untuk lima ambang maupun seluruh eksperimen historis.', '',
        'Semua entry dan outcome v21 digunakan ulang tanpa rebuild profile atau pembacaan '
        'archive raw. Riwayat sudah pernah dipelajari dan tetap development evidence. '
        'Funding, spread, latency dan dampak order belum dihitung. Net account dalam CSV '
        'memakai ilustrasi risiko 0,5% equity dan batas notional 5x, bukan return tahunan.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        source_core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in ('FINDINGS.md', 'VALIDATION.json')}), indent=2))
    print('Findings written; all user thresholds reported without selecting a test winner as a validated rule.')


if __name__ == '__main__':
    main()
