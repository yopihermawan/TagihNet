# Hak akses

Sumber enforcement: `core/permissions.py`, `core/views.py:allowed_invoice`, dan filter tenant pada query. Menu memakai capability yang sama. Tenant request diabaikan. Tidak ada administrator global yang otomatis dapat membaca semua ISP. Django superuser tanpa membership juga tidak mendapat akses aplikasi.

| Aksi | Owner | Operasional | Keuangan | Kasir | NOC | Pelanggan |
| --- | --- | --- | --- | --- | --- | --- |
| Dashboard billing | Ya | Ya | Ya | Ya | Tidak | Tidak |
| Daftar/detail pelanggan | Ya | Ya | Ya | Tidak | Tidak | Milik sendiri lewat portal |
| Tambah/ubah pelanggan | Ya | Ya | Tidak | Tidak | Tidak | Tidak |
| Daftar/detail faktur | Ya | Ya | Ya | Ya | Tidak | Milik sendiri |
| Terbitkan faktur/diskon | Ya | Ya | Tidak | Tidak | Tidak | Tidak |
| Pratinjau/konfirmasi/batal faktur massal | Ya, batch milik sendiri | Peran kustom bulk_issue diperlukan | Peran kustom bulk_issue diperlukan | Peran kustom bulk_issue diperlukan | Peran kustom bulk_issue diperlukan | Tidak |
| Catat pembayaran simulasi | Ya | Tidak | Ya | Ya | Tidak | Tidak |
| Reversal penuh + alasan | Ya | Tidak | Ya | Tidak | Tidak | Tidak |
| PDF | ISP sendiri | ISP sendiri | ISP sendiri | ISP sendiri | Tidak | Milik sendiri |
| CSV pelanggan | Ya | Ya | Ya | Tidak | Tidak | Tidak |
| Antrean jaringan | Ya | Tidak | Tidak | Tidak | Ya | Tidak |
| Setelan billing | Ya | Tidak | Tidak | Tidak | Tidak | Tidak |
| Audit | Ya | Tidak | Tidak | Tidak | Tidak | Tidak |
| Editor akun/peran | Ya, dengan MFA terbaru | Tidak | Tidak | Tidak | Tidak | Tidak |
| Keamanan akun / MFA | Milik sendiri | Milik sendiri | Milik sendiri | Milik sendiri | Milik sendiri | Milik sendiri |
| Secret integrasi / upload / webhook | Belum tersedia untuk semua peran | — | — | — | — | — |

Tabel di atas adalah preset awal. Owner dapat menetapkan peran kustom pada akun operator; capability efektif menggantikan preset pada setiap endpoint dan menu, termasuk PDF. `manage_access` tidak dapat didelegasikan. Owner/customer tidak boleh menerima peran kustom. Izin tulis memerlukan izin baca terkait. Membership dibaca dari database, bukan claim browser. Perubahan akun/role meningkatkan session_version sehingga sesi lama dicabut pada request berikutnya. Pemilik aktif terakhir dilindungi dalam transaksi. Editor membutuhkan MFA terverifikasi dalam 5 menit terakhir. Lihat [AUTHENTICATION](AUTHENTICATION.md).

Scope wilayah/penugasan untuk teknisi/sales belum tersedia; role tersebut belum dapat digunakan. Keuangan/operasional pada checkpoint ini melihat identitas pelanggan satu ISP; minimisasi field lebih lanjut perlu disepakati sebelum produksi. NOC hanya melihat nama pelanggan dalam antrean dan hasil simulasi, tanpa nominal/sandi layanan.

Worker tidak menerima tenant dari request pengguna; tiap job menyimpan tenant dan mengambil pelanggan dengan pasangan tenant+ID. Job silang tenant ditolak oleh lookup. Tidak ada cache bisnis bersama atau realtime pada fondasi ini. Database/shell administrator tetap batas kepercayaan; constraint lintas relasi yang lebih ketat dan audit tahan manipulasi perlu ditambahkan sebelum deployment.

## Batas penerbitan massal

`bulk_issue` adalah capability terpisah dari `issue`; prasyarat role kustom: `issue`, `invoices`, `customers`. Batch bersifat privat untuk pembuat dan tenant. Konfirmasi membutuhkan MFA <=5 menit dan versi sesi masih berlaku. Worker memeriksa kembali izin, user aktif dan session_version sebelum setiap baris; perubahan akses memblokir sisa pekerjaan. Lihat BULK-BILLING.md. Kepemilikan batch tidak mengubah izin biasa atas faktur yang sudah terbit.
