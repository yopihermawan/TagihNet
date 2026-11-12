# Pemberitahuan perubahan halaman pembayaran

Halaman order yang masih **Menyiapkan checkout** atau **Menunggu pembayaran** memantau perubahan yang sudah tersimpan pada aplikasi. Saat webhook, worker, pemeriksaan manual, atau pembuatan checkout menyimpan hasil baru, halaman menampilkan **Ada pembaruan tersimpan** dan tautan **Tampilkan pembaruan**. Pengguna memilih tautan untuk memuat ulang rincian.

Halaman tidak dimuat ulang otomatis, sehingga alasan pengakhiran atau formulir lain yang belum dikirim tetap utuh. Pemberitahuan menyebut status tersimpan terbaru; rincian dan tombol lama baru diperbarui setelah pengguna memilih tautan. Seluruh tindakan pembayaran tetap memvalidasi ulang keadaan/izin di server.

## Batas pemantauan

- Satu pembacaan setiap lima detik, hanya saat tab terlihat dan browser melaporkan online. Batas dua menit dihitung sejak pemantauan dimulai, termasuk waktu tab tersembunyi/offline. Batas ini tidak dijeda atau diperpanjang otomatis.
- Pengguna dapat memilih **Hentikan pemantauan** atau **Pantau lagi**. Memulai lagi memberi jendela dua menit baru.
- Perubahan tersimpan menghentikan pemantauan dan menawarkan muat ulang. Order yang sudah tercatat, terminal atau perlu rekonsiliasi tidak memulai pemantauan baru saat halamannya dibuka.
- Request dibatasi delapan detik atau sisa jendela pemantauan, mana yang lebih singkat. Tidak ada request bersamaan. Kegagalan koneksi, sesi/akses, atau respons tidak valid menghentikan pemantauan tanpa loop retry atau klaim berhasil.
- Tab yang tersembunyi membatalkan request aktif. Navigasi meninggalkan halaman menghentikan pemantauan; jika browser memulihkan halaman dari cache navigasi, pengguna dapat memilih Pantau lagi.
- JavaScript nonaktif tetap menyediakan alur manual. Pemantauan tidak memanggil Midtrans, membuat checkout, mengirim ulang formulir, menjalankan worker, atau mencatat pembayaran.

Pemantauan halaman berbeda dari **Periksa status pembayaran** (POST verifikasi langsung ke Midtrans) dan **Pemeriksaan status otomatis** (worker opt-in). Jika belum ada webhook/worker/verifikasi baru, pemantauan halaman tidak menghasilkan status baru dari merchant. Selesainya jendela dua menit tidak membuktikan pembayaran gagal, kedaluwarsa atau berhasil.

## Akses dan sesi

`GET /payments/orders/<uuid>/snapshot/` memakai cakupan order yang sama dengan halaman detail: operator berizin faktur dapat membaca order ISP-nya, dan portal hanya dapat membaca faktur pelanggan yang terkait akunnya. Izin membuat pembayaran atau MFA step-up terbaru tidak diperlukan untuk pembacaan ini. Mode simulasi, sakelar checkout dan kredensial merchant tidak diperlukan untuk membaca bukti lama.

Sesi tetap harus lolos pemeriksaan akses, MFA revision, idle 30 menit dan durasi absolut delapan jam. Pembacaan latar belakang yang sah tidak memperbarui `last_seen`, menyimpan ulang sesi, atau memperpanjang cookie. Middleware sesi turunan mempertahankan perilaku Django untuk request biasa serta perubahan nyata pada sesi, termasuk logout/rotation; `SESSION_SAVE_EVERY_REQUEST` tetap berlaku di luar pembacaan pasif ini.

Endpoint menghasilkan JSON minimal berisi revision opaque, status/label dan flag untuk melanjutkan pemantauan. Tidak ada nominal, nama pelanggan, identitas merchant, checkout URL/token, alasan owner, payload/signature webhook, atau kredensial. Revision berubah saat snapshot order/hasil verifikasi/pembuatan checkout berubah; ini bukan waktu atau versi status provider. Respons menggunakan `no-store` dan `Vary: Cookie`. Tanpa sesi, ID dikenal dan tidak dikenal sama-sama menghasilkan JSON 401; akses tenant/customer lain menghasilkan 404, dan peran tanpa akses faktur/portal ditolak 403.

Client hanya mengambil endpoint aplikasi dengan origin yang sama, menolak redirect dan menampilkan label memakai `textContent`. CSP tetap `script-src 'self'` dan `connect-src 'self'`. Tidak ada SDK, dependency, migrasi atau layanan baru.

## Verifikasi

```bash
.venv/bin/python manage.py test core.tests.test_gateway_page_updates --verbosity 1
.venv/bin/python scripts/verify_gateway_watch_browser.py
.venv/bin/python scripts/gateway_smoke.py
```

Tes domain/HTTP memeriksa privasi, izin, kedaluwarsa/revocation, tanpa penulisan SQL/provider request, serta revision setelah status terverifikasi. Drill Chromium memakai virtual clock dan route fixture yang diintersep untuk batas lima detik/dua menit, stop/restart, controlled hidden/offline signals, response error dan timeout; tidak mengakses merchant. Smoke aplikasi nyata mengirim notifikasi terautentikasi ke server sementara dengan transport Midtrans tiruan, lalu memverifikasi pemberitahuan, isi formulir tetap utuh dan muat ulang eksplisit. Akun sandbox nyata dan HTTPS publik tetap perlu pengujian terpisah.
