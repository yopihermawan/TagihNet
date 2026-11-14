# Staging lokal TagihNet

Checkpoint15 November2026 menyediakan staging Linux/Docker yang terpisah dari demo development. Gunicorn27.0.1 melayani HTTPS, WhiteNoise6.12.0 melayani aset statis ber-hash, PostgreSQL16.15 memakai TLS verify-full, dan tiga worker berada pada container terpisah. DEBUG=0 dan SIMULATION=1 diwajibkan. Pengiriman email dinonaktifkan pada profil ini; gateway/router/notifikasi tetap simulasi.

## Menjalankan

Pasang Docker Engine/Compose v2 dan Python3.12. Dari root repositori:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-staging.txt
.venv/bin/python scripts/staging_lab.py prepare
.venv/bin/python scripts/staging_lab.py build
.venv/bin/python scripts/staging_lab.py up
.venv/bin/python scripts/staging_lab.py bootstrap
.venv/bin/python scripts/staging_lab.py status
```

Prepare hanya menerima direktori baru dan tidak menimpa secret. Defaultnya `data/staging/`, diabaikan Git, dengan direktori0700/file0600. Manifest menyimpan nama proyek Compose acak, port dan status inisialisasi; tidak berisi nilai credential. Port default18443 terikat loopback127.0.0.1. PostgreSQL tidak menerbitkan port host. Setiap stack mempunyai volume/project sendiri; demo SQLite dan lab PG sebelumnya tidak dipakai.

Pada cloud yang memakai CA proxy sesi, build normal dapat gagal memverifikasi sertifikat registry. Opsi berikut hanya memasang bundle CA selama langkah pip melalui BuildKit secret; tidak menyalinnya ke layer image dan tidak menonaktifkan verifikasi TLS:

```bash
.venv/bin/python scripts/staging_lab.py build --proxy-ca-file /etc/ssl/certs/ca-certificates.crt
```

Image Python dan PostgreSQL dipatok digest; versi aplikasi dipatok dalam requirements-staging.txt beserta requirements-postgres.txt. Ignore file khusus Dockerfile.staging mencegah masuknya data, `.env*`, Git, test, dokumen dan script host. Dockerfile/Compose development lama dipertahankan.

Untuk pemeriksaan HTTPS lokal, gunakan CA `data/staging/secrets/ca.crt`. Contoh client:

```bash
curl --cacert data/staging/secrets/ca.crt https://localhost:18443/health/
```

Sertifikat HTTPS/database berlaku30 hari, CA90 hari. CA hanya untuk lingkungan uji ini, bukan sertifikat domain publik. Browser memerlukan profil uji khusus yang mempercayai CA lokal; jangan mematikan verifikasi sertifikat atau memasang CA ini sebagai kebijakan global organisasi.

## Akun awal

Bootstrap eksplisit membuat satu ISP fiktif dan owner `staging-owner` pada database kosong. Kata sandi acak berada pada file privat `data/staging/secrets/bootstrap-password`; tidak dicetak ke log/argv, ditanam pada image, atau disimpan di Git. Pemilik lingkungan dapat membacanya secara lokal dengan akses file yang sesuai. Login pertama meminta pendaftaran MFA. Akun ini tidak memiliki status Django staff/superuser. Tidak ada akun/password demo bawaan.

Pada instance ini bootstrap sudah dijalankan: satu ISP/owner, tanpa pelanggan/faktur dan tanpa perangkat MFA persisten. Tes MFA berlangsung pada stack sementara lain. Jangan mengulang bootstrap untuk mereset akun: perintah menolak database yang sudah memiliki tenant/user, tidak mengubah sandi atau menambah ISP kedua. Pengelolaan akun selanjutnya memakai fitur owner. SMTP/reset email belum diaktifkan dalam profil simulasi ini.

Untuk instalasi target yang benar-benar baru, command `bootstrap_isp` juga menerima nama ISP/username/email dan path password file milik operator. Password melewati validator Django; pembuatan tenant/user/membership/audit atomik dan initial bootstrap PG memakai advisory lock.

## Secret dan hak database

Initializer prepare dipercaya hanya untuk menyalin file ke volume khusus dengan ownership/mode tepat, lalu keluar. App/worker non-root UID10001, filesystem read-only, capability dibuang dan no-new-privileges. Direktori sumber secret host tidak di-mount ke aplikasi. Root CA private key dan credential bootstrap/migrator tidak ada pada volume runtime.

- Role migrator non-superuser memiliki database/schema dan dipakai job migrasi sekali jalan.
- Role runtime non-superuser tidak memiliki CREATEDB/CREATEROLE atau CREATE schema/tabel. Hak tabel/sequence diberikan melalui default privileges migrator.
- Password database, kunci sesi dan MFA dibaca dari file; `_FILE` dan nilai env langsung untuk secret yang sama tidak boleh digunakan bersamaan. Isinya tidak disalin ke environment/argv.
- TLS PostgreSQL memverifikasi CA dan nama `db`. pg_hba menolak koneksi jaringan tanpa TLS. Unix socket dipercaya hanya di container database dan tidak dibagi ke app.
- Gunicorn menerima HTTPS langsung. Forwarded headers tidak dipercaya sebagai penentu skema/host. Konfigurasi reverse proxy lain memerlukan peninjauan tersendiri.

Container web/worker dibatasi512MiB,1CPU,128 PID. Gunicorn mempunyai dua worker, timeout/graceful30 detik dan daur ulang request. Log runtime/database dibatasi10MiB ×3 per container; access log URL dinonaktifkan agar token pada path tidak terekam. Docker restart policy menangani process exit. Health check melaporkan kondisi; status unhealthy sendiri tidak otomatis memulai ulang container atau mengirim alarm.

## Pemeriksaan dan pemulihan

```bash
.venv/bin/python scripts/verify_staging.py
.venv/bin/python scripts/staging_lab.py stop
.venv/bin/python scripts/staging_lab.py up
```

Verifier membuat project/temp-directory baru, menguji TLS, static, host, bootstrap, MFA/CSRF/cookie, SQL privilege/CA/hostname, non-root/read-only, crash worker dan restart web. Semua nilai secret/factor hanya di memori/private temp. Cleanup hanya menghapus container/volume proyek sementara yang dibuatnya. Staging persisten dan data demo tetap utuh.

`stop` mempertahankan volume dan private files. Manifest yang sudah initialized mengharuskan volume database asli beserta label proyeknya masih ada; jika hilang, `up` berhenti dan meminta pemulihan, tidak membuat pengganti kosong diam-diam. Jangan menghapus volume staging yang berisi data. Simpan file kunci dan backup pada media terpisah sebelum perubahan image/migrasi; pahami batas snapshot rollback pada BACKUPS.md.

Instance ini memiliki `data/staging/TagihNet-staging-20261115.tnbk`, pg_dump custom terenkripsi AES-GCM, dengan kunci independen `data/staging/secrets/backup-key.json`. Authentication/decryption/readback cocok byte-for-byte; plaintext staging tidak dipertahankan. SQL restore archive ini kini diuji pada checkpoint recovery ke proyek baru. CLI backup/restore dan hold worker tersedia di [STAGING-RECOVERY](STAGING-RECOVERY.md); offsite/retensi/penjadwalan host masih pending. File session/MFA/CA tidak berada dalam dump database.

Snapshot cloud filesystem tidak menjamin container/image/volume Docker dipulihkan. Periksa status/volume dan pulihkan archive secara eksplisit bila diperlukan sebelum menyalakan kembali billing. Jangan regenerasi kunci untuk membuka database lama atau bootstrap ulang untuk menyamarkan data yang hilang.

## Hasil dan batas

471 tes lulus pada masing-masing SQLite dan PostgreSQL; checkpoint staging awal menambah11 tes memeriksa profil fail-closed, file secret dan bootstrap/rollback/replay. Stack Docker sebenarnya lulus verified HTTPS, untrusted CA/Host ditolak, static ber-hash/private isolation, login/MFA/CSRF/cookie Secure, runtime DDL ditolak dengan SQLSTATE42501, plaintext/wrong CA/wrong hostname PG ditolak, guard volume hilang, worker restart serta sesi setelah restart web. Hasil CI remote belum dikonfirmasi; job staging menjalankan verifier gabungan dengan recovery.

`check --deploy` hanya menghasilkan dua warning yang tidak disembunyikan: W005 (HSTS tidak mencakup subdomain) dan W021 (tanpa preload). Staging memakai HSTS satu jam untuk host uji, bukan kebijakan domain publik. Kedua paket baru diperiksa pada metadata PyPI dan tidak mencantumkan advisory aktif; ini tidak mencakup audit OS/image/native atau bukti bebas kerentanan.

Build/start awal menemukan CA proxy belum dipercaya dan izin file host terlalu ketat untuk PG/non-root app. Perbaikan: CA build secret sementara, ownership/mode melalui initializer, dan kode image dibuat terbaca. Cluster staging baru sempat terinisialisasi sebelum inisialisasi role/database selesai; pemeriksaan membuktikan keduanya belum ada sebelum langkah awal itu dilengkapi, tanpa menghapus data. Uji crash awal menggunakan Docker API kill yang dianggap administrative stop; verifier kini membunuh proses worker di dalam container agar menguji restart akibat crash sebenarnya. Seluruh uji final lulus.

Staging lokal ini belum membuktikan deployment VPS/domain publik, integrasi nyata, SMTP, uji beban/HA/failover, alarm eksternal, offsite DR atau audit keamanan independen. Scope produksi dan integrasi berikutnya tetap mengikuti FEATURE-MATRIX.md.
