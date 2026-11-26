# Uji beban staging lokal

Checkpoint 15 November 2026 WIB. Ini pengukuran singkat pada data fiktif dan mesin cloud bersama; bukan kapasitas pelanggan atau SLA produksi. Jalankan dari root checkout setelah membaca [STAGING](STAGING.md) dan [STAGING-OPERATIONS](STAGING-OPERATIONS.md).

## Menjalankan

```bash
.venv/bin/python scripts/staging_lab.py build
.venv/bin/python scripts/staging_load.py --output data/evidence/staging-load-baru.json
```

Pada cloud dengan proxy CA, opsi build yang sudah diuji tetap `--proxy-ca-file /etc/ssl/certs/ca-certificates.crt`. TLS tidak dinonaktifkan. Script selalu membuat direktori/proyek/CA/password/volume baru di temporary directory, memilih port loopback dan membersihkan hanya stack itu. Tidak ada argumen URL atau direktori staging tujuan; source `data/staging`, clone recovery dan default SQLite tidak dipakai sebagai target beban. File laporan yang sudah ada atau symlink ditolak sebelum provisioning.

Default: tahap20detik pada1,4,8 klien. Opsi `--seconds`5–60, `--levels`1–16 dengan maksimal4 level unik/urut, `--p95-ms`100–10000. Jangan memakai batas baru untuk mengubah hasil gagal menjadi lulus; hasil pembanding perlu menyebut konfigurasi. Budget awal yang dipakai checkpoint ini adalah p95 maksimal2000ms **pada setiap endpoint di setiap tahap**, nol kegagalan dan minimal3 sampel per endpoint. Latensi seluruh percobaan termasuk timeout/kegagalan dihitung; sampel kosong bernilai null, bukan nol. Exit0 hanya jika seluruh gate dan cleanup lulus.

## Profil dan pengukuran

- Native HTTPS Gunicorn2 sync worker, batas web1CPU/512MiB, PostgreSQL TLS dan3 worker simulasi. Batas container sebenarnya dan image ID dicatat; tidak ada tuning resource saat mengukur.
- Satu owner melakukan login/MFA HTTP nyata pada stack fiktif. Tiap klien memulai dengan salinan cookie owner itu lalu memperbarui cookie sendiri. Ini bukan uji login serentak atau banyak akun/tenant.
- Fixture500 pelanggan,1000 faktur dua periode sebelumnya,250 pembayaran/25 reversal, plus satu pelanggan/faktur tenant canary. SQL ANALYZE dijalankan setelah bulk fixture untuk profil database dengan statistik terisi; bukan cold start.
- GET bergiliran: dashboard,50 pelanggan dengan piutang,50 faktur overdue, tab faktur pelanggan, laporan piutang dan PDF satu faktur. Setiap request memakai koneksi TLS baru, memeriksa status/type/marker/batas2MiB. Redirect login tidak diikuti sebagai sukses. Respons tidak disimpan.
- Scope negatif customer/PDF tenant lain harus404; marker canary tidak boleh muncul pada body yang dibaca. Seluruh pengujian izin yang lebih luas tetap ada pada suite backend/browser.
- Pada tahap terakhir, fixture membuat preview/konfirmasi domain untuk batch500 faktur bulan berjalan, memakai actor/versi akses aktual. Worker billing container yang sudah berjalan mengerjakannya sambil GET berlangsung. Konfirmasi fixture ini bukan pengukuran POST/MFA pengguna di bawah beban.
- Sampler memeriksa HTTPS, heartbeat/antrean/SQL dan resource Docker tiap sekitar5detik, termasuk overhead monitor. Restart container, ID berubah, OOM, health non-ok atau metrik sampler tidak tersedia menggagalkan gate.

Klien menunggu respons sebelum request berikutnya; saat server lambat, laju request masuk ikut turun. Requests/second adalah throughput yang teramati, bukan penawaran traffic konstan. Durasi tahap mencakup drain request terakhir sampai timeout10detik; percentile memakai nearest rank. P50/p95/p99/max serta failures dicatat keseluruhan dan per endpoint. Resource adalah sampel, bukan maksimum setiap saat; Gunicorn child recycle tidak sama dengan restart container.

Setelah batch selesai, jumlah faktur harus1500, kombinasi pelanggan/periode unik,500 item created, replay worker0 faktur tambahan, pembayaran/reversal tetap250/25 dan total/saldo fixture cocok. Durasi `batch_seconds_to_observation` adalah waktu sampai pemeriksaan akhir mengamati selesai, termasuk waktu tahap request; bukan latensi worker yang presisi. Tidak ada gateway/router/pesan nyata.

JSON laporan memakai file0600, publikasi atomic/no-clobber, hanya jumlah/latensi/kode error/resource/metadata image; tanpa cookie, OTP, password, key, nama pelanggan atau body exception. Laporan kegagalan dan hasil cleanup juga dipertahankan bila proses mencapai fase probe. SIGKILL/host crash tidak menjamin finally/cleanup; inspeksi stack milik tes sebelum tindakan pemulihan. Script tidak menghapus direktori/volume pengguna atau mencoba target pengganti.

## Perbaikan dari baseline

Baseline lama mengulang query pembayaran per faktur untuk dashboard dan filter status, serta memuat seluruh faktur ke Python sebelum pagination. Regresi query membuktikan dashboard37→115 dan filter overdue23→72 query setelah25 faktur ditambahkan; halaman50 mencapai105 query. Tes baru gagal pada source lama sebelum perbaikan.

Dashboard sekarang mengagregasi ledger/angka pelanggan di SQL dan mengambil maksimal5 faktur prioritas. Daftar faktur menghitung pembayaran sah dalam SQL, memfilter status sebelum pagination dan memakai row baca untuk status/sisa. Perhitungan tenant/reversal digunakan bersama dengan filter pelanggan. Properti model dan fungsi mutasi uang tidak memakai cache saldo global; tidak ada schema/dependency baru. Tes membatasi dashboard≤18 dan daftar≤12 query serta membuktikan jumlah query tetap saat data bertambah, dengan nominal/status/reversal/scope tetap.

Percobaan awal sebelum ANALYZE mengalami timeout pada halaman pelanggan. Kontrol berikutnya memakai ANALYZE dan tetap gagal budget latensi, dengan satu pemeriksaan HTTPS monitor gagal karena request tertahan. Keduanya menyimpan hasil gagal dan cleanup lulus. Kontrol tersebut berlangsung10detik/tahap; run akhir memakai default20detik/tahap, sehingga bukan perbandingan statistik dengan durasi identik.

## Hasil instance ini

Run final default20detik/tahap lulus,1171 request/0 kegagalan. Ringkasan agregat yang dapat ditinjau ada di [evidence/staging-load-20261115.json](evidence/staging-load-20261115.json), termasuk hash laporan privat lengkap.

| Klien bersamaan | Request | Request/detik | P50 ms | P95 ms | P99 ms | P95 endpoint terburuk ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 288 | 14,39 | 69,74 | 112,20 | 164,85 | 151,56 |
| 4 | 436 | 21,67 | 176,94 | 298,09 | 377,10 | 352,45 |
| 8 | 447 | 22,07 | 350,22 | 533,40 | 594,30 | 558,04 |

Sepuluh sampel monitor semuanya sehat, tidak ada Docker restart/ID berubah/OOM. Sampel CPU tertinggi web80,94% dan database100,74% dari pelaporan Docker; limit masing-masing1CPU. Sampel memori web sekitar138,1MiB dari512MiB. Throughput4→8 klien hampir tetap sementara latensi naik; hasil ini belum menunjukkan ruang untuk terus menambah concurrency. Jangan mengekstrapolasi menjadi jumlah pelanggan produksi.

Worker menyelesaikan500 faktur baru:1500 faktur total,250 pembayaran/25 reversal tetap, saldo aktual315.000.000 dan replay0 faktur baru. Seluruh gate termasuk scope canary, p95 per endpoint, kesehatan sampler dan cleanup lulus. Pemeriksaan akhir mengamati batch selesai setelah36,58detik; ini bukan durasi worker yang presisi. Source pengguna/clone/default demo tidak berisi fixture ini.

## CI dan batas

Workflow manual `.github/workflows/staging-load.yml` melakukan build/probe dan mengunggah JSON agregat selama7hari. Tidak dijalankan pada setiap push dan belum diklaim lulus di GitHub. Runner berbeda akan memberi hasil berbeda. Backend/PG/browser CI biasa tetap tersedia.

Ini belum menguji peak traffic dengan laju tetap, ribuan sesi/tenant, banyak pembayaran POST serentak, data bertahun-tahun, PDF besar, external WAN, soak panjang, failover, restore di server lain, atau performa real adapter. Infrastruktur produksi masih memerlukan pengujian dengan hardware/data/traffic target, retensi/offsite dan integrasi nyata.

## Portal pelanggan pada rilis 23 November 2026 WIB

Image staging aktif lulus pemeriksaan HTTPS fungsional di proyek Compose sementara dengan dua ISP, 500 pelanggan, 2.000 faktur, 8.500 pembayaran, dan 500 reversal. Dua akun fiktif memeriksa daftar faktur, filter bulan layanan dan status, halaman kedua pembayaran bulan WIB, serta kuitansi yang direversal. Delapan permintaan lintas pelanggan atau ISP menerima HTTP 404. Empat permintaan dengan bulan tidak valid menerima HTTP 400. Ruang Docker mencapai nol sebelum pengukuran beban. Proyek uji dibersihkan dan kedua proyek staging tersimpan tetap sehat. Bukti privat tersedia di `data/evidence/staging-portal-load-20261123.json`.

Pengukuran alternatif memakai Gunicorn HTTPS pada host dan database sementara di lab PostgreSQL loopback yang sudah berjalan. Fixture berisi dua ISP, 200 pelanggan, 800 faktur, 3.400 pembayaran, dan 200 reversal. Dua akun pelanggan bergantian pada empat endpoint yang sama. Klien menunggu respons sebelum mengirim permintaan berikutnya. Setiap tahap berlangsung 20 detik dan p95 per endpoint dibatasi 2.000 ms.

| Klien bersamaan | Permintaan | Gagal | Permintaan/detik | P95 keseluruhan (ms) | P95 endpoint terburuk (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 644 | 0 | 32,20 | 42,76 | 45,96 |
| 4 | 1.144 | 0 | 57,07 | 89,71 | 93,08 |
| 8 | 1.172 | 0 | 58,21 | 193,54 | 210,44 |

Database sementara berukuran sekitar 13 MiB dan dibersihkan setelah tes. Isi tabel bisnis, metadata kolom, dan sequence cocok sebelum dan sesudah pengukuran. Pengukuran host memakai profil lab PostgreSQL dan sumber daya host. Kapasitas Compose dengan batas resource container, PostgreSQL verify-full, serta worker bersamaan masih perlu diukur pada host dengan ruang Docker memadai. Bukti privat tersedia di `data/evidence/portal-pg-lab-load-20261123.json`.
