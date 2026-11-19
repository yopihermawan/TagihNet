# Tinjauan setelan billing

Menu **Setelan billing** mengatur nama/alamat penerbit, tanggal jatuh tempo, pajak dalam basis poin dan footer teks. Izin `settings` tersedia untuk owner atau operator dengan peran kustom yang diberi izin tersebut; preset lain tidak memperolehnya. Tenant selalu berasal dari akun, bukan field request.

## Mengubah setelan

1. Buka **Setelan billing**, isi perubahan, lalu pilih **Tinjau perubahan**. Langkah ini belum menyimpan data.
2. Bandingkan nilai sebelum/sesudah, jumlah pelanggan/faktur saat dibaca serta contoh Rp100.000 sebelum pajak/diskon nol. Contoh memakai periode bulan berjalan WIB, termasuk clamp tanggal29–31 ke akhir bulan. Ini bukan faktur atau harga yang dibekukan.
3. Centang persetujuan dan pilih **Konfirmasi setelan billing**. MFA harus telah diverifikasi dalam lima menit terakhir. Jika perlu verifikasi ulang, kembali ke form baru dan tinjau lagi; POST lama tidak diputar otomatis.

Form dan tinjauan mempunyai tanda tangan dengan umur maksimum15menit masing-masing, terikat aktor/ISP/versi akses/revisi MFA serta fingerprint lima nilai awal. Form tanpa token tidak dapat menyimpan atau membuat tinjauan. Mengganti target/nilai tambahan saat konfirmasi tidak mengubah usulan bertanda tangan. Form atau tinjauan dari setelan yang sudah berubah ditolak409; muat ulang, bukan menimpa perubahan operator lain. Token asing403; token rusak/kedaluwarsa ditolak. Token bukan enkripsi dan memuat setelan komersial saat ditinjau; jangan membagikannya. Tidak ada faktor autentikasi di dalamnya.

Fingerprint membandingkan **state saat ini**, bukan rekam versi permanen: perubahan yang dikembalikan persis ke nilai semula tidak dapat dibedakan dari state awal. Tidak ada migrasi/version counter baru. Replay setelah penyimpanan yang mengubah nilai menjadi stale dan tidak menambah audit; no-op tidak mengubah data atau menambah audit.

## Dampak pada tagihan dan identitas

- Nominal, pajak, jatuh tempo, footer, pembayaran dan reversal faktur yang sudah terbit tetap memakai snapshot semula.
- Batch yang sudah disetujui mempertahankan snapshot meski worker berjalan setelah setelan berubah. Pratinjau batch dan kutipan aktivasi PSB yang belum disetujui diperiksa ulang dan menjadi stale jika rincian yang relevan berubah.
- Faktur yang baru disiapkan menggunakan pajak/jatuh tempo/footer terbaru. Jadwal bulanan yang aktif memakai setelan terbaru ketika menyiapkan snapshot siklus berikutnya; hari penerbitan, persetujuan dan status jadwal tidak diubah oleh penyimpanan setelan ini.
- Workspace memakai profil ISP terkini. Faktur baru menyimpan nama/alamat saat diterbitkan; kuitansi baru memakai identitas saat pembayaran dicatat. Cetak ulang HTML/PDF memakai snapshot masing-masing. Dokumen lama tanpa snapshot menampilkan profil terkini dengan penjelasan riwayat yang belum tersimpan, tanpa backfill. Batch yang disetujui mempertahankan nominal/footer/jatuh tempo, tetapi identitas penerbit diambil saat penerbitan aktual. [Kontrak lengkap](DOCUMENT-ISSUERS.md). File yang sudah diunduh tetap.
- Tidak ada faktur, penugasan pelanggan, perintah router atau pesan yang dibuat oleh penyimpanan setelan.

Pajak0 berarti tarif nol, bukan belum diatur; rentang0–10.000bp. Jatuh tempo1–31 mengikuti akhir bulan bila perlu. Nama ISP wajib1–100karakter, alamat wajib1–250, footer opsional0–1.000. Teks dinormalisasi NFC, spasi tepi dibuang, CRLF/CR menjadi LF; baris baru diizinkan untuk alamat/footer, karakter kontrol/format tersembunyi/surrogate ditolak. Markup/URL tetap teks yang di-escape pada renderer, bukan konten aktif.

## Linimasa tagihan

Di **Setelan billing**, pilih **Bulan contoh** lalu **Lihat contoh**. Tinjauan perubahan membandingkan tanggal sebelum dan sesudah berdasarkan bulan yang sama. Bulan contoh dipakai untuk tampilan, sementara setelan yang disetujui tetap berasal dari pratinjau yang ditandatangani.

- **Terbit.** Jadwal aktif dengan persetujuan yang masih berlaku memakai hari jadwal, dibatasi akhir bulan. Saat jadwal nonaktif atau tertahan, contoh mengasumsikan terbit manual pada awal bulan. Tanggal jadwal menunjukkan kapan faktur dapat mulai masuk antrean.
- **Pengingat.** Jika aktif dan berizin, tanggal paling awal adalah tanggal terbit contoh atau awal jendela pengingat, mana yang lebih akhir. Jam kebijakan mengikuti WIB. Pengingat dapat masuk antrean sekali sehari selama ada sisa tagihan. Status persetujuan tertahan dan skenario gangguan tampil di halaman.
- **Jatuh tempo dan tenggang.** Tanggal 29–31 dibatasi akhir bulan. Tambahan tenggang saat ini nol hari. Faktur dapat langsung menunggak jika diterbitkan setelah jatuh tempo dan masih bersaldo.
- **Isolir simulasi.** Untuk faktur contoh yang masih bersaldo, pemeriksaan dapat memperhitungkannya mulai hari setelah jatuh tempo atau tanggal terbit jika terbit lebih lambat. Worker memeriksa semua tunggakan pelanggan saat memproses job jaringan. Lewat jatuh tempo sendiri tidak membuat job.

Contoh dengan jadwal terbit 10 Februari 2024 dan pengingat tiga hari sebelumnya. Perubahan hari jatuh tempo dari 31 ke 20 menggeser jatuh tempo dari 29 Februari ke 20 Februari. Tanggal paling awal pengingat bergeser dari 26 ke 17 Februari. Tanggal faktur dapat diperhitungkan untuk isolir simulasi bergeser dari 1 Maret ke 21 Februari, jika job berjalan dan saldo masih tersisa.

Linimasa merangkum kebijakan ISP saat halaman dimuat. Jadwal terpisah, status worker, persetujuan, saldo dan faktur yang sudah terbit menentukan hasil operasional. Pengguna dengan izin pengelolaan terkait dapat membuka Jadwal bulanan atau Kebijakan pengingat dari linimasa.

## Transaksi dan kompatibilitas SLA

Konfirmasi mengunci tenant→membership, membaca ulang akun/izin/versi akses/revisi MFA serta membandingkan lima nilai awal. Hanya field yang berubah disimpan bersama audit metadata secara atomik. Audit gagal membatalkan perubahan. Isi nama/alamat/footer/token tidak dicatat dalam audit dan disamarkan pada laporan error produksi. Database sibuk tidak mengubah token menjadi form baru atau menimpa data; pengguna diminta memuat ulang.

Jalur **Target SLA tiket** juga membaca ulang akses di bawah lock yang sama dan hanya menulis empat field SLA. Dengan demikian penyimpanan SLA yang validasinya dimulai sebelum konfirmasi billing tidak mengembalikan nilai billing lama. Persetujuan MFA baru pada dokumen ini khusus setelan billing; alur SLA belum memperoleh review/MFA baru. Snapshot SLA tiket lama tetap berlaku.

Route lama `/settings/billing/` tetap menjadi halaman edit, tetapi POST sekarang menghasilkan tinjauan. Konfirmasi ada di `/settings/billing/confirm/`, hanya POST. Integrasi/form lama yang mencoba langsung menyimpan tanpa token/tinjauan harus diperbarui. Alur review awal memakai schema0012. Dukungan identitas historis berikutnya menambah schema0015 tanpa dependency baru. Akses SQL administrator di luar kontrak form aplikasi.

## Verifikasi dan batas

Checkpoint awal schema0012: 30 tes baru menambah total menjadi694 pada masing-masing SQLite/PostgreSQL. Targeted64 settings+operasional SQLite dan30 settings PostgreSQL lulus, termasuk race dua konfirmasi serta jalur SLA/billing aktual, stale/CSRF/checkbox/tenant/MFA/audit rollback, Unicode dan nilai batas, ledger/approved batch, PSB serta jadwal berikutnya. Chromium dengan DB/server/MFA sementara memeriksa tinjauan tanpa penyimpanan, SLA melalui HTTP ketika review terbuka, konfirmasi,409 form/review lama, ledger tetap, identitas penerbit terkini,360/768/1366/1920 dan dark mode. Bukti [desktop](evidence/billing-settings-review-desktop.png) dan [mobile](evidence/billing-settings-review-mobile.png) berisi data fiktif tanpa faktor.

Hasil HTTPS/backup/restore serta rollout lokal dicatat pada [TEST-REPORT](TEST-REPORT.md). Prorata tersedia untuk faktur pertama pada aktivasi PSB. Siklus prabayar/pascabayar fleksibel, tanggal efektif dan tarif berbeda per pelanggan masih tertunda. [FEATURE-MATRIX](FEATURE-MATRIX.md) memuat pekerjaan yang masih terbuka.

Pada schema0015, uji settings/batch memastikan dokumen lama yang memiliki snapshot tetap memakai identitas aslinya, sedangkan batch disetujui yang baru diterbitkan memakai penerbit saat terbit. Hasil terbaru: [TEST-REPORT](TEST-REPORT.md).
