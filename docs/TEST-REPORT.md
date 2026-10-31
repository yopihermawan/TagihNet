# Laporan pengujian — 31 Oktober 2026, WIB

Target: checkpoint fondasi TagihNet, Linux cloud, Python 3.12.14, Django 5.2.18, SQLite, Chromium melalui Playwright 1.62.0. Data seluruhnya fiktif, dua tenant. Test database terpisah dari demo. Tidak ada transaksi gateway atau router nyata.

## Eksekusi dan bukti

| Perintah | Hasil yang diamati |
| --- | --- |
| `python manage.py migrate --noinput` | pass · skema auth/session dan core diterapkan dari checkout awal kosong |
| `python manage.py seed_demo` | pass · dua ISP, paket/pelanggan/faktur/pembayaran fiktif; idempotensi/rename/password diverifikasi melalui tes |
| `python manage.py check` | pass · no issues |
| `python manage.py makemigrations --check --dry-run` | pass · no changes detected |
| `python -m pip check` | pass · no broken requirements |
| `python manage.py test --verbosity 1` | pass · **37 tes dieksekusi**, tanpa fail/skip/expected-failure |
| `python manage.py collectstatic --noinput` | pass · tiga aset lokal terkumpul |
| `python scripts/browser_smoke.py` | pass · Chromium sungguhan: login/password toggle, tambah/reload pelanggan, faktur, pembayaran penuh, reversal, PDF, filter/URL kembali, logout, dark mode, menu mobile; **0 browser errors** |
| Browser responsive checks | pass terbatas · 360/768/1366/1920 px, dashboard/pelanggan/faktur/jaringan/setelan tanpa scroll horizontal seluruh halaman; tabel punya scroll lokal |
| `python scripts/verify_pdf.py` | pass · A4 **14 halaman**, 100 baris teks panjang, total Rp100.000, label simulasi, markup footer tampil sebagai teks; fixture di-rollback |
| `python scripts/verify_restore.py` | pass · backup konsisten dan restore ke direktori kosong, integrity_check ok; pelanggan/faktur/total/pembayaran/piutang sama |
| `python manage.py run_simulation_worker --once` | simulated · batch pertama 18 confirmed dan satu router seeded gagal; pembayaran tetap tersimpan, kegagalan dijadwalkan backoff |
| `pip-audit 2.10.1 -r requirements.txt --no-deps --disable-pip --vulnerability-service pypi` | pass saat scan · no known vulnerabilities found untuk enam paket runtime; tidak mencakup OS, dev dependencies, atau bug logika |
| `manage.py check --deploy` dengan DEBUG off dan secret/host sementara | warning konfigurasi · W021: HSTS preload false. Ini disengaja sampai domain/HTTPS siap, tidak ada klaim deployment HTTPS sudah diverifikasi |

Prefix `python` pada tabel berarti interpreter `.venv/bin/python` proyek. Pip-audit dijalankan dari venv terpisah `/tmp/tagihnet-audit`. Browser memakai Chromium `/usr/bin/chromium`. Bukti gambar tersedia di [UX-AUDIT](UX-AUDIT.md); PDF stress-test disimpan lokal di `data/evidence/` (diabaikan Git). Pengulangan browser menambah pelanggan fiktif berlabel `Uji Browser`; angka demo dapat bertambah. Restore drill menghitung ulang baseline sebelum backup, bukan mengandalkan jumlah seed yang tetap.

## Pemetaan terhadap kriteria brief

| No. | Kriteria | Status / batas bukti |
| --- | --- | --- |
| 1 | Auth/logout/reset/timeout/revocation | pass · rotasi session, POST logout, reset token invalid sesudah dipakai, sesi lama dicabut, idle/absolute expiry; SMTP produksi/MFA belum |
| 2 | Endpoint langsung tanpa izin | pass · kasir/NOC/customer, tidak hanya menu tersembunyi |
| 3 | Batas tenant/pelanggan | pass subset · detail/edit/payment/search/export/PDF/portal/job; upload/realtime tidak ada |
| 4 | Search/filter/sort/pagination URL | pass · 10/25/50 backend dan size=25 browser; filter NAS/jatuh tempo belum |
| 5 | Tambah/ubah pelanggan + reload | pass · HTTP dan browser; duplikat phone/username ditolak |
| 6 | Retry/concurrency penerbitan | pass lokal · dua thread dan retry, constraint customer/periode; multi-process PostgreSQL belum |
| 7 | Rupiah/tax/discount/akhir bulan | pass subset · integer, half-up, Februari/leap year/tanggal31; prorata belum tersedia |
| 8 | Webhook tidak sah/nominal/order | not tested · gateway/webhook belum diimplementasikan; path webhook tidak tersedia |
| 9 | Callback replay/out-of-order | pass subset simulated · pencatatan UUID persisten, mismatch ditolak, dua thread satu ledger; event lifecycle gateway belum |
| 10 | Pembayaran bersamaan isolir | simulated · dua urutan pembayaran/job, recheck utang saat eksekusi; race perangkat nyata belum |
| 11 | Router gagal tidak batalkan bayar | simulated · status failed dan pembayaran lunas bertahan; layanan tidak dipalsukan aktif |
| 12 | Markup tidak dieksekusi | pass lokal · web/detail dan PDF; portal memakai renderer sama dan otorisasi yang diuji; email faktur belum tersedia |
| 13 | Upload/impor/ekspor | pass subset · CSV tenant/izin/formula; upload/impor belum tersedia |
| 14 | Demo guard backend/worker | simulated · payment di luar SIMULATION ditolak, worker fail-closed; tidak ada adapter eksternal |
| 15 | Metrik jelas dan data gagal bukan nol | pass lokal · sumber/tenant/periode/update tersedia, sesi online dinyatakan belum tersedia |
| 16 | UI/accessibility/kontras/fallback | pass subset · screenshots, 4 viewport, password/menu/Escape/return URL, empty/validation states; audit WCAG penuh dan screen reader belum |
| 17 | PDF A4/multipage/nominal/izin | pass · browser PDF, stress test 14 halaman, footer panjang, cross-tenant 404 |
| 18 | Restore & rekonsiliasi | pass lokal · direktori kosong + jumlah/saldo identik; enkripsi/offsite/RPO/RTO belum |

## Tidak dijalankan / gate rilis

Docker build/runtime dan Windows PowerShell belum diuji. GitHub Actions baru ditambahkan; keberhasilan lokal bukan bukti CI remote lulus. Belum ada uji TLS deployment, SMTP nyata, load test, stress multiworker, lab MikroTik/FreeRADIUS/CoA/FastTrack/IPv6, gateway sandbox, upload, migrasi data produksi, payroll, atau penetrasi aplikasi menyeluruh. Seluruh modul pending tercatat di FEATURE-MATRIX. Tidak ada persentase kepatuhan atau klaim production-ready.

## Kesalahan selama setup yang sudah diperbaiki

Bootstrap pertama gagal karena chmod pada file object dan kemudian migrasi core belum terbentuk. Perbaikan pembuatan secret dan migrasi diuji ulang hingga pass. Percobaan awal browser berhenti pada locator label `Paket` yang tidak memperhitungkan tanda wajib; locator diperbaiki dan alur penuh lulus. Verifikasi PDF pertama memakai interpreter global tanpa Django; dijalankan ulang dengan venv proyek dan dependensi dev terkunci, lalu lulus. Keberhasilan pada tabel merujuk eksekusi setelah koreksi tersebut.
