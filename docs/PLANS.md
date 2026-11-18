# Paket layanan — 18 November 2026 WIB

Menu **Paket layanan** menampilkan paket ISP yang sedang login, pencarian nama, pagination 10/25/50, harga bulanan sebelum pajak/diskon, versi, dan jumlah pelanggan dengan tautan filter pelanggan. Akun berizin `customers` boleh membaca; owner memperoleh `manage_plans` untuk membuat/mengubah. Peran kustom dapat diberi `manage_plans` dengan prasyarat `customers`. Ops/keuangan bawaan hanya membaca; kasir/NOC/teknisi/pelanggan tidak mendapat menu maupun endpoint pengelolaan.

## Membuat atau mengubah

1. Pilih **Tambah paket** atau **Ubah**, lalu isi nama, kecepatan dan harga.
2. Pilih **Tinjau perubahan**. Belum ada penyimpanan. Bandingkan rincian saat ini dan usulan serta jumlah pelanggan aktif/diisolir/pending pada saat halaman ditinjau.
3. Centang persetujuan, lalu pilih **Konfirmasi paket**. MFA harus telah diverifikasi dalam lima menit terakhir. Jika kedaluwarsa, lakukan verifikasi ulang lalu isi/tinjau kembali formulir; POST tidak diputar otomatis.

Pratinjau bertanda tangan berlaku 15 menit, terikat akun/tenant/versi akses, berisi rincian sebelum/sesudah dan versi paket. Tanda tangan menjaga integritas, bukan enkripsi. Tidak ada password atau faktor autentikasi di dalamnya. Browser mengirim rincian final melalui token; mengganti field harga tambahan pada konfirmasi tidak mengganti usulan yang ditinjau.

Konfirmasi memeriksa izin terkini, status akun, MFA, versi akses dan paket di dalam transaksi. Dua perubahan bersamaan dari versi yang sama menghasilkan satu penyimpanan dan satu penolakan stale. Konfirmasi paket baru memakai UUID persisten agar replay pratinjau yang sama tidak membuat paket/audit ganda; pratinjau edit yang sudah disimpan menjadi stale. Perubahan tanpa selisih tidak menaikkan versi. Perubahan dan audit sebelum/sesudah atomik, terkait correlation ID yang sama.

Nama dibersihkan dari spasi tepi, 1–80 karakter tanpa karakter kontrol. Nama yang sama tanpa membedakan kapital ditolak oleh jalur pengelolaan aplikasi dalam tenant; pemeriksaan diserialisasi melalui tenant. Nama lama duplikat tidak digabung/dihapus oleh migrasi; tidak ditambahkan constraint nama di database. Kecepatan integer 1–100.000 Mbps, harga integer Rp1–Rp100.000.000. Field tenant/versi internal berasal dari server. Kecepatan merupakan informasi katalog dan belum mengatur profil router.

## Dampak harga

- Tagihan yang sudah terbit beserta baris, pajak, diskon dan pembayaran mempertahankan snapshot semula.
- Faktur berikutnya menggunakan rincian paket terkini, termasuk pelanggan lama yang memakai paket tersebut. Tidak ada jadwal efektif, prorata, atau perubahan harga per pelanggan.
- Batch billing yang sudah disetujui mempertahankan snapshot persetujuannya, walau worker baru berjalan sesudah harga berubah.
- Pratinjau billing/impor CSV dan kutipan aktivasi PSB yang belum disetujui diperiksa ulang pada konfirmasi. Perubahan rincian yang relevan membuatnya stale; muat ulang/buat pratinjau baru.
- Jumlah pelanggan pada halaman tinjauan adalah informasi saat dibaca dan dapat berubah. Menyimpan paket tidak menerbitkan faktur, menugaskan ulang pelanggan atau membuat perintah router.

Paket baru langsung tersedia dalam formulir pelanggan, PSB dan template/referensi impor. Penghapusan permanen, katalog multi-layanan, profil MikroTik/RADIUS nyata, histori harga terjadwal dan pemberitahuan pelanggan belum tersedia.

## Arsip dan aktifkan kembali

Daftar membuka filter **Aktif** secara default; pilih **Arsip** atau **Semua** untuk melihat paket lama. Akun berizin `customers` dapat membacanya. Akun dengan tambahan `manage_plans` dapat memilih **Arsipkan** atau **Aktifkan kembali**, meninjau rincian/dampak, mencentang persetujuan dan mengonfirmasi dengan MFA lima menit terakhir. GET tinjauan tidak menyimpan perubahan.

Arsip hanya membatasi penjualan/penugasan baru. Pelanggan baru, impor CSV create-only dan PSB baru harus memilih paket aktif. Pelanggan atau PSB yang sudah menggunakan paket arsip boleh mempertahankannya ketika identitas diedit; beralih ke paket arsip lain ditolak. PSB lama masih dapat diaktivasi, termasuk pembuatan pelanggan/faktur awalnya. Replay pembuatan PSB yang sudah berhasil tetap mengembalikan pesanan semula. Paket yang diaktifkan kembali mempunyai ID, nama dan harga yang sama serta kembali tersedia di formulir baru.

Pelanggan lama tetap ditagih, termasuk oleh jadwal bulanan. Arsip tidak membatalkan jadwal, mengganti harga/kecepatan, memindahkan pelanggan, mengubah faktur/pembayaran/snapshot batch yang disetujui, atau membuat pekerjaan jaringan/pesan. Paket arsip tetap tersedia untuk filter pelanggan/billing agar data lama dapat ditemukan. Jumlah pelanggan/PSB pada tinjauan adalah informasi saat dibaca, bukan daftar penerima yang dibekukan.

Token tinjauan 15 menit terikat aktor, tenant, versi akses, revisi MFA, versi serta rincian/status paket dan **tujuan status eksplisit**. Setelah MFA ulang, kembali ke tindakan semula dan tinjau lagi. Konfirmasi mengunci tenant, membership dan paket, memeriksa izin/MFA terkini, lalu mengubah hanya `is_active` dan `version` bersama audit metadata. Token stale/replay tidak menghasilkan perubahan/audit kedua; status yang sudah sama tidak menambah versi/audit. Perubahan harga dan status memakai versi paket yang sama sehingga tinjauan lama saling menjadi stale. Kunci tenant menyerialisasi arsip terhadap penugasan pelanggan, impor dan PSB baru; pemeriksaan diulang setelah validasi form. Akses admin langsung ke database tetap di luar kontrak jalur aplikasi ini.

## Migrasi ketersediaan 0012

`0012_plan_availability` menambah `is_active=True` berindeks. Semua paket lama tetap aktif. SQLite membangun ulang tabel untuk field wajib ini; operasi migrasi khusus mempertahankan high-water `sqlite_sequence`, termasuk ID tinggi yang pernah dihapus. PostgreSQL memakai operasi AddField biasa. Tidak ada perubahan dependency atau transformasi nilai lama.

Hentikan penulis dan watcher sebelum migrasi database tersimpan, autentikasi backup dengan kunci lama, lalu uji restore/migrasi pada salinan baru. Bandingkan seluruh baris menggunakan proyeksi kolom lama, foreign key dan sequence; recorder hanya boleh menambah satu baris 0012. Pada checkpoint ini pemeriksa baca-saja SQLite dapat diulang dengan **nama output baru**:

```bash
.venv/bin/python scripts/plan_archive_migration_check.py --database data/tagihnet.sqlite3 --baseline data/evidence/plan-archive-before-0012.json --sequences-before data/evidence/plan-archive-sqlite-sequences-before-0012.json --output data/evidence/plan-archive-recheck-baru.json
```

Baseline privat tersebut khusus instance ini dan diambil ketika penulis berhenti. Setelah ada perubahan bisnis yang sah, perbandingan terhadap baseline lama tentu tidak lagi berlaku. Simpan backup/kunci lama dan buat backup 0012 baru yang diuji restore. Jangan menjalankan reverse migration atau mem-pin image 0011 sebagai rollback otomatis: gate image lama menolak recorder 0012. Gunakan target recovery baru dari snapshot yang sesuai dan rekonsiliasi kejadian setelah snapshot; [runbook](RELEASE-RUNBOOK.md) menjelaskan batasnya.

30 tes baru mencakup hak akses, tenant, MFA, checkbox/CSRF, expiry/stale/replay, audit rollback, pembatasan penugasan, tagihan/PSB lama, dua koneksi bersamaan serta migrasi nonkosong dengan ID tinggi terhapus. Total **664 SQLite dan664 PostgreSQL** lulus. Chromium arsip/aktifkan kembali memeriksa empat viewport/dark mode, pilihan baru vs lama, replay dan fingerprint pelanggan/ledger yang tetap. Bukti: [desktop](evidence/plan-archive-desktop.png), [mobile](evidence/plan-archive-mobile.png). Hasil operasional lengkap dicatat di [TEST-REPORT](TEST-REPORT.md).

## Migrasi dan verifikasi awal — historis 14 November

Migrasi `0008_plan_review_version` menambahkan `version=1` serta `creation_key` nullable unik. Paket lama mempertahankan nama/kecepatan/harga dan `creation_key=NULL`. Sebelum migrasi pada database berisi data, hentikan penulis terkait, buat backup konsisten dan uji migrasi pada salinan. Database demo baru instance 14 November telah diuji melalui backup/salinan sebelum menerapkan migrasi; ledger/MFA dan rincian paket tetap cocok.

27 tes tambahan; total **272 pada masing-masing SQLite dan PostgreSQL**, tanpa gagal/skip. Dua belas skenario dua proses PostgreSQL mencakup konfirmasi paket baru ganda dan dua edit bersaing, disusul restore seluruh tabel termasuk versi/UUID paket. Browser terisolasi memeriksa buat/pratinjau/checkbox/konfirmasi/replay, edit harga, ledger lama, pencarian, scope dan empat viewport. Bukti: [daftar desktop](evidence/plans-list-desktop.png), [tinjauan desktop](evidence/plans-review-desktop.png), [tinjauan mobile](evidence/plans-review-mobile.png). Tidak ada faktor MFA dalam bukti. Lihat [TEST-REPORT](TEST-REPORT.md); hasil CI remote dan kesiapan produksi belum ditetapkan.
