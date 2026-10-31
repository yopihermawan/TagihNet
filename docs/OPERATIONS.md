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

Drill otomatis membandingkan jumlah pelanggan, jumlah faktur, total tagihan, pembayaran setelah reversal, dan piutang. Tidak mengganti data aktif. Backup database tidak mencakup kunci sesi; simpan secret key secara terpisah dan terenkripsi, atau rotasi saat pemulihan untuk mencabut sesi. Email pemulihan development tidak perlu diarsipkan. RPO/RTO produksi belum ditetapkan atau diuji.

## Worker dan monitoring

```bash
python manage.py run_simulation_worker --once
python manage.py run_simulation_worker
```

Worker memproses maksimal 100 job per batch, cek ulang sisa tagihan yang overdue, lalu adapter simulasi. Backoff kegagalan 30, 60, 120 detik; setelah tiga percobaan, job tetap `failed`. Job confirmed tidak dieksekusi ulang. Pembayaran tidak menunggu worker. Tidak ada klaim sesi PPP nyata terputus/aktif. UI menampilkan pending/failed dan status layanan terpisah. Belum ada email/alert eksternal atau supervisor; deployment memerlukan keduanya.

SQLite memiliki retry transaksi untuk konflik lock lokal; itu bukan pengganti lock/queue terdistribusi. Untuk multiworker produksi, gunakan PostgreSQL/outbox/claim job, transaction locking lintas proses, rekonsiliasi periodik, serta pengujian beban dan race pada konfigurasi target. Tidak ada scheduler faktur/pengingat otomatis pada checkpoint ini.

`GET /health/` menjalankan `SELECT 1` dan hanya mengembalikan status/mode. Audit memuat ID korelasi, actor, tenant, action, target dan detail terpilih tanpa password/cookie. Log worker hanya ID/status/attempts. Retensi audit/attempts/email lokal harus dikonfigurasi; log append-only via aplikasi belum tahan perubahan administrator database.

## Secret, TLS, dan deployment berikutnya

- Tetapkan `TAGIHNET_DEBUG=0`, secret key acak minimal 50 karakter, host yang eksplisit, SMTP/TLS, dan cookie Secure. Settings tanpa secret ditolak saat DEBUG off. `check --deploy` harus dijalankan pada konfigurasi target HTTPS.
- Gunakan server WSGI produksi dan reverse proxy HTTPS yang divalidasi. Jangan mempercayai header forwarded dari internet; konfigurasi proxy harus direview sebelum mengaktifkan redirect/forwarded-proto. Konfigurasi saat ini tidak mempercayai `X-Forwarded-For` untuk rate limit.
- Jangan publikasikan akun seed. MFA owner, manajemen role, autentikasi gateway, enkripsi backup dan uji target database adalah gate sebelum penggunaan produksi.
- `SIMULATION=0` **tidak** mengaktifkan integrasi nyata. Pencatatan pembayaran/worker produksi ditolak. Tidak ada kunci router/gateway yang diperlukan untuk demo ini. Jangan memasukkan secret ke chat, source, `.env.example`, screenshot, atau setup script.
- Backup/restore, deployment Windows/Docker, lab MikroTik/FreeRADIUS, webhook sandbox, dan pemulihan HTTPS harus diverifikasi pada infrastruktur sebenarnya sebelum rilis.

## Aturan domain billing

Satu customer = satu layanan/paket. Faktur bulanan full-month, satu customer/periode, tanpa prorata. Tanggal bisnis WIB dari timestamp UTC (`USE_TZ=True`). Jatuh tempo 29–31 dijepit ke hari terakhir bulan termasuk Februari tahun kabisat. Perubahan paket/pajak/footer berlaku pada faktur baru; snapshot lama tidak diubah. Pajak basis poin, nol eksplisit, pembulatan half-up ke rupiah pada pajak total. Mata uang hanya IDR, integer rupiah. Pembayaran sebagian diizinkan; lebih bayar dan faktur nol ditolak. Reversal penuh tidak menghapus payment. Pembatalan faktur, adjustment parsial, refund gateway, cicilan terjadwal, revenue recognition, dan tutup buku belum tersedia.
