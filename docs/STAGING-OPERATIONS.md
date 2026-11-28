# Pemantauan dan backup berkala staging lokal

Checkpoint 15 November 2026 WIB. Jalankan dari root checkout dengan `.venv/bin/python`. Ini alat administrator host Linux, memakai Docker lokal dan manifest staging; tidak tersedia dari portal web. Profil tetap DEBUG=0/SIMULATION=1. Panduan terkait: [STAGING](STAGING.md), [STAGING-RECOVERY](STAGING-RECOVERY.md).

## Pemeriksaan

```bash
.venv/bin/python scripts/staging_ops.py once --directory data/staging
.venv/bin/python scripts/staging_ops.py status --directory data/staging
```

`once` memeriksa layanan dan menjalankan satu backup jatuh tempo bila backup telah diaktifkan. Exit0 hanya untuk `ok`, lainnya1. `status` membaca laporan terakhir beserta identitas watcher dan usia laporan; bukan pemeriksaan jaringan baru. Laporan lebih tua dari2×interval+30detik, bertanggal masa depan >5detik, atau kegagalan putaran watcher setelah laporan terakhir menghasilkan `unknown`. Watcher tidak aktif dilaporkan terpisah; pemeriksaan manual yang baru masih dapat `ok`.

Pemeriksaan mencakup container web/database, health Docker, HTTPS dengan CA/hostname lokal terverifikasi, tanggal sertifikat dan kemajuan loop ketiga worker. Heartbeat JSON privat berada di tmpfs masing-masing container, diperbarui awal/akhir putaran dan sebelum pekerjaan. Gagal menulis heartbeat tidak membatalkan transaksi billing; monitor melaporkan heartbeat tidak tersedia/kedaluwarsa. PID hidup saja tidak cukup. Default usia heartbeat120detik; pekerjaan sah yang sangat lama dapat memicu alarm dan perlu diperiksa sebelum menaikkan ambang.

Probe HTTPS watcher dan pemeriksaan sambungan ulang memakai koneksi langsung ke `localhost` pada port manifest, dengan CA staging dan verifikasi nama host. Respons redirect dianggap gagal.

Statistik antrean dibaca melalui transaksi PostgreSQL READ ONLY sebagai `tagihnet_runtime`: jumlah/umur pekerjaan jatuh tempo, batch antre/processing, retry habis dan blocked. Umur >900detik memberi warning. Kegagalan jaringan yang dihitung adalah job terakhir pelanggan yang gagal setelah tiga percobaan. Job sebelumnya tetap tersedia pada riwayat. Ini agregat seluruh proyek staging, bukan izin portal per ISP. Query gagal menghasilkan `metrics: null`/`unknown`, bukan nol. Keparahan: ok, warning, unknown, critical.

Recovery_pending menampilkan worker `held` dan menahan alarm backlog; worker yang ternyata berjalan tetap critical. Monitor tidak menyalakan/resume worker, mengubah job, mengirim notifikasi atau memperbaiki layanan. Restore preparing/failed diblokir. Gunakan direktori/CA/port yang sesuai untuk source dan clone.

## Kapasitas penyimpanan

Bagian `storage` pada laporan watcher mencatat byte dan inode yang tersedia pada checkout, direktori staging, filesystem root container database, serta volume PostgreSQL. Jika backup otomatis aktif, laporan juga mengukur direktori tujuan. Setiap angka berlaku untuk filesystem tempat komponen berada. Beberapa komponen dapat berbagi filesystem, sehingga penjumlahannya akan menghitung ruang yang sama berulang kali.

Perintah `storage` mengambil sampel baru secara baca-saja. Pemeriksaan di bawah memakai 2 GiB sebagai contoh ambang untuk pekerjaan yang akan dijalankan.

```bash
.venv/bin/python scripts/staging_ops.py storage --directory data/staging
.venv/bin/python scripts/staging_ops.py storage --directory data/staging --min-free-mib 2048
```

Gunakan ambang sesuai kebutuhan build, backup, atau pemulihan. `storage` memakai ambang proyek bila tersedia. `--min-free-mib` menggantinya untuk satu pemeriksaan. Exit 0 berarti semua filesystem terukur dan lolos pemeriksaan. Exit 1 menghasilkan `warning` jika ruang di bawah ambang, `critical` jika byte atau inode habis, atau `unknown` jika pengukuran tidak lengkap. Folder tujuan backup yang belum tersedia dilaporkan `unknown`.

### Peringatan ruang kosong watcher

Setel ambang per proyek dalam MiB. Nilai `0` menonaktifkan peringatan pada putaran berikutnya.

```bash
.venv/bin/python scripts/staging_ops.py configure --directory data/staging --storage-min-free-mib 1536
.venv/bin/python scripts/staging_ops.py status --directory data/staging
```

Saat ambang aktif, `once` dan watcher memasukkan kondisi penyimpanan pada status operasi dan mencatat transisi `raised` atau `resolved` di `ops/events.jsonl`. Ambang berlaku pada tiap filesystem yang dilaporkan. Pemeriksaan langsung `check()` dan preflight tetap menilai layanan secara tersendiri. Peringatan dapat terlihat saat backup periodik tetap berjalan sesuai jadwal.

Konfigurasi lama memulai dengan peringatan nonaktif. Setelah memperbarui script, hentikan watcher yang masih menjalankan kode lama dan tunggu prosesnya selesai sebelum menyimpan ambang. Mulai kembali watcher untuk memuat versi terbaru. [Perintah watcher](#watcher-dan-restart)

### Estimasi ruang pemulihan Docker

Pada host yang memakai driver Docker `vfs`, jalankan pemeriksaan berikut dari staging sumber yang sedang berjalan dengan image yang dipin.

```bash
.venv/bin/python scripts/staging_ops.py storage --directory data/staging --recovery-plan
```

Laporan `recovery_plan` membandingkan ruang yang tersedia pada filesystem Docker dengan ukuran image BusyBox untuk `prepare`, image PostgreSQL untuk `db`, dua kali ukuran image aplikasi untuk `migrate` dan `web`, serta byte terpakai pada volume PostgreSQL sumber. Nilai negatif pada `remaining_after_estimate_bytes` menghasilkan `warning` dan exit 1. Driver lain atau pengukuran yang tidak tersedia menghasilkan `unknown` untuk estimasi. Pada host baru, pull image yang dipin terlebih dahulu agar ukurannya dapat diukur. Sisakan ruang tambahan untuk build atau pull image, operasi PostgreSQL, dan proses lain pada host.

Sisakan ruang tambahan untuk file sementara, WAL, log, dan overhead driver. Besarnya bisa berbeda menurut ukuran arsip, kondisi volume dan host. Estimasi yang muat masih perlu dibuktikan dengan latihan restore. Perintah hanya membaca kapasitas dan tidak memulai container atau mengubah jadwal backup.

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

### Backup manual

Pada proyek aktif yang sudah mengaktifkan backup, perintah ini membuat satu backup terpantau segera.

```bash
.venv/bin/python scripts/staging_ops.py backup-now --directory data/staging
```

Backup manual memakai lock yang sama dengan watcher. Setelah arsip berhasil diautentikasi, metadata menetapkannya sebagai backup terbaru. `last_attempt_at` mencatat waktu mulai, `last_success_at` waktu verifikasi selesai, dan `next_attempt_at` waktu sukses ditambah interval yang dikonfigurasi. Jika backup gagal, metadata mempertahankan rujukan ke arsip sukses sebelumnya dan menjadwalkan percobaan ulang lima menit setelah kegagalan. Exit 0 pada `backup-now` berarti arsip terverifikasi. Kondisi layanan tetap dilaporkan melalui field `status`.

Pemeriksaan rilis dapat menyiapkan pasangan pemulihan dari backup terbaru lewat `--prepare-custody`. [Runbook rilis](RELEASE-RUNBOOK.md#jalankan-gate-aktif)

Inventaris offline memberi tanggal arsip terautentikasi, ukuran logis dan kecocokan paket pemulihan. [Panduan inventaris](RECOVERY-HANDOFF.md#inventaris-backup-staging)

Sesudah backup terpantau berganti, perintah `staging_backup_custody.py` bisa memakai ulang pasangan yang cocok atau menyiapkan pasangan baru. Hasilnya memberi kedua direktori untuk pemeriksaan rilis. [Panduan paket backup terpantau](RECOVERY-HANDOFF.md#paket-untuk-backup-terpantau)

Konfigurasi, laporan, metadata backup dan watcher berada di `ops/*.json` pada direktori staging yang diabaikan Git. File baru0600, direktori baru0700. Laporan hanya timestamp/status/jumlah agregat/path arsip, tanpa nama pelanggan/nominal/password/isi key. `ops/events.jsonl` berisi transisi raised/resolved tanpa body exception. Rotasi sesudah256KiB menyimpan3 file lama. Arsip backup **tidak dihapus otomatis**; pantau ruang disk dan tentukan retensi terpisah. Key dan arsip satu workspace belum memenuhi custody/offsite produksi.

## Watcher dan restart

```bash
.venv/bin/python scripts/staging_ops.py start --directory data/staging
.venv/bin/python scripts/staging_ops.py stop --directory data/staging
.venv/bin/python scripts/staging_ops.py configure --directory data/staging --disable-backup
```

Start idempoten jika PID/starttime/argv/token/proyek cocok. Stop hanya mengirim SIGTERM ke watcher itu; backup aktif diselesaikan dahulu, tanpa forced kill. Stop tetap dapat diminta saat backup memegang lock. Lock nonblocking mencegah dua putaran backup bersamaan; configure/once bisa ditolak saat putaran lain aktif, ulangi setelah selesai. Disable menghentikan backup berikutnya; stop menghentikan pemantauan juga. Arsip/data/konfigurasi dipertahankan.

Daemon lokal terlepas dari sesi shell ini belum memakai systemd/cron atau HA. Proses tidak dijamin bertahan setelah mesin/snapshot cloud berpindah. `watch`/`--watch-token` dipakai internal oleh start.

Tidak ada alert email/Slack/webhook, autoheal, offsite, PITR atau SLA/RPO/RTO produksi. Monitor satu host dapat mati bersama host. VPS/domain, integrasi nyata, beban, retensi, pengawasan eksternal dan restore dari media terpisah masih pending.

## Sambungkan ulang staging yang tersimpan

Gunakan perintah ini setelah Docker tersedia kembali dan proyek telah memiliki container serta volume yang tersimpan.

```bash
.venv/bin/python scripts/staging_reconnect.py --directory data/staging
.venv/bin/python scripts/staging_reconnect.py --directory data/staging-recovery-20261115 --mode recovery
```

Perintah memeriksa manifest proyek, image yang dipin, volume PostgreSQL, konfigurasi container, dan hasil migrasi terakhir untuk image tersebut. Container yang berhenti dinyalakan berdasarkan ID tersimpan. Urutannya dimulai dari database sehat, dilanjutkan HTTPS, lalu worker untuk staging utama. Watcher staging utama dipakai ulang atau dimulai setelah layanan siap. Mode `recovery` hanya menyambungkan database dan web dari clone yang menahan worker.

JSON `services_started` menunjukkan layanan yang dinyalakan. Daftar kosong berarti semua layanan terpilih sudah berjalan. Status `warning` menyertakan kondisi operasional yang perlu ditinjau, seperti antrean tertunda. Kegagalan identitas, konfigurasi, atau kesiapan menghasilkan exit 1. Gunakan diagnosis sesuai kondisi sumber. Untuk proyek yang kehilangan container atau volume, prosedur `up` dan pemulihan terpisah tersedia dalam [panduan staging](STAGING.md) dan [panduan pemulihan](STAGING-RECOVERY.md).

## Validasi

Tes unit mencakup heartbeat optional/private/atomic/I/O, format/usia/masa depan, konfigurasi terikat proyek, data tak tersedia, HTTPS/sertifikat, backup jadwal/interval/retry/missing/hold, log transisi/rotasi, laporan lama, lock dan PID reuse. `scripts/verify_staging_backup.py` menjalankan fault injection hanya pada stack sementara: SIGSTOP loop billing sementara container tetap Running, SIGCONT/pemulihan, Docker pause/resume, backup terautentikasi/tanpa duplikat, missing archive, log tanpa secret, lifecycle watcher dan recovery worker hold. Staging pengguna tidak dipakai untuk fault injection.

Latihan web image gagal lalu kembali ke image sebelumnya tersedia terpisah dalam [STAGING-ROLLOUT](STAGING-ROLLOUT.md). Monitor tetap melaporkan kegagalan; tidak ada autoheal atau resume worker. Latihan hanya menggunakan proyek/volume sementara dengan schema tetap.
