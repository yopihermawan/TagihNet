# Matriks fitur — checkpoint fondasi dan akses

Status `implemented` berarti dapat dijalankan lokal; `simulated` tidak menghubungi layanan nyata. Tidak ada klaim seluruh brief selesai.

| Cakupan | Implementasi | Fase / status |
| --- | --- | --- |
| Brand, shell, login | Logo SVG orisinal, sidebar berbasis peran, pencarian menu, mobile, dark mode | 1–2 · implemented |
| Dashboard billing | Pembayaran berdasarkan tanggal transaksi, faktur berdasarkan periode layanan, piutang/tunggakan seluruh periode ISP | 2 · implemented |
| Dashboard NOC | Antrean perintah simulasi; sesi/trafik/polling dinyatakan belum tersedia | 2 · limited |
| Pelanggan | Satu paket/layanan, cari/filter/sort/URL/pagination, tambah/ubah/detail, deteksi duplikat, CSV | 2 · implemented |
| Filter NAS/jatuh tempo; drawer bertab | NAS belum dimodelkan; detail berupa halaman, URL daftar dipertahankan di sessionStorage | 3 · pending |
| Multi-tenant | Membership satu ISP per akun; tenant dari login, dua tenant seed, objek/PDF/CSV terbatasi | 2 · implemented |
| Role dan cakupan | Enam preset dan peran kustom tenant; owner membuat/mengubah akun dan izin, sesi dicabut, owner terakhir dilindungi. Teknisi/sales dan scope wilayah belum | 2 · limited |
| Faktur | Bulanan full month, tanggal akhir bulan, snapshot harga/pajak/diskon, constraint satu pelanggan/periode | 2 · implemented |
| Massal/otomatisasi/prorata | Belum tersedia; tidak ada tombol massal yang menyesatkan | 3 · pending |
| Pembayaran | Integer rupiah, sebagian/penuh, overpay ditolak, UUID idempotensi persisten, reversal penuh | 2 · simulated |
| Gateway/webhook | Endpoint gateway tidak didaftarkan; tidak ada signature/settlement produksi | 3 · pending |
| PDF | ReportLab lokal, A4 multipage, total/snapshot/label simulasi, footer plain text | 2 · implemented |
| Setelan billing | Pajak basis poin termasuk nol, tanggal 1–31, footer teks, hanya berlaku pada faktur berikutnya | 2 · limited |
| Isolir/aktivasi | Recheck utang saat eksekusi; adapter palsu, backoff 30/60/120 detik, maksimal 3 percobaan | 3 · simulated |
| MikroTik/RADIUS/CoA | Belum ada adapter read-only/write nyata, lab, VPN collector, accounting | 3 · pending |
| Portal | Pelanggan melihat layanan/faktur/PDF milik sendiri | 2 · limited |
| Peta | Tautan pencarian OpenStreetMap; belum koordinat/topologi ODC/ODP | 2 · limited |
| Tiket, PSB, penugasan | Belum dibuat; tidak tampil di navigasi | 4 · pending |
| Kas, laporan akuntansi, payroll, komisi | Belum dibuat; dashboard ledger bukan laporan pendapatan akrual | 4 · pending |
| Hotspot/voucher, marketing, portal promo | Belum dibuat | 4 · pending |
| Upload/impor | Endpoint tidak tersedia; ekspor pelanggan dibatasi izin dan 10.000 baris | 4 · limited |
| Audit | Pelaku/tenant/target/aksi/detail/correlation ID; read-only via aplikasi | 2 · implemented; WORM/retensi pending |
| Auth | CSRF, hashing PBKDF2 Django, rate limit DB, reset sekali pakai, idle/absolute expiry | 2 · implemented |
| MFA dan editor role | TOTP wajib operator, QR lokal, secret Fernet, recovery sekali pakai, step-up, editor akun/peran owner | 2 · implemented lokal |
| Profil mandiri, scope wilayah/penugasan | Belum tersedia | 4 · pending |
| Backup/restore | SQLite consistent backup + restore/reconcile drill ke direktori kosong | 2 · implemented; enkripsi/offsite pending |
| Rilis produksi | TLS, SMTP, secret vault, database/queue multiworker, beban, lab, penetration test | 5 · pending |

Langkah implementasi berikutnya: migrasi PostgreSQL dan pengujian race lintas proses; penerbitan massal dengan preview; adapter gateway sandbox terverifikasi; router/RADIUS lab. Cakupan lanjutan mengikuti urutan brief, tanpa mengubah status fitur pending menjadi selesai.
