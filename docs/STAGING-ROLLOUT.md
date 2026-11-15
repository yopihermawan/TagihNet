# Latihan pergantian image dan rollback staging

Checkpoint 15 November 2026 UTC. Latihan ini membuktikan pergantian **image dengan schema yang sama** pada target recovery sementara. Image normal diganti dengan image yang sengaja gagal, lalu dikembalikan ke image normal. Hasil selalu `production_ready: false` dan tidak menetapkan kesiapan cutover atau kompatibilitas antarversi aplikasi.

## Menjalankan

Dari root checkout, gunakan image aplikasi staging yang sudah dibangun dan cocok dengan source/dependency saat ini:

```bash
.venv/bin/python scripts/staging_lab.py build
.venv/bin/python scripts/verify_staging_rollout.py --output data/evidence/staging-rollout-baru.json
```

Build cloud yang memerlukan proxy CA dapat memakai `--proxy-ca-file /etc/ssl/certs/ca-certificates.crt`. Build ulang aplikasi tidak diperlukan bila image saat ini sudah cocok. Jangan memakai image lama untuk source baru; gate isi aplikasi akan menolaknya. Baca juga [STAGING](STAGING.md), [RELEASE-RUNBOOK](RELEASE-RUNBOOK.md) dan [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md).

Verifier tidak menerima URL, direktori staging pengguna atau image kandidat dari pengguna. Ia membuat sendiri dua proyek sementara, volume, CA, password, arsip/kunci dan port loopback. Source `data/staging`, clone recovery yang dipertahankan dan database development tidak dijadikan target. File laporan existing/symlink ditolak sebelum Docker dijalankan; gunakan nama output baru untuk setiap percobaan.

## Yang diperiksa

1. Image `tagihnet-staging:local` diberi alias sementara unik. Dua turunan lokal dibuat tanpa download, `RUN` atau `COPY`: kandidat normal berbeda label, kandidat gagal menambahkan perintah web yang keluar dengan kode37. Kode aplikasi, dependency dan schema ketiganya sama. Tag staging bersama tetap dipertahankan.
2. Source fiktif menjalani pemeriksaan HTTPS/CA/hostname, login/password/MFA/CSRF, PostgreSQL verify-full/larangan DDL runtime, container restrictions dan restart worker. Setelah penulis dihentikan, fixture menambah dua tenant, faktur249.750, dua pembayaran, satu reversal, foto privat dan pekerjaan simulasi menunggu.
3. Backup terenkripsi serta paket/kunci terpisah dipulihkan ke proyek baru. Target menggunakan kunci sesi baru dan MFA asli. Cookie source ditolak; login/TOTP, checksum foto dan penolakan replay target diuji. Ketiga worker recovery ditahan.
4. Image target diganti menjadi kandidat normal, lalu ditunggu HTTPS dan health Docker. ID container aktual, preflight mode recovery serta sesi login **target yang sudah ada** harus lulus. Semua baris model app/auth/framework dan seluruh sequence PostgreSQL harus cocok. Baris `django_migrations` dihash secara eksplisit karena recorder bukan model aplikasi terdaftar.
5. Image gagal dipilih dan `up` harus gagal pemeriksaan kesiapan. Container proyek/service yang benar harus memakai image itu dan memiliki exit code37. Database, recorder, sequence dan hold worker tetap sama.
6. Pilihan dikembalikan ke ID image kandidat normal sebelumnya. HTTPS/health/preflight/sesi target harus kembali lulus, tabel/sequence/recorder cocok, file kunci/password/sertifikat tidak berubah, dan source fiktif tetap sama. Saldo akhirnya149.750; pembayaran/reversal/foto tidak bertambah atau hilang.

Authenticated GET memperbarui aktivitas sesi secara sah. Perbandingan database dilakukan sebelum request tersebut, lalu baseline baru diambil sebelum pergantian image berikutnya. Tidak ada pengecualian tabel sesi dari fingerprint. `up` menjalankan job migrate seperti biasa; pada latihan ini recorder yang tidak berubah membuktikan tidak ada migrasi diterapkan. Ini tidak menguji reverse migration atau kode/schema lama melawan schema baru.

`staging_lab.py up --timeout N` menerima integer1–120, default120. Batas ini hanya polling HTTPS **setelah** Compose up; bukan deadline keseluruhan build/startup/migrate. Latihan memakai12detik untuk image gagal. Gagal kesiapan tidak mengganti pin kembali, menghapus data atau membuka hold worker. Pemilihan kembali dalam verifier adalah langkah eksplisit pada fixture yang dimilikinya.

## Laporan dan pembersihan

JSON baru dipublikasikan atomic/no-clobber dengan mode0600. Isinya status, ID image, gate boolean, jumlah fixture/saldo fiktif serta kode kegagalan; tidak ada key, OTP, cookie, password, identitas pelanggan atau body exception. Exit0 mensyaratkan seluruh pemeriksaan **dan cleanup** lulus.

Cleanup mencoba seluruh stack sementara meski ada yang gagal. Turunan hanya dihapus melalui tag unik milik run yang label tokennya cocok. Build gagal yang sempat menerbitkan tag tetap diperiksa. Tag yang hilang boleh dilewati setelah list Docker berhasil; daemon error tidak dianggap sebagai ketiadaan image. Alias base hanya dihapus lewat nama alias bila ID-nya masih sama, tanpa force/prune atau penghapusan ID/tag staging bersama. SIGKILL/host crash tidak menjamin cleanup; periksa kepemilikan proyek/image sebelum tindakan manual.

Workflow manual `.github/workflows/staging-rollout.yml` menyediakan build/verifier dan menyimpan laporan agregat7hari. Eksekusi GitHub belum diklaim. Bukti run lokal tercatat di [TEST-REPORT](TEST-REPORT.md); latihan ini tidak mengukur kapasitas, durasi gangguan atau RTO.

Run akhir instance ini lulus seluruh gate dan cleanup; ringkasan aman ada di [evidence/staging-rollout-20261115.json](evidence/staging-rollout-20261115.json), termasuk hash laporan privat/verifier dan hasil pemeriksaan staging yang dipertahankan. Seluruh588 tes SQLite dan588 PostgreSQL lulus, termasuk15 tes baru untuk timeout/hold/ownership/cleanup/privacy. Runtime aplikasi tetap image yang sudah diverifikasi; kedua image turunan hanya hidup selama latihan.

Schema/data antarversi, perubahan setelah snapshot, traffic cutover, tulisan serentak, target host lain, offsite custody, soak/failover dan integrasi nyata masih memerlukan pengujian sendiri. Jangan memakai latihan ini sebagai prosedur rollback otomatis pada data produksi.
