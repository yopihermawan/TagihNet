# Paket pemulihan portabel

Checkpoint 15 November 2026 WIB. `scripts/recovery_package.py` menyiapkan salinan pemulihan yang dapat dibaca tanpa direktori staging asli. Ini CLI administrator host, mencakup seluruh database/ISP. Gunakan dari root checkout dengan interpreter `.venv/bin/python`. Panduan SQL restore dan rekonsiliasi tetap [STAGING-RECOVERY](STAGING-RECOVERY.md).

## Membuat salinan

Input adalah arsip PostgreSQL custom terenkripsi yang sudah selesai, kunci backup asli, dan kunci enkripsi MFA asli. Ketiganya diperiksa sebelum direktori hasil dibuat. Contoh memakai arsip staging yang sudah ada:

```bash
.venv/bin/python scripts/recovery_package.py export --archive data/staging/backups/TagihNet-staging-20261115-managed.tnbk --archive-key-file data/staging/secrets/backup-key.json --mfa-key-file data/staging/secrets/mfa-key --package-dir data/recovery-handoff/archives/staging-20261115 --keys-dir data/recovery-handoff/keys/staging-20261115
.venv/bin/python scripts/recovery_package.py verify --package-dir data/recovery-handoff/archives/staging-20261115 --keys-dir data/recovery-handoff/keys/staging-20261115
```

Nama tujuan pada contoh dipakai satu kali. Untuk salinan berikutnya pilih dua nama baru. Folder yang sudah ada, symlink, atau folder yang bersarang satu sama lain ditolak. Input tidak ditimpa/dihapus. Arsip yang disalin diautentikasi lagi; manifest diterbitkan terakhir secara atomic/no-clobber. Export terputus dapat menyisakan direktori privat parsial; verify menolaknya, dan retry memakai nama baru. Tidak ada penghapusan otomatis file pengguna.

| Direktori | Isi tepat |
| --- | --- |
| Paket | `archive.tnbk`, `manifest.json` |
| Custody kunci terpisah | `backup-key.json`, `mfa.key` |

Direktori hasil dan parent baru0700, file0600. Pembacaan menerima file reguler privat0400/0600; root folder wajib privat dan bukan symlink. File tambahan/kurang ditolak. Manifest tidak merekam path asli, password DB, session key atau kredensial pengguna. Penyalinan memakai kunci yang sama; ini bukan rotasi key. Package dan folder key tetap dipertahankan bersama pasangan yang benar, dengan akses dan retensi masing-masing.

Dua direktori contoh berada pada **satu mesin/workspace**. Keduanya belum menjadi media/offsite independen. Untuk penerapan nyata, pindahkan paket dan custody kunci melalui media/akses terpisah yang disetujui organisasi, pertahankan permission, lalu jalankan verify di tujuan. Alat ini tidak mengunggah, mengonfigurasi layanan cloud storage atau mengirim kunci keluar.

## Verifikasi dan format

Manifest v1 berisi ID paket, waktu UTC, HEAD Git exporter, ukuran/SHA-256 arsip, SHA-256 kunci MFA dan HMAC. HMAC-SHA256 memakai key turunan HKDF-SHA256 dari key backup, salt ID paket16byte dan info `TagihNet/recovery-handoff/manifest/v1`. Data HMAC adalah JSON canonical ASCII dengan key terurut/separator ringkas, tanpa field mac. Pembandingan memakai compare_digest. Ini autentikasi oleh pemegang key, bukan tanda tangan identitas pihak ketiga.

Manifest diautentikasi sebelum mempercayai hash/metadata. Hash arsip/pasangan MFA lalu diperiksa, disusul autentikasi **seluruh** AES-GCM dan jenis/signature PGDMP. SHA/HMAC manifest tidak menggantikan GCM. Metadata plain dapat dibaca, tetapi tidak mengandung material key; fingerprint MFA mengikat key yang diberikan saat export, termasuk bila backup tidak memuat perangkat MFA. Kecocokan key dengan cipher perangkat dalam database tetap diperiksa oleh helper SQL setelah restore; export tidak membuktikan bahwa operator memilih key historis yang tepat. Format arsip TNBK AES-GCM v1/batas1GiB tidak berubah. Metadata manifest maksimal16KiB, field/tipe/duplikat/versi/digest divalidasi; tidak ada ekstraksi tar/zip atau path dari manifest yang dieksekusi.

`exporter_commit` mencatat HEAD checkout saat export, bukan bukti commit pembuat dump, status working tree bersih atau kompatibilitas schema. Catat image/revision aplikasi dan migrasi saat backup secara operasional; tinjau kode/schema target sebelum up. File dump tetap input SQL tepercaya dari pemegang key. Paket tidak memuat image Docker/dependency, domain/TLS credential, password DB atau konfigurasi server asli.

`verify` bekerja offline tanpa Docker/database/sumber staging. Kunci salah, metadata diubah, arsip ditukar, kunci MFA tertukar, atau paket tidak lengkap menghasilkan exit1. CLI hanya mencetak hasil/jenis error generik, tanpa body exception/faktor. Tidak ada proses web/worker yang dimulai oleh verify.

## Memulihkan pada target baru

Siapkan kode/dependency/image yang sesuai, direktori baru dan port loopback kosong. Baca panduan recovery sebelum tindakan berikut:

```bash
.venv/bin/python scripts/staging_lab.py prepare --directory data/recovered-from-handoff-baru --port 20443
.venv/bin/python scripts/recovery_package.py restore --package-dir data/recovery-handoff/archives/staging-20261115 --keys-dir data/recovery-handoff/keys/staging-20261115 --target-dir data/recovered-from-handoff-baru
.venv/bin/python scripts/staging_lab.py up --directory data/recovered-from-handoff-baru
```

Restore membekukan salinan arsip terenkripsi dan key MFA terverifikasi ke temporary directory privat sebelum operasi target. Perubahan file input sesudah verifikasi tidak mengganti isi yang dikirim ke helper SQL. Salinan sementara dibersihkan pada penyelesaian normal; perlu ruang untuk ciphertext sementara dan payload SQL plaintext privat dari helper restore. Crash/SIGKILL bukan jaminan cleanup/secure erase.

Helper lama tetap memverifikasi GCM penuh sebelum Docker, identitas/volume/proyek/owner, target kosong dan shared lock; menjalankan pg_restore single transaction/no-clean. Key sesi target tetap baru, MFA memakai key asli, restore preparing/failed diblokir, dan recovery_pending menahan worker. Up membuka web untuk validasi. Tidak ada bootstrap/reset akun, auto-cutover atau resume worker dari paket ini. Password akun berasal dari snapshot; password bootstrap baru target tidak menggantikan sandi akun yang dipulihkan.

Tinjau akses/MFA, saldo/transaksi setelah snapshot, foto dan antrean/jadwal sebelum keputusan rekonsiliasi/resume yang eksplisit. Snapshot lama dapat mengembalikan consent/revocation/counter lama; paket tidak membuktikan anti-replay lintas rollback atau merekonstruksi transaksi pascasnapshot.

## Latihan terisolasi

```bash
.venv/bin/python scripts/verify_staging_backup.py
```

Verifier gabungan membuat sumber fiktif dengan dua tenant, ledger/pembayaran/reversal, MFA, foto binary, jadwal/batch dan outbox. Setelah seluruh recovery lama lulus, ia membuat paket+custody, menyalinnya ke direktori penerima, menguji key MFA salah, lalu menghapus **hanya sumber/volume/arsip/key sementara yang dibuat oleh latihan itu**. Paket ekspor awal juga dihapus; pemulihan hanya membaca salinan penerima.

Target baru harus cocok seluruh tabel aplikasi/auth/framework dan sequence sebelum up. HTTPS/password/TOTP/CSRF, penolakan sesi lama, unduhan foto/checksum, saldo149.750, replay OTP dalam target dan worker hold diperiksa lagi. Staging utama/default demo/clone yang dipertahankan tidak dihapus atau dienroll MFA untuk latihan. CI staging memakai verifier ini; hasil remote harus diperiksa terpisah.

Latihan ini masih pada Docker host yang sama. Pemulihan server/region terpisah, vault/custody independen, retensi, PITR/failover, RPO/RTO, beban dan audit produksi tetap belum dibuktikan. Tidak ada perubahan model atau dependensi pada checkpoint ini.
