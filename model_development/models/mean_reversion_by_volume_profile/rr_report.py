"""Write a concise review of the completed aligned-bias barrier study."""
import argparse
import json
from pathlib import Path

import pandas as pd

LABELS = {
    "ny_0930_1300": "Hari ini 09:30–13:00",
    "pre_ny_0130_0930": "Pre-NY 01:30–09:30",
    "previous_day_0930_0930": "Kemarin 09:30–hari ini 09:30",
    "previous_0930_today_1300": "Kemarin 09:30–hari ini 13:00",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    out = args.run
    cfg = json.loads((out / "config_snapshot.json").read_text())
    s = pd.read_csv(out / "summary.csv")
    lines = ["# Reversal ke POC, TP:SL 1:1, bias harus selaras", "",
        "Syarat long 09:30–13:00: harga terakhir sebelum 13:00 > harga awal 09:30 DAN > POC. Entry short pada transaksi pertama sejak 13:00.",
        "Syarat short: harga terakhir sebelum 13:00 < harga awal 09:30 DAN < POC. Entry long pada transaksi pertama sejak 13:00.",
        "Kesamaan harga atau kedua arah yang tidak selaras berarti tidak ada entry. Setiap POC diuji sebagai model terpisah.",
        "TP = POC; SL = 2 × entry − POC. Semua sesi valid dipertimbangkan; filter pernah-menjauhi versi sebelumnya tidak menjadi syarat tambahan.",
        "POC diambil dari run empat profile yang sudah selesai; tidak dibangun ulang. Jika POC sudah di sisi yang salah terhadap harga entry aktual, entry dilewati.", "",
        "## Hasil historis lengkap", "",
        "TP/semua entry adalah peluang TP lebih dulu sebelum batas waktu. TP/(TP+SL) hanya memakai trade yang mencapai salah satu barrier. Time exit ditutup pada transaksi terakhir sebelum 17:00 dan dapat untung atau rugi secara gross.", ""]
    def pct(value):
        return f"{value:.1f}%" if pd.notna(value) else "N/A"
    def table(frame):
        rows = ["| Market | Profile (NY) | Entry | TP | SL | Time exit | TP/semua entry | TP/(TP+SL) |",
                "|---|---|---:|---:|---:|---:|---:|---:|"]
        for r in frame.itertuples():
            rows.append(f"| {r.symbol} | {LABELS[r.profile]} | {r.entries} | {r.tp_wins} | {r.sl_losses} | {r.time_exits} | {pct(r.tp_pct_all_entries)} | {pct(r.tp_pct_resolved)} |")
        return rows
    lines.extend(table(s))
    common = pd.read_csv(out / "common_dates_summary.csv")
    lines += ["", "## Tanggal valid yang sama untuk 12 kombinasi", ""] + table(common)
    lines += ["", "## Batas perhitungan", "",
        "Urutan barrier memakai transaksi agregat aktual, termasuk urutan ID untuk timestamp yang sama. TP = +1R dan SL = −1R adalah penilaian pada level barrier; gap harga dapat memberi fill aktual yang berbeda.",
        "Belum ada fee, spread, slippage, latency, funding atau simulasi fill. Angka ini merupakan hasil gross, bukan win rate/profit yang sudah memperhitungkan biaya.",
        "Profile sampai 13:00 baru diketahui pada akhir interval tersebut; data setelah 13:00 tidak dipakai untuk menentukan POC atau bias. Anggap entry di transaksi pertama sejak 13:00 sebagai asumsi ideal tanpa waktu komputasi.",
        "Batasnya 13:00 NY tetap: 00:00 WIB saat DST, 01:00 WIB saat standard time. Rentang historis tiap market berbeda karena ketersediaan arsip. Exchange dan produk arsip lokal belum terverifikasi.",
        "trades.csv menyimpan TP, SL, entry, hasil, waktu keluar dan alasan tidak entry. statuses.csv memisahkan seluruh pengecualian. summary.csv juga menyimpan positive_gross_closes, mean_gross_r, dan median_risk_bps.", "",
        "## Menjalankan kembali", "", "```powershell",
        ".\\.venv\\Scripts\\python.exe model_development\\models\\mean_reversion_by_volume_profile\\backtest_poc_rr.py --profiles-run model_development\\models\\mean_reversion_by_volume_profile\\runs\\four_profiles_1300_v02 --bias-mode aligned --all-valid --output model_development\\models\\mean_reversion_by_volume_profile\\runs\\poc_rr_aligned_v03",
        ".\\.venv\\Scripts\\python.exe model_development\\models\\mean_reversion_by_volume_profile\\rr_report.py model_development\\models\\mean_reversion_by_volume_profile\\runs\\poc_rr_aligned_v03",
        "```", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
