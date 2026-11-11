# Laporan ledger gateway — 12 November 2026 WIB

Owner atau Keuangan membuka **Ledger gateway** di navigasi atau **Laporan ledger gateway** pada riwayat pembayaran. Peran kustom memerlukan `gateway_reports` beserta `invoices`. Operator lain, NOC, kasir, teknisi dan pelanggan portal tidak mendapat akses bawaan. Hak ekspor pelanggan tidak memberikan hak laporan gateway. Peran keuangan kustom yang sudah ada tetap memakai izin eksplisitnya; owner dapat menambahkan izin laporan bila diperlukan.

## Periode dan sumber

Pilih tanggal awal/akhir WIB; keduanya termasuk. Default tujuh tanggal sampai hari ini. Batas 31 hari dan 5.000 entri ledger. Jika batas terlampaui, seluruh laporan/CSV ditolak dengan penjelasan agar periode dipersempit; tidak ada pemotongan yang membuat total tampak lengkap. Pagination 10/25/50 hanya membatasi tampilan, sedangkan ringkasan dan CSV mencakup seluruh periode.

Sumber laporan adalah pembayaran `midtrans_sandbox` yang terhubung ke order/invoice ISP sendiri serta koreksi pada pembayaran tersebut. Pembayaran memakai `Payment.created_at`; koreksi memakai `PaymentReversal.created_at`. Tanggal order, periode layanan invoice, waktu pembaruan status dan waktu transaksi provider tidak menentukan periode laporan. Pembayaran manual dan checkout pending tidak dihitung. Laporan tidak mengambil status dari Midtrans dan tetap dapat dibaca saat checkout dimatikan atau kredensial tidak tersedia.

| Entri | Dampak pada perubahan bersih | Makna |
| --- | --- | --- |
| Pembayaran tercatat | Positif | Pembayaran gateway masuk ledger pada tanggal tersebut |
| Refund penuh tercatat | Negatif | Reversal memiliki bukti refund penuh dengan link/nominal/transaksi sesuai |
| Koreksi lain · perlu ditinjau | Negatif | Reversal ledger ada, tetapi bukti refund penuh yang sesuai tidak ditemukan |

Contoh: pembayaran Rp249.750 dicatat 31 Oktober, refund penuh dicatat 1 November. Laporan 31 Oktober tetap menunjukkan +Rp249.750; laporan 1 November menunjukkan -Rp249.750; laporan gabungan menunjukkan nol. Refund tidak menghapus pembayaran dari periode asal. Status refund di provider yang belum dicatat sebagai reversal belum mengurangi ledger. Koreksi lain tetap mengurangi ledger, tanpa diklaim sebagai refund provider yang sudah terverifikasi.

Ringkasan menampilkan nominal dan jumlah pembayaran/refund/koreksi, serta pembayaran dikurangi keduanya. Angka ini bukan pencairan bank, biaya gateway, laba atau pendapatan akrual. Waktu provider dan waktu pencatatan aplikasi dapat berbeda. Data hanya mencakup ledger yang sudah tersimpan di TagihNet; laporan bukan bukti bahwa seluruh transaksi merchant sudah diterima.

## Snapshot dan status

Satu pernyataan SQL membaca kedua jenis entri dalam satu snapshot. Ringkasan dan rincian dalam satu respons berasal dari daftar lengkap yang sama, dengan integer rupiah. Urutan memakai waktu, jenis dan ID ledger agar entri dengan timestamp sama tetap stabil. Pembacaan tidak mengunci baris pembayaran atau mengubah ledger. Uji dua proses memeriksa laporan saat reversal masih dalam transaksi yang belum committed: laporan melihat keadaan sebelumnya, kemudian pembacaan berikutnya melihat koreksi lengkap.

Setiap permintaan halaman atau CSV mengambil snapshot baru. Jika ada pembayaran/refund di antara permintaan, halaman berikutnya atau unduhan dapat berubah; perhatikan waktu pembuatan laporan. Tidak ada snapshot permanen lintas pagination/unduhan. Status order dan waktu verifikasi di tabel adalah keadaan saat pembacaan, bukan status historis pada tanggal akhir. Pemeriksaan terakhir gagal tetap diberi label dan tidak dianggap sebagai verifikasi baru.

## CSV dan privasi

**Unduh CSV** mengekspor seluruh entri dalam periode, dengan UTF-8 BOM. Kolom mencakup referensi ledger, waktu WIB, jenis, invoice, snapshot nama pelanggan, referensi order/transaksi, pembayaran/refund/koreksi/net rupiah, status saat laporan, waktu verifikasi/percobaan, periode dan waktu laporan. Angka negatif tetap berupa angka. Teks yang dapat dibaca spreadsheet sebagai formula diberi awalan apostrof; karakter kontrol teks diganti spasi. Aplikasi tidak mengeksekusi formula.

CSV tidak memuat server key, URL checkout privat, UUID idempotensi pembayaran, digest bukti refund atau alasan internal owner. Data nama/faktur tetap privat; akses HTML dan CSV diperiksa terpisah pada setiap request dan hanya mencakup ISP akun. Ekspor mencatat audit actor/tenant, periode dan jumlah entri, tanpa menyimpan isi CSV. Respons memakai kebijakan `no-store` aplikasi. Unduhan yang sudah tersimpan pada perangkat tidak dicabut otomatis saat izin berubah.

## Bukti dan batas

412 tes pada masing-masing SQLite/PostgreSQL lulus, termasuk 27 tes laporan/periode/izin/CSV. Chromium memeriksa total 2 pembayaran dan 1 refund, larangan akses portal, empat viewport, dan nominal panjang pada kartu ringkasan. Verifier PostgreSQL menjalankan 21 skenario dua proses. Restore terenkripsi SQLite juga merekonsiliasi laporan/CSV: dua pembayaran gateway Rp399.500, satu refund Rp249.750, net Rp149.750; pembayaran manual Rp100.000 dikecualikan. Tidak ada dependency, migrasi, worker atau panggilan provider baru. Akun Midtrans/HTTPS publik dan kapasitas produksi belum diverifikasi.
