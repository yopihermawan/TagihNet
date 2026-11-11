# Matriks fitur — checkpoint billing dan operasional

Status `implemented` berarti dapat dijalankan lokal; `simulated` tidak menghubungi layanan nyata. Tidak ada klaim seluruh brief selesai.

| Cakupan | Implementasi | Fase / status |
| --- | --- | --- |
| Brand, shell, login | Logo SVG orisinal, sidebar berbasis peran, pencarian menu, mobile, dark mode | 1–2 · implemented |
| Dashboard billing | Pembayaran berdasarkan tanggal transaksi, faktur berdasarkan periode layanan, piutang/tunggakan seluruh periode ISP | 2 · implemented |
| Monitoring NOC | NAS/snapshot sesi PPPoE contoh, segar/stale/gagal, nol vs unknown, ringkasan bersumber/cakupan; histori dan collector idempoten | 3 · simulated; trafik/polling nyata pending |
| Pelanggan | Satu paket/layanan, cari/filter/sort/URL/pagination, tambah/ubah/detail, deteksi duplikat, CSV | 2 · implemented |
| Filter NAS/jatuh tempo; drawer bertab | NAS simulasi monitoring terpisah, belum dipetakan ke pelanggan; detail berupa halaman, URL daftar dipertahankan di sessionStorage | 3 · pending |
| Multi-tenant | Membership satu ISP per akun; tenant dari login, dua tenant seed, objek/PDF/CSV terbatasi | 2 · implemented |
| Role dan cakupan | Tujuh preset termasuk Teknisi dan peran kustom tenant; owner membuat/mengubah akun dan izin, sesi dicabut, owner terakhir dilindungi. Teknisi dibatasi penugasan; sales dan scope wilayah geografis belum | 2 · limited |
| Faktur | Bulanan full month, tanggal akhir bulan, snapshot harga/pajak/diskon, constraint satu pelanggan/periode | 2 · implemented |
| Penerbitan massal | Preview <=500 pelanggan, stale/expiry, MFA, snapshot persetujuan, worker per baris, deduplikasi/resume/cancel | 3 · implemented lokal |
| Scheduler/prorata | Penerbitan terjadwal tanpa konfirmasi per batch dan prorata belum tersedia | 3 · pending |
| Pengingat billing/SLA | Kebijakan opt-in/MFA, outbox simulasi, dedupe harian/deadline+siklus, recheck pelunasan/resolusi/izin, retry <=3 | 3/4 · simulated; kanal nyata pending |
| Pembayaran | Integer rupiah, sebagian/penuh, overpay ditolak, UUID idempotensi persisten, reversal penuh | 2 · simulated |
| Gateway/webhook | Midtrans Sandbox opt-in, redirect checkout, signature + GET status, ledger atomik/idempoten, rekonsiliasi refund/konflik saldo, daftar/filter/riwayat, expiry owner/MFA dengan intent persisten dan audit | 3 · implemented lokal; akun sandbox nyata/HTTPS publik dan produksi belum diverifikasi |
| PDF | ReportLab lokal, A4 multipage, total/snapshot/label simulasi, footer plain text | 2 · implemented |
| Setelan billing | Pajak basis poin termasuk nol, tanggal 1–31, footer teks, hanya berlaku pada faktur berikutnya | 2 · limited |
| Isolir/aktivasi | Recheck utang saat eksekusi; adapter palsu, backoff 30/60/120 detik, maksimal 3 percobaan | 3 · simulated |
| MikroTik/RADIUS/CoA | Belum ada adapter read-only/write nyata, lab, VPN collector, accounting | 3 · pending |
| Portal | Pelanggan melihat layanan/faktur/PDF milik sendiri | 2 · limited |
| Peta | Tautan pencarian OpenStreetMap; belum koordinat/topologi ODC/ODP | 2 · limited |
| Tiket/portal/teknisi | Kategori/prioritas/SLA snapshot, komentar publik/internal, state machine/version/replay, assignment-only teknisi | 4 · implemented lokal |
| PSB | Prospek sampai Terpasang, jadwal/penugasan, cancel beralasan, quote aktivasi/MFA, customer+invoice+job atomik | 4 · implemented lokal |
| Edit/jadwal ulang PSB | Edit sebelum Pengerjaan; perubahan pada Terjadwal kembali Survei; reschedule beralasan/version/replay dan histori | 4 · implemented lokal |
| Lampiran/eskalasi nyata | Upload lampiran biner, kanal notifikasi, eskalasi ke pihak lain dan jam kerja SLA belum tersedia | 4 · pending |
| Kas, laporan akuntansi, payroll, komisi | Belum dibuat; dashboard ledger bukan laporan pendapatan akrual | 4 · pending |
| Hotspot/voucher, marketing, portal promo | Belum dibuat | 4 · pending |
| Impor/ekspor pelanggan | CSV UTF-8 <=200 baris/128 KiB, preview privat/MFA/atomic create-only, laporan formula-safe; ekspor <=10.000 baris | 4 · implemented lokal; lampiran/binary import pending |
| Audit | Pelaku/tenant/target/aksi/detail/correlation ID; read-only via aplikasi | 2 · implemented; WORM/retensi pending |
| Auth | CSRF, hashing PBKDF2 Django, rate limit DB, reset sekali pakai, idle/absolute expiry | 2 · implemented |
| MFA dan editor role | TOTP wajib operator, QR lokal, secret Fernet, recovery sekali pakai, step-up, editor akun/peran owner | 2 · implemented lokal |
| Profil mandiri, scope wilayah geografis | Penugasan teknisi tersedia; profil mandiri dan batas wilayah geografis belum tersedia | 4 · pending |
| Backup/restore | AES-256-GCM streaming SQLite/PG, kunci terpisah, verify, target kosong/no-clobber, restore ledger+MFA | 5 · implemented lokal; offsite/retensi/PITR pending |
| Backend PostgreSQL | Opsional, role non-superuser lab, 316 tes pada dua backend, 16 race proses + pg_dump/pg_restore | 5 · implemented lokal; migrasi data/kapasitas produksi pending |
| Rilis produksi | TLS, SMTP, secret vault, queue/capacity multiworker, beban, lab perangkat, penetration test | 5 · pending |

Dukungan PostgreSQL dan race dua proses kini diuji lokal; perpindahan data/kapasitas produksi masih pending. Langkah berikutnya mencakup verifikasi akun Midtrans sandbox nyata/HTTPS publik, router/RADIUS lab dan modul tersisa sesuai brief. Cakupan lanjutan mengikuti urutan brief, tanpa mengubah status fitur pending menjadi selesai.
