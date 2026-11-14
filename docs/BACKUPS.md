# Backup terenkripsi dan pemulihan

Checkpoint `feature/tagihnet-encrypted-backups`, 1 November 2026 WIB. CLI menyediakan arsip AES-256-GCM untuk SQLite dan PostgreSQL, kunci backup terpisah, verifikasi autentikasi, dan restore tanpa menimpa database yang sudah berisi data. Tidak ada endpoint/menu backup web; ini operasi administrator host yang mencakup seluruh ISP dan data autentikasi.

## Membuat kunci dan arsip SQLite

Gunakan interpreter proyek, path privat di luar source yang dilacak Git, dan media penyimpanan kunci terpisah dari arsip. Contoh Linux:

```bash
cd /workspace/TagihNet
.venv/bin/python scripts/backup.py keygen /lokasi/kunci/backup-v1.json
.venv/bin/python scripts/backup.py backup /lokasi/arsip/TagihNet-20261101.tnbk --key-file /lokasi/kunci/backup-v1.json
.venv/bin/python scripts/backup.py verify /lokasi/arsip/TagihNet-20261101.tnbk --key-file /lokasi/kunci/backup-v1.json
```

Keygen membuat key acak 256-bit dan ID acak dalam JSON khusus, mode 0600, tanpa menampilkan nilainya. File yang sudah ada tidak ditimpa. Pembaca kunci menerima file reguler privat mode 0600/0400 pada POSIX; symlink, FIFO, mode publik, format/ukuran/material salah ditolak. Ini bukan file SECRET_KEY atau Fernet MFA. Tidak ada input passphrase/KDF atau pembuatan key otomatis saat aplikasi mulai.

Kunci hilang berarti arsip tidak dapat dipulihkan. Untuk rotasi, buat path baru (misalnya backup-v2.json), gunakan untuk backup berikutnya, dan pertahankan kunci lama secara terlindungi selama arsip lamanya diperlukan. Mengganti isi file kunci lama bukan rotasi yang aman. Penyimpanan vault/rotasi produksi belum diotomatisasi.

SQLite memakai API consistent backup, termasuk committed WAL, lalu integrity_check sebelum enkripsi. Jangan menyalin hanya file .sqlite3 saat aplikasi menulis. Snapshot sumber dapat diambil ketika web aktif; pemulihan target harus offline. Perintah backup menerbitkan arsip setelah stream selesai dan filesystem di-fsync; jalankan verify untuk membacanya kembali.

Dalam workspace development saat checkpoint ini, satu arsip terverifikasi disimpan pada `data/backups/encrypted/` dan key khusus pada `data/backup-keys/cloud-development-v1.json`, keduanya 0600 dan diabaikan Git. Restore ke direktori sementara mempertahankan counts/ledger, sumber tetap 33 pelanggan/40 faktur. Kunci dan arsip dalam satu workspace adalah demonstrasi pengembangan; itu bukan vault atau backup offsite. Arsip plaintext lama tidak dihapus.

## Restore SQLite

```bash
TAGIHNET_DATA_DIR=/lokasi/restore-baru .venv/bin/python scripts/backup.py restore /lokasi/arsip/TagihNet-20261101.tnbk --key-file /lokasi/kunci/backup-v1.json
```

Database `tagihnet.sqlite3` target harus belum ada, termasuk symlink rusak dan sidecar `-wal`, `-shm`, `-journal`. Tidak ada flag force/overwrite. Arsip didekripsi ke staging privat, autentikasi GCM diverifikasi, jenis payload dan integrity_check diperiksa, lalu file lengkap diterbitkan dengan create-if-absent. Kunci salah, tag/header/ciphertext berubah, arsip terpotong, payload salah atau target lama tidak menghasilkan database baru yang dipublikasikan.

Catat commit aplikasi dan daftar migrasi yang cocok di catatan operasional; header v1 bukan identitas versi kode. Setelah validasi database, pulihkan kunci sesi dan MFA dari penyimpanan terpisah yang sesuai. Server tidak dimulai otomatis. Bandingkan jumlah/saldo/histori, uji autentikasi, dan baru lakukan cutover terencana saat seluruh penulisan berhenti. Jangan menghapus database aktif untuk memaksa restore pada tempatnya.

## PostgreSQL

Instal requirements-postgres.txt dan gunakan konfigurasi database pada [POSTGRESQL](POSTGRESQL.md). Engine harus dipilih eksplisit; host/user/name/password/TLS berasal dari konfigurasi proses, bukan dari header arsip. Local pg_dump/pg_restore harus sesuai versi server. Lab dapat memakai tools dari container yang disediakan postgres_lab.py.

Contoh lab (migrasi dan startup lab mengikuti panduan PostgreSQL):

```bash
.venv/bin/python scripts/postgres_lab.py run scripts/backup.py keygen /lokasi/kunci/pg-backup-v1.json
.venv/bin/python scripts/postgres_lab.py run scripts/backup.py backup /lokasi/arsip/TagihNet-PG.tnbk --key-file /lokasi/kunci/pg-backup-v1.json
.venv/bin/python scripts/postgres_lab.py run scripts/backup.py verify /lokasi/arsip/TagihNet-PG.tnbk --key-file /lokasi/kunci/pg-backup-v1.json
# Administrator lebih dahulu membuat database kosong tagihnet_restored, dimiliki role restore:
.venv/bin/python scripts/postgres_lab.py run scripts/backup.py restore /lokasi/arsip/TagihNet-PG.tnbk --key-file /lokasi/kunci/pg-backup-v1.json --target-database tagihnet_restored
```

Restore tidak membuat database secara otomatis. Target wajib berbeda dari sumber terkonfigurasi dan bukan postgres/template0/template1. Role harus pemilik target; target tanpa objek pengguna (tabel/view/sequence/function/type/extension/schema tambahan/large object). Target terisi ditolak tanpa menghapus objek.

pg_dump custom stdout langsung masuk stream enkripsi; arsip tidak diterbitkan jika pg_dump gagal. Restore mengautentikasi seluruh arsip dahulu sebelum menghubungi target, memeriksa magic PGDMP, mengambil advisory lock antarhelper restore, memeriksa ownership/empty state lalu menjalankan pg_restore dengan **single transaction, no-owner, no-acl, exit-on-error**. Tidak memakai --clean. Kegagalan pg_restore tidak dilaporkan sebagai sukses. Layanan yang dapat menulis ke target harus berhenti; advisory lock tidak mengunci aplikasi lain yang tidak memakai helper ini.

Local PG password berada pada environment child, bukan argv. TLS mengikuti verify-full konfigurasi; disable hanya DEBUG lab. Jika memakai tools container, helper memeriksa bahwa container mencapai server/database yang sama menggunakan marker advisory acak pada koneksi terkonfigurasi. Pemilih Docker host/context/TLS yang diwariskan diabaikan; daemon lokal dan proxy/registry settings tetap dipertahankan. Tidak ada perluasan jaringan atau secret yang ditempel ke argumen command.

Dump dari pemegang key dan administrator backup adalah input tepercaya. AES-GCM membuktikan autentikasi data, bukan sandbox untuk SQL berbahaya yang sengaja dibuat oleh pemegang key. Atur hak role restore dan validasi staging sebelum cutover; restore tidak menyalin owner/ACL server secara otomatis.

## Format dan batas

| Bagian | Aturan v1 |
| --- | --- |
| Magic / panjang header | TNBK1 + NUL; uint32 big-endian, header 1–4096 byte |
| Metadata JSON | Versi, algoritma, key ID acak, jenis DB, waktu UTC, nonce; field/tipe/duplikat divalidasi |
| Cipher | AES-256-GCM, nonce acak 96-bit per arsip, tag 128-bit |
| AAD | Magic, panjang header dan byte metadata terautentikasi bersama ciphertext |
| Payload | Snapshot SQLite atau pg_dump custom, maksimal 1 GiB, chunk stream 1 MiB |
| Output | File privat0600, staging selesai → hard-link create-if-absent → fsync, tidak overwrite |

Header tidak dienkripsi; hanya metadata teknis di atas, tanpa identitas pelanggan/host/password. Key ID tidak menggantikan verifikasi tag: key dengan ID sama tetapi material salah tetap ditolak. Format unknown/version/algorithm salah, oversized/duplicate header dan truncation ditolak.

Snapshot SQLite dan payload restore perlu plaintext sementara pada file0600 dalam direktori0700. Normal cleanup diuji pada sukses/gagal. File yang sudah dipublikasikan tidak berisi plaintext, tetapi proses crash/SIGKILL, journal filesystem atau secure erase di SSD tidak dijamin oleh enkripsi arsip. Gunakan encrypted storage dan prosedur pembersihan staging milik aplikasi setelah pemeriksaan administrator. Tidak ada auto-delete yang menyapu file pengguna.

## Kunci MFA dan status setelah restore

Arsip hanya mengemas database, termasuk password hash, encrypted TOTP secret, recovery digests dan versi akses saat snapshot. Tidak memuat SECRET_KEY, MFA_ENCRYPTION_KEY, key backup, password DB, konfigurasi Docker, atau file email lokal. Pulihkan key aplikasi dari vault/media terpisah. Key backup tidak dapat dipakai sebagai key MFA.

Drill SQLite membuktikan: salah backup key tidak membuat DB; salah key MFA tidak dapat mendekripsi perangkat; key sesi/MFA yang benar memungkinkan password+TOTP login dengan CSRF. Dalam database hasil restore, pemakaian ulang OTP/recovery ditolak dan saldo cocok.

Restore snapshot lama juga memulihkan status akses, pembayaran, sesi dan recovery-code pada waktu snapshot. Perubahan/revocation/pemakaian kode **sesudah** snapshot tidak otomatis direkonstruksi. Sebelum produksi, rekonsiliasi sumber transaksi terbaru, cabut sesi lama dan tinjau kembali akses/perangkat/recovery dengan prosedur identitas yang sah. Drill tidak membuktikan replay protection lintas rollback snapshot atau mempertahankan semua perubahan pascabackup.

## Kompatibilitas dan verifikasi

Mode lama tetap tersedia untuk script migrasi/drill:

```bash
.venv/bin/python scripts/backup.py backup /lokasi/privat/legacy.sqlite3
```

Tanpa --key-file, ini **SQLite tanpa enkripsi** dan output menyatakannya. Mode tersebut menolak PG serta arsip encrypted. verify_restore.py tetap drill SQLite kompatibilitas; untuk enkripsi/MFA gunakan:

```bash
.venv/bin/python scripts/verify_encrypted_backup.py
.venv/bin/python scripts/postgres_lab.py run scripts/verify_postgres.py
```

20 tes baru (245 pada tiap backend) mencakup key/format/tamper/limit/stream/atomic publication/target rejection. Drill SQLite memeriksa semua tabel, restore MFA dan replay. Verifier PG mempertahankan 10 race proses, menambah encrypted restore dengan rekonsiliasi semua tabel/MFA payload, wrong-key/source/occupied target rejection serta authenticated envelope berisi dump invalid yang tidak meninggalkan objek publik. Semua fixture/database/key temporary dibersihkan; akun demo pengguna tidak diubah.

CLI dan hard-link publication diuji pada Linux/POSIX; Windows/full application Docker runtime belum diverifikasi. Tidak ada scheduler/offsite upload, vault/rotasi otomatis, retensi, PITR/failover, RPO/RTO produksi atau klaim production-ready. Snapshot cloud tidak menjamin volume Docker atau pemisahan key/archive. Hasil lokal dan CI yang baru didaftarkan dijelaskan di [TEST-REPORT](TEST-REPORT.md).


## Binary foto pekerjaan — 14 November 2026 WIB

Schema0010 menyimpan foto yang dinormalisasi di WorkPhoto.content (SQLite BLOB/PostgreSQL bytea), bukan folder media. Backup database mencakup metadata dan binary tanpa jalur file eksternal. Verifier SQLite menghitung digest payload binary tanpa mencetak byte; encrypted SQLite/MFA drill memulihkan foto dan mengunduhnya setelah autentikasi. PG verifier mencakup20 foto hasil race/kuota sebelum dump/restore biasa/terenkripsi. Arsip development schema0010 baru `data/backups/encrypted/TagihNet-20261114-work-photos.tnbk` diverifikasi dan dipulihkan sementara; default demo tidak berisi foto. Batas arsip1 GiB tetap berlaku dan kapasitas/offsite/retensi produksi belum diuji. Lihat [WORK-PHOTOS](WORK-PHOTOS.md).

## Jadwal otomatis pada hasil restore

Schema0011 menyimpan konfigurasi/pemberi persetujuan/revisi MFA serta siklus dan snapshot batch di database; backup mencakupnya bersama ledger. Drill SQLite/PG memakai jadwal/run nonkosong. Sebelum menyalakan worker billing pada hasil restore, tinjau/nonaktifkan jadwal dan rekonsiliasi faktur/transaksi yang mungkin terjadi setelah snapshot, lalu setujui ulang melalui UI. Restore dapat mengembalikan versi izin/MFA dan consent lama; jangan menganggap snapshot mengetahui kejadian sesudah backup. Batas snapshot rollback pada bagian pemulihan tetap berlaku. Archive development0011 terbaru bernama data/backups/encrypted/TagihNet-20261114-billing-scheduler.tnbk; key tetap terpisah, archive lama dipertahankan.
