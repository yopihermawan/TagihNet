# Audit UX — fondasi TagihNet

Input adalah observasi UI pada brief. Tidak dilakukan akses ulang demo KelolaNet atau audit source referensi. Semua aset TagihNet dibuat sendiri, semua pelanggan fiktif.

| Observasi / kebutuhan | Perubahan TagihNet | Bukti / batasan |
| --- | --- | --- |
| Navigasi terlalu banyak | Hanya modul yang punya alur aktif; menu berizin, pencarian menu, sidebar responsif | Screenshot dashboard; modul pending di FEATURE-MATRIX |
| Aksi tabel mudah keluar layar | Kolom aksi sticky di kanan, detail sekunder di halaman detail, area scroll lokal berlabel | Screenshot pelanggan; cek 360/768/1366/1920 tanpa overflow halaman |
| Ukuran halaman dan rentang tidak konsisten | Paginator Django satu sumber state, 10/25/50, GET query tersimpan di URL | Backend tests + browser size=25/filter/return list |
| Widget koneksi bisa ambigu | Tidak menampilkan angka online yang belum tersedia; status layanan vs perintah job dipisahkan | Dashboard/network menyatakan adapter simulasi |
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
