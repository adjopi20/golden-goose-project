# Daily Profile — aturan penelitian v2

Status: `strategy.py` menghasilkan observation dan kandidat entry. Hasil v1 lama tetap tersedia untuk perbandingan; v2 menggunakan dua profile alternatif dan delta per 15 menit. Simulasi fill/P&L dilakukan oleh `backtest_engine.replay_aggtrades`, bukan oleh strategi.

## Aturan yang sudah diputuskan

1. Zona waktu: `America/New_York` (mengikuti perubahan DST).
2. Dua pilihan volume profile yang selesai tepat pada 09:00 NY: `prior_24h` = 09:00 hari sebelumnya sampai sebelum 09:00 hari ini; `pre_ny` = 01:00 sampai sebelum 09:00 hari ini. Masing-masing menghasilkan VAL, POC, dan VAH sendiri, dihitung dari volume transaksi pada harga masing-masing.
3. Candle 1 jam tetap disimpan sebagai konteks. Keputusan entry menggunakan harga dan delta dari **bar 15 menit yang sudah selesai**, tanpa menunggu candle 1 jam selesai.
4. Dua pilihan profile dievaluasi secara terpisah terhadap urutan harga/orderflow yang sama.

## Premis trading yang sudah diputuskan

- Gaya: **trend following**, mengikuti arah lokasi relatif terhadap value area.
- Observasi untuk entry hanya berlangsung **09:00–12:00 NY**. Entry baru tidak diperbolehkan setelah batas waktu ini.
- Kandidat long hanya ketika harga keluar ke **atas VAH** lalu bertahan di luar value area; kandidat short adalah kebalikannya, keluar ke **bawah VAL** lalu bertahan. **Delta/order flow harus diamati untuk menilai apakah tekanan agresif mendukung arah breakout dan tetap menghasilkan kemajuan harga**. Delta bukan sinyal mandiri: usaha agresif harus dibandingkan dengan hasil harga. Definisi kuantitatif "bertahan" dan dukungan delta belum dipilih.
- Konfirmasi memakai **effort versus result secara berkelanjutan**, bukan keputusan dari satu candle. Untuk long, amati delta beli/agresi pembeli relatif terhadap kemajuan dan penerimaan harga di atas VAH; untuk short, lakukan kebalikannya pada penjual di bawah VAL. Jika harga makin tinggi tetapi delta beli per interval konsisten mengecil, urgensi pembeli di harga lebih tinggi mungkin berkurang. Itu **peringatan**, terutama bila kemajuan harga ikut melemah atau harga kembali ke value; bukan bukti tunggal bahwa breakout pasti gagal. Harga yang tetap maju efisien dan bertahan di luar value meskipun volumenya menurun masih mungkin menunjukkan path of least resistance. Interpretasi short simetris.
- Jika sampai batas 12:00 tidak ada breakout VAH/VAL yang memenuhi syarat, **tidak ada entry** untuk sesi tersebut.
- **Stop loss struktural awal: POC** dari pilihan profile yang dipakai (di bawah entry long, di atas entry short).
- Posisi yang masih terbuka ditutup berdasarkan waktu **tepat sebelum 09:00 NY pada hari berikutnya**. Waktu trigger dan mekanisme fill yang persis belum ditetapkan.
- Untuk mencegah informasi masa depan, level VAH/VAL/POC sudah harus lengkap pada 09:00 sebelum observasi entry dimulai.

## Belum ditentukan

1. Parameter dua/tiga bar 15 menit untuk konfirmasi adalah definisi penelitian awal, belum temuan yang divalidasi.
2. Stop awal tepat POC tanpa buffer; bagaimana mengeksekusi stop jika harga melewatinya adalah tanggung jawab engine eksekusi.
3. Harga eksekusi entry dan forced exit, ukuran posisi, fee, serta slippage untuk backtest.

Jangan mewarisi aturan model ORB/Pre-NY lama secara otomatis. Tidak ada TP atau trailing yang dibekukan.

## Implementasi sederhana v2

`strategy.py` menyediakan `evaluate_session(profile, bars, symbol=...)`, mengembalikan `(signals, decisions)`. Default penelitian:

- Dua **close 15 menit** berturut-turut di luar edge yang sama (`--confirmation-bars 2`, opsional 3). Kandidat paling awal 09:30.
- Delta = volume market-buy dikurangi market-sell. Untuk short, tanda dibalik. Delta kumulatif selama rangkaian di luar value dan delta bar terakhir harus searah, begitu pula body terakhir dan kemajuan close.
- Kembali ke value, ganti sisi, atau bar 15 menit yang hilang mereset rangkaian. Tiga delta searah yang berturut-turut mengecil bersama kemajuan harga yang ikut mengecil menghasilkan WAIT. Delta mengecil saja tidak membatalkan setup.
- Maksimum satu kandidat per sesi **per pilihan profile**. Signal eligible sejak close bar konfirmasi sampai tepat pukul 12:00. Stop tepat POC profile yang dipakai, tanpa buffer; time exit dijadwalkan 08:59 NY esok hari. TP dan trailing belum dipakai.
- Signal memakai harga close konfirmasi sebagai referensi saja. Engine harus mengisi pada transaksi eligible pertama dan menghitung biaya. Jika tidak ada fill sebelum cutoff, tidak ada trade.
- 50 bin profile dan value area 70% adalah parameter awal (`--profile-bins`, `--value-fraction`), bukan hasil optimasi.

## Jalankan langsung dari aggTrades

Dari root `C:\Users\adjop\OneDrive\Documents\golden-goose-project`:

```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -m models.daily_profile_1h.strategy `
  --input "storage/btcusdc/BTCUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet" `
  --symbol BTCUSDC `
  --start-date 2026-08-01 `
  --end-date 2026-08-31 `
  --profile-window both `
  --output-dir "models/daily_profile_1h/runs/btcusdc_2026-08_v2"
```

Satu pembacaan raw membangun kedua profile dan bar 15 menit. Outputnya terpisah pada subfolder `prior_24h/` dan `pre_ny/`, masing-masing dengan `signals.jsonl`, `decisions.jsonl`, dan `summary.json`. Di root output tersimpan `prepared_profiles.jsonl`, `prepared_bars_15m.jsonl`, dan `context_candles_1h.jsonl`. Gunakan `--profile-window prior_24h` atau `pre_ny` jika hanya ingin mengevaluasi satu pilihan.

Untuk mengevaluasi ulang tanpa membaca aggTrades:

```powershell
.\.venv\Scripts\python.exe -m models.daily_profile_1h.strategy `
  --profiles "models/daily_profile_1h/runs/btcusdc_2026-08_v2/prepared_profiles.jsonl" `
  --bars-15m "models/daily_profile_1h/runs/btcusdc_2026-08_v2/prepared_bars_15m.jsonl" `
  --symbol BTCUSDC `
  --profile-window both `
  --confirmation-bars 3 `
  --output-dir "models/daily_profile_1h/runs/btcusdc_2026-08_v2_3bars"
```

Raw input adalah Binance Futures aggTrades Parquet dengan `timestamp` (epoch UTC millisecond), `price`, `qty`, `is_buyer_maker`, `agg_trade_id`. `is_buyer_maker=True` berarti market seller. Bar memiliki `open_timestamp_ms`, `close_timestamp_ms`, `open`, `high`, `low`, `close`, `buy_volume`, `sell_volume`; batas akhir bar eksklusif. Profile memiliki `session_day`, `profile_window`, `profile_start_timestamp_ms`, `profile_end_timestamp_ms`, `val`, `poc`, `vah`.

## Koneksi ke observation dan replay

- `evaluate_session()` menerima profile lengkap dan bar 15 menit untuk sesi itu. Semua keputusan berasal dari data yang sudah selesai sebelum timestamp sinyal.
- `to_path_audit_signal(signal, entry_tick)` mengubah sinyal dan fill aktual (`timestamp_ms`, `price`, `agg_trade_id`) ke kontrak `backtest_engine.path_audit`. Engine tetap harus memilih tick eligible pertama, lalu mengelola stop, time exit, fee, dan sizing.
- CLI CP004/CP005 memvalidasi strategi lamanya dan belum menerima v2 langsung. `summary.json` melaporkan jumlah kandidat, **bukan** win rate, profit, atau equity.

Output v1 Agustus memakai close 1 jam dan tidak boleh dicampur dengan output v2 yang memakai bar 15 menit dan dua profile.

## Backtest eksekusi dua pilihan profile

`backtest_engine.replay_aggtrades` membaca kontrak `signals.jsonl` yang sudah dibuat v2 dan transaksi aggTrades raw. Engine ini tidak mengimpor strategi ini dan dapat dipakai model lain yang mengeluarkan kontrak sinyal sama. `prior_24h` dan `pre_ny` menjalankan **portfolio terpisah**, masing-masing mulai dari equity yang sama. Jika kedua profile memberi kandidat pada hari yang sama, keduanya dihitung di portfolio masing-masing; ini bukan strategi gabungan untuk mengambil dua posisi.

```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -m backtest_engine.replay_aggtrades `
  --input "storage/btcusdc/BTCUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet" `
  --observation-dir "models/daily_profile_1h/runs/btcusdc_2026-08_v2" `
  --start-date 2026-08-01 `
  --end-date 2026-08-30 `
  --output-dir "models/daily_profile_1h/runs/btcusdc_2026-08_v2_backtest_shared" `
  --initial-equity 1000 `
  --risk-fraction 0.005 `
  --fee-bps 4 `
  --max-leverage 5 `
  --max-entry-delay-seconds 300
```

Setiap subfolder output berisi `trades.jsonl`, `paper_orders.jsonl`, `decisions.jsonl`, `equity_curve.jsonl`, dan `summary.json` dengan metrics dari `backtest_engine`. Posisi berukuran target risiko 0,5% dari equity saat entry, dibatasi 5x leverage. Fee 4 bps dikenakan pada entry dan exit. Tidak ada take-profit atau trailing: stop POC dari profile yang dipilih, jika tersentuh, diisi pada aggTrade **setelah** print pemicu; time exit memakai print pertama pada/selepas 08:59 NY esok hari. Entry memakai print pertama pada/selepas signal selama belum lewat 12:00 dan belum terlambat lebih dari 300 detik.

31 Agustus 2026 tidak mempunyai data raw sampai exit 1 September 08:59. Karena itu rentang backtest berakhir 30 Agustus; signal 31 Agustus dicatat sebagai `outside_requested_dates` dan tidak dimasukkan ke P&L. Perbandingan Agustus ini memakai 30 sesi yang mempunyai horizon lengkap.

Harga aggTrade berikutnya hanyalah **proxy market fill**. File tidak menunjukkan harga bid/ask, kedalaman order book, market impact, atau funding Futures; biaya itu tidak disimulasikan. Equity curve dan drawdown dihitung pada penutupan posisi, bukan mark-to-market intratrade.

## Audit fitur pra-entry dan peluang seluruh hari

`audit_opportunity.py` membaca output observation/backtest v2 **tanpa mengubah sinyal atau trade**. Input harga adalah cache `candles_1m.parquet` yang sudah ada; tidak perlu memindai raw aggTrades lagi. Output:

Audit output `...opportunity_audit_v01` yang dibuat sebelum perbaikan timestamp jam tidak valid untuk EMA, ATR, ADX, realized volatility, dan semua hasil ternormalisasi ATR. Jalankan ulang ke folder `...opportunity_audit_v02`; sinyal dan hasil backtest tidak perlu dihitung ulang.

- `feature_snapshots.jsonl`: fitur yang hanya memakai candle menit selesai sebelum 09:00, sinyal, fill, atau edge test. EMA(20/200), ATR(14), ADX(14) berasal dari bar 1 jam selesai. Volume 1/3 jam dinormalisasi terhadap median sampai 20 blok waktu terdahulu; delta dibanding volume, dan hasil harga dibanding perjalanan harga. VWAP hanya **aproksimasi typical-price × volume dari candle menit**, bukan VWAP transaksi yang presisi. Nilai indikator tanpa warmup yang cukup adalah `null`.
- `setup_audit.jsonl`: satu baris per hari dan pilihan profile. Trade aktual mempertahankan entry/stop dari backtest. *Shadow setup* pertama adalah close 15 menit pertama di luar VAH/VAL, terlepas dari syarat delta. Entry shadow adalah open candle menit berikutnya, stop POC, batas entry 12:00. Ini riset peluang yang ditolak, **bukan** eksekusi/P&L. Jika tidak ada edge close atau geometri POC invalid, MFE shadow tetap tidak terdefinisi.
- `session_opportunity.jsonl`: **setiap hari** mendapat pergerakan maksimum naik dan turun dari open menit pertama pada/selepas 09:00 sampai sebelum 08:59 esok hari, dalam persen dan ATR yang dibekukan pada 09:00. Ini ukuran peluang *ex post*, bukan MFE trade dan bukan sinyal arah yang dapat diperdagangkan.

MFE-before-stop dihitung dari entry sampai stop awal tersentuh atau batas exit. Karena candle menit tidak menunjukkan urutan harga di dalam menit entry/stop, audit melaporkan **batas bawah dan atas**. Minut yang memuat stop tidak dihitung ke batas bawah, tetapi dihitung ke batas atas. Untuk analisis final kasus ambigu, replay raw aggTrades secara terarah. Kekosongan candle dalam cache dianggap tidak ada transaksi, bukan diisi harga sintetis; cakupan menit setiap sesi dilaporkan.

Contoh untuk riwayat BTCUSDC yang telah diobservasi dan dibacktest:

```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -m models.daily_profile_1h.audit_opportunity `
  --observation-dir "models/daily_profile_1h/runs/btcusdc_2024-01-05_to_2026-08-30_v2_full" `
  --backtest-dir "models/daily_profile_1h/runs/btcusdc_2024-01-05_to_2026-08-30_v2_full_backtest" `
  --candle-cache "tmp/cache/btcusdc/btcusdc_pre_ny_submodels_full_24h_orderflow_2024-01-05_to_2026-07-31/candles_1m.parquet" `
  --candle-cache "tmp/cache/btcusdc/btcusdc_pre_ny_oos_2026-08-01_to_2026-08-31_orderflow_v3/candles_1m.parquet" `
  --start-date 2024-01-05 `
  --end-date 2026-08-30 `
  --output-dir "models/daily_profile_1h/runs/btcusdc_2024-01-05_to_2026-08-30_opportunity_audit_v02"
```

Jangan memilih filter/threshold dari hasil hari yang sama lalu menyebutnya prediktif. Untuk menguji fitur, pisahkan tanggal secara kronologis dan pertahankan holdout yang belum dipakai memilih aturan.
