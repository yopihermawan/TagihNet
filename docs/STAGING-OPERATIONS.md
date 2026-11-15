# Pemantauan dan backup berkala staging lokal

Checkpoint 15 November 2026 WIB. Jalankan dari root checkout dengan `.venv/bin/python`. Ini alat administrator host Linux, memakai Docker lokal dan manifest staging; tidak tersedia dari portal web. Profil tetap DEBUG=0/SIMULATION=1. Panduan terkait: [STAGING](STAGING.md), [STAGING-RECOVERY](STAGING-RECOVERY.md).

## Pemeriksaan

```bash
.venv/bin/python scripts/staging_ops.py once --directory data/staging
.venv/bin/python scripts/staging_ops.py status --directory data/staging
```

`once` memeriksa layanan dan menjalankan satu backup jatuh tempo bila backup telah diaktifkan. Exit0 hanya untuk `ok`, lainnya1. `status` membaca laporan terakhir beserta identitas watcher dan usia laporan; bukan pemeriksaan jaringan baru. Laporan lebih tua dari2×interval+30detik, bertanggal masa depan >5detik, atau kegagalan putaran watcher setelah laporan terakhir menghasilkan `unknown`. Watcher tidak aktif dilaporkan terpisah; pemeriksaan manual yang baru masih dapat `ok`.

Pemeriksaan mencakup container web/database, health Docker, HTTPS dengan CA/hostname lokal terverifikasi, tanggal sertifikat dan kemajuan loop ketiga worker. Heartbeat JSON privat berada di tmpfs masing-masing container, diperbarui awal/akhir putaran dan sebelum pekerjaan. Gagal menulis heartbeat tidak membatalkan transaksi billing; monitor melaporkan heartbeat tidak tersedia/kedaluwarsa. PID hidup saja tidak cukup. Default usia heartbeat120detik; pekerjaan sah yang sangat lama dapat memicu alarm dan perlu diperiksa sebelum menaikkan ambang.

Statistik dibaca melalui transaksi PostgreSQL READ ONLY sebagai `tagihnet_runtime`: jumlah/umur pekerjaan jatuh tempo, batch antre/processing, retry habis dan blocked. Umur >900detik memberi warning. Ini agregat seluruh proyek staging, bukan izin portal per ISP. Tidak mencakup kapasitas, persentil latensi, jadwal yang belum menghasilkan batch, atau rekonsiliasi finansial. Query gagal menghasilkan `metrics: null`/`unknown`, bukan nol. Keparahan: ok, warning, unknown, critical.

Recovery_pending menampilkan worker `held` dan menahan alarm backlog; worker yang ternyata berjalan tetap critical. Monitor tidak menyalakan/resume worker, mengubah job, mengirim notifikasi atau memperbaiki layanan. Restore preparing/failed diblokir. Gunakan direktori/CA/port yang sesuai untuk source dan clone.

## Backup opt-in

Key harus sudah ada, valid dan privat0400/0600. Jangan mengganti key lama; pembuatan key untuk proyek baru mengikuti panduan recovery. Contoh source yang telah memiliki key:

```bash
.venv/bin/python scripts/staging_ops.py configure --directory data/staging --enable-backup --backup-key-file data/staging/secrets/backup-key.json --backup-directory data/staging/backups/automated --backup-interval-hours 24 --check-interval 60
.venv/bin/python scripts/staging_ops.py once --directory data/staging
.venv/bin/python scripts/staging_ops.py start --directory data/staging
.venv/bin/python scripts/staging_ops.py status --directory data/staging
```

Watcher memeriksa setiap60detik setelah putaran sebelumnya selesai. Backup pertama langsung jatuh tempo, berikutnya24jam dari sukses terakhir. Interval1–168jam; perubahan berlaku dari sukses terakhir. Kegagalan ditandai critical dan retry paling cepat5menit; backup terlalu tua memberi warning. Tidak ada catch-up banyak arsip untuk setiap interval terlewat. Recovery yang ditahan tidak membuat backup otomatis meskipun enabled.

Nama arsip memakai UTC + suffix acak, snapshot pg_dump konsisten, AES-GCM dan publikasi tanpa overwrite. Seluruh arsip baru diautentikasi lagi sebelum sukses dicatat. Batas payload1GiB tetap. Arsip terakhir yang hilang memberi critical; autentikasi penuh tidak diulang setiap menit. Gunakan verify/restore eksplisit untuk pemeriksaan arsip berikutnya. Tidak ada perubahan tabel aplikasi oleh monitor/backup.

Konfigurasi, laporan, metadata backup dan watcher berada di `ops/*.json` pada direktori staging yang diabaikan Git. File baru0600, direktori baru0700. Laporan hanya timestamp/status/jumlah agregat/path arsip, tanpa nama pelanggan/nominal/password/isi key. `ops/events.jsonl` berisi transisi raised/resolved tanpa body exception. Rotasi sesudah256KiB menyimpan3 file lama. Arsip backup **tidak dihapus otomatis**; pantau ruang disk dan tentukan retensi terpisah. Key dan arsip satu workspace belum memenuhi custody/offsite produksi.

## Watcher dan restart

```bash
.venv/bin/python scripts/staging_ops.py start --directory data/staging
.venv/bin/python scripts/staging_ops.py stop --directory data/staging
.venv/bin/python scripts/staging_ops.py configure --directory data/staging --disable-backup
```

Start idempoten jika PID/starttime/argv/token/proyek cocok. Stop hanya mengirim SIGTERM ke watcher itu; backup aktif diselesaikan dahulu, tanpa forced kill. Stop tetap dapat diminta saat backup memegang lock. Lock nonblocking mencegah dua putaran backup bersamaan; configure/once bisa ditolak saat putaran lain aktif, ulangi setelah selesai. Disable menghentikan backup berikutnya; stop menghentikan pemantauan juga. Arsip/data/konfigurasi dipertahankan.

Daemon lokal terlepas dari sesi shell ini belum memakai systemd/cron atau HA. Proses tidak dijamin bertahan setelah mesin/snapshot cloud berpindah. Setelah restart, periksa Docker/direktori asli lalu `up` dan `start` sekali; missing volume harus dipulihkan secara eksplisit. Jangan prepare/bootstrap ulang data yang dipertahankan. `watch`/`--watch-token` dipakai internal oleh start.

Tidak ada alert email/Slack/webhook, autoheal, offsite, PITR atau SLA/RPO/RTO produksi. Monitor satu host dapat mati bersama host. VPS/domain, integrasi nyata, beban, retensi, pengawasan eksternal dan restore dari media terpisah masih pending.

## Validasi

Tes unit mencakup heartbeat optional/private/atomic/I/O, format/usia/masa depan, konfigurasi terikat proyek, data tak tersedia, HTTPS/sertifikat, backup jadwal/interval/retry/missing/hold, log transisi/rotasi, laporan lama, lock dan PID reuse. `scripts/verify_staging_backup.py` menjalankan fault injection hanya pada stack sementara: SIGSTOP loop billing sementara container tetap Running, SIGCONT/pemulihan, Docker pause/resume, backup terautentikasi/tanpa duplikat, missing archive, log tanpa secret, lifecycle watcher dan recovery worker hold. Staging pengguna tidak dipakai untuk fault injection.

Latihan web image gagal lalu kembali ke image sebelumnya tersedia terpisah dalam [STAGING-ROLLOUT](STAGING-ROLLOUT.md). Monitor tetap melaporkan kegagalan; tidak ada autoheal atau resume worker. Latihan hanya menggunakan proyek/volume sementara dengan schema tetap.
