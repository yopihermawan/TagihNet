# Runbook kandidat rilis dan pemeriksaan staging

Checkpoint 19 November 2026 WIB. Alat ini menyiapkan bukti untuk **staging lokal simulasi**. `passed` berarti pemeriksaan teknis yang disebutkan lulus pada saat itu. Field `production_ready` selalu false; tidak ada mode untuk mengubahnya melalui persetujuan manual. Tidak ada deployment publik, cutover, migrasi, pembayaran/router nyata atau resume worker dari preflight.

Panduan terkait: [STAGING](STAGING.md), [STAGING-RECOVERY](STAGING-RECOVERY.md), [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md), [STAGING-LOAD](STAGING-LOAD.md), [FEATURE-MATRIX](FEATURE-MATRIX.md).

## Pilih image untuk satu proyek

Build memakai versi kode/dependency yang ingin diuji. Catat commit/working tree dan image ID hasil build. Dari root checkout:

```bash
git status --short
.venv/bin/python scripts/staging_lab.py build
.venv/bin/python -c "from scripts.staging_lab import docker; print(docker('image','inspect','tagihnet-staging:local','--format','{{.Id}}',capture=True).stdout.strip())"
```

Cloud dengan proxy CA dapat memakai opsi build `--proxy-ca-file /etc/ssl/certs/ca-certificates.crt`. Gunakan image ID `sha256:`64hex hasil build, bukan tag yang dapat berubah. Pada instance checkpoint ini ID yang sudah diuji adalah `sha256:55ee60b3f061b1005a90f46928ba243c1703d66e8b8b795509a0d435a58be4a1`.

```bash
.venv/bin/python scripts/staging_lab.py pin-image --directory data/staging --image-id sha256:55ee60b3f061b1005a90f46928ba243c1703d66e8b8b795509a0d435a58be4a1
```

Pin hanya menyimpan `image_id` pada manifest di bawah operation lock, setelah membuktikan image tersedia lokal. Tidak ada restart/migrate/service start atau perubahan recovery_pending. Seluruh panggilan Compose melalui helper memilih image itu untuk web/migrate/worker dan mengabaikan selector image dari environment luar. Perintah yang dapat menyalakan container menolak image pin yang hilang sebelum Compose dapat melakukan fallback/pull. Stop/down/status tetap dapat dipakai saat image hilang. Direktori lama tanpa pin memakai tag lokal untuk kompatibilitas, tetapi preflight rilis menolaknya.

`up` tetap tindakan terpisah dan dapat me-recreate container/menjalankan job migrate. Untuk perubahan kode/schema, uji kandidat pada proyek/volume baru hasil prepare/restore sesuai panduan recovery; pertahankan sumber lama sampai rekonsiliasi. Pemilihan image tidak membuktikan kompatibilitas database. Setiap proyek mempunyai pin sendiri, jadi build berikutnya tidak menggeser pilihan image proyek lain.

## Backup dan custody kandidat

Staging aktif harus mempunyai backup berkala yang enabled, percobaan terakhir tidak gagal, metadata sukses masih dalam interval+1jam, dan timestamp **arsip terautentikasi** juga cukup baru. File harus reguler privat; seluruh GCM dibaca ulang. Paket pemulihan yang dipilih harus memuat ciphertext yang sama dengan backup terakhir tersebut dan key MFA yang sesuai source.

Pada instance ini paket baru yang cocok dengan backup terpantau berada di:

- `data/recovery-handoff/archives/locations-20261119`
- `data/recovery-handoff/keys/locations-20261119`

Paket `staging-20261115`, `release-20261115`, `self-password-20261116`, `profile-20261118` dan `plan-archive-20261118` sebelumnya tetap valid dan dipertahankan; arsipnya berbeda sehingga tidak memenuhi gate aktif terhadap backup terpantau saat ini. Untuk backup berikutnya, export dari `last_archive`/key konfigurasi operasi ke **dua nama tujuan baru**, mengikuti RECOVERY-HANDOFF. Jangan mengubah metadata backup untuk membuat paket lama terlihat cocok. Folder/key tetap privat dan terpisah; ini masih satu workspace, bukan offsite.

## Jalankan gate aktif

Pastikan pekerjaan jatuh tempo sudah selesai dan tidak ada proses rilis/migrasi lain. Jalankan pemeriksaan setelah kandidat berjalan di lingkungan uji:

```bash
.venv/bin/python scripts/staging_preflight.py --directory data/staging --image-id sha256:55ee60b3f061b1005a90f46928ba243c1703d66e8b8b795509a0d435a58be4a1 --package-dir data/recovery-handoff/archives/locations-20261119 --keys-dir data/recovery-handoff/keys/locations-20261119 --output data/evidence/preflight-active-baru.json
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
.venv/bin/python scripts/staging_preflight.py --directory data/staging-recovery-20261115 --mode recovery --image-id sha256:55ee60b3f061b1005a90f46928ba243c1703d66e8b8b795509a0d435a58be4a1 --package-dir data/recovery-handoff/archives/staging-20261115 --keys-dir data/recovery-handoff/keys/staging-20261115 --output data/evidence/preflight-recovery-baru.json
```

Recovery tidak mensyaratkan backup otomatis clone atau antrean snapshot kosong. Web/database dan paket diverifikasi; worker yang ternyata berjalan menyebabkan gagal. Passed tidak mengizinkan resume/cutover otomatis. Operator tetap merekonsiliasi transaksi, akses/MFA/consent dan antrian pascasnapshot sebelum tindakan eksplisit berikutnya.

## Migrasi koordinat0012 →0013

Kandidat ini memakai core0013 dan digest aplikasi `44df7efa26391fdcf51bc4ba8895c6a3c618f290a78fee7d149a407aed6dac13`. [LOCATIONS](LOCATIONS.md) menjelaskan input, scope dan migrasinya. Penulis/watchernya dihentikan, backup0012 memakai kunci lama diautentikasi, dan migrasi diuji pada salinan sebelum data tersimpan. Bandingkan seluruh nilai lama lewat proyeksi kolom lama, FK dan sequence; empat kolom baruNULL serta satu recorder0013/sequence+1 adalah perubahan yang diharapkan. SQLite constraint rebuild harus menjaga high-water ID pelanggan/PSB.

Backup0013 baru dibuat/diautentikasi sesudah migrasi. Catatan sukses memakai waktu aktual backup manual; interval24jam tetap dan tidak ada timestamp yang dimajukan. Pasangan custody terbaru berada pada dua folder locations-20261119 di atas; semua pasangan lama tetap. Source kembali dipantau dengan tick sehat baru; clone tetap menahan worker. Jangan otomatis mem-pin image0012 lama atau reverse-migrate sesudah0013. Pertahankan target gagal, restore snapshot kompatibel ke target baru, lalu rekonsiliasi kejadian setelah snapshot sebelum resume.

## Checkpoint setelan billing — historis schema0012

Tinjauan/MFA, deteksi form lama dan pembatasan field billing/SLA dijelaskan di [BILLING-SETTINGS](BILLING-SETTINGS.md). Perubahan kode tidak memerlukan seed atau update data bisnis yang tersimpan. Bandingkan seluruh model/auth/framework/sequence dan recorder serta kunci sebelum/sesudah pin/up. Worker clone tetap ditahan. Pada checkpoint tersebut pasangan custody0012 dipakai selama ciphertext-nya cocok dengan backup terpantau dan timestamp terautentikasinya masih valid; jangan membuat arsip duplikat atau mengubah timestamp hanya untuk mengganti nama checkpoint.

## Migrasi paket 0011 → 0012 — historis18November

Pada migrasi18November, schema menjadi `0012_plan_availability`. Semua paket lama tetap aktif; penulis/watcher dihentikan, backup0011 dengan kunci lama diautentikasi dan migrasi lebih dahulu diuji pada salinan. SQLite mempertahankan high-water ID saat rebuild tabel. Rekonsiliasi membandingkan semua baris dengan proyeksi kolom lama, seluruh sequence dan recorder baru tunggal. Lihat [PLANS](PLANS.md).

Setelah migrasi, backup0012 baru dibuat dan seluruh autentikasinya diperiksa. Catatan backup source memakai waktu keberhasilan aktual backup manual tersebut; interval24jam tetap. Tidak ada pemajuan jam atau klaim bahwa scheduler sudah jatuh tempo. Watcher dimulai kembali dan harus menghasilkan tick sehat baru. Clone recovery tetap worker hold. Semua arsip/kunci/image lama dipertahankan; paket schema0011 lama dapat dipulihkan ke target baru lalu diuji migrasi maju, bukan ditimpa ke database aktif.

Image lama0011 tidak boleh dipakai sebagai rollback otomatis setelah recorder0012 tercatat. Latihan rollback schema tetap terdahulu tidak membuktikan reverse migration ini. Pertahankan target gagal; gunakan restore ke target baru dengan image/backup kompatibel dan rekonsiliasi setelah snapshot. Tidak ada pengujian reverse migration produksi.

## Urutan rilis dan rollback

Untuk rilis berikutnya, simpan kode/image lama beserta backup/custody yang sudah diuji. Uji build dan schema/data pada kandidat terpisah, jalankan suite/restore/preflight, lalu rencanakan penghentian penulisan dan rekonsiliasi akhir pada target nyata. Cutover baru dapat dirancang setelah domain/server/traffic target tersedia. Helper ini tidak menulis DNS, membuka port publik atau merutekan traffic.

Rollback image hanya dapat dipakai bila schema/data masih kompatibel. Applied migration asing menyebabkan gagal; tidak ada auto-reverse migrate. Jika perubahan data/schema tidak kompatibel, gunakan target recovery baru dari snapshot yang tepat, dengan sesi baru/worker hold dan rekonsiliasi kejadian pascasnapshot. Pertahankan target gagal untuk diagnosis; jangan reset database aktif untuk mengulang. Tidak ada jaminan rollback tanpa kehilangan perubahan setelah snapshot.

[STAGING-ROLLOUT](STAGING-ROLLOUT.md) menyediakan latihan terpisah dengan image normal dan image yang sengaja gagal, keduanya memakai kode/schema identik. Verifier memeriksa ID aktual, HTTPS/preflight, tabel/sequence/recorder, sesi target dan hold worker sebelum/sesudah rollback. Ia hanya menerima output baru, membuat target sementara sendiri dan tidak menjalankan pergantian image pada staging pengguna. Latihan ini bukan controller deployment atau bukti kompatibilitas migrasi antarversi.

Produksi masih memerlukan infrastruktur/domain/TLS publik, gateway/router/notifikasi/SMTP nyata yang diuji, custody/offsite/retensi, pengawasan dari host lain, profil beban/soak/failover pada server tujuan dan penilaian keamanan independen. Fitur brief yang masih pending tetap tercatat di matriks.

## Bukti pengujian

Unit mencakup digest/source/dependency, data probe malformed, image/tag/pin/lock/fallback, privilege/mount/port/resource/log, migrasi/pin/deploy warnings, queue/backup lama/tamper/pasangan dan output no-clobber. Verifier staging gabungan menjalankan gate aktif/pass/image salah dengan fingerprint seluruh tabel/sequence/backup metadata tetap. Pada target sementara, leaf core aktual0013 sengaja dihapus dari recorder dan applied migration9999 ditambahkan; gate harus gagal tanpa memperbaikinya. Hanya fixture tersebut dipulihkan, fingerprint/sequence kembali sama, worker recovery tetap ditahan. CI staging memakai verifier ini; hasil remote belum diklaim.
