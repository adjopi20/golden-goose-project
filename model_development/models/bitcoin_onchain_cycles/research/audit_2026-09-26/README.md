# Audit 2026-09-26 — skrip pendukung

Alat bantu untuk audit `models/bitcoin_onchain_cycles`, bukan artefak riset dan
bukan sumber kebenaran. Temuan lengkapnya ada di
[`../AUDIT_2026-09-26.md`](../AUDIT_2026-09-26.md).

| Skrip | Fungsi |
| --- | --- |
| `reproducibility_check.py` | Menjalankan ulang tiga keluarga eksperimen in-sample ke direktori scratch, lalu membandingkan setiap kolom numerik dengan artefak beku. Hasil audit ini: **identik**, dengan enam perubahan pelaporan yang terdokumentasi dan dilaporkan otomatis. |
| `oos_conventions.py` | Menghitung ulang read OOS di tiga konvensi (replika terskala, akun $10.000 baru, akun yang sama berlanjut) untuk menunjukkan besarnya kerusakan akibat bug satuan. |
| `oos_scale_artifacts.py` | Membuktikan bahwa selisih antar-strategi di read OOS yang tercatat adalah artefak faktor skala, bukan perbedaan perilaku. |
| `z_forward_information.py` | Deskriptif: apakah MVRV Z punya informasi tentang return dan drawdown ke depan. Bukti untuk `../EDGE_IDEATION_2026-09-26.md`. |
| `felt_loss_frontier.py` | Deskriptif: frontier felt loss vs kekayaan untuk 13 varian akumulasi, dari ledger terekam, tanpa backtest baru. |

Jalankan dari root repo dengan `.venv\Scripts\python.exe`. `reproducibility_check.py`
butuh direktori `_replay/` yang dihasilkan dengan menjalankan ulang eksperimen
(perintahnya ada di docstring skrip). Direktori `_replay/` adalah ruang scratch;
hapus setelah selesai. Artefak beku hanya dibaca, tidak pernah ditulis.
