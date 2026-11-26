# Status rilis TagihNet

Pada 23 November 2026 WIB, **staging lokal simulasi** menjalankan image yang telah lulus pemulihan Compose dan preflight aktif. Skema tetap `0018_network_recovery`. Digest aplikasi aktif adalah `372809b8aae7b9b5c0612824ea8e5243956c9ed29eac85e2b8895e7acadf48ff`. Clone pemulihan sehat dengan image sebelumnya dan worker tertahan. [Bukti pembaruan staging](TEST-REPORT.md#pembaruan-image-staging-utama)

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
| 16. Tampilan desktop/mobile, keyboard, dan kondisi gagal | Terverifikasi sebagian | 105 pemindaian axe, 20 pemeriksaan keyboard, dan tujuh skenario pohon aksesibilitas Chromium lulus. Uji ucapan pembaca layar desktop masih terbuka |
| 17. PDF A4 multipage, isi dan batas izin | Lulus lokal | Faktur panjang, kuitansi/reversal, readback nominal, dan tes lintas tenant |
| 18. Restore terenkripsi ke target kosong | Lulus lokal | SQLite dengan login MFA ulang, PostgreSQL dengan rekonsiliasi tabel, dan pemeriksaan paket clone staging |

## Bukti yang dijalankan ulang

- PostgreSQL lulus 930 tes. SQLite lulus 929 tes dengan satu tes penguncian PostgreSQL dilewati. Pada pengujian 22 November, lab PostgreSQL juga lulus 17 skenario lintas proses dan dua latihan pemulihan pada database terpisah. [Laporan pengujian](TEST-REPORT.md)
- Sepuluh alur Chromium memakai akun fiktif. Alur utama mencakup login TOTP, pelanggan, faktur, pembayaran simulasi, reversal, dan logout. `collectstatic`, faktur multipage dan kuitansi A4 lulus.
- Metadata PyPI untuk 16 versi runtime terkunci berhasil diperiksa. Tidak ada advisory aktif yang tercantum pada saat pemeriksaan. [Cakupan audit keamanan](SECURITY-AUDIT.md)
- Pemeriksaan 347 file teks terlacak pada snapshot aplikasi mencakup format kunci privat PEM dan pola token GitHub, AWS, serta Slack. Hasilnya nol kecocokan pada pola yang diperiksa.
- Laporan dan log privat tersedia di `data/evidence/local-release-20261122/`. Gunakan perintah pada runbook untuk membuat laporan baru ketika kode, image, atau backup berganti. [Runbook rilis staging](RELEASE-RUNBOOK.md)
- Pengujian portal Compose memverifikasi filter periode, paginasi, kuitansi reversal, dan isolasi akun. Pengukuran alternatif di lab PostgreSQL lulus 2.960 permintaan HTTPS dengan nol kegagalan. Ruang Docker mencapai nol sebelum pengukuran beban Compose. [Uji beban portal](STAGING-LOAD.md#portal-pelanggan-pada-rilis-23-november-2026-wib)

## Prasyarat rilis produksi

| Bagian | Dependensi yang perlu tersedia | Bukti penutupan |
| --- | --- | --- |
| Infrastruktur | Server staging/produksi dan domain, sertifikat TLS publik, PostgreSQL privat dan penyimpanan secret | Verifikasi target nyata, konfigurasi HSTS, isolasi jaringan, migrasi dan cutover yang direkonsiliasi |
| Pembayaran | Penyedia gateway, akun sandbox, identitas merchant dan webhook sesuai dokumentasi resmi | Tanda tangan, order dan nominal benar, replay dan kejadian tidak berurutan aman, ledger serta aktivasi cocok |
| Jaringan | Lab MikroTik/RADIUS yang diizinkan, akses privat dan akun dengan hak minimum | Sesi sebelum/sesudah isolir dan aktivasi, polling, IPv4/IPv6 laboratorium dan rekonsiliasi antrean |
| Pengiriman | SMTP untuk reset dan kanal pemberitahuan yang dipilih | Pengiriman melalui akun uji, pengulangan, deduplikasi, gagal kirim dan izin lingkungan |
| Operasi | Penyimpanan backup terpisah, aturan retensi, pemantauan dari host lain, dan anggaran kapasitas | Restore dari media penerima, hasil waktu pemulihan, beban dan failover terukur, audit keamanan independen |

Pemulihan Compose dari backup terpantau 23 November mencocokkan 62 tabel, 327 baris, metadata kolom, dan 57 sequence. Setelah image kandidat diterapkan pada staging utama, ruang Docker yang tersedia sekitar 1,85 GiB dan estimasi pemulihan 0,90 GiB. Matriks fitur mencatat pengembangan tersisa, termasuk siklus penagihan fleksibel, cakupan wilayah, dan portal lanjutan. [Prosedur estimasi ruang](STAGING-OPERATIONS.md#estimasi-ruang-pemulihan-docker) · [Matriks fitur](FEATURE-MATRIX.md)

Backup terpantau 26 November pukul 17.10 WIB lulus pemulihan ulang pada lab PostgreSQL. Jumlah baris, isi tabel, kolom, dan sequence cocok dengan staging sumber. Bukti privat tersedia di `data/evidence/monitored-restore-20261126.json`.
