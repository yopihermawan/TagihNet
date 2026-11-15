# Runbook kandidat rilis dan pemeriksaan staging

Checkpoint 15 November 2026 WIB. Alat ini menyiapkan bukti untuk **staging lokal simulasi**. `passed` berarti pemeriksaan teknis yang disebutkan lulus pada saat itu. Field `production_ready` selalu false; tidak ada mode untuk mengubahnya melalui persetujuan manual. Tidak ada deployment publik, cutover, migrasi, pembayaran/router nyata atau resume worker dari preflight.

Panduan terkait: [STAGING](STAGING.md), [STAGING-RECOVERY](STAGING-RECOVERY.md), [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md), [STAGING-LOAD](STAGING-LOAD.md), [FEATURE-MATRIX](FEATURE-MATRIX.md).

## Pilih image untuk satu proyek

Build memakai versi kode/dependency yang ingin diuji. Catat commit/working tree dan image ID hasil build. Dari root checkout:

```bash
git status --short
.venv/bin/python scripts/staging_lab.py build
.venv/bin/python -c "from scripts.staging_lab import docker; print(docker('image','inspect','tagihnet-staging:local','--format','{{.Id}}',capture=True).stdout.strip())"
```

Cloud dengan proxy CA dapat memakai opsi build `--proxy-ca-file /etc/ssl/certs/ca-certificates.crt`. Gunakan image ID `sha256:`64hex hasil build, bukan tag yang dapat berubah. Pada instance checkpoint ini ID yang sudah diuji adalah `sha256:f1944baa50ecff03caf5bd2b3a728b1abca977c9ad2d7aa3da7f9314c1bb7858`.

```bash
.venv/bin/python scripts/staging_lab.py pin-image --directory data/staging --image-id sha256:f1944baa50ecff03caf5bd2b3a728b1abca977c9ad2d7aa3da7f9314c1bb7858
```

Pin hanya menyimpan `image_id` pada manifest di bawah operation lock, setelah membuktikan image tersedia lokal. Tidak ada restart/migrate/service start atau perubahan recovery_pending. Seluruh panggilan Compose melalui helper memilih image itu untuk web/migrate/worker dan mengabaikan selector image dari environment luar. Perintah yang dapat menyalakan container menolak image pin yang hilang sebelum Compose dapat melakukan fallback/pull. Stop/down/status tetap dapat dipakai saat image hilang. Direktori lama tanpa pin memakai tag lokal untuk kompatibilitas, tetapi preflight rilis menolaknya.

`up` tetap tindakan terpisah dan dapat me-recreate container/menjalankan job migrate. Untuk perubahan kode/schema, uji kandidat pada proyek/volume baru hasil prepare/restore sesuai panduan recovery; pertahankan sumber lama sampai rekonsiliasi. Pemilihan image tidak membuktikan kompatibilitas database. Setiap proyek mempunyai pin sendiri, jadi build berikutnya tidak menggeser pilihan image proyek lain.

## Backup dan custody kandidat

Staging aktif harus mempunyai backup berkala yang enabled, percobaan terakhir tidak gagal, metadata sukses masih dalam interval+1jam, dan timestamp **arsip terautentikasi** juga cukup baru. File harus reguler privat; seluruh GCM dibaca ulang. Paket pemulihan yang dipilih harus memuat ciphertext yang sama dengan backup terakhir tersebut dan key MFA yang sesuai source.

Pada instance ini paket baru yang cocok dengan backup terpantau berada di:

- `data/recovery-handoff/archives/release-20261115`
- `data/recovery-handoff/keys/release-20261115`

Paket `staging-20261115` sebelumnya tetap valid dan dipertahankan; arsipnya berbeda sehingga tidak memenuhi gate aktif terhadap backup terpantau saat ini. Untuk backup berikutnya, export dari `last_archive`/key konfigurasi operasi ke **dua nama tujuan baru**, mengikuti RECOVERY-HANDOFF. Jangan mengubah metadata backup untuk membuat paket lama terlihat cocok. Folder/key tetap privat dan terpisah; ini masih satu workspace, bukan offsite.

## Jalankan gate aktif

Pastikan pekerjaan jatuh tempo sudah selesai dan tidak ada proses rilis/migrasi lain. Jalankan pemeriksaan setelah kandidat berjalan di lingkungan uji:

```bash
.venv/bin/python scripts/staging_preflight.py --directory data/staging --image-id sha256:f1944baa50ecff03caf5bd2b3a728b1abca977c9ad2d7aa3da7f9314c1bb7858 --package-dir data/recovery-handoff/archives/release-20261115 --keys-dir data/recovery-handoff/keys/release-20261115 --output data/evidence/preflight-active-baru.json
```

Output baru wajib; file/symlink yang sudah ada ditolak. JSON file0600 diterbitkan atomic/no-clobber. Exit0 hanya passed, exit1 untuk kegagalan/ketidaktersediaan. Tidak ada exception body, environment dump, key, OTP, password, pelanggan atau nominal pada laporan. Git HEAD/dirty hanya provenance checkout; gunakan commit bersih yang dapat direproduksi untuk kandidat rilis. Gate membandingkan byte aplikasi aktual, tidak menganggap label Git sebagai bukti image.

| Pemeriksaan | Syarat |
| --- | --- |
| Identitas | Manifest initialized/tidak failed, mode sesuai, image dipin/tersedia lokal dan semua app container memakai ID pilihan |
| Isi aplikasi | Digest tree v1 dari config/core/templates/static/deploy serta manage.py/3 requirements cocok antara checkout dan semua app container; tests/bytecode dikecualikan |
| Dependensi | Seluruh16 pin requirements staging cocok dengan metadata package terpasang |
| Schema | Tidak ada migrasi tertunda atau applied migration yang tidak dikenal kode; fingerprint migrasi antarlayanan konsisten |
| Django/settings | DEBUG0/SIMULATION1, Secure cookies, native TLS, dummy mail, HSTS staging; check deploy hanya W005/W021 yang memang untuk HSTS lokal |
| PostgreSQL | Koneksi TLS, role runtime tanpa superuser/CREATEDB/CREATEROLE/DDL; seluruh inspeksi Django berada dalam transaksi READ ONLY/timeout5detik |
| Container | Project/service/network/volume benar, app non-root/read-only/cap-drop/no-new-privileges, tanpa bind mount tambahan, batas resource/log, HTTPS loopback dan DB tanpa published port |
| Operasi | Health/sertifikat/heartbeat/metrik semuanya ok, queue due aktif0, backup/authenticated timestamp valid dan custody cocok backup |

Digest membandingkan source terpilih, bukan tanda tangan supply chain, seluruh filesystem/base image atau penilaian advisory. Metadata deploy/migrasi yang tidak tersedia dianggap gagal. Gate tidak menjalankan migrate, backup baru, perbaikan metadata, queue cancel, restart atau penghentian traffic. Setelah kode/image/DB/backup berubah, bukti lama perlu diperbarui. Hasil tidak membuktikan data pascasnapshot telah direkonsiliasi.

## Validasi target recovery

Clone yang worker-nya ditahan harus memakai mode eksplisit recovery. Mode aktif menolaknya. Contoh memakai snapshot clone yang sudah ada:

```bash
.venv/bin/python scripts/staging_preflight.py --directory data/staging-recovery-20261115 --mode recovery --image-id sha256:f1944baa50ecff03caf5bd2b3a728b1abca977c9ad2d7aa3da7f9314c1bb7858 --package-dir data/recovery-handoff/archives/staging-20261115 --keys-dir data/recovery-handoff/keys/staging-20261115 --output data/evidence/preflight-recovery-baru.json
```

Recovery tidak mensyaratkan backup otomatis clone atau antrean snapshot kosong. Web/database dan paket diverifikasi; worker yang ternyata berjalan menyebabkan gagal. Passed tidak mengizinkan resume/cutover otomatis. Operator tetap merekonsiliasi transaksi, akses/MFA/consent dan antrian pascasnapshot sebelum tindakan eksplisit berikutnya.

## Urutan rilis dan rollback

Untuk rilis berikutnya, simpan kode/image lama beserta backup/custody yang sudah diuji. Uji build dan schema/data pada kandidat terpisah, jalankan suite/restore/preflight, lalu rencanakan penghentian penulisan dan rekonsiliasi akhir pada target nyata. Cutover baru dapat dirancang setelah domain/server/traffic target tersedia. Helper ini tidak menulis DNS, membuka port publik atau merutekan traffic.

Rollback image hanya dapat dipakai bila schema/data masih kompatibel. Applied migration asing menyebabkan gagal; tidak ada auto-reverse migrate. Jika perubahan data/schema tidak kompatibel, gunakan target recovery baru dari snapshot yang tepat, dengan sesi baru/worker hold dan rekonsiliasi kejadian pascasnapshot. Pertahankan target gagal untuk diagnosis; jangan reset database aktif untuk mengulang. Tidak ada jaminan rollback tanpa kehilangan perubahan setelah snapshot.

Produksi masih memerlukan infrastruktur/domain/TLS publik, gateway/router/notifikasi/SMTP nyata yang diuji, custody/offsite/retensi, pengawasan dari host lain, profil beban/soak/failover pada server tujuan dan penilaian keamanan independen. Fitur brief yang masih pending tetap tercatat di matriks.

## Bukti pengujian

Unit mencakup digest/source/dependency, data probe malformed, image/tag/pin/lock/fallback, privilege/mount/port/resource/log, migrasi/pin/deploy warnings, queue/backup lama/tamper/pasangan dan output no-clobber. Verifier staging gabungan menjalankan gate aktif/pass/image salah dengan fingerprint seluruh tabel/sequence/backup metadata tetap. Pada target sementara, migrasi0011 sengaja dihapus dari recorder dan applied migration9999 ditambahkan; gate harus gagal tanpa memperbaikinya. Hanya fixture tersebut dipulihkan, fingerprint/sequence kembali sama, worker recovery tetap ditahan. CI staging memakai verifier ini; hasil remote belum diklaim.
