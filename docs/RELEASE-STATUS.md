# Status rilis TagihNet

Pada 22 November 2026 WIB, kandidat **staging lokal simulasi** memakai skema `0018_network_recovery`. Pengujian aplikasi memakai snapshot `f0d20cc15a7ddab0652f7b7da755a077b0f51c8d`. Penyerahan ke `main` menambahkan laporan rilis dan cakupan CI untuk regresi browser yang sudah dijalankan. Image staging terpilih memakai digest aplikasi `5e7080f651854f61f16294c15253ea523cf7c84b899355b9cb9db96831087fab`.

## Hasil pengujian penerimaan

Nomor kriteria mengikuti brief awal. Istilah **Simulasi** menunjukkan alur yang benar-benar dijalankan dengan input fiktif di lingkungan lokal. [Brief awal](PRODUCT-BRIEF.md#8-pengujian-penerimaan)

| Kriteria | Hasil saat rilis lokal | Bukti |
| --- | --- | --- |
| 1. Login, logout, reset, timeout, dan pencabutan akses | Lulus lokal | Suite backend, login TOTP, browser keamanan dan ubah sandi |
| 2. Penolakan akses langsung | Lulus lokal | Tes izin endpoint, CSRF, aksi per peran, dan sesi dicabut |
| 3. Batas data dua ISP/pelanggan | Lulus untuk permukaan yang tersedia | HTTP, pencarian, PDF, ekspor, unggah privat, job. Kanal realtime belum tersedia |
| 4. Pencarian, filter, sorting, pagination, dan URL | Lulus lokal | Suite backend, workspace pelanggan, alur buka kembali daftar |
| 5. Simpan data pelanggan dan baca setelah reload | Lulus lokal | Browser pelanggan → faktur dan regresi backend |
| 6. Faktur unik setelah retry atau penerbitan bersamaan | Lulus lokal | PostgreSQL dua proses, overlap massal dan satuan, scheduler |
| 7. Rupiah, diskon, pajak, pembulatan, prorata, akhir bulan | Lulus lokal | Suite billing dan PSB, pratinjau tanggal, PDF |
| 8. Validasi autentikasi, nominal, mata uang, dan order webhook | Tertahan | Memerlukan gateway dan kontrak sandbox yang dipilih |
| 9. Callback sah yang diulang, bersamaan, atau terlambat | Tertahan | Replay ledger manual lulus. Kontrak callback menunggu gateway sandbox |
| 10. Pembayaran bersamaan dengan isolir | Lulus simulasi | PostgreSQL dua proses memeriksa ledger dan rekonsiliasi layanan |
| 11. Router gagal, riwayat pembayaran tetap benar | Lulus simulasi | Worker dan browser menguji kegagalan router, perbaikan skenario, dan retry |
| 12. Markup aman pada web dan dokumen | Lulus lokal | Encoding template, footer teks biasa, PDF tanpa tautan luar |
| 13. Validasi upload, impor, dan ekspor | Lulus lokal | Foto privat, CSV preview, scope tenant, batas ukuran, formula safe |
| 14. Aksi demo dibatasi pada simulasi | Lulus simulasi | Profil staging fail-closed, worker sintetis, dan guard server |
| 15. Metrik dashboard memiliki sumber dan penanda data lama | Lulus simulasi | Fixtures dashboard billing dan snapshot monitoring |
| 16. Tampilan desktop/mobile, keyboard, dan kondisi gagal | Terverifikasi sebagian | 88 pemindaian axe dan 20 pemeriksaan keyboard pada empat lebar dalam dua tema. Seluruh elemen yang ditandai untuk tinjauan kontras lulus pengukuran. Uji pembaca layar masih terbuka |
| 17. PDF A4 multipage, isi dan batas izin | Lulus lokal | Faktur panjang, kuitansi/reversal, readback nominal, dan tes lintas tenant |
| 18. Restore terenkripsi ke target kosong | Lulus lokal | SQLite dengan login MFA ulang, PostgreSQL dengan rekonsiliasi tabel, dan pemeriksaan paket clone staging |

## Bukti yang dijalankan ulang

- SQLite dan PostgreSQL masing-masing lulus 879 tes. Lab PostgreSQL juga lulus 17 skenario lintas proses dan dua latihan pemulihan pada database terpisah. Data SQLite dan profil browser uji berada di `/tmp`. [Laporan pengujian](TEST-REPORT.md)
- Sepuluh alur Chromium memakai akun fiktif. Alur utama mencakup login TOTP, pelanggan, faktur, pembayaran simulasi, reversal, dan logout. `collectstatic`, faktur multipage dan kuitansi A4 lulus.
- Metadata PyPI untuk 16 versi runtime terkunci berhasil diperiksa. Tidak ada advisory aktif yang tercantum pada saat pemeriksaan. [Cakupan audit keamanan](SECURITY-AUDIT.md)
- Pemeriksaan 347 file teks terlacak pada snapshot aplikasi mencakup format kunci privat PEM dan pola token GitHub, AWS, serta Slack. Hasilnya nol kecocokan pada pola yang diperiksa.
- Laporan dan log privat tersedia di `data/evidence/local-release-20261122/`. Gunakan perintah pada runbook untuk membuat laporan baru ketika kode, image, atau backup berganti. [Runbook rilis staging](RELEASE-RUNBOOK.md)

## Prasyarat rilis produksi

| Bagian | Dependensi yang perlu tersedia | Bukti penutupan |
| --- | --- | --- |
| Infrastruktur | Server staging/produksi dan domain, sertifikat TLS publik, PostgreSQL privat dan penyimpanan secret | Verifikasi target nyata, konfigurasi HSTS, isolasi jaringan, migrasi dan cutover yang direkonsiliasi |
| Pembayaran | Penyedia gateway, akun sandbox, identitas merchant dan webhook sesuai dokumentasi resmi | Tanda tangan, order dan nominal benar, replay dan kejadian tidak berurutan aman, ledger serta aktivasi cocok |
| Jaringan | Lab MikroTik/RADIUS yang diizinkan, akses privat dan akun dengan hak minimum | Sesi sebelum/sesudah isolir dan aktivasi, polling, IPv4/IPv6 laboratorium dan rekonsiliasi antrean |
| Pengiriman | SMTP untuk reset dan kanal pemberitahuan yang dipilih | Pengiriman melalui akun uji, pengulangan, deduplikasi, gagal kirim dan izin lingkungan |
| Operasi | Penyimpanan backup terpisah, aturan retensi, pemantauan dari host lain, dan anggaran kapasitas | Restore dari media penerima, hasil waktu pemulihan, beban dan failover terukur, audit keamanan independen |

Ruang bebas cloud saat validasi sekitar 1,3 GiB. Pengujian ini memakai image yang sudah dipin dan fixture terisolasi. Matriks fitur mencatat pengembangan tersisa, termasuk siklus penagihan fleksibel, cakupan wilayah, dan portal lanjutan. [Matriks fitur](FEATURE-MATRIX.md)

Latihan Docker penuh dari backup terpantau terbaru tertahan karena filesystem Docker habis saat membuat `pg_wal`. Pengukuran berikutnya memperkirakan 1,31 GiB untuk image dan volume, dengan sekitar 1,28 GiB tersedia. Pemulihan SQL dan halaman login HTTPS dari arsip yang sama lulus di lab PostgreSQL lokal dengan Gunicorn host. [Prosedur estimasi ruang](STAGING-OPERATIONS.md#estimasi-ruang-pemulihan-docker)

Kandidat dengan perbaikan aksesibilitas lulus uji Chromium lokal. Staging utama dan clone pemulihan masih sehat dengan image sebelumnya. Build dan penerapan image kandidat memerlukan ruang tambahan pada host ini atau host lain. [Laporan aksesibilitas](TEST-REPORT.md#aksesibilitas-alur-utama)
