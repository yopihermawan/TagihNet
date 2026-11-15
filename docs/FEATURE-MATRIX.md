# Matriks fitur — checkpoint billing dan operasional

Status `implemented` berarti dapat dijalankan lokal; `simulated` tidak menghubungi layanan nyata. Tidak ada klaim seluruh brief selesai.

| Cakupan | Implementasi | Fase / status |
| --- | --- | --- |
| Brand, shell, login | Logo SVG orisinal, sidebar berbasis peran, pencarian menu, mobile, dark mode | 1–2 · implemented |
| Dashboard billing | Pembayaran berdasarkan tanggal transaksi, faktur berdasarkan periode layanan, piutang/tunggakan seluruh periode ISP | 2 · implemented |
| Monitoring NOC | NAS/snapshot sesi PPPoE contoh, segar/stale/gagal, nol vs unknown, ringkasan bersumber/cakupan; histori dan collector idempoten | 3 · simulated; trafik/polling nyata pending |
| Pelanggan | Satu paket/layanan, cari/filter/sort/URL/pagination, tambah/ubah/detail, deteksi duplikat, CSV | 2 · implemented |
| Paket/profil | Katalog tenant, buat/ubah, tinjauan rincian/dampak, MFA, versi/replay/audit; faktur lama dan batch disetujui mempertahankan snapshot | 2 · implemented lokal; arsip/jadwal harga/profil router nyata pending |
| Filter NAS/jatuh tempo | Pemetaan NAS simulasi nullable per pelanggan, tenant/CSRF/audit, filter NAS/unassigned dan overdue/today/next7/unpaid/clear berbasis ledger, piutang/tanggal terawal, query ekspor sama | 3 · implemented lokal; sesi/router nyata pending |
| Detail pelanggan bertab | Ringkasan/layanan/faktur/pembayaran/tiket/lokasi/riwayat, izin server per tab, pagination, ledger tanpa query per baris, URL + scroll kembali | 3 · implemented lokal; drawer overlay dan koordinat/topologi pending |
| Multi-tenant | Membership satu ISP per akun; tenant dari login, dua tenant seed, objek/PDF/CSV terbatasi | 2 · implemented |
| Role dan cakupan | Tujuh preset termasuk Teknisi dan peran kustom tenant; owner membuat/mengubah akun dan izin, sesi dicabut, owner terakhir dilindungi. Teknisi dibatasi penugasan; sales dan scope wilayah geografis belum | 2 · limited |
| Faktur | Bulanan full month, tanggal akhir bulan, snapshot harga/pajak/diskon, constraint satu pelanggan/periode | 2 · implemented |
| Penerbitan massal | Preview <=500 pelanggan, stale/expiry, MFA, snapshot persetujuan, worker per baris, deduplikasi/resume/cancel | 3 · implemented lokal |
| Scheduler bulanan | Opt-in/review/MFA, tanggal WIB 1–31/clamp, cakupan dinamis <=500, snapshot/worker/retry/dedupe, pembatalan dan consent revocation | 3 · implemented lokal; simulasi; backfill/prorata/per-pelanggan pending |
| Pengingat billing/SLA | Kebijakan opt-in/MFA, outbox simulasi, dedupe harian/deadline+siklus, recheck pelunasan/resolusi/izin, retry <=3 | 3/4 · simulated; kanal nyata pending |
| Pembayaran | Integer rupiah, sebagian/penuh, overpay ditolak, UUID idempotensi persisten, reversal penuh | 2 · simulated |
| Kuitansi pembayaran | Satu transaksi/sebagian, nomor stabil/WIB/terbilang, HTML/PDF A4/font lokal, reversal terkini, scope operator/portal, read-only | 2 · implemented lokal; simulasi; tanda tangan/verifikasi publik pending |
| Gateway/webhook | Endpoint gateway tidak didaftarkan; tidak ada signature/settlement produksi | 3 · pending |
| PDF | ReportLab lokal, A4 multipage, total/snapshot/label simulasi, footer plain text | 2 · implemented |
| Setelan billing | Pajak basis poin termasuk nol, tanggal 1–31, footer teks, hanya berlaku pada faktur berikutnya | 2 · limited |
| Isolir/aktivasi | Recheck utang saat eksekusi; adapter palsu, backoff 30/60/120 detik, maksimal 3 percobaan | 3 · simulated |
| MikroTik/RADIUS/CoA | Belum ada adapter read-only/write nyata, lab, VPN collector, accounting | 3 · pending |
| Portal | Pelanggan melihat layanan/faktur/PDF dan kuitansi transaksi milik sendiri | 2 · limited |
| Peta | Tautan pencarian OpenStreetMap; belum koordinat/topologi ODC/ODP | 2 · limited |
| Tiket/portal/teknisi | Kategori/prioritas/SLA snapshot, komentar publik/internal, state machine/version/replay, assignment-only teknisi | 4 · implemented lokal |
| PSB | Prospek sampai Terpasang, jadwal/penugasan, cancel beralasan, quote aktivasi/MFA, customer+invoice+job atomik | 4 · implemented lokal |
| Edit/jadwal ulang PSB | Edit sebelum Pengerjaan; perubahan pada Terjadwal kembali Survei; reschedule beralasan/version/replay dan histori | 4 · implemented lokal |
| Foto pekerjaan privat | JPG/PNG tiket/PSB, decode/re-encode/metadata strip, scope internal/portal/penugasan, bounded transport/kuota/replay/audit, backup binary | 4 · implemented lokal; format lain/delete/retensi/storage eksternal pending |
| Eskalasi/kanal nyata | Kanal notifikasi, eskalasi ke pihak lain dan jam kerja SLA belum tersedia | 4 · pending |
| Laporan penagihan | Penerimaan/koreksi berdasar waktu peristiwa WIB, faktur terbit, piutang as-of/roll-forward, snapshot baca konsisten, CSV berizin/aman | 4 · implemented lokal; kas/pengeluaran/pendapatan akrual/tutup buku pending |
| Kas, laporan akuntansi, payroll, komisi | Belum dibuat; dashboard ledger bukan laporan pendapatan akrual | 4 · pending |
| Hotspot/voucher, marketing, portal promo | Belum dibuat | 4 · pending |
| Impor/ekspor pelanggan | CSV UTF-8 <=200 baris/128 KiB, preview privat/MFA/atomic create-only, laporan formula-safe; ekspor <=10.000 baris | 4 · implemented lokal; lampiran/binary import pending |
| Audit | Pelaku/tenant/target/aksi/detail/correlation ID; read-only via aplikasi | 2 · implemented; WORM/retensi pending |
| Auth | CSRF, hashing PBKDF2 Django, rate limit DB, reset sekali pakai, idle/absolute expiry | 2 · implemented |
| MFA dan editor role | TOTP wajib operator, QR lokal, secret Fernet, recovery sekali pakai, step-up, editor akun/peran owner | 2 · implemented lokal |
| Profil mandiri, scope wilayah geografis | Penugasan teknisi tersedia; profil mandiri dan batas wilayah geografis belum tersedia | 4 · pending |
| Backup/restore | AES-256-GCM streaming SQLite/PG, kunci terpisah, verify, target kosong/no-clobber, restore ledger+MFA | 5 · implemented lokal; offsite/retensi/PITR pending |
| Backend PostgreSQL | Opsional, role non-superuser lab, 588 tes pada dua backend, 17 skenario lintas proses + pg_dump/pg_restore pada checkpoint sebelumnya | 5 · implemented lokal; migrasi data/kapasitas produksi pending |
| Staging lokal | Gunicorn HTTPS/WhiteNoise, PG verify-full, migrator/runtime terpisah, secret file, non-root/read-only, restart worker, bootstrap/MFA dan guard volume | 5 · implemented lokal; simulasi; VPS/domain/kapasitas/offsite pending |
| Recovery staging | Backup streaming AES-GCM, fresh-project SQL restore, identitas/empty/key/lock guards, sesi baru, MFA tetap, worker hold/review dan rekonsiliasi tabel/sequence | 5 · implemented lokal; offsite/PITR/RPO/RTO pending |
| Operasi staging | HTTPS/SQL/sertifikat/loop heartbeat/antrean, log transisi privat, watcher lokal dan backup periodik opt-in terautentikasi | 5 · implemented lokal; alert eksternal/systemd/offsite/retensi pending |
| Uji beban staging | Klien HTTPS terbatas, 500 pelanggan fiktif, batch worker500, latency/error/resource/health gates dan ledger replay; query dashboard/faktur dibatasi | 5 · measured lokal; kapasitas/soak produksi pending |
| Paket pemulihan | Arsip/manifest terautentikasi, kunci di folder terpisah, verify offline, freeze input dan restore setelah sumber fiktif hilang | 5 · implemented lokal; media/host/custody offsite pending |
| Kandidat rilis staging | Pin image ID per proyek, isi/dependency/schema/container/TLS/backup+custody gates, bukti read-only dan guard metadata migrasi | 5 · implemented lokal; bukan sertifikasi/deployment produksi |
| Latihan rollback image | Kandidat normal/gagal dengan schema sama, pin/up/failure/rollback, sesi target, seluruh tabel/sequence/recorder serta hold worker pada fixture terpisah | 5 · verified lokal; migrasi lintas versi/cutover produksi pending |
| Rilis produksi | TLS, SMTP, secret vault, queue/capacity multiworker, beban, lab perangkat, penetration test | 5 · pending |

Dukungan PostgreSQL dan race dua proses kini diuji lokal; perpindahan data/kapasitas produksi masih pending. Langkah berikutnya mencakup adapter gateway sandbox terverifikasi, router/RADIUS lab dan modul tersisa sesuai brief. Cakupan lanjutan mengikuti urutan brief, tanpa mengubah status fitur pending menjadi selesai.
