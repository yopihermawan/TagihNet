# Matriks fitur — checkpoint billing dan operasional

Status `implemented` berarti dapat dijalankan lokal; `simulated` tidak menghubungi layanan nyata. Tidak ada klaim seluruh brief selesai.

| Cakupan | Implementasi | Fase / status |
| --- | --- | --- |
| Brand, shell, login | Logo SVG orisinal, grup navigasi native dapat dilipat/berizin, pencarian grup tertutup, fokus mobile, dark mode | 1–2 · implemented |
| Dashboard billing | Pembayaran berdasarkan tanggal transaksi, faktur berdasarkan periode layanan, piutang/tunggakan seluruh periode ISP | 2 · implemented |
| Monitoring NOC | NAS/snapshot sesi PPPoE contoh, segar/stale/gagal, nol vs unknown, ringkasan bersumber/cakupan; histori dan collector idempoten | 3 · simulated; trafik/polling nyata pending |
| Pelanggan | Satu paket/layanan, cari/filter/sort/URL/pagination, tambah/ubah/detail, deteksi duplikat, CSV | 2 · implemented |
| Paket/profil | Katalog tenant, buat/ubah/arsip/aktifkan kembali, tinjauan/MFA/versi/replay/audit; paket arsip dibatasi untuk penugasan baru, pelanggan/PSB/billing lama tetap dilayani | 2 · implemented lokal; jadwal harga/profil router nyata pending |
| Filter NAS/jatuh tempo | Pemetaan NAS simulasi nullable per pelanggan, tenant/CSRF/audit, filter NAS/unassigned dan overdue/today/next7/unpaid/clear berbasis ledger, piutang/tanggal terawal, query ekspor sama | 3 · implemented lokal; sesi/router nyata pending |
| Detail pelanggan bertab | Ringkasan/layanan/faktur/pembayaran/tiket/lokasi/riwayat, izin server per tab, pagination, ledger tanpa query per baris, URL + scroll kembali | 3 · implemented lokal; drawer ringkasan privat/keyboard/mobile/fallback tanpa JS; topologi administratif tersedia terpisah |
| Multi-tenant | Membership satu ISP per akun; tenant dari login, dua tenant seed, objek/PDF/CSV terbatasi | 2 · implemented |
| Role dan cakupan | Delapan preset termasuk Teknisi dan Sales; peran kustom tenant, pencabutan sesi, owner terakhir dilindungi. Teknisi dibatasi penugasan, sales prospek/komisi sendiri; scope wilayah geografis belum | 2 · limited |
| Faktur | Bulanan dengan opsi prorata saat aktivasi PSB, tanggal akhir bulan, snapshot harga/pajak/diskon/penerbit saat terbit, constraint satu pelanggan/periode | 2 · implemented |
| Penerbitan massal | Preview <=500 pelanggan, stale/expiry, MFA, snapshot persetujuan, worker per baris, deduplikasi/resume/cancel | 3 · implemented lokal |
| Scheduler bulanan | Opt-in/review/MFA, tanggal WIB 1–31/clamp, cakupan dinamis <=500, snapshot/worker/retry/dedupe, pembatalan dan consent revocation | 3 · implemented lokal; simulasi; backfill dan jadwal per pelanggan pending |
| Pengingat billing/SLA | Kebijakan opt-in/MFA, outbox simulasi, dedupe harian/deadline+siklus, recheck pelunasan/resolusi/izin, retry <=3 | 3/4 · simulated; kanal nyata pending |
| Pembayaran | Integer rupiah, sebagian/penuh, overpay ditolak, UUID idempotensi persisten, reversal penuh | 2 · simulated |
| Kuitansi pembayaran | Satu transaksi/sebagian, nomor stabil/WIB/terbilang, HTML/PDF A4/font lokal, reversal terkini, scope operator/portal, penerbit saat pembayaran dicatat, read-only | 2 · implemented lokal; simulasi; tanda tangan/verifikasi publik pending |
| Gateway/webhook | Endpoint gateway tidak didaftarkan; tidak ada signature/settlement produksi | 3 · pending |
| PDF | ReportLab lokal, A4 multipage, total/snapshot/label simulasi, footer plain text | 2 · implemented |
| Setelan billing | Identitas penerbit, pajak, jatuh tempo, tenggang isolir 0–30 hari, footer, tinjauan dan MFA. Linimasa bulan pilihan membandingkan tanggal sebelum dan sesudah perubahan | 2 · implemented lokal; siklus fleksibel masih tertunda |
| Isolir/aktivasi | Jadwal isolir opsional dengan tinjauan dan MFA, cakupan mulai tanggal aktivasi WIB, deduplikasi, pemeriksaan ulang saldo dan persetujuan, tiga percobaan per job | 3 · simulated; provisioning perangkat nyata pending |
| Pemulihan antrean jaringan | Filter dan detail job, pengaturan skenario router per pelanggan, alasan, tinjauan dan MFA, job pengganti tertaut, deduplikasi dan pemeriksaan persetujuan saat eksekusi | 3 · simulated |
| MikroTik/RADIUS/CoA | Belum ada adapter read-only/write nyata, lab, VPN collector, accounting | 3 · pending |
| Portal | Ringkasan saldo dan jatuh tempo, filter/paginasi tagihan dan pembayaran sendiri, bulan pembayaran WIB, PDF/kuitansi, serta pengajuan upgrade lewat tiket penagihan | 2/4 · implemented lokal; perubahan layanan melalui operator |
| Lokasi/peta | Koordinat WGS84 pelanggan/PSB nullable, paired/range checks, editor berscope/stale, portal sendiri/teknisi ditugaskan, copy aktivasi, tautan OpenStreetMap tanpa request otomatis | 2/4 · implemented lokal; akurasi lapangan/geofencing/geocoder pending; topologi administratif tersedia |
| Tiket/portal/teknisi | Kategori/prioritas/SLA snapshot, komentar publik/internal, state machine/version/replay, assignment-only teknisi | 4 · implemented lokal |
| PSB | Prospek sampai Terpasang, jadwal/penugasan, cancel beralasan, tinjauan periode/prorata opsional, MFA, customer+invoice+job atomik | 4 · implemented lokal |
| Edit/jadwal ulang PSB | Edit sebelum Pengerjaan; perubahan pada Terjadwal kembali Survei; reschedule beralasan/version/replay dan histori | 4 · implemented lokal |
| Foto pekerjaan privat | JPG/PNG tiket/PSB, decode/re-encode/metadata strip, scope internal/portal/penugasan, bounded transport/kuota/replay/audit, backup binary | 4 · implemented lokal; format lain/delete/retensi/storage eksternal pending |
| Eskalasi/kanal nyata | Kanal notifikasi, eskalasi ke pihak lain dan jam kerja SLA belum tersedia | 4 · pending |
| Laporan penagihan | Penerimaan/koreksi berdasar waktu peristiwa WIB, faktur terbit, piutang as-of/roll-forward, snapshot baca konsisten, CSV berizin/aman | 4 · implemented lokal; pendapatan akrual pending; kas/pengeluaran/tutup buku tersedia terpisah |
| Kas/dana/pengeluaran/tutup buku | Dana eksplisit, pemetaan pembayaran opsional, arus kas WIB, modal/penerimaan/pengeluaran, transfer dua sisi, koreksi historis, snapshot bulan selesai | 4 · implemented lokal; akrual/pajak/rekonsiliasi bank pending |
| Tim/payroll/slip sendiri | Komponen gaji terpisah, snapshot/approve/pay/reversal, 1–200 anggota, satu run terbuka/periode, slip historis milik sendiri | 4 · implemented lokal; absensi/PPh/BPJS/transfer bank pending |
| Sales/prospek/komisi | Preset own-assignment, konversi satu PSB, komisi historis setelah aktivasi, catat pembayaran/koreksi terpisah | 4 · implemented lokal; komisi bertingkat/berulang pending |
| Hotspot/voucher | Profil/batch snapshot, penjualan kas sekali, kredensial acak terenkripsi, PDF privat/step-up, aktivasi/expiry/revoke/refund | 4 · simulated; RADIUS/sesi/internet nyata pending |
| Marketing/promo | Kampanye bertanggal dan publikasi opt-in, paket aktif/harga katalog terkini, halaman publik UUID/telepon manual | 4 · implemented lokal; otomasi marketing/kanal pesan pending |
| Topologi ODC/ODP | Inventaris induk/kapasitas/lokasi, port unik per pelanggan, pemindahan/pencabutan, baca NOC tanpa finansial | 4 · implemented lokal; verifikasi lapangan/OLT provisioning pending |
| Impor/ekspor pelanggan | CSV UTF-8 <=200 baris/128 KiB, preview privat/MFA/atomic create-only, laporan formula-safe; ekspor <=10.000 baris | 4 · implemented lokal; lampiran/binary import pending |
| Audit | Filter tanggal WIB, username, aksi/kelompok aksi, target dan ID korelasi per ISP. Tautan dari batch faktur dan job router simulasi | 2 · implemented lokal; WORM/retensi pending |
| Auth | CSRF, hashing PBKDF2 Django, rate limit DB, reset sekali pakai, idle/absolute expiry | 2 · implemented |
| MFA dan editor role | TOTP wajib operator, QR lokal, secret Fernet, recovery sekali pakai, step-up, editor akun/peran owner | 2 · implemented lokal |
| Keamanan akun mandiri | Ubah sandi dengan sandi lama/MFA/CSRF/rate limit, pemeriksaan sesi dalam transaksi, pencabutan sesi dan audit | 4 · implemented lokal |
| Profil mandiri | Nama tampilan sendiri, informasi akun, token sesi/snapshot15menit, konflik bersamaan, audit atomik; sandi/MFA/akses dan consent tetap | 4 · implemented lokal; perubahan username/email/avatar pending |
| Scope wilayah geografis | Penugasan teknisi tersedia; batas wilayah geografis belum tersedia | 4 · pending |
| Backup/restore | AES-256-GCM streaming SQLite/PG, inventaris offline dan pencocokan paket, target kosong/no-clobber, restore ledger+MFA | 5 · implemented lokal; offsite/retensi/PITR pending |
| Backend PostgreSQL | Lab non-superuser. PostgreSQL lulus 921 tes dan SQLite lulus 920 tes, dengan satu tes penguncian PostgreSQL dilewati di SQLite. Migrasi `0018` diuji dari backup `0017` | 5 · implemented lokal; migrasi data/kapasitas produksi pending |
| Staging lokal | Gunicorn HTTPS/WhiteNoise, PG verify-full, migrator/runtime terpisah, secret file, non-root/read-only, restart worker, bootstrap/MFA dan guard volume | 5 · implemented lokal; simulasi; VPS/domain/kapasitas/offsite pending |
| Recovery staging | Restore paket terpantau ke database uji, rekonsiliasi baris/kolom/sequence, restore proyek baru dengan sesi baru dan worker ditahan untuk tinjauan | 5 · implemented lokal; offsite/PITR/RPO/RTO pending |
| Operasi staging | HTTPS/SQL/sertifikat, heartbeat, antrean, estimasi ruang recovery VFS, sambungan ulang container tersimpan, serta backup terpantau periodik dan manual | 5 · implemented lokal; alert eksternal/systemd/offsite/retensi pending |
| Uji beban staging | Klien HTTPS terbatas, 500 pelanggan fiktif, batch worker500, latency/error/resource/health gates dan ledger replay; query dashboard/faktur dibatasi | 5 · measured lokal; kapasitas/soak produksi pending |
| Paket pemulihan | Arsip/manifest terautentikasi, kunci terpisah, penyiapan pasangan backup terpantau, verify offline, freeze input dan restore setelah sumber fiktif hilang | 5 · implemented lokal; media/host/custody offsite pending |
| Kandidat rilis staging | Pin image ID per proyek, pemeriksaan isi/dependency/schema/container/TLS/backup, opsi satu perintah untuk paket pemulihan, dan penolakan jika metadata berubah saat verifikasi | 5 · implemented lokal; sertifikasi/deployment produksi pending |
| Latihan rollback image | Kandidat normal/gagal dengan schema sama, pin/up/failure/rollback, sesi target, seluruh tabel/sequence/recorder serta hold worker pada fixture terpisah | 5 · verified lokal; migrasi lintas versi/cutover produksi pending |
| Rilis produksi | TLS, SMTP, secret vault, queue/capacity multiworker, beban, lab perangkat, penetration test | 5 · pending |

Dukungan PostgreSQL dan race dua proses kini diuji lokal; perpindahan data/kapasitas produksi masih pending. Langkah berikutnya mencakup adapter gateway sandbox terverifikasi, router/RADIUS lab dan modul tersisa sesuai brief. Cakupan lanjutan mengikuti urutan brief, tanpa mengubah status fitur pending menjadi selesai.
