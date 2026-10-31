# Matriks fitur — checkpoint billing dan operasional

Status `implemented` berarti dapat dijalankan lokal; `simulated` tidak menghubungi layanan nyata. Tidak ada klaim seluruh brief selesai.

| Cakupan | Implementasi | Fase / status |
| --- | --- | --- |
| Brand, shell, login | Logo SVG orisinal, sidebar berbasis peran, pencarian menu, mobile, dark mode | 1–2 · implemented |
| Dashboard billing | Pembayaran berdasarkan tanggal transaksi, faktur berdasarkan periode layanan, piutang/tunggakan seluruh periode ISP | 2 · implemented |
| Dashboard NOC | Antrean perintah simulasi; sesi/trafik/polling dinyatakan belum tersedia | 2 · limited |
| Pelanggan | Satu paket/layanan, cari/filter/sort/URL/pagination, tambah/ubah/detail, deteksi duplikat, CSV | 2 · implemented |
| Filter NAS/jatuh tempo; drawer bertab | NAS belum dimodelkan; detail berupa halaman, URL daftar dipertahankan di sessionStorage | 3 · pending |
| Multi-tenant | Membership satu ISP per akun; tenant dari login, dua tenant seed, objek/PDF/CSV terbatasi | 2 · implemented |
| Role dan cakupan | Tujuh preset termasuk Teknisi dan peran kustom tenant; owner membuat/mengubah akun dan izin, sesi dicabut, owner terakhir dilindungi. Teknisi dibatasi penugasan; sales dan scope wilayah geografis belum | 2 · limited |
| Faktur | Bulanan full month, tanggal akhir bulan, snapshot harga/pajak/diskon, constraint satu pelanggan/periode | 2 · implemented |
| Penerbitan massal | Preview <=500 pelanggan, stale/expiry, MFA, snapshot persetujuan, worker per baris, deduplikasi/resume/cancel | 3 · implemented lokal |
| Scheduler/prorata | Penerbitan terjadwal tanpa konfirmasi per batch dan prorata belum tersedia | 3 · pending |
| Pengingat billing/SLA | Kebijakan opt-in/MFA, outbox simulasi, dedupe harian/deadline+siklus, recheck pelunasan/resolusi/izin, retry <=3 | 3/4 · simulated; kanal nyata pending |
| Pembayaran | Integer rupiah, sebagian/penuh, overpay ditolak, UUID idempotensi persisten, reversal penuh | 2 · simulated |
| Gateway/webhook | Endpoint gateway tidak didaftarkan; tidak ada signature/settlement produksi | 3 · pending |
| PDF | ReportLab lokal, A4 multipage, total/snapshot/label simulasi, footer plain text | 2 · implemented |
| Setelan billing | Pajak basis poin termasuk nol, tanggal 1–31, footer teks, hanya berlaku pada faktur berikutnya | 2 · limited |
| Isolir/aktivasi | Recheck utang saat eksekusi; adapter palsu, backoff 30/60/120 detik, maksimal 3 percobaan | 3 · simulated |
| MikroTik/RADIUS/CoA | Belum ada adapter read-only/write nyata, lab, VPN collector, accounting | 3 · pending |
| Portal | Pelanggan melihat layanan/faktur/PDF milik sendiri | 2 · limited |
| Peta | Tautan pencarian OpenStreetMap; belum koordinat/topologi ODC/ODP | 2 · limited |
| Tiket/portal/teknisi | Kategori/prioritas/SLA snapshot, komentar publik/internal, state machine/version/replay, assignment-only teknisi | 4 · implemented lokal |
| PSB | Prospek sampai Terpasang, jadwal/penugasan, cancel beralasan, quote aktivasi/MFA, customer+invoice+job atomik | 4 · implemented lokal |
| Edit/jadwal ulang PSB | Edit sebelum Pengerjaan; perubahan pada Terjadwal kembali Survei; reschedule beralasan/version/replay dan histori | 4 · implemented lokal |
| Lampiran/eskalasi nyata | Upload, kanal notifikasi, eskalasi ke pihak lain dan jam kerja SLA belum tersedia | 4 · pending |
| Kas, laporan akuntansi, payroll, komisi | Belum dibuat; dashboard ledger bukan laporan pendapatan akrual | 4 · pending |
| Hotspot/voucher, marketing, portal promo | Belum dibuat | 4 · pending |
| Upload/impor | Endpoint tidak tersedia; ekspor pelanggan dibatasi izin dan 10.000 baris | 4 · limited |
| Audit | Pelaku/tenant/target/aksi/detail/correlation ID; read-only via aplikasi | 2 · implemented; WORM/retensi pending |
| Auth | CSRF, hashing PBKDF2 Django, rate limit DB, reset sekali pakai, idle/absolute expiry | 2 · implemented |
| MFA dan editor role | TOTP wajib operator, QR lokal, secret Fernet, recovery sekali pakai, step-up, editor akun/peran owner | 2 · implemented lokal |
| Profil mandiri, scope wilayah geografis | Penugasan teknisi tersedia; profil mandiri dan batas wilayah geografis belum tersedia | 4 · pending |
| Backup/restore | SQLite consistent backup + restore/reconcile drill ke direktori kosong | 2 · implemented; enkripsi/offsite pending |
| Rilis produksi | TLS, SMTP, secret vault, database/queue multiworker, beban, lab, penetration test | 5 · pending |

Langkah implementasi berikutnya: migrasi PostgreSQL dan pengujian race lintas proses; adapter gateway sandbox terverifikasi; router/RADIUS lab. Cakupan lanjutan mengikuti urutan brief, tanpa mengubah status fitur pending menjadi selesai.
