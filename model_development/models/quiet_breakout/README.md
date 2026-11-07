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

Dokumen ini adalah pernyataan awal. Definisi setup, konfirmasi entry, pembatalan, masa observasi, SL, TP, dan sizing masih perlu dirancang. Belum ada implementasi strategi atau hasil backtest quiet_breakout.

Implementasi berikutnya menggunakan satu strategy.py dalam folder ini dan runner observe/backtest/evaluate proyek yang sudah ada. Perhitungan reusable dan eksekusi mengikuti komponen bersama yang sesuai.
