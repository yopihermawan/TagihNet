# Operasional dan pemulihan

## Lingkup runtime

Checkpoint ini disiapkan untuk Linux, Python 3.12, Django 5.2, dan satu file SQLite. Semua integrasi berjalan simulasi. Docker/Windows disediakan sebagai konfigurasi pengembangan, belum diverifikasi eksekusinya. Jangan expose Django runserver ke internet atau menganggap konfigurasi ini production-ready.

## Instalasi berulang

Gunakan checkout yang ada di `/workspace/TagihNet`; task cloud sudah terisolasi, tidak memerlukan worktree tambahan. Buat/aktifkan `.venv`, pasang `requirements.txt`, `migrate --noinput`, lalu `seed_demo` hanya di development. Tidak ada Node build. Pengujian browser/PDF memakai `requirements-dev.txt` dan Chromium lokal. Dependensi/kode/database bertahan pada snapshot; proses server/worker harus dijalankan ulang.

## Migrasi dan rollback

`core/migrations/0001_initial.py` merupakan skema awal. Sebelum migrasi berikutnya: hentikan penulisan, ambil backup konsisten, uji migrasi pada salinan, bandingkan jumlah pelanggan/faktur dan saldo. Simpan commit aplikasi dan daftar migrasi yang cocok dengan backup. Migrasi rollback yang menghapus tabel/data tidak aman sebagai prosedur pemulihan; pulihkan backup ke direktori baru dan jalankan commit aplikasi yang kompatibel, lalu pindahkan lokasi database saat semua proses berhenti. Belum ada perpindahan data produksi yang dilakukan.

```bash
python manage.py showmigrations
python manage.py migrate --plan
python scripts/backup.py backup /lokasi/privat/tagihnet-YYYYMMDD.sqlite3
python manage.py migrate --noinput
python manage.py check
```

## Backup dan restore drill

`scripts/backup.py` memakai API backup SQLite dan `PRAGMA integrity_check`, bukan menyalin file database saat penulisan berjalan. File tujuan tidak boleh ada dan dibuat dengan mode 0600 pada POSIX. SQLite backup ini **belum terenkripsi**. Untuk produksi perlu vault/key terpisah, enkripsi authenticated, offsite storage, retensi, dan alarm backup; jangan menggunakan data pelanggan nyata sebelum itu siap.

```bash
python scripts/verify_restore.py
# Restore manual ke direktori kosong, dengan server/worker tujuan berhenti:
TAGIHNET_DATA_DIR=/lokasi/restore-kosong python scripts/backup.py restore /lokasi/privat/backup.sqlite3
```

Drill otomatis membandingkan jumlah pelanggan, jumlah faktur, total tagihan, pembayaran setelah reversal, dan piutang. Tidak mengganti data aktif. Backup database tidak mencakup kunci sesi atau kunci MFA. `data/.mfa_key` / TAGIHNET_MFA_KEY harus dipulihkan dari vault terpisah agar TOTP dapat diverifikasi; jangan mengganti kunci enkripsi tanpa re-enkripsi data. Lihat AUTHENTICATION.md. Untuk kunci sesi, simpan secret key secara terpisah dan terenkripsi, atau rotasi saat pemulihan untuk mencabut sesi. Email pemulihan development tidak perlu diarsipkan. RPO/RTO produksi belum ditetapkan atau diuji.

## Worker dan monitoring

```bash
python manage.py run_simulation_worker --once
python manage.py run_simulation_worker
```

Worker memproses maksimal 100 job per batch, cek ulang sisa tagihan yang overdue, lalu adapter simulasi. Backoff kegagalan 30, 60, 120 detik; setelah tiga percobaan, job tetap `failed`. Job confirmed tidak dieksekusi ulang. Pembayaran tidak menunggu worker. Tidak ada klaim sesi PPP nyata terputus/aktif. UI menampilkan pending/failed dan status layanan terpisah. Belum ada email/alert eksternal atau supervisor; deployment memerlukan keduanya.

SQLite memiliki retry transaksi untuk konflik lock lokal; itu bukan pengganti lock/queue terdistribusi. Untuk multiworker produksi, gunakan PostgreSQL/outbox/claim job, transaction locking lintas proses, rekonsiliasi periodik, serta pengujian beban dan race pada konfigurasi target. Worker penerbitan massal tersedia melalui `run_billing_worker`, tetapi hanya memproses batch yang telah dikonfirmasi dengan MFA; tidak membuat persetujuan sendiri. Scheduler faktur tanpa konfirmasi belum tersedia. Worker pengingat simulasi opt-in tersedia pada checkpoint 0005; kanal pengiriman nyata belum tersedia.

`GET /health/` menjalankan `SELECT 1` dan hanya mengembalikan status/mode. Audit memuat ID korelasi, actor, tenant, action, target dan detail terpilih tanpa password/cookie. Log worker hanya ID/status/attempts. Retensi audit/attempts/email lokal harus dikonfigurasi; log append-only via aplikasi belum tahan perubahan administrator database.

## Secret, TLS, dan deployment berikutnya

- Tetapkan `TAGIHNET_DEBUG=0`, secret key acak minimal 50 karakter, TAGIHNET_MFA_KEY Fernet terpisah, host yang eksplisit, SMTP/TLS, dan cookie Secure. Settings tanpa secret ditolak saat DEBUG off. `check --deploy` harus dijalankan pada konfigurasi target HTTPS.
- Gunakan server WSGI produksi dan reverse proxy HTTPS yang divalidasi. Jangan mempercayai header forwarded dari internet; konfigurasi proxy harus direview sebelum mengaktifkan redirect/forwarded-proto. Konfigurasi saat ini tidak mempercayai `X-Forwarded-For` untuk rate limit.
- Jangan publikasikan akun seed. MFA dan editor role telah tersedia lokal; audit independen, autentikasi gateway, enkripsi backup dan uji target database tetap gate produksi.
- `SIMULATION=0` **tidak** mengaktifkan integrasi nyata. Pencatatan pembayaran/worker produksi ditolak. Tidak ada kunci router/gateway yang diperlukan untuk demo ini. Jangan memasukkan secret ke chat, source, `.env.example`, screenshot, atau setup script.
- Backup/restore, deployment Windows/Docker, lab MikroTik/FreeRADIUS, webhook sandbox, dan pemulihan HTTPS harus diverifikasi pada infrastruktur sebenarnya sebelum rilis.

## Aturan domain billing

Satu customer = satu layanan/paket. Faktur bulanan full-month, satu customer/periode, tanpa prorata. Tanggal bisnis WIB dari timestamp UTC (`USE_TZ=True`). Jatuh tempo 29–31 dijepit ke hari terakhir bulan termasuk Februari tahun kabisat. Perubahan paket/pajak/footer berlaku pada faktur baru; snapshot lama tidak diubah. Pajak basis poin, nol eksplisit, pembulatan half-up ke rupiah pada pajak total. Mata uang hanya IDR, integer rupiah. Pembayaran sebagian diizinkan; lebih bayar dan faktur nol ditolak. Reversal penuh tidak menghapus payment. Pembatalan faktur, adjustment parsial, refund gateway, cicilan terjadwal, revenue recognition, dan tutup buku belum tersedia.

## Migrasi checkpoint akses

Migrasi `0002` menambah perangkat MFA, role kustom, limiter atomic, dan versi sesi tanpa menghapus ledger. Sesi lama tidak dianggap terverifikasi dan harus login ulang. Operator tanpa MFA diarahkan ke enrollment setelah password benar. Tidak ada enrollment otomatis atau reset perangkat pengguna saat seed diulang. `reset_mfa` adalah pemulihan offline oleh administrator host setelah verifikasi identitas, bukan fitur reset publik.

## Worker faktur massal dan migrasi 0003

Sebelum migrasi 0003 lokal, backup konsisten dibuat di direktori privat `data/backups/`. Migrasi menambah BillingBatch/BillingBatchItem/index/constraint tanpa mengubah ledger lama. Jalankan `python manage.py run_billing_worker` sebagai proses terpisah dari web dan worker jaringan. `--once --limit 100` memproses satu putaran maksimal 10 batch antre/processing; batas 1–500 baris per batch. Proses dapat dihentikan dan diulang tanpa menggandakan invoice. Validasi atau perubahan hak pembuat memblokir sisa batch; jangan memaksa status melalui SQL.

Backup/restore kini juga membandingkan jumlah batch, state batch dan jumlah item per status. Uji browser melakukan drill pada antrean parsial, selain rekonsiliasi saldo. Restorasi autentikasi tetap membutuhkan kunci MFA terpisah. Scheduler, supervisor produksi, kapasitas PostgreSQL/multi-process, dan alert eksternal masih belum diverifikasi. Detail status dan pemulihan ada di BULK-BILLING.md.

## Tiket/PSB, migrasi 0004

Backup konsisten sebelum migrasi tersimpan privat di data/backups. Migrasi 0004 menambah field SLA, pilihan status pending/role Teknisi dan tabel Ticket/Installation/WorkEvent tanpa mengubah data invoice lama. Restart web untuk memuat route/model baru. Tidak ada worker baru; job provisioning PSB menggunakan run_simulation_worker. Ticket tidak mengirim pesan/notifikasi otomatis. SLA berjalan terus dan memakai snapshot awal.

Backup/restore SQL juga membandingkan state tiket, state PSB dan jumlah WorkEvent. operations_smoke.py melakukan restore drill dengan data operasional nyata di database sementara; kunci MFA tetap dikelola terpisah dan tidak diuji sebagai bagian restore SQL. PSB teraktivasi berarti administrasi selesai, bukan bukti router online/lunas. Edit/jadwal ulang PSB tersedia pada checkpoint 0005. Lampiran, kanal eskalasi SLA nyata dan scope wilayah geografis masih belum tersedia. Panduan pada SUPPORT-AND-INSTALLATIONS.md.

## Migrasi 0005 dan worker pengingat

Backup konsisten privat dibuat sebelum migrasi 0005, yang menambah Ticket.sla_cycle dan tabel ReminderPolicy/Reminder tanpa mengubah ledger lama. Restart web dan worker lama, lalu jalankan `python manage.py run_reminder_worker` sebagai proses keempat. Mode `--once --limit 100` dibatasi 1–500 objek per kebijakan dan 1–500 pemrosesan per putaran. Maksimal sepuluh kebijakan dipilih bergilir berdasarkan waktu pemeriksaan; kebijakan dengan izin tidak berlaku juga diputar. SLA dipilih sebelum billing dalam satu kebijakan. Loop menunggu lima detik; ini bukan jaminan kapasitas/latensi produksi.

Kebijakan nonaktif secara bawaan; worker tidak membuat atau menyetujuinya. Tidak ada pengiriman SMTP/HTTP/WhatsApp. Gagal adapter simulasi memakai backoff 30/60 detik sebelum percobaan berikutnya, maksimal tiga; nilai next_attempt_at terakhir tidak berarti ada percobaan keempat. `--once` keluar nonzero bila ada proses gagal. Billing yang menunggu jam baru dijadwalkan ulang tanpa mengonsumsi percobaan. Antrean tertahan karena akses perlu persetujuan ulang MFA; jangan mengubah status melalui SQL. Detail deduplikasi/expiry ada di REMINDERS-AND-RESCHEDULING.md.

Restore drill kini membandingkan status outbox, konfigurasi/versi persetujuan kebijakan, siklus/versi tiket dan versi/jadwal PSB selain ledger/state lama. Browser terisolasi mengisi dan memulihkan data tersebut. Uji ini tidak memverifikasi pemulihan MFA tanpa kunci terpisah, layanan pengiriman nyata, atau database multi-process produksi.

## Monitoring NAS dan migrasi 0006

Migrasi 0006 menambah MonitoringDevice/MonitoringPoll, unique constraint perangkat/versi/menit, constraint waktu snapshot dan index jadwal/history. Sebelum penerapan, empat proses milik sesi dihentikan, backup privat konsisten dibuat, lalu migrasi diuji pada hasil restore sementara. Jumlah pelanggan, faktur, pembayaran/reversal, MFA, tiket/PSB dan total ledger dibandingkan sebelum/sesudah; sama. Migrasi lalu diterapkan ke database lokal dan proses dimulai kembali.

Tidak ada worker kelima. `run_simulation_worker` sekarang memanggil collector monitoring setelah rekonsiliasi. `collect_monitoring --limit 100` hanya satu putaran NAS enabled yang jatuh tempo, urut waktu terlama; limit 1–500. Tiap NAS maksimal satu snapshot per 60 detik pada versi yang sama. Kegagalan dijadwalkan 60 detik kemudian; tidak memakai batas 3 percobaan milik job perubahan layanan. Persetujuan yang berubah menghasilkan blocked dan next_poll_at kosong, sehingga perlu konfigurasi/MFA baru sebelum lanjut. `--once` worker jaringan atau command collector keluar nonzero pada monitoring failed/blocked. Loop worker mencatat kegagalan lalu tetap berjalan.

Maksimal 50 NAS per ISP dan 100 sesi per snapshot. Simulasi saja, tanpa alamat, password, HTTP/API router atau trafik nyata. Riwayat belum dipangkas otomatis; perlu kebijakan retensi sebelum penggunaan jangka panjang. Restore SQL membandingkan konfigurasi/versi/jadwal NAS dan seluruh payload/status/waktu snapshot, di samping state/ledger lama. Uji browser menggunakan database sementara berisi snapshot sukses, gagal, stale dan kosong untuk drill tersebut. Panduan di NETWORK-MONITORING.md.
