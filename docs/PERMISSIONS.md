# Hak akses

Sumber enforcement: `core/permissions.py`, `core/views.py:allowed_invoice`, dan filter tenant pada query. Menu memakai capability yang sama. Tenant request diabaikan. Tidak ada administrator global yang otomatis dapat membaca semua ISP. Django superuser tanpa membership juga tidak mendapat akses aplikasi.

| Aksi | Owner | Operasional | Keuangan | Kasir | NOC | Pelanggan |
| --- | --- | --- | --- | --- | --- | --- |
| Laporan penagihan / CSV laporan | Ya | Tidak | Ya | Tidak | Tidak | Tidak |
| Dashboard billing | Ya | Ya | Ya | Ya | Tidak | Tidak |
| Daftar/detail pelanggan | Ya | Ya | Ya | Tidak | Tidak | Milik sendiri lewat portal |
| Filter NAS/jatuh tempo/piutang pelanggan | Ya | Ya | Ya | Tidak | Tidak | Tidak |
| Simpan/hapus pemetaan NAS pelanggan | Ya | Ya | Tidak | Tidak | Tidak | Tidak |
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

Tabel di atas adalah preset awal. Owner dapat menetapkan peran kustom pada akun operator; capability efektif menggantikan preset pada setiap endpoint dan menu, termasuk PDF. `manage_access` tidak dapat didelegasikan. Owner/customer/teknisi tidak boleh menerima peran kustom. Izin tulis memerlukan izin baca terkait. Membership dibaca dari database, bukan claim browser. Perubahan akun/role meningkatkan session_version sehingga sesi lama dicabut pada request berikutnya. Pemilik aktif terakhir dilindungi dalam transaksi. Editor membutuhkan MFA terverifikasi dalam 5 menit terakhir. Lihat [AUTHENTICATION](AUTHENTICATION.md).

Preset Teknisi kini tersedia dengan akses hanya pada tiket/PSB yang ditugaskan. Scope wilayah geografis dan role Sales masih belum tersedia. Keuangan/operasional pada checkpoint ini melihat identitas pelanggan satu ISP; minimisasi field lebih lanjut perlu disepakati sebelum produksi. NOC melihat nama pelanggan dalam antrean rekonsiliasi serta snapshot sesi sintetis monitoring ISP sendiri, tanpa nominal/sandi layanan.

Worker tidak menerima tenant dari request pengguna; tiap job menyimpan tenant dan mengambil pelanggan dengan pasangan tenant+ID. Job silang tenant ditolak oleh lookup. Tidak ada cache bisnis bersama atau realtime pada fondasi ini. Database/shell administrator tetap batas kepercayaan; constraint lintas relasi yang lebih ketat dan audit tahan manipulasi perlu ditambahkan sebelum deployment.

## Batas penerbitan massal

`bulk_issue` adalah capability terpisah dari `issue`; prasyarat role kustom: `issue`, `invoices`, `customers`. Batch bersifat privat untuk pembuat dan tenant. Konfirmasi membutuhkan MFA <=5 menit dan versi sesi masih berlaku. Worker memeriksa kembali izin, user aktif dan session_version sebelum setiap baris; perubahan akses memblokir sisa pekerjaan. Lihat BULK-BILLING.md. Kepemilikan batch tidak mengubah izin biasa atas faktur yang sudah terbit.

## Operasional dan preset Teknisi

| Aksi | Owner | Ops preset | Teknisi | Pelanggan portal | Preset lain |
| --- | --- | --- | --- | --- | --- |
| Kelola tiket ISP | Ya | Ya | Tugas sendiri: status/komentar; tidak triase/menutup | Milik sendiri: buat/baca/komentar publik | Butuh role kustom tickets |
| Kelola PSB ISP | Ya | Ya | Tugas sendiri: tahap/catatan; tidak cancel/assign/aktivasi | Tidak | Butuh role kustom installations |
| Aktivasi PSB/customer/invoice awal | Ya + MFA terbaru | Tidak bawaan | Tidak | Tidak | Butuh activate_installations dan prasyarat |
| Catatan internal tiket | Ya | Ya | Hanya tiket ditugaskan | Tidak pernah dibaca/ditulis | Sesuai tickets |
| Target SLA | Sesuai settings | Tidak bawaan | Tidak | Tidak | Sesuai settings |

Batch dan scope invoice tidak berubah. Peran Teknisi tidak dapat diberi custom role untuk menghindari scope global yang tidak disengaja. State mutation memakai versi dan UUID replay; komentar publik/internal difilter pada backend. Detail aktivasi dan keterbatasan data minimum ada di SUPPORT-AND-INSTALLATIONS.md. Tabel preset awal di atas tetap berlaku untuk fungsi billing; teknisi tidak mendapat fungsi finansial tersebut.

## Pengingat dan perubahan PSB

`reminders` tersedia untuk Owner secara bawaan, atau role kustom dengan seluruh prasyarat `tickets`, `invoices`, `customers`. Role ini dapat melihat isi pengingat finansial/operasional satu ISP. Teknisi dan pelanggan tidak mendapat akses. Kebijakan perlu MFA <=5 menit, versi sesi dan versi kebijakan yang cocok; GET tidak membuat kebijakan. Worker memeriksa akun aktif, tenant, semua capability dan session_version pemberi persetujuan. Perubahan akses menahan antrean sampai persetujuan baru; kondisi objek tetap diperiksa ulang.

Edit identitas/paket dan jadwal ulang PSB menggunakan `installations`, bukan `field_work`. Owner/Ops dapat mengubah PSB satu ISP, teknisi tetap hanya mengubah tahap/catatan tugasnya. Identitas/paket terkunci mulai Pengerjaan. Riwayat perubahan internal dan plaintext; pelanggan tidak melihatnya.

## Monitoring NAS simulasi

`network` memberi akses list/detail/history dan POST pengambilan snapshot yang dibatasi interval. Owner/NOC memilikinya secara bawaan. NOC tidak boleh membuat/mengubah konfigurasi. `manage_monitoring` tersedia untuk Owner atau role kustom dengan prasyarat `network`; penyimpanan memerlukan MFA <=5 menit dan versi sesi/form yang berlaku. Semua lookup perangkat/snapshot mengambil tenant dari membership, bukan POST.

Worker memeriksa persetujuan konfigurasi terhadap akun aktif, ISP, capability dan session_version. Perubahan izin menahan pengambilan; ringkasan langsung mengecualikan snapshotnya sebelum worker berjalan. Simpan ulang konfigurasi dengan MFA memulai versi baru. Teknisi/pelanggan/preset finansial tidak mendapat akses monitoring secara bawaan. Tidak ada secret router atau endpoint target jaringan yang dapat dimasukkan.

## Backend database

Pilihan PostgreSQL tidak mengubah capability, tenant scope atau hak portal/teknisi. Role database lab adalah non-superuser dengan CREATEDB agar dapat membuat/menghapus database tes; hak ini bukan role runtime produksi. Secret database hanya dari konfigurasi proses/file privat, tidak ada form web pengatur koneksi. Worker tetap memeriksa actor/tenant/versi persetujuan seperti sebelumnya. Skenario proses diverifikasi dengan role tersebut; akses administrator database tetap batas kepercayaan terpisah dari role aplikasi.

## Impor pelanggan CSV

`import_customers` diberikan ke Owner secara bawaan. Peran kustom memerlukan `customers` dan `edit_customers`; preset Ops yang hanya dapat menambah satuan tidak otomatis boleh impor. NOC/teknisi/pelanggan/preset finansial tidak mendapatnya secara bawaan. Capability yang sama berlaku pada upload, template, list/detail, konfirmasi/batal dan laporan CSV. Batch/rincian/laporan privat untuk pembuat dan ISP; owner lain di ISP sama juga mendapat 404.

Unggah membuat pratinjau saja. Konfirmasi memerlukan CSRF, checkbox review, MFA <=5 menit, versi sesi yang berlaku dan versi akses saat preview. Data/paket diperiksa ulang. Tidak ada pilihan tenant/status layanan/sandi/nominal di CSV. Pelanggan hasil impor tersimpan di ISP dari membership dengan status pending. Tidak membuat akun portal, invoice atau job jaringan. Pelanggan yang sudah tercipta selanjutnya mengikuti scope pelanggan biasa.

## Backup host administrator

Backup/restore terenkripsi adalah CLI untuk administrator host, bukan capability/menu web. Arsip mencakup seluruh database, seluruh ISP, auth dan MFA ciphertext. Role Owner ISP tidak otomatis mendapat akses host, key file, archive atau Docker socket. Tidak ada endpoint download/restore publik. Kunci backup, sesi, MFA dan password DB dikelola terpisah; backup hanya mengemas database. Database PG restore harus dimiliki role yang melakukan restore dan kosong; hak CREATEDB lab bukan hak runtime produksi.


## Tab detail pelanggan — 14 November 2026 WIB

Workspace membutuhkan `customers`. Ringkasan, layanan/koneksi dan lokasi mengikuti izin ini, termasuk piutang ringkasan yang sudah tersedia sebelumnya. Faktur dan Pembayaran memerlukan tambahan `invoices`, Tiket memerlukan `tickets`, Riwayat memerlukan `audit`. Menu yang disembunyikan juga ditolak di endpoint langsung. Isi tab dibatasi pelanggan+tenant; riwayat audit hanya aksi customer.created/updated pada target yang sama. Tidak ada hak tulis tambahan melalui workspace. Lihat [CUSTOMER-WORKSPACE](CUSTOMER-WORKSPACE.md).


## Laporan penagihan — 14 November 2026 WIB

`billing_reports` memerlukan `invoices`; `export_billing_reports` memerlukan keduanya. Owner dan Keuangan preset memperoleh baca/ekspor. Role kustom tidak otomatis mengikuti tambahan preset. Domain memeriksa ulang aktor aktif dan izin, data dibatasi tenant membership, input tenant di URL tidak mengubah scope. Endpoint baca/ekspor hanya GET; ekspor berhasil menambah audit dan tidak memerlukan izin ekspor pelanggan. Lihat [BILLING-REPORTS](BILLING-REPORTS.md).
