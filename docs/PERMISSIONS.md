# Hak akses

Sumber enforcement: `core/permissions.py`, `core/views.py:allowed_invoice`, dan filter tenant pada query. Menu memakai capability yang sama. Tenant request diabaikan. Tidak ada administrator global yang otomatis dapat membaca semua ISP. Django superuser tanpa membership juga tidak mendapat akses aplikasi.

| Aksi | Owner | Operasional | Keuangan | Kasir | NOC | Pelanggan |
| --- | --- | --- | --- | --- | --- | --- |
| Dashboard billing | Ya | Ya | Ya | Ya | Tidak | Tidak |
| Daftar/detail pelanggan | Ya | Ya | Ya | Tidak | Tidak | Milik sendiri lewat portal |
| Tambah/ubah pelanggan | Ya | Ya | Tidak | Tidak | Tidak | Tidak |
| Daftar/detail faktur | Ya | Ya | Ya | Ya | Tidak | Milik sendiri |
| Terbitkan faktur/diskon | Ya | Ya | Tidak | Tidak | Tidak | Tidak |
| Catat pembayaran simulasi | Ya | Tidak | Ya | Ya | Tidak | Tidak |
| Reversal penuh + alasan | Ya | Tidak | Ya | Tidak | Tidak | Tidak |
| PDF | ISP sendiri | ISP sendiri | ISP sendiri | ISP sendiri | Tidak | Milik sendiri |
| CSV pelanggan | Ya | Ya | Ya | Tidak | Tidak | Tidak |
| Antrean jaringan | Ya | Tidak | Tidak | Tidak | Ya | Tidak |
| Setelan billing | Ya | Tidak | Tidak | Tidak | Tidak | Tidak |
| Audit | Ya | Tidak | Tidak | Tidak | Tidak | Tidak |
| Editor role / secret / upload / webhook | Belum tersedia untuk semua peran | — | — | — | — | — |

Perubahan role berlaku pada request berikutnya dalam sesi yang sama. Membership dibaca dari database, bukan claim pada browser. Pencabutan user `is_active=False`, reset kata sandi, dan logout menggunakan mekanisme Django. Owner belum dapat mengubah role melalui UI; pengelolaan sementara melalui operasi Django shell lokal oleh administrator mesin tepercaya. Jangan membuka shell/admin Django ke pengguna.

Scope wilayah/penugasan untuk teknisi/sales belum tersedia; role tersebut belum dapat digunakan. Keuangan/operasional pada checkpoint ini melihat identitas pelanggan satu ISP; minimisasi field lebih lanjut perlu disepakati sebelum produksi. NOC hanya melihat nama pelanggan dalam antrean dan hasil simulasi, tanpa nominal/sandi layanan.

Worker tidak menerima tenant dari request pengguna; tiap job menyimpan tenant dan mengambil pelanggan dengan pasangan tenant+ID. Job silang tenant ditolak oleh lookup. Tidak ada cache bisnis bersama atau realtime pada fondasi ini. Database/shell administrator tetap batas kepercayaan; constraint lintas relasi yang lebih ketat dan audit tahan manipulasi perlu ditambahkan sebelum deployment.
