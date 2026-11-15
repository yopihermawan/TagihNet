# Backup dan pemulihan staging

CLI host-admin `scripts/staging_backup.py` sekarang menangani backup, verifikasi dan SQL restore staging Docker. Format tetap arsip TagihNet AES-256-GCM versi1, maksimal1GiB. Database/password/keys development tidak dipakai. Operasi ini bukan endpoint web dan tidak menambah hak role runtime.

## Backup yang dapat diulang

Staging instance ini sudah mempunyai key privat `data/staging/secrets/backup-key.json`. Jangan membuat ulang atau menimpanya. Untuk lingkungan baru yang belum punya key, jalankan `keygen` dengan path tujuan baru, lalu simpan kunci terpisah dari arsip.

```bash
.venv/bin/python scripts/staging_backup.py keygen /private/backup-key-baru.json
```

Contoh backup pada instance yang ada:

```bash
.venv/bin/python scripts/staging_backup.py backup data/staging/backups/TagihNet-staging-20261115-managed.tnbk --directory data/staging --key-file data/staging/secrets/backup-key.json
.venv/bin/python scripts/staging_backup.py verify data/staging/backups/TagihNet-staging-20261115-managed.tnbk --key-file data/staging/secrets/backup-key.json
```

Arsip contoh itu sudah dibuat dan diverifikasi. Pengulangan perlu nama arsip baru; target lama, termasuk symlink yang sudah ada, tidak ditimpa. CLI tidak menghapus backup lama. Gunakan nama bertimestamp pada penjadwal host bila diperlukan; pemasangan cron/timer/offsite dan retensi belum diotomatisasi.

Identitas proyek, service database, image PG yang dipatok dan volume diperiksa sebelum pg_dump. Tools PG dari container yang sama berjalan sebagai UID999 dengan role migrator pemilik database, melalui Unix socket privat yang tidak dibagi ke aplikasi. Tidak ada password pada argv/output. pg_dump memakai snapshot konsisten saat sumber berjalan; semua stdout dialirkan ke enkripsi, diagnostik dipisahkan. Arsip baru dipublikasikan secara atomik hanya jika pg_dump selesai sukses. Tidak ada dump plaintext permanen saat backup.

## Restore ke proyek baru

Pilih direktori baru dan port loopback yang belum dipakai. **Jangan jalankan up/bootstrap pada target sebelum restore**, karena keduanya dapat membuat tabel/akun dan target yang sudah digunakan akan ditolak.

```bash
.venv/bin/python scripts/staging_lab.py prepare --directory data/staging-recovery-baru --port 20443
.venv/bin/python scripts/staging_backup.py restore data/staging/backups/TagihNet-staging-20261115-managed.tnbk --directory data/staging-recovery-baru --key-file data/staging/secrets/backup-key.json --mfa-key-file data/staging/secrets/mfa-key
.venv/bin/python scripts/staging_lab.py up --directory data/staging-recovery-baru
```

Restore mengautentikasi seluruh arsip dan memeriksa jenis/signature PG sebelum mengakses Docker/database atau mengganti kunci target. Target wajib manifest prepare baru, belum initialized/restore, dan belum mempunyai volume database. Error socket/izin tidak dianggap bukti volume hilang. Pengunci direktori dipakai bersama oleh restore/up/resume-workers agar operasi yang bersaing tidak mengaktifkan target saat pemulihan.

Hanya database target baru yang dimulai. Role restore harus pemilik database; catalog diperiksa kosong, kecuali public schema/default privileges bawaan provisioning. `pg_restore --single-transaction --exit-on-error --no-owner --no-acl` memakai role migrator dan tidak menjalankan --clean/--create. Tidak ada objek database lama yang dihapus. Archive/dump dari pemegang kunci dipercaya sebagai SQL host-admin, bukan sandbox SQL.

Kunci MFA asli dibaca dari file privat0400/0600 dan disalin hanya ke kunci target yang belum pernah dipakai. Cipher seluruh MFADevice yang dipulihkan diuji dengan kunci tersebut. Kunci sesi target tetap acak baru, sehingga cookie/sesi serta token bertanda tangan dari lingkungan lama tidak langsung berlaku. Password hash dan perangkat MFA dalam database tidak direset. File bootstrap-password yang baru dibuat target tidak mengganti sandi akun hasil restore; gunakan sandi akun dari backup, bukan bootstrap ulang.

Restore yang gagal/interupsi diberi status failed/preparing dan aplikasi/worker diblokir. Dump SQL invalid di-rollback. Jika SQL sudah berhasil tetapi kunci MFA salah, data dipertahankan pada target gagal yang tetap terisolasi; jangan menganggapnya kosong atau siap digunakan. Periksa hasil, gunakan target baru untuk percobaan berikutnya, dan tangani sisa resource hanya setelah memastikan kepemilikannya. Tidak ada cleanup otomatis pada target pengguna.

## Tinjau sebelum menyalakan worker

Restore sukses diberi `recovery_pending`. Up hanya membuka web; tiga worker tetap berhenti. Bandingkan jumlah/digest data, saldo, faktur, foto, konfigurasi dan hak akses. Rekonsiliasi transaksi/perubahan hak/recovery code yang mungkin terjadi setelah waktu snapshot, lalu tinjau jadwal/reminder/job yang masih antre. Sesi baru tidak menyelesaikan rollback counter MFA atau kejadian pascabackup; tidak ada jaminan anti-replay lintas rollback snapshot.

Sesudah rekonsiliasi, operator host dapat secara eksplisit menjalankan:

```bash
.venv/bin/python scripts/staging_lab.py resume-workers --directory data/staging-recovery-baru
```

Perintah ini memulai ketiga worker dan menghapus hold pada manifest. Up berikutnya mengikuti perilaku normal. Manifest dipublikasikan dengan rename atomik dan fsync. Restore/up/resume tidak boleh dipaksa dengan mengubah flag JSON atau menjalankan compose langsung untuk melewati hold.

## Bukti pengujian lokal

16 tes baru,471 total pada masing-masing backend. Kasus mencakup pg_dump sukses/gagal dan non-clobber, authenticasi sebelum Docker, tipe arsip, key/MFA file, identitas source/volume, error socket, pengunci bersama, failed-start dan explicit worker resume.17 skenario proses PG serta dump/restore generik lulus setelah jalur stream backup dibagi pakai.

`scripts/verify_staging_backup.py` menjalankan seluruh verifier HTTPS staging lalu menambah dua tenant, faktur249.750, pembayaran100.000 +25.000 yang direversal, saldo149.750, foto binary, MFA, batch jadwal dan reminder/network job antre. Sumber uji dihentikan penulisnya saat perbandingan supaya snapshot pembanding stabil. Backup/restore membandingkan setiap tabel aplikasi/auth/framework dan sequence last_value/is_called. Old cookie ditolak, password/TOTP/CSRF dan unduhan foto berizin bekerja; replay OTP yang diterima pada target ditolak. Kunci salah/tamper/source-target/target terpakai/MFA salah/dump invalid diuji. Seluruh stack sementara dibuang; data demo dan staging pengguna tetap.

Pada instance ini arsip awal `data/staging/TagihNet-staging-20261115.tnbk` yang sebelumnya hanya dibaca ulang kini juga dipulihkan ke `data/staging-recovery-20261115`, port19443. Semua payload aplikasi/auth cocok dengan staging sumber:1 ISP/owner,0 MFA/pelanggan/faktur. Web/database clone sehat; worker clone tetap ditahan dan tidak diaktifkan untuk tes. Source dan kunci/arsip lama dipertahankan.

CI staging kini menjalankan verifier gabungan; hasil remote belum dikonfirmasi. Ini bukan offsite/PITR/HA, uji kapasitas atau RPO/RTO produksi. Container/volume tidak dijamin ikut snapshot cloud; pertahankan archive, key, MFA key dan metadata di media yang dapat dipulihkan, dengan custody terpisah. Panduan runtime: [STAGING](STAGING.md).

Checkpoint operasi berikutnya menyediakan watcher lokal dan backup berkala opt-in; panduan [STAGING-OPERATIONS](STAGING-OPERATIONS.md). Ini tidak mengubah batas restore/recovery hold atau menambahkan offsite/systemd/retensi.

Checkpoint paket pemulihan menambah export/verify offline/restore dari salinan paket dan custody key terpisah. Verifier kini memulihkan ulang setelah sumber fiktif/volume/arsip/key asal dihapus, dengan full table/sequence/auth/photo checks dan worker hold. Source pengguna dipertahankan. Lihat [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md); ini belum offsite/host terpisah.
