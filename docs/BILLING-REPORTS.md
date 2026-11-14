# Laporan penagihan — 14 November 2026 WIB

Checkpoint `feature/tagihnet-billing-reports` menambahkan **Laporan penagihan** dengan tiga tampilan: Penerimaan & koreksi, Faktur terbit, dan Piutang akhir. Pilih tanggal mulai/akhir WIB, lihat angka dan rekonsiliasinya, lalu ekspor CSV jika memiliki izin. Data berasal dari ledger simulasi; mutasi bank, pengeluaran, pendapatan akrual dan tutup buku belum dicakup.

## Periode dan dasar hitung

Rentang mencakup tanggal mulai sampai akhir hari tanggal akhir di **Asia/Jakarta**, maksimal366 hari kalender, mulai paling awal1 Januari2000 dan tanggal akhir maksimal hari ini. Awal bulan berjalan menjadi default. Input tidak valid mendapat400 tanpa menggantinya dengan periode lain; jenis laporan asing mendapat404.

| Angka | Dasar |
| --- | --- |
| Piutang awal | Faktur terbit sebelum awal hari pertama − pembayaran sebelum hari pertama + reversal sebelum hari pertama. |
| Faktur terbit | Total snapshot faktur dengan `issued_at` dalam rentang, termasuk pajak/diskon yang tersimpan. Bukan filter periode layanan. |
| Penerimaan tercatat | Nominal pembayaran berdasarkan `created_at` dalam rentang, termasuk yang dikoreksi kemudian. |
| Koreksi (reversal) | Nominal pembayaran yang dikoreksi berdasarkan waktu reversal dalam rentang. |
| Penerimaan setelah koreksi | Penerimaan − koreksi; dapat negatif bila periode ini mengoreksi pembayaran periode sebelumnya. |
| Piutang akhir | Piutang awal + faktur terbit − penerimaan + koreksi. Dibandingkan dengan perhitungan saldo tiap faktur pada tanggal akhir. |

Reversal pada November tidak menghapus penerimaan September dari laporan September. Saldo historis menghitung pembayaran yang sudah masuk sebelum cutoff dan belum direversal sebelum cutoff. Reversal tepat00:00 hari berikutnya termasuk periode berikutnya. Tab Piutang akhir mencakup seluruh faktur terbit sampai tanggal akhir, termasuk sebelum tanggal mulai. Jatuh tempo yang tepat pada tanggal akhir belum dianggap menunggak; tanggal sebelumnya dengan sisa positif termasuk tunggakan.

Periode layanan ditampilkan sebagai informasi terpisah. Perubahan paket/identitas pelanggan sekarang tidak mengganti total maupun nama pelanggan saat faktur diterbitkan. Tautan detail faktur tetap membuka keadaan terkini. Pembayaran dan reversal adalah pencatatan billing; reversal tidak otomatis membuktikan pengembalian kas/bank. Jika persamaan ledger tidak cocok dengan saldo per faktur akibat data host yang tidak konsisten, halaman menampilkan **Saldo perlu diperiksa** dan tidak mengubah ledger.

## Izin dan ekspor

Owner/Keuangan bawaan mendapat `billing_reports` dan `export_billing_reports`. Ops/kasir/NOC/teknisi/portal tidak mendapat akses baru. Peran kustom memerlukan `invoices` untuk membaca laporan; ekspor membutuhkan `billing_reports` + `invoices` + `export_billing_reports`. Menyembunyikan tombol bukan satu-satunya kontrol: endpoint/domain memeriksa izin aktif dan scope tenant. Parameter tenant request tidak memindahkan ISP.

CSV sesuai mode/rentang dan mencakup seluruh hasil, bukan hanya halaman saat ini. Maksimal10.000 baris; kelebihan ditolak400, tidak dipotong diam-diam. Tanggal/angka/nama kolom ditetapkan server, CSV UTF-8 BOM, waktu ISO dengan+07:00. Teks yang dapat menjadi formula diberi apostrof. Nominal integer yang dikendalikan server tetap numerik, termasuk reversal negatif. ID peristiwa memakai awalan P-/R- agar ID pembayaran dan reversal tidak tertukar. Ekspor berhasil mencatat pelaku/tenant/mode/rentang/jumlah baris pada audit tanpa menyalin catatan transaksi ke audit. GET tampilan tidak membuat record bisnis; ekspor hanya menambah audit.

Tab, tanggal, size10/25/50 dan page tersimpan dalam URL. Mengganti rentang/tab membuka halaman pertama. Untuk hasil piutang besar yang melampaui batas ekspor, gunakan pagination; ekspor besar melalui job belum tersedia. Menyempitkan tanggal hanya mempersempit daftar transaksi/faktur terbit, bukan menghilangkan piutang lama.

## Konsistensi dan verifikasi

Seluruh angka dan baris halaman dibaca dalam satu transaksi. PostgreSQL menggunakan **REPEATABLE READ, READ ONLY** sebelum query pertama; SQLite menggunakan transaksi baca. CSV dimaterialisasi dalam transaksi, lalu audit ditulis sesudahnya. Mode read-only tidak tertinggal pada koneksi sesudah transaksi selesai. `report_snapshot` memerlukan transaksi tersendiri; konfigurasi Django saat ini memakai `ATOMIC_REQUESTS=False`. Uji endpoint memakai `TransactionTestCase` agar menguji batas transaksi nyata, tanpa bypass khusus tes.

**352 tes lulus pada masing-masing SQLite/PostgreSQL**,26 tes baru, tanpa fail/skip. Cakupan meliputi piutang awal/akhir, koreksi beda bulan/negatif, WIB/cutoff0:00 dan23:59:59, rentang366 hari termasuk tahun kabisat, issued_at versus periode layanan, snapshot harga/nama, pembayaran penuh setelah cutoff, scope/role/dependensi/revocation, invalid input/mode, CSV formula/nilai negatif/batas baris/audit, pagination/total semua halaman, output teks, transaksi tanpa efek bisnis, status rekonsiliasi dan pengembalian mode koneksi.

`verify_postgres.py` kini menguji **13 skenario lintas proses**: snapshot laporan tetap sama ketika dua proses pembayaran commit satu transaksi idempoten; pembacaan berikutnya memasukkan pembayaran baru. Regresi12 skenario sebelumnya serta restore PG biasa/terenkripsi semua tabel/MFA tetap lulus. Browser menguji ringkasan terhadap SQL sumber independen, rekonsiliasi, tiga tab, CSV/jumlah/total, scope, invalid input dan360/768/1366/1920px. Browser security/billing/MFA regresi juga lulus. Bukti fiktif: [penerimaan desktop](evidence/billing-report-cash-desktop.png), [piutang mobile](evidence/billing-report-receivables-mobile.png).

Tidak ada migrasi/dependensi/worker/secret/domain baru. Demo pengguna tidak memperoleh fixture laporan atau enrollment MFA. System/model-drift/pip/static lulus; restore SQLite dijalankan browser. PDF14-page/encrypted SQLite MFA drill/advisory scan merujuk checkpoint sebelumnya, tidak diulang di tahap laporan ini. CI remote/kapasitas produksi belum diverifikasi. Lihat [TEST-REPORT](TEST-REPORT.md).
