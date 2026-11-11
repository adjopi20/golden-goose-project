# quiet_breakout

Model riset untuk mengidentifikasi ekspansi harga yang bertahap, tenang, bersih, dan persisten. Pergerakan dapat terbangun melalui beberapa candle tanpa satu candle pun mencapai P85 body dan P85 volume secara bersamaan.

Kebutuhan ini muncul dari peninjauan HYPEUSDT pada 2 Juli 2026. Candle 1H pukul 20.00 WIB memiliki body 0,825 USDT, di bawah P85 0,913 USDT, meskipun volumenya sekitar 999 ribu HYPE dan melewati P85 sekitar 798 ribu HYPE. Burst breakout melewatkannya karena meminta kedua ambang terlewati dalam satu candle. Kejadian tersebut adalah contoh pengembangan yang sudah diinspeksi. Aturan quiet_breakout perlu dinilai juga pada pergerakan lain.

Tujuan model adalah mengenali progres harga lintas waktu, termasuk pergerakan yang mulai di luar value area sebelum ekspansi menguat. High/low harga dan VAH/VAL volume profile adalah referensi berbeda. Peran masing-masing dalam pembentukan setup akan ditentukan saat perancangan logika. Model tidak otomatis mewajibkan penembusan high/low 24 jam.

Rancangan observasi awal menggunakan candle 1H untuk konteks. Respons harga dibaca dalam rolling 5 menit dan diperbarui pada setiap close 1 menit. Harga per waktu menjadi jangkar penilaian. Delta, volume absolut masing-masing sisi, serta efektivitas agresi digunakan untuk menjelaskan progres harga.

Perilaku yang perlu diamati meliputi:

- Initiative, ketika agresi searah menghasilkan progres harga.
- Kandidat absorption, ketika agresi lawan tetap kuat tetapi gagal menghambat progres searah.
- Kandidat exhaustion, ketika tekanan lawan surut dan harga mulai bergerak searah.
- Ekspansi dengan volume rendah, ketika perpindahan harga besar terjadi dengan sedikit volume. Dugaan likuiditas book yang tipis memerlukan data order book.
- Opposite takeover, ketika pihak lawan mengambil alih agresi dan harga ikut bergerak melawan setup.
- Pelemahan kelanjutan, ketika harga berhenti membuat progres, kembali ke area sebelumnya, atau berulang kali membatalkan dorongan awal.

Angka hasil observasi menjadi bukti untuk menilai setup. Threshold, bobot, skor, dan probabilitas belum ditetapkan. Syarat P85 body dan P85 volume pada satu candle 1H tidak dijadikan prasyarat wajib untuk kandidat quiet breakout.

## Setup dan grid riset 1H

Setup dinilai saat candle 1H tutup terhadap rentang harga penuh volume profile yang sudah tersedia sebelum candle tersebut. Untuk long, close 1H harus lebih tinggi daripada harga tertinggi profil. Untuk short, close 1H harus lebih rendah daripada harga terendah profil. Close tepat di batas belum memenuhi syarat. Candle boleh berangkat dari bagian mana pun pada profil, termasuk dari VAH untuk long dan VAL untuk short. Body dan volume dicatat tanpa ambang minimum. Semua kombinasi memakai setup 1H ini.

- Lookback volume profile: 12H, 16H, atau 24H.
- Order flow: evaluasi tiap 1m dengan rolling 5m, tiap 3m dengan rolling 12m, atau tiap 5m dengan rolling 10m. Semua membaca order flow setelah setup breakout 1H yang sama.
- Full exit: tutup seluruh sisa posisi 24H atau 48H sejak fill entry, jika stop belum menutup posisi.

| RM | Perlakuan saat harga mencapai +1R |
| --- | --- |
| RM-A | Pindahkan stop ke harga entry. Tidak ada exit sebagian. |
| RM-D | Tutup 10% posisi dan pindahkan stop sisanya ke harga entry. |
| RM-E | Tutup 10% posisi dan pertahankan stop sisanya di POC awal. |

Semua RM mulai dengan stop di POC dari profil yang dibekukan saat setup. 1R adalah jarak dari fill entry ke stop awal. Untuk semua RM, pada +2R stop mengunci +1R, pada +4R mengunci +2R, pada +8R mengunci +4R, dan pola berlanjut setiap target berlipat dua.

RM-B dipensiunkan untuk Quiet Breakout. Perilaku lama RM-C tercakup oleh RM-A dengan full exit 48H. Ketiga RM dapat dikombinasikan dengan kedua batas waktu sehingga ada 6 kombinasi exit. Total grid adalah 3 lookback profil × 3 variasi order flow × 6 kombinasi exit = 54 kombinasi.

## Keputusan yang masih terbuka

Baseline development memakai konfirmasi statis Burst selama maksimal 60 menit, dengan pembatalan jika POC tersentuh atau jalur menit tidak lengkap. Candle pemicu harus buka di dalam atau tepat pada batas rentang profil. Variasi A, yang dipakai pada run awal v01, tidak membatasi jumlah entry per hari. Variasi B membatasi satu fill entry per akun per hari UTC melalui opsi model one_entry_per_utc_day=true. Kedua variasi memakai grid 54 kombinasi dan cache input yang sama. Variasi C meneruskan batas harian B dan menambahkan volume candle breakout 1H minimal P75 dari candle 1H sebelumnya dalam lookback profil yang sama (12H, 16H, atau 24H). Candle breakout dikeluarkan dari baseline P75. Grid memakai periode pembanding HYPEUSDT 2025-10-01 sampai 2026-10-31 UTC, dengan modal awal 1.000 USDT, risiko 1% dari equity berjalan, fee 4 bps per sisi, dan leverage maksimum 5x.

Gunakan strategy.py dan grid.json dengan runner Phase 2 bersama. Pertahankan direktori cache yang sama antar iterasi. Cache candle dan profil dapat dipakai ulang. Cache order flow dibedakan menurut setup dan cadence, serta dibagi oleh keenam kombinasi exit untuk setup dan cadence tersebut.

## Variasi D harian

Gunakan candle 1D UTC. Profil dibentuk dari 12, 16, atau 24 candle harian selesai sebelum candle pemicu. Candle pemicu buka di dalam atau tepat pada batas rentang penuh profil, lalu tutup di luar rentang. Volume candle pemicu harus mencapai P75 dari volume candle harian pada lookback yang sama. Entry menggunakan print pertama yang tersedia pada candle berikutnya dalam batas keterlambatan runner. Konfirmasi order flow dinonaktifkan dan batas satu fill per hari UTC tetap aktif. Full exit menutup sisa posisi 14 hari setelah fill, dengan stop dan exit sebagian mengikuti RM-A, RM-D, atau RM-E. Gunakan daily_14d_grid.json untuk 9 kombinasi.

## Variasi E dan F pada baseline C

E menguji jarak dari harga konfirmasi ke POC sebagai fraksi harga konfirmasi. Lewati sinyal di atas P85 dari maksimal 100 konfirmasi C sebelumnya dengan minimal 30 observasi. Runner mengecek ulang batas yang sama pada print calon fill dan membatalkan entry jika jarak melebihi batas.

F menguji efisiensi jalur dalam window konfirmasi. Ukur jarak absolut dari open menit pertama ke close menit terakhir dan bagi dengan total perpindahan absolut dari open pertama melalui semua close 1m. Lewati sinyal di bawah P15 dari riwayat konfirmasi C sebelumnya dengan panjang dan minimum yang sama.

Kedua tes berdiri sendiri. Sinyal selama 30 konfirmasi pertama memakai aturan C, dan semua konfirmasi C masuk ke riwayat berikutnya, termasuk yang kemudian ditolak. Ambang hanya memakai konfirmasi yang sudah terjadi. Gunakan grid 1H dan cache C yang sama.

## Matriks kombinasi

| Variasi | Timeframe | Batas entry per hari UTC | Filter volume breakout | Filter entry tambahan | Kombinasi per aset |
| --- | --- | --- | --- | --- | --- |
| A | 1H | Bebas | — | — | 54 |
| B | 1H | Satu fill | — | — | 54 |
| C | 1H | Satu fill | P75 | — | 54 |
| D | 1D | Satu fill | P75 harian | Entry pada candle berikutnya | 9 |
| E | 1H | Satu fill | P75 | Jarak entry ke POC maksimal P85 | 54 |
| F | 1H | Satu fill | P75 | Efisiensi jalur harga minimal P15 | 54 |

Grid 1H mencakup semua hasil perkalian 3 lookback profil (12H, 16H, 24H), 3 cadence flow (1/5, 3/12, 5/10), 3 RM (A, D, E), dan 2 full exit (24H, 48H). Variasi A, B, C, E, dan F memakai konfirmasi order flow dan akun independen per kombinasi. E dan F diuji terpisah.

Grid D mencakup 3 lookback profil (12D, 16D, 24D) dan 3 RM (A, D, E) dengan satu full exit 14D. Total katalog A–F adalah 279 kombinasi per aset.

Pengujian lintas aset yang direncanakan mencakup UNIUSDC, AAVEUSDC, BTCUSDC, ETHUSDC, dan INJUSDT pada A, B, C, E, dan F. D tetap ditunda. Gunakan periode pembanding 1 Oktober 2025 sampai 31 Oktober 2026 UTC dan grid 1H yang sama. Totalnya 270 kombinasi per aset dan 1.350 job. Modal awal 1.000 dalam quote currency masing-masing akun, dengan risiko 1% dari equity berjalan, fee 4 bps per sisi, dan leverage maksimum 5x.

## Exit eksperimen ATR 1H

Gunakan position_exit=hourly-atr pada setup 1H. Stop awal tetap POC. Setelah fill, setiap close 1H menghitung kandidat long dari high tertinggi sejak fill dikurangi 2,5 ATR, atau kandidat short dari low terendah sejak fill ditambah 2,5 ATR. ATR memakai Wilder 14 candle dan mengulang seed setelah gap satu jam penuh. Stop hanya diketatkan. Jika close sudah melewati kandidat baru, jadwalkan exit pada print berikutnya. Jalur ATR mengganti tangga stop RM dan batas waktu full exit. RM-D dan RM-E tetap mengambil 10% di +1R sehingga keduanya ekuivalen pada exit ATR yang sama. Posisi yang masih terbuka pada akhir data mengikuti censoring runner Phase 2. Jalankan replay penuh untuk mengukur kembali entry, posisi terblokir, dan equity.

## Eksperimen satu add

Opsi pyramid_add=true berlaku pada setup 1H dengan exit baseline. Impulse 1H searah wajib mulai setelah fill awal, tutup minimal +1R, dan body minimal P85 dari 24 candle 1H sebelumnya. Tiga candle berikutnya harus overlap, dengan close di sepertiga ujung impulse dan rentang gabungan maksimal 50% range impulse. Dalam tiga jam selanjutnya, close harus menembus ekstrem impulse dan balance sebelum close menembus sisi invalidasi balance. Kandidat continuation memakai konfirmasi order flow statis sesuai cadence posisi awal, maksimal 60 menit. Sentuhan sisi invalidasi balance membatalkan konfirmasi.

Satu add boleh terisi setelah konfirmasi jika print masih melewati level continuation dan stop aktif. Ukuran dibatasi 80% kuantitas awal, 80% risiko awal sebelum biaya, dan kapasitas leverage akun tersisa. Stop, full exit, target parsial untuk leg awal, dan tangga R tetap mengacu pada posisi awal. Add berbagi exit final dan tidak mengulang TP1. Ledger menggabungkan P&L dan fee kedua leg untuk equity geometrik. R tetap dinormalisasi terhadap risiko posisi awal. Rincian add, termasuk yang masih terbuka, tercatat di pyramid_add_fills dalam run.json.

Opsi pyramid_balance=local_2h menguji dua candle 1H yang overlap dan tutup di sisi favorable dari midpoint impulse. Range gabungannya maksimal sebesar range impulse dan balance boleh terbentuk melewati ujung impulse. Continuation dalam tiga candle berikutnya cukup menembus ekstrem balance lokal. Impulse P85, syarat +1R, konfirmasi flow P75, invalidasi balance, batas sizing, dan exit tetap mengikuti aturan eksperimen di atas. Default pyramid_balance=strict_3h mempertahankan aturan awal tiga candle.

## Eksperimen sizing initiative 2x

Opsi initiative_risk_2x=true pada replay baseline 1H tanpa add menggandakan fraksi risiko awal hanya ketika snapshot konfirmasi memuat initiative_candidate dan tidak memuat exhaustion_candidate. Risiko trade lain memakai fraksi dasar. Quantity dihitung dari equity berjalan dan tetap dibatasi leverage yang sama. Entry, stop, TP1 dan jam exit tetap memakai aturan baseline. Gunakan replay penuh dan bandingkan terhadap baseline dari sumber serta konfigurasi yang sama.

## Eksperimen pelonggaran entry dan timing add

Bandingkan risk_p95 dengan risk_p85 dan path_p5 dengan path_p15 pada baseline C yang sama. P95 memperlebar cap risiko dan masih diperiksa pada harga calon fill. P5 menurunkan batas efisiensi jalur. Riwayat ambang, pemanasan, dan konfirmasi entry tetap sama.

Bandingkan pyramid_balance=local_2h_fast dengan local_2h. Balance tetap dua candle 1H. Setelah balance selesai, pemicu continuation menggunakan close 1m, 3m, atau 5m sesuai cadence strategi. Gunakan hanya bar cadence yang lengkap. Kandidat pertama yang close menembus level balance memulai konfirmasi P75 dengan window semula dan tanpa akses ke candle berikutnya. Jika bar sebelum trigger hilang atau close melanggar sisi invalidasi, setup dibatalkan. Batas satu add, impulse +1R, sizing add dan exit tetap sama. Jalankan kedua eksperimen terpisah dengan risiko dasar.

## Eksperimen gabungan HYPEUSDT P5 dan sizing initiative 1,5x

Untuk stable, ganti entry_filter=path_p15 dengan path_p5. Untuk tail_dependent, pakai entry_filter=risk_p85 dan additional_path_filter=path_p5. Kedua ambang pada tail_dependent memakai riwayat konfirmasi C sebelum filter diterapkan. P85 tetap diperiksa saat calon fill.

Gunakan initiative_risk_multiplier=1.5 agar initiative tanpa exhaustion memakai risiko 1,5 kali risiko dasar. Bandingkan tiap versi dengan baseline asal melalui replay penuh dan return closed equity per kuartal exit UTC, termasuk kuartal parsial terakhir.


## Eksperimen entry INJUSDT dan AAVEUSDC

Gunakan `inj_aave_entry_variants_grid.json` dengan runner Phase 2 dan periode
pembanding 1 Oktober 2025 sampai 31 Oktober 2026 UTC. Grid berisi lima replay
per aset. Kontrol C memakai profil 24H, flow 3m/12m, RM-A, exit 48H,
volume P75, maksimal satu fill per hari UTC, dan risiko geometrik 1%.

- `breakout_volume_percentile=90` mengganti ambang volume breakout P75
  dengan P90 dari 24 candle 1H sebelum candle breakout.
- `entry_filter=path_p5` menambahkan ambang minimum efisiensi P5 ke kontrol C.
- `breakout_continuation=volume_p75_twice` mewajibkan candle breakout 1H
  dan candle 1H berikutnya sama-sama mencapai P75. Ambang setiap candle
  berasal dari lookback selesai sebelum candle yang dinilai.
- `breakout_continuation=structure_2_candles` mewajibkan high dan low lebih
  tinggi untuk long, atau high dan low lebih rendah untuk short, relatif
  terhadap candle breakout 1H. Periksa dahulu close candle 15m pertama
  setelah breakout. Jalur ini juga mewajibkan body searah dan minimal
  sebesar body candle breakout 1H. Bila jalur 15m tidak lolos, evaluasi
  kembali pada close 1H berikutnya memakai syarat high dan low.

Untuk dua variasi kelanjutan, observasi order flow dimulai setelah close
candle yang meloloskan setup. POC profil awal tetap menjadi stop. Sentuhan
POC sebelum konfirmasi membatalkan setup. Candle kelanjutan perlu jalur
menit lengkap. Audit flow memuat waktu close breakout pertama, waktu
setup lolos, dan jalur 15m atau 1H.



## Eksperimen satu add saat stop mengunci +1R

Gunakan `pyramid_add=true` dan `pyramid_balance=lock_1r` dengan tangga stop
RM-A, RM-D, atau RM-E dan exit baseline. Saat harga pertama kali mencapai setidaknya +2R
dan stop naik ke +1R atau lebih tinggi, jadwalkan satu add untuk print
berikutnya. Print tersebut harus berada di sisi aman dari stop aktif.
Kuantitas add dibatasi 80% kuantitas awal, 80% risiko awal sebelum biaya,
dan sisa kapasitas leverage. Add memakai stop serta full exit posisi
awal dan tidak mempunyai TP1 sendiri. Mode ini mendukung sizing
`initiative_risk_multiplier` pada entry awal.

Preset UNIUSDC ada di `uniusdc_profit_add_grid.json`. Konfigurasi dan
parameter eksekusinya disalin dari `runs/uniusdc_p85_p5_init15_lock1r_v01_add/run.json`.
