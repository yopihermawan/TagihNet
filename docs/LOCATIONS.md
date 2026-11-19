# Lokasi pelanggan dan PSB — 19 November 2026 WIB

Koordinat WGS84 kini dapat diisi secara manual. Lintang berada pada−90…90 dan bujur−180…180, maksimal enam angka desimal. Keduanya harus diisi atau dikosongkan bersama. **Kosong berbeda dari0,0**: data lama tetapNULL, tanpa titik yang ditebak dari alamat. NaN/infinity, boolean/float pada fungsi domain, nilai di luar batas dan presisi berlebihan ditolak; database juga memeriksa pasangan serta rentang.

## Akses dan alur

- Operator berizin `customers`+`edit_customers` mengubah lokasi melalui tab **Lokasi → Ubah koordinat pelanggan**. Editor ini terpisah dari identitas/paket agar kedua penyimpanan tidak menimpa field yang tidak sedang dikerjakan.
- Owner/operator berizin `installations` dan teknisi pada PSB yang ditugaskan dapat mengubah koordinat PSB sampai tahap Terpasang. PSB Teraktivasi/Dibatalkan terkunci. Teknisi tetap tidak mendapat editor/daftar pelanggan global atau hak aktivasi finansial.
- Teknisi membaca koordinat pelanggan pada tiket yang ditugaskan. Pembacaan detail memeriksa kembali scope sebelum menampilkan lokasi; pekerjaan teknisi lain ditolak404. Ini scope penugasan, bukan pembatasan wilayah geografis.
- Portal membaca lokasi layanan sendiri, tanpa editor. Preset kasir/NOC tidak mendapat lokasi global melalui endpoint baru; operator berizin baca pelanggan tetap dapat melihat tab Lokasi.

Editor memakai CSRF dan token bertanda tangan15menit yang terikat aktor/tenant/objek/versi akses/revisi MFA serta pasangan koordinat awal. Pada PSB, token juga memuat versi dan teknisi yang ditugaskan. Form lama ditolak409, akses/assignment yang hilang ditolak403/404, bukan dipaksakan tersimpan. Token bukan enkripsi dan dapat memuat koordinat; jangan membagikannya. Tidak ada faktor autentikasi di token. Operator mengikuti MFA login yang sudah wajib; fitur ini tidak menambah step-up lima menit pada setiap perubahan lokasi.

Transaksi mengunci tenant→membership→objek, membaca ulang izin/scope, lalu hanya menulis dua koordinat. Update PSB juga menaikkan versi dan waktu pembaruan. Membership memakai NO KEY UPDATE agar FK penugasan tidak menimbulkan urutan lock terbalik. Audit/timeline hanya mencatat bahwa lokasi diperbarui/dikosongkan, tanpa angka koordinat atau token. Gagal audit membatalkan perubahan. No-op tidak menambah audit/versi; database sibuk mempertahankan token lama dan meminta muat ulang.

Pelanggan memakai fingerprint nilai saat ini, bukan counter historis: perubahan yang kemudian dikembalikan persis ke nilai awal tidak dapat dibedakan. PSB mempunyai counter versi yang sudah ada. Perubahan identitas/status layanan tidak ditulis kembali oleh editor koordinat. Sebaliknya, form pelanggan mengambil row terbaru sebelum menyimpan field identitas sehingga tidak mengembalikan koordinat lama.

## Peta dan aktivasi

Halaman menampilkan angka koordinat serta tautan HTTPS dengan host tetap **OpenStreetMap**. Tidak ada iframe, tile, geocoder, WebGL, tracking GPS browser atau permintaan peta otomatis. Tautan baru mengirim koordinat ke penyedia peta hanya ketika pengguna membukanya; bila kosong, pencarian alamat tetap tersedia dan menyertakan alamat. Link memakai `noopener noreferrer`. Data disebut input pengguna, bukan hasil verifikasi lapangan/ketepatan alamat.

Pratinjau aktivasi menampilkan lokasi yang akan diteruskan. Koordinat dan versi PSB termasuk fingerprint persetujuan; perubahan lokasi membuat quote lama stale. Aktivasi menyalin koordinat ke pelanggan baru dalam transaksi yang sama dengan faktur/job. Replay tetap menghasilkan pelanggan/faktur semula. Setelah aktivasi, koordinat pelanggan dapat diperbarui oleh operator berwenang tanpa mengubah snapshot PSB, harga atau ledger. Invoice/PDF, CSV impor/ekspor dan foto tidak memperoleh kolom/geotag baru; tidak ada ekstraksi koordinat dari EXIF.

## Migrasi0013

`0013_location_coordinates` menambah dua DecimalField nullable pada Customer dan Installation, serta enam CHECK untuk pasangan/rentang. Tidak ada PostGIS atau dependency baru. Nilai lama tidak diubah, status/versi PSB lama tetap. SQLite membangun ulang tabel saat menambah constraint; `AddLocationConstraint` mempertahankan sequence high-water, termasuk ID tinggi yang pernah dihapus dan kondisi tabel kosong tanpa sequence. PostgreSQL memakai operasi constraint standar. Uji migrasi berisi customer/PSB/faktur lama, FK dan deleted-ID pada kedua backend.

Sebelum menerapkan pada data tersimpan: hentikan penulis, simpan dan autentikasi backup dengan kunci lama, restore ke tujuan baru, migrate salinan lalu rekonsiliasi proyeksi kolom lama/seluruh sequence/recorder/FK. Recorder hanya boleh menambah satu row0013; empat field baru harusNULL. Pada instance checkpoint ini pemeriksa baca-saja dapat dijalankan dengan output baru:

```bash
.venv/bin/python scripts/location_migration_check.py --database data/tagihnet.sqlite3 --baseline data/evidence/locations-before-0013.json --output data/evidence/locations-recheck-baru.json
```

Baseline privat khusus instance ini dan hanya berlaku sebelum ada edit bisnis berikutnya. Jangan menimpa baseline/arsip lama. Buat dan uji restore backup0013 sesudah migrasi. Image/schema0012 lama tidak boleh menjadi rollback otomatis setelah recorder0013; gunakan target recovery baru yang kompatibel, sesi baru/kunci MFA asli serta worker hold dan rekonsiliasi setelah snapshot. Reverse migration produksi tidak diuji.

## Bukti dan batas

31 tes baru membawa total menjadi725 pada masing-masing SQLite/PostgreSQL. Cakupan: nol/kosong/batas/finite/presisi/constraint, tipe token/expiry/stale/CSRF/aktor/tenant/revisi MFA/akses, no-op/audit rollback, update identitas/status bersamaan, scope teknisi dan portal, penugasan hilang sebelum respons, PSB terminal, quote stale/aktivasi copy/replay, dua penulis satu pemenang dan migrasi nonkosong dengan ID tinggi terhapus. Browser memakai data/DB/server/MFA sendiri, menguji alur pelanggan→PSB teknisi→quote lama→aktivasi, tidak ada request eksternal otomatis, empat viewport dan dark mode. [Desktop](evidence/customer-location-desktop.png) dan [mobile](evidence/customer-location-mobile.png) hanya data fiktif, tanpa faktor.

HTTPS/backup/restore dan rekonsiliasi runtime dicatat pada [TEST-REPORT](TEST-REPORT.md). Topologi ODC/ODP, geofencing/scope wilayah, pengukuran lapangan, geocoder, import/export koordinat, geotag foto dan produksi tetap belum tersedia. Ini bukan verifikasi akurasi posisi atau kepatuhan privasi menyeluruh.
