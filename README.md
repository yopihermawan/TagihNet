# TagihNet

**Billing rapi, jaringan terkendali.** Fondasi aplikasi billing ISP dengan backend nyata, data fiktif, dan integrasi simulasi.

![Dashboard TagihNet](docs/evidence/dashboard-desktop.png)

Proyek baru ini dibangun dari [brief pengguna](docs/PRODUCT-BRIEF.md). Repositori awal tidak memiliki commit atau branch `main`. Pengerjaan dilakukan di branch `feature/tagihnet-foundation`. Ini checkpoint fondasi, belum seluruh cakupan brief dan belum siap produksi. Tidak dilakukan audit backend, pengambilan aset, atau perubahan pada KelolaNet.

## Yang bisa digunakan

- Login/logout, pemulihan akun lokal, rate limit persisten, timeout idle 30 menit dan absolut 8 jam.
- Dua ISP fiktif dengan pembatasan tenant dari akun yang terautentikasi; preset owner, operasional, keuangan, kasir, NOC, dan pelanggan.
- Dashboard yang menghitung data dari ledger, disertai definisi metrik, periode, cakupan, serta waktu pembaruan WIB.
- Cari/filter/sort pelanggan, pagination server-side 10/25/50 yang tersimpan di URL, tambah/ubah/detail, dan ekspor CSV berizin.
- Satu layanan/paket per pelanggan; faktur bulanan idempoten, snapshot harga/pajak, diskon, pembayaran sebagian, penolakan lebih bayar, dan reversal penuh dengan histori.
- Detail faktur, cetak/PDF A4 multipage dengan footer teks aman, serta portal untuk faktur milik pelanggan sendiri.
- Antrean rekonsiliasi jaringan **simulasi** dengan pengecekan ulang tunggakan, backoff, dan maksimal tiga percobaan. Pembayaran tetap tercatat bila adapter gagal.
- Mode terang/gelap, navigasi mobile, fallback peta eksternal, dan jejak audit.

[FEATURE-MATRIX](docs/FEATURE-MATRIX.md) merinci fungsi yang selesai, terbatas, dan belum dibuat. Gateway pembayaran, MikroTik/RADIUS nyata, MFA, izin yang dapat diedit, tiket/PSB, hotspot, payroll, dan migrasi produksi belum tersedia. Tidak ada menu palsu untuk modul tersebut.

## Stack dan struktur

Python 3.12+, Django **5.2 LTS**, SQLite untuk satu mesin pengembangan, template HTML/CSS/JavaScript tanpa build frontend, dan ReportLab untuk PDF. Django dipilih karena autentikasi, ORM, migrasi, validasi, dan CSRF bawaannya. PDF digambar dari teks lokal; tidak memakai browser atau mengambil URL eksternal.

```text
config/                  pengaturan, URL, WSGI
core/models.py           tenant, pelanggan, faktur, ledger, audit, antrean
core/billing.py          transaksi domain dan hitungan integer rupiah
core/network.py          adapter simulasi dan rekonsiliasi
core/permissions.py      izin deny-by-default
core/management/commands worker dan seed lokal
core/tests/              pengujian domain, HTTP, izin, concurrency
templates/, static/      antarmuka orisinal TagihNet
scripts/                 smoke browser, PDF, backup/restore
docs/                    cakupan, audit, pengujian, operasional
data/                    database/kunci/email lokal; diabaikan Git
```

## Menjalankan lokal — Linux/macOS/cloud

Gunakan checkout yang ada; lingkungan cloud sudah terisolasi dan tidak memerlukan Git worktree baru.

```bash
cd /workspace/TagihNet       # atau direktori clone Anda
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000 --noreload
```

`seed_demo` membuat kata sandi acak dan menampilkannya sekali untuk akun baru. Untuk kata sandi lokal yang Anda pilih sendiri (minimal 12 karakter), set `TAGIHNET_DEMO_PASSWORD` sebelum seed. Seed ditolak saat `DEBUG=0` atau `SIMULATION=0`; pengulangan tidak mereset kata sandi maupun menimpa data yang sudah ada. Tidak ada kata sandi produksi bawaan.

Akun seed: `owner`, `owner2` (ISP kedua), `keuangan`, `kasir`, `noc`, `pelanggan`. Semua akun baru memakai kata sandi seed pada eksekusi tersebut. Dalam instance cloud yang disiapkan pada 31 Oktober 2026, kata sandi **khusus lokal** adalah `TagihNet-Lokal-2026!`. Jangan gunakan akun/kata sandi ini untuk deployment publik.

Jalankan worker di terminal kedua:

```bash
source .venv/bin/activate
python manage.py run_simulation_worker          # loop; Ctrl+C untuk berhenti
# atau satu batch:
python manage.py run_simulation_worker --once
```

Gunakan URL lokal hanya saat menjalankan sendiri; lingkungan onboarding tidak menyediakan preview web publik.

## Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate --noinput
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
```

## Docker Desktop — konfigurasi pengembangan

```bash
docker compose up --build -d
docker compose exec web python manage.py seed_demo
docker compose exec web python manage.py run_simulation_worker --once
docker compose down
```

Volume `tagihnet-data` mempertahankan database. `docker compose down -v` menghapusnya; jangan gunakan bila ingin menyimpan data. Image berjalan sebagai user non-root; port hanya diikat ke localhost. Dockerfile dan konfigurasi Windows disediakan, tetapi belum dieksekusi pada checkpoint Linux ini.

## Pengujian

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python -m pip check
python manage.py test --verbosity 2
python -m pip install -r requirements-dev.txt
python scripts/verify_pdf.py
python scripts/verify_restore.py
```

Uji browser memerlukan server aktif, Chromium, dan seed lokal. Gunakan kata sandi seed aktual. Skrip menolak host non-lokal, menambahkan satu pelanggan uji berlabel `Uji Browser`, dan melakukan transaksi **simulasi**. Bukti visual disimpan di `docs/evidence/`.

```bash
export TAGIHNET_DEMO_PASSWORD='kata-sandi-seed-lokal-Anda'
export CHROMIUM_PATH=/usr/bin/chromium   # sesuaikan lokasi executable
python scripts/browser_smoke.py
```

[TEST-REPORT](docs/TEST-REPORT.md) memuat hasil yang benar-benar dijalankan. GitHub Actions memeriksa backend; browser/PDF/restore dijalankan terpisah.

## Konfigurasi dan troubleshooting

Variabel tersedia di [.env.example](.env.example). Aplikasi membaca **environment process**, tidak otomatis memuat file `.env`. Secret lokal acak disimpan dengan mode 0600 di `data/.secret_key`. Jangan commit database, email reset, backup, atau kunci.

- **Login ditolak setelah percobaan berulang:** tunggu 15 menit; batas awal lima percobaan per identitas/IP, termasuk percobaan sukses. Jangan menonaktifkan rate limit untuk deployment.
- **Kata sandi seed berbeda:** menjalankan seed ulang tidak mengubah kata sandi. Gunakan `python manage.py changepassword owner` secara lokal.
- **Status pembayaran lunas tetapi layanan belum aktif:** jalankan worker; lihat antrean/gagal. Data seed memiliki satu router simulasi sengaja tidak terjangkau.
- **Database busy:** ulangi request dengan kunci idempotensi yang sama. SQLite untuk pengembangan; uji PostgreSQL serta lock/queue terdistribusi sebelum skala produksi.
- **Email reset tidak terkirim:** pada development, lihat file privat di `data/emails/`. SMTP/TLS harus dikonfigurasi dan diuji untuk deployment.
- **Sesi online tidak ada:** adapter monitoring nyata belum dibuat; UI sengaja menampilkan “belum tersedia”.

Lihat [OPERATIONS](docs/OPERATIONS.md), [SECURITY-AUDIT](docs/SECURITY-AUDIT.md), [ASVS-CHECKLIST](docs/ASVS-CHECKLIST.md), [PERMISSIONS](docs/PERMISSIONS.md), dan [UX-AUDIT](docs/UX-AUDIT.md) sebelum melanjutkan implementasi atau deployment.
