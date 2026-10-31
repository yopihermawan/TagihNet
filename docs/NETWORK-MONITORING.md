# Monitoring NAS dan sesi simulasi

Checkpoint `feature/tagihnet-network-monitoring` menyediakan alur NOC yang dapat diuji tanpa router: konfigurasi NAS contoh, pengambilan snapshot sesi PPPoE sintetis, kualitas data, ringkasan dengan sumber/cakupan, dan histori. Tidak ada koneksi MikroTik/RADIUS, alamat perangkat, password PPP, trafik, CoA atau perintah perubahan layanan pada adapter ini.

## Menggunakan alur

1. Owner membuka **Monitoring simulasi → NAS simulasi**, mengisi nama/lokasi dan memilih skenario. Aktifkan pengambilan bila diperlukan, tinjau checkbox, lalu simpan dengan MFA terbaru (<=5 menit).
2. Buka detail dan pilih **Ambil snapshot simulasi**, atau jalankan worker jaringan yang sudah ada. GET/Muat ulang hanya membaca data.
3. Periksa waktu pengamatan, versi konfigurasi dan kualitas sumber; snapshot terakhir tetap dapat dibaca sebagai histori bila sudah tidak berlaku untuk ringkasan.

```bash
python manage.py run_simulation_worker
# Rekonsiliasi dan monitoring satu putaran:
python manage.py run_simulation_worker --once
# Monitoring saja, satu putaran:
python manage.py collect_monitoring --limit 100
```

Tidak diperlukan proses worker tambahan. Collector memilih NAS enabled yang jatuh tempo, urut next_poll_at/ID; batas 1–500 per putaran. NAS baru nonaktif secara bawaan; tidak ada NAS/poll otomatis dari seed. Maksimal 50 NAS per ISP dan 100 sesi per snapshot. Limit ini tidak membuktikan kapasitas produksi.

## Skenario dan sumber

| Skenario | Hasil adapter | Dampak ringkasan |
| --- | --- | --- |
| Sesi contoh online/offline | Tiga sesi sintetis: dua online, satu offline, waktu pengamatan sekarang | Dihitung selama segar |
| Tidak ada sesi | Snapshot berhasil dengan daftar kosong | Nol online/offline yang diketahui |
| Data kedaluwarsa | Tiga sesi contoh diamati sepuluh menit sebelumnya | Dikecualikan; jumlah bukan nol |
| Router tidak dapat dijangkau | Percobaan gagal tanpa snapshot baru | Histori sukses tetap ada, dikecualikan dari jumlah terkini |

Username `demo-ppp-01` sampai `demo-ppp-03` adalah label fiktif dan dapat muncul pada tiap NAS. Ringkasan menghitung **sesi per NAS**, bukan pelanggan/username unik lintas NAS. Data tidak dipetakan ke akun pelanggan, status active/isolated/pending, faktur atau penugasan NAS pelanggan. Filter NAS pada billing/pelanggan belum tersedia. Sesi sintetis tidak membuktikan layanan atau perangkat online.

## Kualitas data dan ringkasan

| Kualitas | Arti |
| --- | --- |
| Segar | Konfigurasi terkini, persetujuan berlaku, enabled, pengambilan terakhir berhasil, umur pengamatan 0–120 detik |
| Kedaluwarsa | Snapshot konfigurasi terkini di luar umur valid, termasuk bila jam mundur sebelum waktu pengamatan |
| Gagal diambil | Percobaan versi terkini gagal; snapshot sukses terdahulu tetap sebagai histori |
| Belum ada snapshot versi ini | Belum berhasil mengambil konfigurasi saat ini; histori versi sebelumnya tidak dihitung |
| Persetujuan tertahan | Akun, tenant, capability atau session_version pemberi persetujuan berubah |
| Nonaktif | Pengambilan dimatikan; histori tetap ada |

Ringkasan hanya menjumlahkan NAS Segar dan menyebut **X dari Y NAS**, jumlah dikecualikan, pengamatan tertua yang digunakan, waktu perhitungan, ISP dan sumber adapter simulasi. Jika tidak ada NAS segar, jumlah tampil **Belum ada data segar**, bukan 0. Ringkasan seluruh ISP tidak berubah karena filter tabel. Snapshot lama di tabel/detail selalu mempertahankan waktu/versi, dan diberi keterangan histori bila tidak masuk ringkasan.

Jam tersimpan timezone-aware dan ditampilkan Asia/Jakarta. Snapshot masa depan, lebih tua dari satu hari, naive, payload >100 sesi, field/status/durasi salah, offline dengan uptime bukan nol, atau username duplikat dalam satu snapshot ditolak. Kegagalan validasi menghasilkan failed tanpa menyimpan payload tersebut; detail exception tidak dicatat.

## Izin dan persetujuan

`network` memberi akses baca/history dan permintaan pengambilan satu ISP; Owner/NOC memilikinya. NOC tidak mendapat akses konfigurasi, billing atau secret. `manage_monitoring` diperlukan untuk konfigurasi dan hanya tersedia bawaan untuk Owner; custom role wajib menyertakan `network`. Pelanggan, teknisi dan preset finansial tidak memiliki monitoring secara bawaan.

Penyimpanan memerlukan CSRF, review eksplisit, versi form, versi sesi dan MFA terbaru. Persetujuan terikat akun aktif, ISP, capability dan session_version, bukan umur sesi web; worker dapat berjalan setelah sesi persetujuan habis. Bila hak berubah, ringkasan langsung mengecualikan data. Saat pengambilan berikutnya, worker menyimpan blocked dan mengosongkan next_poll_at agar tidak mengulang blocked tanpa persetujuan baru. Pengelola sah harus menyimpan ulang dengan MFA. Semua lookup memakai tenant dari membership; POST tidak menerima tenant, host atau secret.

Menyimpan konfigurasi membuat versi baru dan menjadwalkan pengambilan bila enabled. Snapshot versi lama menjadi histori, termasuk ketika hanya nama/lokasi yang berubah. Form lama ditolak; dua update bersamaan tidak saling menimpa. Tidak ada edit/delete snapshot atau hard delete NAS melalui aplikasi; nonaktifkan NAS untuk menghentikan pengambilan.

## Interval, idempotensi dan kegagalan

Pengambilan maksimal sekali per 60 detik pada versi yang sama; next_poll_at disimpan dan constraint unik device/version/menit melindungi duplikasi. Restart worker atau POST berulang dalam interval mengembalikan hasil terakhir tanpa menambah poll. Perubahan konfigurasi oleh pengelola ber-MFA memulai versi baru dan dapat diambil segera.

Kegagalan monitoring diulang paling awal 60 detik kemudian selama enabled dan persetujuan berlaku. Ini pengambilan berkala, berbeda dari job isolir/aktivasi yang maksimal tiga percobaan. Satu kegagalan tidak menghapus snapshot sukses atau mengubah ledger/status layanan. Collector dan `run_simulation_worker --once` mengembalikan exit nonzero bila monitoring gagal/blocked pada putaran itu; loop mencatat kegagalan dan melanjutkan putaran berikutnya. Keberhasilan percobaan tidak otomatis berarti datanya segar: skenario stale tetap dikecualikan.

Adapter ini tidak melakukan I/O. Lock/retry SQLite dan dua thread diuji lokal; tidak ada klaim exactly-once perangkat nyata, lock PostgreSQL lintas proses, sinkronisasi clock produksi, atau transport RADIUS terverifikasi. Tidak menambahkan endpoint diagnosis dengan perintah mentah/URL bebas.

## Migrasi, restore dan bukti

Migrasi 0006 menambah MonitoringDevice/MonitoringPoll, constraint dan index. Backup konsisten dibuat setelah menghentikan proses sesi. Migrasi dijalankan pada hasil restore sementara dan ledger/jumlah dibandingkan sebelum diterapkan ke database lokal. Data pengguna dan kunci MFA tidak diubah; tidak ada perangkat MFA/NAS/sesi uji yang ditambahkan ke database pengguna.

`verify_restore.py` membandingkan konfigurasi/versi/jadwal monitoring serta payload/status/waktu snapshot, selain ledger dan state operasional. Browser memakai database sementara dengan seluruh skenario sebelum restore drill. Backup tetap belum terenkripsi dan autentikasi hasil restore memerlukan kunci MFA terpisah. Riwayat monitoring belum memiliki retensi otomatis; pemakaian jangka panjang memerlukan pembatasan storage/retensi dan monitoring worker.

25 tes baru (187 total), browser NOC/Owner pada empat viewport, serta pemeriksaan migrasi/restore memberi bukti lokal. Lihat [TEST-REPORT](TEST-REPORT.md) dan [UX-AUDIT](UX-AUDIT.md). Gateway, router/RADIUS lab, trafik/polling nyata, PostgreSQL multi-process, TLS deployment dan kesiapan produksi tetap pekerjaan lanjutan.
