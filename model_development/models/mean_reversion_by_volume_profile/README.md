# Selected research reference — 2026-11-02

At the user's request to choose the latest historically profitable supported
entry model, select LTCUSDC frozen_sizing_v31 baseline_intersection as the research
reference. The later v32 study retains v31 entries and evaluates sizing; it does
not define a newer entry strategy. Do not choose a new Kelly/leverage policy here.

Existing fee-net evidence: 2024-04-11 through 2026-09-30, 116 closed trades,
+12.4379579% compounded return, 1.2078593% closed-equity DD, net-R profit factor
1.6112775. These baseline figures use 0.5% current-equity risk, 4 bps per side and
a 5x notional cap. They are existing historical simulations, not current/live
profits. Spread, funding, latency, impact, liquidation and intratrade DD are not
covered. The same LTCUSDT intersection lost 10.807%; USDC selection is based on
inspected outcomes and must not be described as untouched OOS evidence.

Sources: runs/frozen_sizing_v31_full/{summary.csv,REPORT.md,audit.json} and
runs/kelly_mc_v32_5000/{historical_compounding.csv,REPORT.md,audit.json}. Saved
summary/report hashes were checked against those existing audits; no raw archive,
backtest, Kelly fit or Monte Carlo simulation was run or rebuilt.

Paper_v33 is a later forward adaptation, not verified as profitable by these
figures: it uses next-print fills, a later time exit and 2.5% risk. It is not
silently overwritten, resized or promoted by this selection. No equal-return or
research/paper parity claim is made. Operational version binding needs an explicit
implementation/parity step; no new Phase 2 adapter is supplied in this pass.
Historical definitions, result artifacts and commands below remain historical;
new protocol work must reuse the global phase runners rather than create scripts.

---
# Active backtest: frozen LTC sizing — v31

Current model: [rules and commands](frozen_sizing_v31/README.md).
Artifact map: [ARTIFACTS.md](ARTIFACTS.md).

Full-history LTCUSDC + LTCUSDT; fixed outside-VA location, fixed distance >=1%,
six conditional sizing cases at 1.5x / 2x, plus normal-sizing baselines.
Only ATR-high classification recalibrates from eight past months every two months.
No profit-based group selection. No old research run is needed to execute v31.

```powershell
python models/mean_reversion_by_volume_profile/frozen_sizing_v31/backtest.py --root . --output models/mean_reversion_by_volume_profile/runs/frozen_sizing_v31_full
```

## Historical research documentation

The content below documents earlier experiments. Their definitions and results
remain versioned historical evidence; the active model is described above.

---

# Mean reversion by volume profile

## Observasi tambahan: VWAP rolling 24 jam

observe_rolling_vwap_bands.py membandingkan tiga metode pada profile-session yang sama: VWAP harian 00:00 UTC beku (hasil v07), VWAP 24 jam beku sebelum 13:00, dan VWAP rolling 24 jam. VWAP/sigma rolling diperbarui tiap awal menit dari 1440 candle 1 menit yang sudah selesai, memakai HLC3 berbobot quantity volume. Tidak memakai candle yang sedang berjalan. POC tetap empat profile lama; tidak dibangun ulang.

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\observe_rolling_vwap_bands.py --profiles-run models\mean_reversion_by_volume_profile\runs\four_profiles_1300_v02 --output models\mean_reversion_by_volume_profile\runs\rolling_24h_vwap_bands_v08
```

Observasi first-reach pada [13:00,17:00) NY tetap terpisah dari already_outside_1300, dengan urutan raw aggregate trades. VWAP rolling bisa mengubah waktu band tercapai karena harga atau band bergerak. POC inward/outward dipisahkan. Gerak lanjutan dalam sigma memakai sigma pada event, sedangkan end_z_signed memakai VWAP/sigma pada transaksi terakhir. Tidak ada entry, SL, TP atau PnL.

Output: REPORT.md, summary.csv, common_dates_summary.csv, yearly_summary.csv, event_hour_summary.csv, rolling_24h/events.csv, frozen_24h/events.csv, frozen_daily_matched_events.csv, anchors.csv, quality.csv, config_snapshot.json dan rolling_vs_frozen_common_dates.png. --frozen-run dapat mengganti baseline v07; --start/--end membatasi tanggal. Kesamaan kalender tidak menjamin kesamaan event antar metode. Pembanding frozen 24h memisahkan efek panjang anchor dari pergeseran jendela.

## Observasi aktif: band VWAP dan sigma beku sebelum 13:00

observe_frozen_vwap_bands.py mengamati first-reach band 0.5 sampai 5 sigma pada [13:00,17:00) NY, lalu sentuh/crossing setiap POC sesudah event. VWAP berjangkar 00:00 UTC dan sigma dihitung dari HLC3 candle 1 menit berbobot volume, keduanya dibekukan tepat sebelum 13:00. Harga event dan urutan crossing memakai aggregate trades. Tidak ada entry, SL, TP atau PnL.

Semua profile-session valid dipakai tanpa filter bias pagi atau pernah-menjauhi. Harga yang sudah berada di luar band sebelum 13:00 masuk kelompok terpisah. Persentase kembali utama mensyaratkan POC berada pada sisi inward dari harga event; POC outward dan event tepat di POC dicatat terpisah. Unit hitungan adalah sesi × arah × tingkat × profile, sehingga satu sesi bisa muncul pada beberapa tingkat dan kedua arah.

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\observe_frozen_vwap_bands.py --profiles-run models\mean_reversion_by_volume_profile\runs\four_profiles_1300_v02 --output models\mean_reversion_by_volume_profile\runs\frozen_vwap_bands_v07
```

Output: REPORT.md, events.csv, summary.csv, common_dates_summary.csv, event_hour_summary.csv, yearly_summary.csv, anchors.csv, quality.csv, config_snapshot.json dan poc_return_by_sigma.png. Tidak membangun volume profile ulang. --levels mengatur tingkat untuk observasi, --start/--end membatasi tanggal, --max-sessions tersedia untuk pemeriksaan kecil per asset. Hitungan 30/60 menit hanya memakai event dengan sisa waktu cukup; tidak kembali sebelum 17:00 tidak otomatis diberi label continuation.

Uji filter trading VWAP v05 di bawah adalah eksperimen terdahulu, bukan definisi observasi aktif ini.

## Uji VWAP harian dan screening asset

Pilihan awal untuk crypto: VWAP reset 00:00 UTC, harga HLC3 candle 1 menit berbobot volume, dibekukan tepat sebelum 13:00 NY. Memakai ulang indicator.vwap.vwap dan outcome TP/SL/time-exit yang sudah ada. Target tetap POC. Reset ini pilihan eksplisit eksperimen, bukan default semua platform.

Empat kelompok: baseline, bias pagi selaras posisi terhadap VWAP, kondisi tersebut + jarak minimal 1 sigma, dan kondisi tersebut + slope VWAP 30 menit maksimum 0.05 sigma secara absolut. Sigma memakai RMS deviasi HLC3 dari VWAP final berbobot volume. Ambang tidak dituning. Efficiency ratio 5 menit hanya proxy arah pagi, bukan bukti regime multihari.

Hasil BTC/HYPE/XRP tersedia di runs/vwap_daily_utc_v05/REPORT.md. Baseline perbandingan dibatasi pada trade dengan fitur VWAP valid; kalender bersama tersedia terpisah. Periode 2026 pernah terlihat pada analisis sebelumnya sehingga tidak dianggap holdout yang belum tersentuh. Angka utama gross, fee 2 bps per sisi hanya skenario.

Untuk mengulang analisis ringan tanpa membangun volume profile:

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\observe_vwap_filters.py --run models\mean_reversion_by_volume_profile\runs\poc_rr_aligned_v03 --output models\mean_reversion_by_volume_profile\runs\vwap_daily_utc_v05
```

Untuk screening delapan asset, jalankan dari root proyek:

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\screen_crypto_vwap.py --existing-profiles-run models\mean_reversion_by_volume_profile\runs\four_profiles_1300_v02 --output models\mean_reversion_by_volume_profile\runs\crypto_vwap_screen_v06
```

Pipeline memakai ulang profile BTCUSDC/HYPEUSDT/XRPUSDT, lalu membangun hanya profile tambahan ETHUSDC/BNBUSDC/SOLUSDC/AVAXUSDC/AAVEUSDC dari arsip lokal. Pipeline tambahan belum dijalankan oleh agent. Universe dipilih berdasarkan ketersediaan data, belum berdasarkan kecocokan strategi. Untuk asset tanpa cache candle, candle 1 menit dibangun dari aggregate trades; menit tanpa transaksi memakai close sebelumnya dan volume nol, setelah lolos filter kualitas.

Output pipeline: additional_profiles, combined_profiles, aligned_rr dan vwap_filters. filter_metrics.csv mencakup seluruh periode, development sebelum 2026, validation sejak 2026, gross dan skenario fee; common_dates_filter_metrics.csv membatasi tanggal fitur bersama semua asset. Screening memerlukan data lain yang belum dipakai untuk membuktikan edge, serta pembandingan periode yang sama antar asset.

## Performa semua posisi ditutup: satuan R

performance_poc_rr.py mengolah trades.csv dari uji aligned-bias, termasuk posisi time exit. Tidak membangun profile atau membaca ulang arsip transaksi besar.

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\performance_poc_rr.py --run models\mean_reversion_by_volume_profile\runs\poc_rr_aligned_v03 --output models\mean_reversion_by_volume_profile\runs\poc_rr_performance_v04
```

Output: REPORT.md, metrics.csv, exit_type_metrics.csv, monthly_metrics.csv, yearly_metrics.csv, direction_metrics.csv, common_dates_metrics.csv, hypothetical_fee_sensitivity.csv, daily_curves.csv, closed_trades.csv, performance_curves.png. Metrik utama masih gross; skenario fee 2/4 bps per sisi tidak dianggap fee aktual exchange.

Untuk equity akun, tambahkan --initial-equity NILAI dan salah satu --risk-percent PERSEN (compound) atau --risk-cash NOMINAL (tetap). Account metrics dihasilkan hanya ketika pengguna menyediakan sizing. Return %/CAGR/Calmar tidak disimpulkan dari total R tanpa aturan risiko.

## Uji reversal TP:SL 1:1, bias selaras

backtest_poc_rr.py memakai POC dari runs/four_profiles_1300_v02 tanpa membangun profile ulang.

Bias long mensyaratkan harga tepat sebelum 13:00 > harga awal 09:30 DAN > POC; bias short mensyaratkan harga sebelum 13:00 < keduanya. Entry pada transaksi pertama sejak 13:00 memakai arah sebaliknya. Ketidaksesuaian atau kesamaan harga berarti tidak entry.

TP = POC, SL = 2 × entry − POC. Jika gap pada entry menjadikan POC berada pada sisi yang salah, tidak entry. Uji utama mencakup semua sesi valid dengan kedua syarat tersebut; filter pernah-menjauhi tidak ditambahkan sebagai syarat ketiga.

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\backtest_poc_rr.py --profiles-run models\mean_reversion_by_volume_profile\runs\four_profiles_1300_v02 --bias-mode aligned --all-valid --output models\mean_reversion_by_volume_profile\runs\poc_rr_aligned_v03
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\rr_report.py models\mean_reversion_by_volume_profile\runs\poc_rr_aligned_v03
```

Hasil utama: runs/poc_rr_aligned_v03/REPORT.md, summary.csv, trades.csv, common_dates_summary.csv. TP win rate terhadap seluruh entry dan terhadap TP+SL tersedia terpisah. Posisi yang belum mencapai TP/SL ditutup pada harga transaksi terakhir sebelum 17:00. Angka masih gross; fill, fee, spread, slippage, latency, funding belum dimodelkan. Lompatan harga dinilai pada barrier TP/SL, bukan fill aktual.

## Observasi aktif: empat POC, koreksi waktu 13:00 NY

Pengguna mengoreksi waktu acuan menjadi 13:00 NY. Script aktif: observe_four_profiles.py; konfigurasi: four_profiles_config.json. Hitungan historis penuh versi ini belum dijalankan oleh agent; command diberikan agar pengguna menjalankannya sendiri.

Empat profile untuk tanggal observasi D (semuanya memakai America/New_York dan interval akhir eksklusif):

| ID | Awal | Akhir |
|---|---|---|
| ny_0930_1300 | D 09:30 | D 13:00 |
| pre_ny_0130_0930 | D 01:30 | D 09:30 |
| previous_day_0930_0930 | D-1 09:30 | D 09:30 |
| previous_0930_today_1300 | D-1 09:30 | D 13:00 |

Menjauhi (dikonfirmasi pengguna): selama [09:30,13:00), harga pernah memiliki jarak absolut terhadap POC lebih besar daripada jarak harga transaksi pertama sejak 09:30. Tidak ada ambang minimum atau syarat tetap di sisi POC yang sama. Masing-masing profile memakai POC sendiri.

Kembali: sentuh atau crossing POC pada [13:00,17:00). Harga sebelumnya untuk crossing pertama adalah transaksi terakhir sebelum 13:00. Sesi yang sudah menyentuh POC sebelum 13:00 tetap membutuhkan event baru setelah 13:00 agar dihitung kembali. Denominator = sesi valid yang pernah menjauhi; numerator = yang kembali setelah 13:00.

POC dibangun menggunakan indicator.volume_profile.build_basic_volume_profile, dengan 50 bin berlebar sama dan volume quantity. POC adalah pusat bin bervolume terbesar. Profile yang berakhir 13:00 memakai POC final tersebut secara retrospektif untuk klasifikasi 09:30–13:00; POC tidak berkembang selama pengamatan.

13:00 NY tetap = 00:00 WIB saat DST, 01:00 WIB saat standard time. Ini bukan jam 00:00 WIB tetap sepanjang tahun.

Jalankan dari root proyek:

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\observe_four_profiles.py --departure-mode ever --start 2022-01-01 --end 2026-10-05 --output models\mean_reversion_by_volume_profile\runs\four_profiles_1300_v02
```

summary.csv langsung menjawab jumlah sesi menjauhi, jumlah kembali dan persentasenya untuk 3 market × 4 POC. daily.csv, yearly_summary.csv, quality.csv, common_dates_summary.csv, source_manifest.json dan DEFINITIONS.md melengkapi hasil. Snapshot mencatat parameter. Data arsip yang belum tersedia sejak 2022 tetap dilaporkan kosong; exchange/produk belum terverifikasi.

## Versi sebelumnya: acuan 01:00 NY (sudah dikoreksi)

Hasil runs/observation_2022_v01 mengikuti salah pemahaman waktu terdahulu dan tidak menjawab observasi aktif di atas. Script dan hasil lama dipertahankan sebagai riwayat.

Observasi historis BTCUSDC, HYPEUSDT, XRPUSDT dari 2022-01-01 sampai akhir data lokal tersedia. Tanggal akhir permintaan awal: 2026-10-05, yaitu hari sebelum tanggal chat (2026-10-06).

## Waktu yang sudah dikonfirmasi

- ORB acuan: 09:30 America/New_York pada hari sebelumnya sampai 01:00 NY pada tanggal observasi.
- Observasi: 01:00–17:00 NY.
- Pengguna mengonfirmasi reversal setelah 01:00 NY, bukan titik tengah malam WIB.
- Timestamp UTC, NY dan WIB memakai timezone dengan DST. Pada pengulangan 01:00 musim gugur, dipilih kejadian pertama (fold=0).
- 00:00 WIB sebenarnya 12:00/13:00 NY, sedangkan 01:00 NY adalah 12:00/13:00 WIB. Rentang panjang yang disebut ORB di sini mengikuti definisi pengguna.

## Observasi awal

1. Arah ORB ditentukan oleh harga pertama dan terakhir pada range acuan.
2. Ukur harga pada 17:00 yang berlawanan arah dengan ORB, serta counter-move minimal 0.25 lebar range selama observasi.
3. Bangun volume profile dari transaksi ORB, dengan 50 bin dan value area 70%; ukur crossing POC dan midpoint.
4. Ukur breakout pertama setelah 01:00, reentry range, dan pencapaian sisi berlawanan setelah breakout.
5. Catat jumlah valid, hari yang dikecualikan, tahun, dan periode valid bersama antar market.

Parameter berada di config.json. Ambang counter-move dan kualitas adalah keputusan awal peneliti, belum dituning. Report menjelaskan denominator dan keterbatasan; hasil adalah statistik perilaku, bukan PnL atau strategi trading.

## Sumber data

Memakai arsip aggregate trades di storage proyek. Venue dan produk belum dikonfirmasi pengguna; metadata parquet tidak membuktikan exchange/spot/futures. Report harus mempertahankan label provenance belum terverifikasi.

Nama file tidak sama persis dengan waktu transaksi awal. Cakupan aktual diukur dari statistik timestamp parquet. Tahun yang tidak tersedia tidak diisi atau diganti market lain.

## Menjalankan

Dari root proyek, dengan Python environment proyek yang sudah menyediakan numpy, pandas, pyarrow, dan tzdata:

```powershell
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\observe.py --output models\mean_reversion_by_volume_profile\runs\observation_2022_v01
.\.venv\Scripts\python.exe models\mean_reversion_by_volume_profile\plot_results.py models\mean_reversion_by_volume_profile\runs\observation_2022_v01
.\.venv\Scripts\python.exe -m unittest discover -s models\mean_reversion_by_volume_profile -p test_observe.py
```

Override opsional: --start YYYY-MM-DD, --end YYYY-MM-DD, --root PATH, --config PATH.

File hasil: runs/observation_2022_v01/REPORT.md, daily_observations.csv, summary.json, yearly_summary.csv, common_period_summary.json, first_counter_move_hour.csv, source_manifest.json, config_snapshot.json.

plot_results.py menambahkan observation_summary.png dan value_area_groups.csv.

Jangan gunakan hari yang gagal filter kualitas dalam denominator. Periode di luar arsip dan sesi boundary yang tidak lengkap dilaporkan tersendiri.

## Context observation v09: effort/result, value and larger regimes

Cache-only observation completed for delta/volume versus price progress, POC migration, completed 4h/daily EMA50/EMA200/ADX and geometry controls. Uses the existing reversal entries/outcomes and retrospective rolling train 8 months / test 2 months / step 2 months. All features are separate hypotheses; no live rule is adopted.

Read [initial findings](runs/edge_context_v09/FINDINGS.md), [all results](runs/edge_context_v09/REPORT.md), and [registered definitions and commands](EDGE_CONTEXT_DESIGN.md). WR includes time exits; gross R is not net profitability. Test results were previously inspected; this is not an untouched holdout. `audit.json` verifies source/outcome identity, cutoff times and result arithmetic. Choose a new output directory for each run.

VAH/VAL acceptance/rejection remains pending an explicit raw-trade export; the commands are in the design document. The cache-only run does not rebuild volume profiles. Daily indicators can be unavailable after long data gaps and the 200-bar warm-up; unavailable/limited folds remain visible.

## Volatility observation v10

Cache-only observation of the sizing research's EWMA20 volatility, target POC distance relative to that volatility, and the separate macro daily-close 20/60 volatility ratio. Half-lives 10/40 are sensitivity checks. Original entry/TP/SL/time exit/R and position sizing remain unchanged. Uses retrospective train 8 months / test 2 / step 2, train-only terciles and category selection, with matched baseline comparisons and missing-history coverage.

Read [findings](runs/volatility_context_v10/FINDINGS.md), [report](runs/volatility_context_v10/REPORT.md), [all four profile results](runs/volatility_context_v10/aggregate.csv), and [definitions/command](VOLATILITY_CONTEXT_DESIGN.md). `audit.json` checks unchanged outcomes, causal snapshot clocks, warmup, keys and R/WR arithmetic. `category_summary.csv` is descriptive pooling of all available test categories; `leave_one_fold_out.csv` examines concentration without changing the strategy. Gross historical results do not establish fresh holdout performance. No filter or sizing rule is adopted.

