# Operasional lokal: kas, tim, sales, hotspot dan topologi

Checkpoint `feature/tagihnet-local-operations`, schema `0014_local_operations`.
Seluruh alur di bawah dapat digunakan pada staging lokal dalam mode simulasi.
Tidak ada transfer bank, pembayaran gateway, pengiriman pesan atau provisioning perangkat nyata.

## Akses dan tinjauan

Owner memperoleh seluruh izin modul. Preset lama mempertahankan hak sebelumnya;
NOC memperoleh tambahan baca topologi. Sales adalah preset kedelapan dengan prospek
yang **ditugaskan kepadanya** dan komisinya sendiri. Sales tidak dapat memakai peran
kustom untuk memperluas cakupan. Perubahan penugasan langsung mencabut akses lama.

| Izin baca | Izin perubahan | Prasyarat peran kustom untuk perubahan |
| --- | --- | --- |
| `cash` | `manage_cash` | `cash` |
| `team` | `manage_team` | `team` |
| `payroll` | `manage_payroll` | `payroll`, `team`, `cash` |
| `sales` | `manage_sales` | `sales`, `cash` |
| `hotspot` | `manage_hotspot` | `hotspot`, `cash` |
| `topology` | `manage_topology` | `topology`, `network`, `customers` |

Perubahan menggunakan formulir → tinjauan → konfirmasi checkbox/CSRF/MFA maksimal
5 menit. Token bertanda tangan berlaku 15 menit, terikat pelaku, ISP, versi akses,
revisi MFA, objek, baseline dan pilihan terkait. Token bukan enkripsi; jangan
membagikannya. Form atau tinjauan lama ditolak saat data terkait berubah.

Transaksi mengunci tenant kemudian membership, memeriksa kembali izin aktif dan
menyimpan hasil permintaan UUID di `BusinessAction`. Pengulangan konfirmasi yang
sama mengembalikan hasil lama tanpa mengulangi efek. Tinjauan yang kedaluwarsa
harus dibuat kembali. Audit mencatat metadata serta alasan koreksi/pembatalan yang dimasukkan pengguna, tanpa kata sandi voucher atau token. Teks dinormalisasi NFC, divalidasi ulang panjangnya, dan menolak karakter kontrol/bidi/surrogate. Kegagalan audit membatalkan pencatatan dan receipt permintaan bersama-sama.
Halaman privat tidak disimpan dalam cache. GET tidak membuat transaksi bisnis.

## Kas, dana, pengeluaran dan tutup buku

Menu **Kas & dana** menyediakan dana kas/bank/dompet, modal/saldo awal eksplisit,
penerimaan lain, pengeluaran, transfer dua sisi dan koreksi. Tidak ada dana atau
saldo awal otomatis. Nominal integer rupiah, Rp1–2 miliar per pencatatan. Tanggal bisnis dimulai sejak 1 Januari 2000 agar konsisten dengan rentang laporan. Saldo
pengeluaran/transfer/payroll/komisi harus cukup; keputusan diserialisasi per ISP.

Form pembayaran faktur memiliki dana penerimaan opsional. Relasi `PaymentFunding`
bersifat tetap untuk pembayaran tersebut. Pembayaran lama tetap **Belum dipetakan**;
tidak dimasukkan diam-diam ke dana baru. Pengulangan UUID pembayaran harus memakai
dana yang sama, termasuk pilihan tanpa dana.

Arus kas berisi pembayaran pada waktu aslinya, reversal pembayaran pada waktu
reversal, kas manual pada tanggal transaksi, dan koreksi kas pada tanggal koreksi.
Laporan rentang maksimal 366 hari menggunakan batas tanggal WIB serta snapshot baca
konsisten; PostgreSQL memakai transaksi `REPEATABLE READ, READ ONLY`. Modal dan
transfer tidak boleh ditafsirkan sebagai pendapatan usaha. Laporan ini bukan
akuntansi akrual, laporan laba rugi atau perhitungan pajak.

Transfer mencatat keluar/masuk atomik, total gabungan nol. Koreksinya membalik kedua
sisi sekaligus, mempertahankan catatan asli. Koreksi payroll, komisi dan voucher
harus melalui modul asal; tidak melalui koreksi kas umum. Reversal pembayaran dan
koreksi penerimaan tetap dapat menciptakan saldo negatif apabila uang sudah
dibelanjakan; saldo negatif menunjukkan selisih yang perlu direkonsiliasi.

Tutup buku hanya untuk bulan yang sudah selesai dan lebih baru dari penutupan
terakhir. Snapshot per dana menyimpan awal/masuk/keluar/akhir, termasuk dana belum
dipetakan. Kas manual tidak boleh ditanggalkan ke periode yang telah ditutup.
Reversal/koreksi sesudahnya masuk periode terbuka tanpa mengubah snapshot lama.
Tidak ada edit/hapus transaksi asli lewat aplikasi. Dokumen bukti pengeluaran,
rekonsiliasi rekening bank dan ekspor laporan kas khusus belum tersedia.

## Tim dan payroll

Data nama/bagian/kontak/akun anggota tim terpisah dari komponen gaji. Izin melihat
tim tidak memberikan akses nominal gaji. Nonaktif tim tidak mengubah akun login.

Atur pokok, tunjangan dan potongan anggota aktif terlebih dahulu. Payroll satu
bulan memuat 1–200 anggota aktif, total maksimal Rp2 miliar. Pratinjau menyimpan
identitas, bagian, penerima slip, versi data/gaji dan bruto/potongan/bersih.
Perubahan tim/gaji sesudah pratinjau memerlukan pembatalan dan pratinjau baru.
Satu periode hanya memiliki satu run terbuka; run baru dapat dibuat setelah
pembatalan atau reversal, dengan rincian historis lama tetap tersimpan.

Pratinjau → persetujuan → pencatatan dibayar dari dana. Pembayaran memerlukan akun
yang memberi persetujuan serta versi akses/revisi MFA yang sama; persetujuan ulang
dapat dilakukan dengan akun saat ini. Ini bukan kontrol dua orang. Satu run dibayar
hanya membuat satu pencatatan kas. Reversal menambahkan koreksi dan menandai run;
slip lama tidak diubah. Tidak ada transfer otomatis, kalkulasi PPh/BPJS, absensi
atau prorata otomatis.

**Slip gaji saya** hanya menampilkan run dibayar/direversal untuk membership
historis penerima. Mengubah nama atau relasi anggota tim tidak mengalihkan slip
lama. Hak payroll global terpisah dari akses slip sendiri.

## Prospek, komisi dan kampanye

Sales membuat/mengubah prospek sendiri, menandainya siap diajukan, lalu mengajukan
menjadi satu PSB. Sales tidak memperoleh hak mengubah tahap/penugasan/aktivasi PSB;
perkembangan PSB ditampilkan baca saja pada prospeknya. Username/telepon yang sudah
dipakai pelanggan atau PSB aktif ditolak saat pengajuan. Paket harus aktif.

Pengelola menetapkan penanggung jawab sales dan komisi tetap sebelum konversi.
Sales tidak dapat menentukan nominal komisi melalui POST tambahan. Sesudah konversi,
identitas/komisi prospek dikunci; perubahan operasional memakai alur PSB yang ada.
Aktivasi administrasi PSB menimbulkan satu komisi dengan nominal/penerima historis,
jika penanggung jawab dan nominal positif tersedia. Pembayaran komisi dari dana
merupakan tindakan terpisah; reversal mempertahankan pencatatan asal. Tidak ada
pembayaran ulang otomatis atau kalkulasi komisi bertingkat/berulang.

Kampanye berisi judul, teks, paket aktif, nomor telepon, tanggal WIB dan publikasi
opt-in. Halaman `/promo/<UUID>/` hanya tersedia selama dipublikasikan, dalam rentang
tanggal dan paket masih aktif. Harga mengikuti katalog saat dibuka, sebelum pajak
dan diskon; kampanye tidak mengubah billing. Kontak berupa tautan telepon manual,
tanpa pengiriman pesan atau formulir pengumpulan data publik. Data pelanggan,
payroll dan token operator tidak disertakan pada halaman publik.

## Hotspot dan voucher simulasi

Profil mencatat informasi kecepatan, durasi 1–525.600 menit, batas aktivasi 1–365
hari dan harga Rp1–20 juta. Penjualan 1–100 voucher membekukan profil/harga dan
mencatat penerimaan kas sekali. Perubahan profil tidak mengubah batch lama.

Username/kata sandi acak; kata sandi disimpan dengan Fernet memakai kunci turunan
HKDF-SHA256 berlabel `hotspot-vouchers-v1` dari **kunci MFA asli yang sudah ada**.
Tidak dibuat master key baru. Cadangan database memuat ciphertext; pemulihan kata
sandi voucher memerlukan kunci MFA asli dalam custody terpisah. Jangan mengganti
kunci MFA pada database yang dipertahankan tanpa migrasi enkripsi yang direncanakan.

Daftar/detail GET tidak menampilkan kata sandi. Cetak PDF privat menggunakan POST,
CSRF, izin `manage_hotspot` dan MFA terbaru. PDF attachment tidak disimpan dalam
cache; pelaku cetak diaudit tanpa mencatat kredensial. PDF bisa memuat kredensial
simulasi yang sudah dicabut dengan status saat cetak; hasil cetak perlu dikelola
oleh operator.

Simulasi aktivasi menetapkan waktu mulai/akhir sekali; replay tidak memperpanjang
durasi. Batas aktivasi diperiksa kembali. Expiry dihitung baca saja; revoke bersifat
terminal. Koreksi penjualan menambahkan pembalik kas dan mencabut seluruh voucher
batch, termasuk yang pernah diaktifkan. Ini tidak menjalankan RADIUS/router,
memutus sesi, menyediakan internet atau mengukur pemakaian nyata.

## Topologi administratif

ODC → ODP → port pelanggan. ODP memiliki satu ODC aktif dari ISP yang sama. Kapasitas
1–1.024; ODC membatasi jumlah ODP aktif, ODP membatasi nomor port. Satu port hanya
untuk satu pelanggan dan satu pelanggan memiliki satu endpoint. Pemindahan port
atomik; nomor port yang terpakai dan kapasitas diperiksa kembali saat konfirmasi.

Jenis node tetap sesudah dibuat. Kapasitas tidak boleh turun melewati port terpakai
atau jumlah ODP aktif. Node tidak dapat dinonaktifkan selama masih mempunyai ODP
aktif atau port pelanggan. Relasi lama tetap terlihat bila node induk disimpan;
tidak ada provisioning OLT atau klaim verifikasi fisik.

Koordinat nullable berpasangan WGS84 menggunakan validasi lokasi yang sudah ada.
Peta hanya tautan OpenStreetMap yang dibuka manual. NOC dapat membaca nama/kode
pelanggan pada port tanpa data finansial; pelanggan/teknisi/sales tidak memperoleh
daftar topologi global. Scope wilayah geografis tetap pekerjaan terpisah.

## Migrasi dan pengujian

0014 menambah 18 tabel dan pilihan role Sales, tanpa mengubah kolom/nilai bisnis
lama. Django menambah 18 content type, 72 permission dan satu baris recorder;
pertambahan sequence metadata tersebut diharapkan. Tidak ada seed bisnis otomatis.
Checker SQLite/PG memeriksa identitas metadata, hash seluruh baris/kolom lama,
high-water sequence dan tabel baru kosong. Salinan backup diuji sebelum data lokal
yang dipertahankan diperbarui. Image schema0013 tidak boleh dijalankan sebagai
rollback otomatis setelah schema0014; gunakan restore terpisah yang kompatibel.

46 tes baru mencakup izin/tenant/MFA/CSRF/stale/replay, kas dan koreksi/tutup buku,
snapshot payroll/slip, scope sales/aktivasi/komisi, enkripsi/PDF/expiry/refund voucher,
kapasitas/pemetaan port serta dua race pencatatan. Chromium menjalankan keenam
modul pada empat lebar layar, mode gelap, MFA nyata dan tanpa request eksternal.
Latihan cadangan SQLite/PG menyertakan semua 18 tabel berisi data fiktif, ciphertext
voucher, payroll dibayar, komisi, kampanye, pemetaan port dan tutup buku. Hasil akhir
dan batas bukti dicatat dalam [TEST-REPORT](TEST-REPORT.md).
