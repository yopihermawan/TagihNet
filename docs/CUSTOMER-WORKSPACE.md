# Detail pelanggan bertab — 14 November 2026 WIB

Checkpoint `feature/tagihnet-customer-workspace` menyatukan informasi pelanggan dalam tujuh tab pada halaman detail yang sudah ada. URL `/customers/ID/` membuka Ringkasan. Pilihan tab tersimpan sebagai `?tab=...`; daftar data memakai `size=10/25/50` dan `page`. Tautan tab memakai navigasi server biasa, dapat dibuka/bookmark dan tetap berfungsi tanpa JavaScript. Tidak ada drawer atau API realtime pada checkpoint ini.

| Tab / parameter | Data | Izin tambahan setelah `customers` |
| --- | --- | --- |
| Ringkasan / `summary` | Identitas, paket, status layanan/NAS, piutang dan tanggal terawal bersisa. | Tidak ada; ringkasan piutang tetap mengikuti izin pelanggan sebelumnya. |
| Layanan & Koneksi / `service` | Paket/harga katalog, username, NAS dan seluruh pekerjaan rekonsiliasi simulasi secara paginated. | Tidak ada. |
| Faktur / `invoices` | Periode, tanggal jatuh tempo, status finansial, total terbit/pembayaran sah/sisa, tautan detail/PDF. | `invoices` |
| Pembayaran / `payments` | Histori transaksi, pencatat, catatan, reversal/alasan; total bruto/direversal/sah seluruh periode dan seluruh halaman. | `invoices` |
| Tiket / `tickets` | Tiket pelanggan, status, prioritas/SLA, teknisi dan pembaruan. Isi komentar tidak disalin ke daftar ini. | `tickets` |
| Lokasi / `location` | Alamat/wilayah dan pencarian OpenStreetMap eksternal; koordinat belum diverifikasi. | Tidak ada. |
| Riwayat / `history` | Audit penambahan/perubahan identitas dan layanan pelanggan, pelaku/waktu/rincian. | `audit` |

Tab tanpa izin tidak muncul. Permintaan langsung tetap ditolak403; tab asing/path-like ditolak404 dan tidak dipakai untuk memilih template arbitrer. Pelanggan tenant lain/tidak ditemukan ditolak404 pada seluruh tab. Kasir/NOC/teknisi/portal tidak mendapat akses workspace melalui izin modul lain. Peran kustom `customers` saja tetap dapat membaca piutang ringkasan seperti sebelumnya, tetapi tidak mendapat daftar faktur, transaksi pembayaran, tiket atau audit. Query data tambahan dijalankan setelah izin tab diperiksa.

Semua daftar memfilter pelanggan dan ISP dari membership. Riwayat audit mencocokkan **jenis aksi pelanggan + target + tenant**, sehingga nomor target yang sama pada faktur/pembayaran tidak dianggap histori pelanggan. Riwayat tidak merekonstruksi transaksi yang tidak tercatat sebagai `customer.created/updated`; transaksi keuangan dan gangguan dibaca melalui tabnya sendiri. Status layanan, data koneksi yang belum tersedia, dan status finansial tetap terpisah.

## Ledger dan navigasi

Saldo berasal dari seluruh faktur bersisa setelah pembayaran yang belum direversal, bukan harga paket terkini. Tabel faktur menghitung pembayaran sah per faktur dalam query agregat; pembacaan halaman tidak menambah query per baris. Tab Pembayaran mempertahankan pembayaran asli yang direversal dan menampilkan koreksinya. Total semua halaman tidak berubah saat ukuran halaman diganti. Pencatatan/koreksi tetap dilakukan melalui alur detail faktur yang sudah berizin; GET seluruh tab tidak membuat/mengubah record bisnis.

Paket/NAS pada tab layanan menunjukkan keadaan administratif terkini; nominal faktur mempertahankan rincian saat diterbitkan. Tidak ada pencocokan otomatis ke sesi PPPoE atau klaim online. Data halaman dibaca ulang pada setiap request dan bukan snapshot persetujuan billing yang dibekukan.

Tombol **Kembali ke pelanggan** memakai URL daftar terakhir di sessionStorage, termasuk pencarian/filter/sort/size/page, dan kini mengembalikan posisi scroll vertikal bila URL cocok. Mengganti filter tetap membuka posisi baru. Penyimpanan browser bersifat opsional; bila JavaScript/storage tidak tersedia, navigasi tab tetap berjalan dan tombol kembali memakai daftar pelanggan default. Tidak ada URL eksternal yang diambil dari storage. Tab navigasi memakai link semantik, `aria-current`, focus keyboard, tombol sentuh minimal44px dan pembungkusan pada layar kecil; tabel bergeser secara lokal.

## Verifikasi

26 tes baru membawa total ke **326 pada masing-masing SQLite dan PostgreSQL**, tanpa fail/skip. Cakupan: menu/tab/GET-only; izin preset/custom/penarikan izin; tenant/pelanggan; pemilihan template aman; saldo sebagian/lunas/reversal; status jatuh tempo; harga katalog versus faktur terbit; total seluruh halaman; pagination; query-count konstan pada tabel faktur/pembayaran; teks aman; empty state; tiket tanpa salinan komentar internal; audit tanpa tabrakan ID; GET tanpa efek bisnis.

Browser operasi terisolasi memeriksa tujuh tab, `aria-current`, scope tiket/pembayaran, penolakan peran lain, perpindahan link dengan JavaScript dinonaktifkan, pemulihan URL lengkap + scroll400px, serta360/768/1366/1920px. Pembacaan workspace tidak mengubah counts pelanggan/faktur/pembayaran/job/audit pada database uji. Tiga bukti fiktif: [pembayaran desktop](evidence/customer-workspace-payments-desktop.png), [tiket desktop](evidence/customer-workspace-tickets-desktop.png), [faktur mobile](evidence/customer-workspace-invoices-mobile.png). Tidak ada faktor MFA di bukti. Browser security/billing/MFA regresi tetap lulus.

Tidak ada perubahan model/migrasi/dependensi/worker/secret/domain jaringan. Skema tetap0009, database demo dan kunci privat tidak diubah oleh tes. System/model-drift/pip/static checks lulus. Restore SQLite yang sudah ada dijalankan browser sebagai regresi. Pengujian12 proses PostgreSQL, PDF14 halaman, encrypted/MFA drill dan scan advisory merujuk checkpoint sebelumnya; tidak diulang untuk perubahan halaman baca ini. CI remote belum diverifikasi. Lihat [TEST-REPORT](TEST-REPORT.md); drawer overlay, koordinat/topologi dan integrasi nyata masih pending.
