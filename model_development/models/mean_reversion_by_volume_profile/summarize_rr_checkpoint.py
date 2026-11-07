"""Full RR sweep report with frozen checkpoint and R/percentage sizing interpretation."""
import argparse
import json
from pathlib import Path
import pandas as pd
import observe_rr_checkpoint as model


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    assert json.loads((out/'VALIDATION.json').read_text())['status'] == 'passed'
    summary = pd.read_csv(out/'summary.csv')
    years = pd.read_csv(out/'yearly_metrics.csv')
    stability = pd.read_csv(out/'stability.csv')
    confidence = pd.read_csv(out/'absolute_confidence.csv')
    labels = {'A_first_touch':'A first touch', 'distance':'Jarak >=1%', 'location':'Lokasi open',
        'intersection':'Irisan', 'union':'Gabungan'}
    order = {m:i for i,m in enumerate(model.MODELS)}
    summary = summary.assign(_order=summary.model.map(order)).sort_values(['_order', 'rr'])
    lines = ['# LTC: reward:risk sweep dengan entry checkpoint tetap — v28', '',
        'Checkpoint v27 tidak diubah. Irisan checkpoint RR 1:1 tetap 69 trade, '
        'WR gross 62,32%, mean net +0,2563R dan total net +17,68R. '
        'Dalam sweep ini, irisan RR 1:2 memberi net R agregat tertinggi, '
        'tetapi WR lebih rendah, drawdown lebih besar, serta return harga per notional '
        'lebih kecil. RR 1:1 tetap paling halus menurut fold positif dan drawdown sampel ini.', '',
        '## Aturan', '',
        'Hanya LTCUSDC. rr = reward distance / risk distance. rr=1,2 berarti SL:TP '
        '1:1,2. TP selalu POC yang sudah dibekukan; reward distance = |POC − entry|. '
        'Risk distance = reward distance / rr; SL long = entry − risk distance; '
        'SL short = entry + risk distance. RR diuji 1; 1,2; 1,5; 1,8; 2. '
        'Tidak ada entry baru atau pemindahan TP. Setiap outcome dipindai ulang dari raw '
        'trade prints; hasil RR lama tidak sekadar dikalikan.', '',
        'A first touch adalah pembanding terpisah dari v23: NY open strictly inside VA, '
        'batas pertama disentuh VAH short/VAL long (349 entry test). Empat model filter '
        'memakai entry simple base dan membership checkpoint v27: jarak 249, lokasi 138, '
        'irisan 69, gabungan 318. Filter lokasi tetap hasil pemilihan train v25 pada RR '
        '1:1; tidak dilatih ulang untuk tiap RR. Gabungan tetap deduplicated. '
        'Tidak ada second push, delta/CVD atau SL ujung profile.', '',
        'Kalender test sama: 10 blok dua bulan dengan train 8 bulan sebelumnya. '
        'Sweep ini hanya mengubah stop pada test entry yang sudah dikunci, tanpa '
        'memilih RR dari train. Semua rasio ditetapkan user dan dilaporkan. '
        'Riwayat telah dipelajari; ini eksperimen development dan bukan holdout baru. '
        'Time exit sebelum NY open berikutnya tetap berlaku.', '',
        '## Hasil lengkap bagian test', '',
        '| Model | SL:TP | Entry | WR gross | Mean gross R | Mean net R | Total net R |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in summary.itertuples():
        lines.append(f'| {labels[r.model]} | 1:{r.rr:g} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
            f'{r.gross_mean_r:+.4f} | {r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'Net memakai fee 0,04% setiap sisi. WR termasuk time exit yang profit, '
        'bukan hanya TP POC. Harga barrier exit adalah print pemicu; overshoot dapat '
        'membuat gross loss lebih besar dari -1R atau profit melebihi planned RR.', '',
        '## Irisan: hasil harga, sizing dan risiko akun', '',
        '| SL:TP | Mean net return posisi | Net account kumulatif | Closed-trade max drawdown akun | Fold net positif / bertransaksi | Bootstrap 95% mean net R |',
        '|---|---:|---:|---:|---:|---:|']
    for r in summary.loc[summary.model.eq('intersection')].itertuples():
        s = stability.loc[stability.model.eq(r.model) & stability.rr.eq(r.rr)].iloc[0]
        ci = confidence.loc[confidence.model.eq(r.model) & confidence.rr.eq(r.rr)].iloc[0]
        lines.append(f'| 1:{r.rr:g} | {r.net_mean_position_pct:+.4f}% | {r.net_account_return_pct:+.2f}% | '
            f'{r.net_account_max_dd_pct:.2f}% | {int(s.positive_net_test_folds)}/{int(s.nonempty_test_folds)} | '
            f'[{ci.net_mean_ci025:+.4f}, {ci.net_mean_ci975:+.4f}] |')
    lines += ['', 'R berubah karena jarak SL berubah. Kolom return posisi memakai pergerakan '
        'harga net relatif entry, sehingga dapat dibandingkan tanpa perubahan unit R. '
        'Ilustrasi akun tetap risiko 0,5% equity per trade dengan batas entry notional 5x; '
        'SL lebih sempit meningkatkan notional agar risiko akun tetap sama sampai cap tercapai. '
        'Jadi kenaikan total R tidak otomatis berarti pergerakan harga yang didapat lebih besar. '
        'Return akun adalah kumulatif sampel, bukan return tahunan; drawdown memakai equity '
        'setelah trade ditutup dan tidak mencakup drawdown intratrade.', '',
        'Irisan RR 1:1 → 1:2: mean net R naik +0,2563 → +0,3231 dan total net R '
        '+17,68 → +22,30. Namun mean net return posisi turun +0,4332% → +0,3580%. '
        'Dengan risiko akun 0,5% yang sama, net account naik +9,17% → +11,58%, '
        'sedangkan drawdown naik 1,21% → 2,91%. RR 1:1 memiliki 8/8 fold aktif '
        'positif; seluruh rasio lain 7/8. Dua fold sisanya kosong.', '',
        '## Irisan per tahun', '',
        '| SL:TP | Tahun | Entry | WR gross | Mean net R | Total net R |',
        '|---|---:|---:|---:|---:|---:|']
    for r in years.loc[years.model.eq('intersection')].sort_values(['rr', 'year']).itertuples():
        lines.append(f'| 1:{r.rr:g} | {r.year} | {int(r.trades)} | {r.gross_wr_pct:.2f}% | '
            f'{r.net_mean_r:+.4f} | {r.net_total_r:+.2f} |')
    lines += ['', 'RR 1:2 irisan menghasilkan +17,59R pada 2025 dan +4,71R pada '
        'bagian test 2026. Seluruh rasio irisan masih net positif secara tahunan pada '
        'kedua tahun, tetapi RR 1:1,2 tahun 2026 nyaris impas. Ini bukan tahun penuh 2026.', '',
        '## Keterbatasan', '',
        'Bootstrap 4.000 pengulangan memakai blok kalender tujuh hari, mempertahankan '
        'membership dan pilihan filter asli; tidak mengulang pemilihan train. '
        'Interval tidak dikoreksi untuk lima model × lima RR ataupun eksperimen historis. '
        'RR dengan net R tertinggi di sampel ini belum menjadi optimum terkonfirmasi. '
        'Checkpoint lama tetap tersimpan dan tidak diganti otomatis.', '',
        'Semua lima baseline RR=1 direkonsiliasi terhadap sumber. Seluruh unique raw '
        'entry × RR diperiksa ulang secara independen, bukan hanya spotcheck. '
        'Nol volume profile dibangun ulang. Funding, spread, latency dan dampak order '
        'belum dihitung; sizing lebih besar dapat meningkatkan biaya eksekusi yang belum dimodelkan.']
    (out/'FINDINGS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    outputs = {name:model.base.sha(out/name) for name in ('FINDINGS.md', 'VALIDATION.json', 'raw_replay_checks.csv')}
    (out/'findings_audit.json').write_text(json.dumps(dict(status='passed', code_sha256=model.base.sha(__file__),
        core_audit_sha256=model.base.sha(out/'audit.json'), outputs=outputs), indent=2))
    print('All 25 model/RR combinations reported; checkpoint retained.')


if __name__ == '__main__':
    main()
