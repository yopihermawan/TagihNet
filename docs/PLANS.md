# Paket layanan — 14 November 2026 WIB

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

Paket baru langsung tersedia dalam formulir pelanggan, PSB dan template/referensi impor. Penghapusan/pengarsipan paket, katalog multi-layanan, profil MikroTik/RADIUS nyata, histori harga terjadwal dan pemberitahuan pelanggan belum tersedia.

## Migrasi dan verifikasi

Migrasi `0008_plan_review_version` menambahkan `version=1` serta `creation_key` nullable unik. Paket lama mempertahankan nama/kecepatan/harga dan `creation_key=NULL`. Sebelum migrasi pada database berisi data, hentikan penulis terkait, buat backup konsisten dan uji migrasi pada salinan. Database demo baru instance 14 November telah diuji melalui backup/salinan sebelum menerapkan migrasi; ledger/MFA dan rincian paket tetap cocok.

27 tes tambahan; total **272 pada masing-masing SQLite dan PostgreSQL**, tanpa gagal/skip. Dua belas skenario dua proses PostgreSQL mencakup konfirmasi paket baru ganda dan dua edit bersaing, disusul restore seluruh tabel termasuk versi/UUID paket. Browser terisolasi memeriksa buat/pratinjau/checkbox/konfirmasi/replay, edit harga, ledger lama, pencarian, scope dan empat viewport. Bukti: [daftar desktop](evidence/plans-list-desktop.png), [tinjauan desktop](evidence/plans-review-desktop.png), [tinjauan mobile](evidence/plans-review-mobile.png). Tidak ada faktor MFA dalam bukti. Lihat [TEST-REPORT](TEST-REPORT.md); hasil CI remote dan kesiapan produksi belum ditetapkan.
