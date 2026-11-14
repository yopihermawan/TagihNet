# Audit UX — fondasi TagihNet

Input adalah observasi UI pada brief. Tidak dilakukan akses ulang demo KelolaNet atau audit source referensi. Semua aset TagihNet dibuat sendiri, semua pelanggan fiktif.

| Observasi / kebutuhan | Perubahan TagihNet | Bukti / batasan |
| --- | --- | --- |
| Navigasi terlalu banyak | Hanya modul yang punya alur aktif; menu berizin, pencarian menu, sidebar responsif | Screenshot dashboard; modul pending di FEATURE-MATRIX |
| Aksi tabel mudah keluar layar | Kolom aksi sticky di kanan, detail sekunder di halaman detail, area scroll lokal berlabel | Screenshot pelanggan; cek 360/768/1366/1920 tanpa overflow halaman |
| Ukuran halaman dan rentang tidak konsisten | Paginator Django satu sumber state, 10/25/50, GET query tersimpan di URL | Backend tests + browser size=25/filter/return list |
| Widget koneksi bisa ambigu | Data router nyata dinyatakan belum tersedia; monitoring contoh terpisah, status layanan vs perintah job dipisahkan | Dashboard/network menyatakan adapter simulasi |
| Pembayaran vs faktur lunas bercampur | Metrik berbeda menurut tanggal pembayaran/periode layanan; piutang seluruh periode | Definisi pada kartu + tenant/source/update WIB |
| Aksi faktur terlalu ramai | Satu CTA “Buat faktur”; detail berisi pembayaran, histori, PDF; reversal memerlukan alasan dan konfirmasi | Browser full workflow; massal belum dibuat |
| Footer dapat berisi markup | Footer default teks, autoescape web dan escape teks PDF | Tes markup aman; tidak ada renderer HTML |
| Peta tidak dapat dimuat | Tautan pencarian OpenStreetMap, tanpa ketergantungan WebGL | Detail pelanggan; belum ada koordinat tervalidasi |
| Data demo tidak konsisten | Relasi paket wajib, invoice positif, price/tax snapshot, dua ISP seed | Seed ulang idempoten, tes relasi/tenant |
| Identitas dan visual | Logo SVG, navy/teal, kartu netral, angka tabular, mode terang/gelap | Aset lokal tanpa font/ikon eksternal |

## Bukti visual data fiktif

- [Login desktop](evidence/login-desktop.png) dan [login mobile](evidence/login-mobile.png).
- [Dashboard desktop](evidence/dashboard-desktop.png), [mobile 360 px](evidence/dashboard-mobile.png), dan [mode gelap](evidence/dashboard-dark.png).
- [Pelanggan desktop](evidence/customers-desktop.png) dan [faktur desktop](evidence/invoice-desktop.png).

## Aksesibilitas dan pemeriksaan responsif

HTML memakai landmark/label form, skip link, error form framework, status live, fokus terlihat, keyboard login, tombol password dengan aria-pressed, serta navigasi mobile dengan inert saat tertutup dan Escape kembali ke pemicu. Konfirmasi sensitif memakai dialog native browser. Status selalu memiliki teks. Mobile tombol utama sekitar 44 px. Tidak ada klaim pengujian screen-reader atau sertifikasi WCAG lengkap.

Browser smoke memeriksa luapan horizontal seluruh halaman pada empat viewport untuk dashboard, pelanggan, faktur, jaringan, dan setelan. Tabel kompleks menggunakan scroll lokal dengan petunjuk mobile dan sticky action. Kondisi empty/filter diuji di backend dan browser. Kegagalan jaringan ditampilkan sebagai notice offline; error form/izin tidak disamarkan sebagai keberhasilan.

Target WCAG 2.2 AA memerlukan audit lanjutan semua pasangan kontras, zoom/reflow, screen reader, keyboard lengkap, forced colors, dan alur error. Warna utama teal `#0F766E` pada putih mempunyai kontras memadai untuk teks normal; itu tidak membuktikan semua komponen lulus. Loading network lambat, peta provider gagal, dan perangkat sentuh nyata belum diuji secara menyeluruh.

## Checkpoint akses

MFA menambahkan QR lokal dan fallback kunci manual, faktor kedua sebelum workspace, kode pemulihan yang tampil sekali, serta verifikasi ulang sebelum perubahan akses. Akun & peran hanya muncul untuk owner. [Akses desktop](evidence/access-desktop.png), [akses mobile](evidence/access-mobile.png), [keamanan akun](evidence/account-security.png), dan [tantangan MFA](evidence/mfa-challenge.png) memakai data fiktif; QR, secret, dan recovery codes tidak direkam. Empat viewport diuji untuk akun/peran/form/security selain layar billing.

## Checkpoint faktur massal

Tindakan massal dipisahkan dari Buat faktur satuan. Preview menunjukkan jumlah pelanggan, nominal baru, invoice terbit dan dilewati, dengan rincian harga/diskon/pajak/jatuh tempo dan checkbox persetujuan. Status menunggu/diproses/selesai/dibatalkan tampil berbeda; pengguna dapat memuat ulang progres dan membatalkan sisa pekerjaan tanpa menghapus invoice. Stale/expiry meminta preview baru.

Bukti: [preview desktop](evidence/bulk-preview-desktop.png), [preview mobile](evidence/bulk-preview-mobile.png), [selesai](evidence/bulk-completed-desktop.png). Browser memeriksa 360/768/1366/1920 px tanpa luapan halaman; tabel memakai gulir lokal. Caption filter berbahasa Indonesia dan tombol Buat pratinjau menjelaskan bahwa belum ada invoice terbit. WCAG/screen-reader penuh tetap belum diverifikasi.

## Checkpoint operasional

Tiket memisahkan prioritas, status dan target SLA; label catatan status menyatakan terlihat pelanggan, sementara checkbox komentar internal jelas. Teknisi melihat daftar tugas sendiri dengan identitas/lokasi minimum tanpa nominal. Portal hanya menampilkan event publik. PSB menampilkan tahap dan jadwal, lalu quote harga awal sebelum persetujuan MFA; status administrasi/provisioning dipisah.

Bukti: [tugas teknisi](evidence/technician-ticket-desktop.png), [portal tiket mobile](evidence/customer-ticket-mobile.png), [tiket selesai](evidence/ticket-closed-desktop.png), [quote PSB](evidence/psb-activation-preview.png), [PSB administrasi selesai](evidence/psb-activated-desktop.png). Empat viewport 360/768/1366/1920 diuji tanpa luapan seluruh halaman. Lampiran dan notifikasi ditandai belum tersedia; kontrol terminal tidak ditampilkan sebagai aksi aktif. Audit screen reader/WCAG penuh tetap belum.

## Pengingat dan jadwal ulang PSB

Form edit menjelaskan bahwa perubahan pada Terjadwal membatalkan jadwal dan kembali Survei. Jadwal ulang memerlukan alasan dan menampilkan waktu lama/baru pada timeline. Kontrol hanya muncul untuk tahap/peran yang berlaku. [PSB dijadwalkan ulang](evidence/psb-rescheduled-desktop.png).

Pengingat menyatakan Tidak ada pesan nyata dikirim, nonaktif bawaan, memakai persetujuan eksplisit dan status Menunggu/Tercatat simulasi/Dilewati/Tertahan/Gagal. Detail menunjukkan attempts, alasan, retry dan isi plaintext saat pemrosesan. [Receipt desktop](evidence/reminder-detail-desktop.png) dan [antrean mobile](evidence/reminders-mobile.png). Browser memeriksa empat viewport, tanpa overflow halaman; tabel lebar memakai scroll lokal agar alasan tidak membungkus menjadi baris terlalu tinggi. Audit WCAG/screen reader menyeluruh tetap belum dilakukan.

## Monitoring jaringan simulasi

Ringkasan menyebut sumber, ISP, waktu, umur maksimum dan jumlah NAS yang masuk/dikecualikan. Data belum diketahui tampil sebagai teks, sedangkan nol hanya dari snapshot berhasil. Label Segar/Kedaluwarsa/Gagal/Persetujuan tertahan/Nonaktif memisahkan kualitas data dari status layanan. Snapshot lama tetap bertanggal dan berlabel histori, tanpa dijumlahkan sebagai data terkini. Tombol Muat ulang status tidak mengambil data; POST Ambil snapshot dibatasi interval.

Bukti: [snapshot segar](evidence/monitoring-fresh-desktop.png), [gagal dengan histori](evidence/monitoring-failed-history.png), [kedaluwarsa](evidence/monitoring-stale-desktop.png), [mobile](evidence/monitoring-mobile.png). Empat viewport diuji pada list/detail/form. NOC tidak melihat kontrol konfigurasi atau billing. Pengujian screen reader/WCAG menyeluruh dan perangkat nyata tetap belum.

## Impor pelanggan CSV

Alur upload → preview → review/MFA → hasil menjelaskan bahwa upload belum menambah pelanggan. Header/template/paket ISP dan batas 200 baris/128 KiB ditampilkan. Telepon/username yang dinormalisasi dapat ditinjau; kesalahan per baris menghalangi seluruh konfirmasi. Status stale/expired meminta upload ulang. Hasil pending, tanpa faktur/akun portal/router dijelaskan sebelum persetujuan. Riwayat privat dan laporan CSV dapat diunduh dengan izin yang sama.

Bukti: [preview desktop](evidence/customer-import-preview.png), [selesai](evidence/customer-import-completed.png), [mobile](evidence/customer-import-mobile.png). Browser memeriksa 360/768/1366/1920 px tanpa overflow halaman. Tabel punya scroll lokal dan hasil sticky; lebar hasil mobile disesuaikan agar identitas masih terlihat. Audit WCAG/screen reader lengkap belum dilakukan.

## Penagihan terjadwal — 14 November 2026 WIB

Halaman jadwal opt-in menampilkan cakupan/status/filter, review jumlah/nominal contoh dan tiga pasangan tanggal penerbitan/jatuh tempo. Nama paket/status/bulan menggunakan label Indonesia, bukan ID/kode internal. Penjelasan membedakan contoh saat review dari data dinamis saat worker berjalan, menyatakan current-month-only dan akibat mengubah/nonaktifkan jadwal. Riwayat menampilkan versi/cakupan/nominal/status dan hanya menautkan batch milik pembaca. Tabel bergulir lokal, empat viewport tanpa overflow seluruh halaman. Bukti billing-schedule-review-desktop.png dan billing-schedule-mobile.png memakai fixture fiktif; halaman tidak dianggap scheduler produksi.
