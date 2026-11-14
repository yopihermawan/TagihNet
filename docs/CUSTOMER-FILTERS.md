# NAS pelanggan dan filter jatuh tempo — 14 November 2026 WIB

Checkpoint `feature/tagihnet-customer-filters` melengkapi pencarian pelanggan dengan pemetaan NAS simulasi, filter NAS dan kondisi jatuh tempo, serta kolom piutang/jatuh tempo terawal. Daftar, pagination, jumlah hasil dan ekspor memakai query filter yang sama. Filter tidak membuat transaksi atau mengambil snapshot perangkat.

## Pemetaan NAS

Pengguna berizin `edit_customers` memilih **NAS simulasi (opsional)** pada tambah/ubah pelanggan. Pilihan hanya NAS ISP yang sedang login. Pilih **Belum dipetakan** untuk mengosongkan pemetaan. NAS nonaktif tetap boleh dipilih, karena pemetaan administratif terpisah dari persetujuan pengambilan snapshot. NAS dibuat di menu Monitoring simulasi oleh akun berizin dengan MFA; tidak ada perangkat otomatis dari seed.

Detail/daftar menampilkan nama NAS dan penjelasan simulasi. Pemetaan tidak menyatakan pelanggan online, tidak mencocokkan username ke sesi contoh, tidak mengubah router atau memindahkan layanan/faktur. Snapshot sintetis monitoring tetap terpisah. Menyimpan pelanggan mencatat ID NAS sebelum/sesudah dalam audit yang sama dengan perubahan pelanggan. Keuangan membaca saja; NOC, kasir, teknisi dan pelanggan portal tidak mendapat endpoint daftar/edit pelanggan melalui parameter filter.

Migrasi0009 menambahkan FK nullable `Customer.nas` dengan PROTECT. Semua pelanggan lama tetap tanpa pemetaan; tidak ada pemilihan berdasarkan nama/wilayah. Validasi formulir/model menolak NAS ISP lain, pilihan dan pembacaan nama dibatasi tenant. Operasi SQL/ORM administrator yang melewati validasi tetap batas kepercayaan host. Impor CSV mempertahankan tujuh header yang sudah ada dan pelanggan impor/aktivasi PSB mulai tanpa NAS; pemetaan dilakukan melalui edit pelanggan setelahnya.

## Kondisi tagihan

Perhitungan memakai tanggal bisnis **Asia/Jakarta (WIB)** dan seluruh faktur pelanggan di ISP tersebut. Sisa per faktur = total dikurangi pembayaran yang belum direversal. Faktur dengan sisa nol tidak menentukan jatuh tempo. Piutang pada baris adalah jumlah seluruh sisa positif; tanggal pada baris merupakan jatuh tempo terawal dari faktur bersisa.

| Parameter `due` | Hasil |
| --- | --- |
| `overdue` | Ada faktur bersisa dengan tanggal sebelum hari bisnis. |
| `today` | Ada faktur bersisa yang jatuh tempo hari ini. |
| `next7` | Ada faktur bersisa dari besok sampai hari ke-7, inklusif. |
| `unpaid` | Ada faktur bersisa, tanpa batas tanggal. |
| `clear` | Tidak ada faktur bersisa; termasuk pelanggan yang belum pernah mendapat faktur. |

Satu pelanggan dapat memiliki faktur di beberapa rentang dan muncul dalam beberapa kondisi. Filter adalah kecocokan **salah satu faktur**, sedangkan kolom tanggal selalu yang terawal dari seluruh faktur bersisa. Pembayaran/pelunasan/reversal terlihat pada permintaan berikutnya; tidak mengubah status layanan menjadi status finansial. Pembayaran sebagian tetap menyisakan tagihan; reversal mengembalikan sisa sesuai ledger.

Filter `nas` menerima ID NAS tenant sendiri atau `unassigned`. NAS nonaktif tetap dapat difilter. NAS asing/tidak dikenal dan parameter NAS/due yang tidak valid menghasilkan daftar kosong. ID paket/NAS dibatasi ke integer positif64-bit sebelum diteruskan ke ORM. Input paket/NAS yang tidak valid atau terlalu panjang menghasilkan hasil kosong, tanpa galat konversi maupun perluasan ekspor ke semua pelanggan. Filter lama q/state/plan/area/sort dapat digabung dengan nas/due; size/page tetap 10/25/50 dan tersimpan dalam URL. Tombol kembali detail memakai URL daftar terakhir dalam sessionStorage seperti sebelumnya.

Ekspor CSV mempertahankan enam kolom sebelumnya dan batas10.000 baris. NAS/due membatasi baris dengan query yang sama, tanpa membatasi ekspor ke halaman aktif. Ekspor tidak menambah kolom NAS/piutang pada checkpoint ini. Pengambilan data live dapat berubah antara membaca daftar dan mengekspor jika transaksi baru masuk; bukan snapshot persetujuan billing.

## Validasi

28 tes baru, **300 pada masing-masing SQLite/PostgreSQL**, tanpa fail/skip. Meliputi batas hari WIB17:00UTC, hari0/1/7/8, piutang nol/sebagian/reversal, beberapa faktur/pembayaran tanpa penggandaan join, filter gabungan, ekspor, pagination, jumlah query tetap saat ukuran halaman bertambah, NAS lintas tenant/invalid/disabled/null dan ID paket malformed/terlalu panjang, CSRF/peran, audit, output teks, GET tanpa efek dan impor kompatibel.

Migrasi diuji pada salinan backup terenkripsi sebelum database demo/base lab diterapkan. Semua payload aplikasi/akun lama cocok dan NAS awal NULL. Restore SQLite membandingkan pemetaan; browser membuat satu pemetaan terisi dalam database sementara sebelum restore. Verifikasi PostgreSQL12 skenario lintas proses tetap lulus dan menambahkan pemetaan terisi ke fingerprint dump/restore biasa/terenkripsi. Browser memeriksa simpan NAS, filter/ekspor/kembali daftar, pelunasan yang menghapus hasil overdue dan reversal yang memunculkannya kembali, scope serta empat viewport. Kolom identitas memiliki lebar minimum dengan scroll tabel lokal; baris hasil mobile diuji di bawah170px. Bukti baru: [desktop](evidence/customer-filters-desktop.png), [mobile](evidence/customer-filters-mobile.png).

Tidak ada fixture atau MFA yang ditambahkan ke demo pengguna. Integrasi router/sesi nyata, filter layanan multi-subscription, drawer bertab, scope wilayah geografis dan kapasitas produksi masih belum tersedia. Lihat [TEST-REPORT](TEST-REPORT.md) untuk hasil dan batas bukti.
