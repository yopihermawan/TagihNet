# Laporan pengujian — 1 November 2026, WIB

Laporan awal di bawah bersifat historis. Hasil terbaru checkpoint pengakhiran Midtrans Sandbox pada **11 November 2026 WIB**: **316 tes pada SQLite dan 316 pada PostgreSQL**, tanpa fail/skip/expected-failure; rincian ada di bagian akhir.

Target: checkpoint billing, operasional, pengingat, PSB dan monitoring NAS simulasi TagihNet, Linux cloud, Python 3.12.14, Django 5.2.18, SQLite dan PostgreSQL 16.15 (psycopg 3.3.6), Chromium melalui Playwright 1.62.0. Data seluruhnya fiktif, dua tenant. Test database terpisah dari demo. Tidak ada transaksi gateway atau router nyata.

## Eksekusi dan bukti

| Perintah | Hasil yang diamati |
| --- | --- |
| `python manage.py migrate --noinput` | pass · skema auth/session dan core diterapkan dari checkout awal kosong |
| `python manage.py seed_demo` | pass · dua ISP, paket/pelanggan/faktur/pembayaran fiktif; idempotensi/rename/password diverifikasi melalui tes |
| `python manage.py check` | pass · no issues |
| `python manage.py makemigrations --check --dry-run` | pass · no changes detected |
| `python -m pip check` | pass · no broken requirements |
| `python manage.py test --verbosity 1` | pass · **245 tes dieksekusi pada SQLite dan 245 pada PostgreSQL**, tanpa fail/skip/expected-failure |
| `python manage.py collectstatic --noinput` | pass · tiga aset lokal terkumpul |
| `python scripts/browser_smoke.py` | pass · Chromium sungguhan: login/password toggle, tambah/reload pelanggan, faktur, pembayaran penuh, reversal, PDF, filter/URL kembali, logout, dark mode, menu mobile; **0 browser errors** |
| `python scripts/security_smoke.py` | pass · server/database sementara, MFA enrollment/challenge, custom role assignment/edit, session revocation, recovery regeneration/rejection, penerbitan 28 faktur massal dengan diskon, replay, worker resume/duplikat/cancel parsial/restore antrean, lalu seluruh alur billing; tanpa mengubah akun demo pengguna |
| `python scripts/operations_smoke.py` | pass · owner/teknisi/pelanggan, catatan internal tersembunyi, scope tugas, tiket sampai Ditutup, PSB sampai aktivasi/replay, satu customer/invoice/job, provisioning simulasi, edit/reschedule/reset Survei, opt-in pengingat, dedupe, pelunasan sebelum proses, monitoring NAS normal/gagal/stale/nol, NOC scope, impor CSV invalid/preview/MFA/atomic/replay, 4 viewport dan restore outbox/snapshot/impor |
| Browser responsive checks | pass terbatas · 360/768/1366/1920 px, dashboard/pelanggan/faktur/jaringan/setelan tanpa scroll horizontal seluruh halaman; tabel punya scroll lokal |
| `python scripts/verify_pdf.py` | pass · A4 **14 halaman**, 100 baris teks panjang, total Rp100.000, label simulasi, markup footer tampil sebagai teks; fixture di-rollback |
| `python scripts/verify_encrypted_backup.py` | pass · arsip AES-GCM, semua tabel cocok, wrong key/target lama ditolak, restored password+TOTP/CSRF dan replay OTP/recovery diverifikasi |
| `python scripts/verify_restore.py` | pass · backup konsisten dan restore ke direktori kosong, integrity_check ok; pelanggan/faktur/total/pembayaran/piutang, state batch/items/tiket/PSB/outbox, versi kebijakan/siklus SLA/jadwal PSB, konfigurasi dan payload snapshot NAS sama |
| `python manage.py run_simulation_worker --once` | simulated · batch pertama 18 confirmed dan satu router seeded gagal; pembayaran tetap tersimpan, kegagalan dijadwalkan backoff |
| `pip-audit 2.10.1 -r requirements.txt --no-deps --disable-pip --vulnerability-service pypi` | pass saat scan · no known vulnerabilities found untuk sebelas paket runtime; tidak mencakup OS, dev dependencies, atau bug logika |
| `manage.py check --deploy` dengan DEBUG off dan secret sesi/MFA/host sementara | warning konfigurasi · W021: HSTS preload false. Ini disengaja sampai domain/HTTPS siap, tidak ada klaim deployment HTTPS sudah diverifikasi |

Prefix `python` pada tabel berarti interpreter `.venv/bin/python` proyek. Pip-audit dijalankan dari venv terpisah `/tmp/tagihnet-audit`. Browser memakai Chromium `/usr/bin/chromium`. Bukti gambar tersedia di [UX-AUDIT](UX-AUDIT.md); PDF stress-test disimpan lokal di `data/evidence/` (diabaikan Git). Pengulangan browser menambah pelanggan fiktif berlabel `Uji Browser`; angka demo dapat bertambah. Restore drill menghitung ulang baseline sebelum backup, bukan mengandalkan jumlah seed yang tetap.

## Pemetaan terhadap kriteria brief

| No. | Kriteria | Status / batas bukti |
| --- | --- | --- |
| 1 | Auth/logout/reset/timeout/revocation | pass · rotasi session, POST logout, reset token invalid sesudah dipakai, sesi lama dicabut, idle/absolute expiry; MFA TOTP/recovery/step-up juga lulus; SMTP produksi belum |
| 2 | Endpoint langsung tanpa izin | pass · kasir/NOC/customer, tidak hanya menu tersembunyi |
| 3 | Batas tenant/pelanggan | pass subset · detail/edit/payment/search/export/PDF/portal/job serta preview/laporan impor privat; realtime belum tersedia |
| 4 | Search/filter/sort/pagination URL | pass · 10/25/50 backend dan size=25 browser; filter NAS/jatuh tempo belum |
| 5 | Tambah/ubah pelanggan + reload | pass · HTTP dan browser; duplikat phone/username ditolak |
| 6 | Retry/concurrency penerbitan | pass lokal · dua worker batch sama/bertumpang tindih/race dengan penerbitan satuan, resume/konfirmasi replay, constraint customer/periode; PostgreSQL dua proses terpisah lulus pada lab |
| 7 | Rupiah/tax/discount/akhir bulan | pass subset · integer, half-up, Februari/leap year/tanggal31; prorata belum tersedia |
| 8 | Webhook tidak sah/nominal/order | not tested · gateway/webhook belum diimplementasikan; path webhook tidak tersedia |
| 9 | Callback replay/out-of-order | pass subset simulated · pencatatan UUID persisten, mismatch ditolak, dua thread satu ledger; event lifecycle gateway belum |
| 10 | Pembayaran bersamaan isolir | simulated · dua urutan pembayaran/job, recheck utang saat eksekusi; race perangkat nyata belum |
| 11 | Router gagal tidak batalkan bayar | simulated · status failed dan pembayaran lunas bertahan; layanan tidak dipalsukan aktif |
| 12 | Markup tidak dieksekusi | pass lokal · web/detail dan PDF; portal memakai renderer sama dan otorisasi yang diuji; email faktur belum tersedia |
| 13 | Upload/impor/ekspor | pass subset · impor CSV dibatasi ukuran/format/baris/tenant/creator, preview/MFA/atomic; laporan/ekspor formula-safe; lampiran biner belum tersedia |
| 14 | Demo guard backend/worker | simulated · payment di luar SIMULATION ditolak, worker fail-closed; tidak ada adapter eksternal |
| 15 | Metrik jelas dan data gagal bukan nol | pass lokal · sumber/tenant/periode/update tersedia; snapshot sesi contoh membedakan nol/unknown/stale/gagal, data router nyata belum tersedia |
| 16 | UI/accessibility/kontras/fallback | pass subset · screenshots, 4 viewport, password/menu/Escape/return URL, empty/validation states; audit WCAG penuh dan screen reader belum |
| 17 | PDF A4/multipage/nominal/izin | pass · browser PDF, stress test 14 halaman, footer panjang, cross-tenant 404 |
| 18 | Restore & rekonsiliasi | pass lokal · SQLite/PG encrypted restore + tabel/saldo identik dan autentikasi MFA terpulihkan; offsite/RPO/RTO/PITR belum |

## Tidak dijalankan / gate rilis

Build/runtime image aplikasi Docker dan Windows PowerShell belum diuji. Container server PostgreSQL lab sudah diuji; itu tidak memverifikasi image aplikasi. GitHub Actions baru ditambahkan; keberhasilan lokal bukan bukti CI remote lulus. Belum ada uji TLS deployment, SMTP nyata, load test, stress multiworker, lab MikroTik/FreeRADIUS/CoA/FastTrack/IPv6, gateway sandbox, upload lampiran biner, migrasi data produksi, payroll, atau penetrasi aplikasi menyeluruh. Seluruh modul pending tercatat di FEATURE-MATRIX. Tidak ada persentase kepatuhan atau klaim production-ready.

## Kesalahan selama setup yang sudah diperbaiki

Bootstrap pertama gagal karena chmod pada file object dan kemudian migrasi core belum terbentuk. Perbaikan pembuatan secret dan migrasi diuji ulang hingga pass. Percobaan awal browser berhenti pada locator label `Paket` yang tidak memperhitungkan tanda wajib; locator diperbaiki dan alur penuh lulus. Verifikasi PDF pertama memakai interpreter global tanpa Django; dijalankan ulang dengan venv proyek dan dependensi dev terkunci, lalu lulus. Keberhasilan pada tabel merujuk eksekusi setelah koreksi tersebut.

## Tambahan checkpoint akses

29 tes baru dan penyesuaian fixture session MFA mempertahankan pemeriksaan billing terdahulu tanpa mematikan MFA/CSRF/rate limit. Test domain memakai session berstatus MFA sah; alur password/OTP sesungguhnya diverifikasi terpisah melalui HTTP dan browser. Race awal pada fixture pre-read membership diperbaiki dengan menyiapkan ID sebelum thread dimulai; assertion last-owner tetap dijalankan terhadap dua transaksi paralel. Percobaan browser awal mencapai batas percobaan ketika login berulang terlalu banyak; skenario disusun agar pemeriksaan kode lama dilakukan melalui step-up dan seluruh limiter tetap aktif. Pemindaian menemukan advisory cryptography 49.0.0, diperbaiki ke 50.0.0 sebelum retest akhir.

Migration 0002 dan backup/restore diuji dengan saldo/jumlah faktur tetap cocok. Kunci MFA tidak termasuk backup database; pemulihan TOTP membutuhkan kunci terpisah yang sesuai. Tidak ada enrollment/reset MFA pada akun demo pengguna selama pengujian browser terisolasi.

## Checkpoint faktur massal

29 pengujian baru menambah cakupan pratinjau tanpa efek finansial, stale/expiry, harga/snapshot/diskon, batas 500/filter, hak terpisah bulk_issue, privasi tenant/pembuat, MFA/CSRF/step-up-return, antrean/replay, rollback per baris, izin dicabut, resume/retry, pembatalan parsial, serta dua worker/race dengan faktur satuan. Total suite kini 95 tes lulus tanpa skip/fail. Peringatan pagination awal pada queryset annotate diperbaiki dengan urutan created_at/id eksplisit.

Browser memverifikasi 28 invoice baru senilai Rp6.310.000 dari pratinjau diskon Rp5.000 per pelanggan, worker satu baris lalu resume, replay konfirmasi/worker, 28 faktur periode lama dilewati, dan satu faktur tetap ada sesudah sisa batch dibatalkan. Empat viewport juga mencakup list/form/detail batch. Restore drill pada database sementara saat status processing membandingkan state batch/item selain ledger; bukti di BULK-BILLING.md dan UX-AUDIT.md. Dependensi tidak berubah dari checkpoint akses; hasil pip-audit pada tabel adalah hasil pemindaian checkpoint akses, bukan pemindaian baru untuk kode billing ini.

## Checkpoint tiket dan PSB

34 tes operasional baru menambah cakupan SLA snapshot/prioritas/reopen, komentar publik/internal dan plaintext, scope tenant/customer/teknisi, reassignment (termasuk race HTTP), versi/replay/status, jadwal/penugasan, cancel, duplicate identitas, signed quote/expiry/MFA, rollback atomik dan dua aktivasi bersamaan. Total terakhir: 129 tes lulus tanpa skip/fail.

Browser operations_smoke.py pada database sementara membuktikan alur owner membuat/menugaskan tiket → teknisi menangani → pelanggan menulis komentar tanpa membaca catatan internal → resolusi/penutupan; lalu PSB survei/jadwal/pengerjaan/terpasang → owner quote bertanda tangan/MFA → tepat satu pelanggan/invoice/job meski replay → provisioning simulasi. Empat viewport mencakup list/form/detail/SLA. Restore membandingkan state operasional dan ledger. Foto tidak menyimpan faktor MFA. Akun/data demo pengguna tidak ditambah tiket/PSB/enrollment dari pengujian ini.

Pada checkpoint operasional sebelumnya, edit/rescheduling dan seluruh notifikasi masih pending. Edit/rescheduling serta outbox pengingat simulasi kini tersedia; lampiran, kanal notifikasi/eskalasi nyata, scope wilayah geografis, kalender jam kerja dan PostgreSQL multi-process tetap belum tersedia/diuji. CI baru didaftarkan untuk browser operasional; hasil lokal tidak membuktikan CI remote lulus. Dependensi runtime tidak berubah dari checkpoint akses.

## Checkpoint pengingat dan jadwal ulang PSB

33 tes baru, total **162**, lulus tanpa fail/skip/expected-failure. Cakupan mencakup edit normalisasi/duplikat/paket lintas ISP, reset Terjadwal ke Survei, before/after, versi/replay dan dua reschedule bersamaan. Pengingat diuji default-off/GET tanpa efek, izin/dependensi role kustom, CSRF/review/MFA, batas 0–14 hari dan 0–23 WIB, dedupe invoice/hari dan SLA/deadline/siklus, pembayaran parsial/lunas terbaru, resolusi/reprioritas/reopen, persetujuan berubah/ulang, retry/max3, plaintext dan penolakan mode nyata. Dua planner/worker bersamaan menghasilkan satu receipt dengan satu percobaan. Rotasi >10 kebijakan dan penundaan jam billing tanpa menghambat antrean juga diuji.

Browser operations_smoke.py memakai database/server sementara: edit prospek → jadwal ulang beralasan → perubahan alamat kembali Survei → penjadwalan baru → aktivasi. Kebijakan pengingat disetujui melalui UI; satu faktur dilunasi melalui UI setelah perencanaan dan sebelum worker, menghasilkan skipped/0 attempts. SLA overdue fiktif menghasilkan satu receipt simulated/1 attempt; pengulangan worker tidak menggandakan antrean. Teknisi/pelanggan ditolak dari receipt. Fixture SLA hanya diubah pada database sementara. Empat viewport memverifikasi list/detail/kebijakan; tabel mobile memakai scroll lokal dan tinggi baris terbatas.

Migrasi 0005 telah diterapkan dengan backup sebelum migrasi. PDF 14 halaman, restore drill, model drift check, system check, pip check dan collectstatic lulus. Dependensi tidak berubah; audit runtime pada tabel tetap hasil checkpoint akses. Database pengguna tetap 33 pelanggan/40 faktur tanpa kebijakan/outbox/tiket/PSB/perangkat MFA uji. Browser security_smoke.py dijalankan ulang untuk regresi MFA/akses/billing massal. Hasil lokal tidak membuktikan CI remote atau kesiapan produksi.

## Checkpoint monitoring jaringan simulasi

25 tes baru, **187 total**, lulus tanpa fail/skip/expected-failure. Cakupan: default-off/GET tanpa efek; snapshot normal/empty/failure/stale/expiry 120 detik; kegagalan menjaga histori dan tidak menjadi nol; ringkasan hanya NAS segar ISP sendiri dan cakupan parsial; data konfigurasi lama dikecualikan; dedupe/restart/dua collector bersamaan; dua edit konfigurasi bersamaan; status layanan/ledger tidak berubah; MFA/session/version/CSRF/review; role kustom/dependensi/revocation; direct endpoint lintas tenant/NOC/role lain; plaintext; batas perangkat/session/command dan penolakan SIMULATION=0. Snapshot malformed, duplikat username, durasi salah dan timestamp masa depan ditolak.

Browser operations_smoke.py kini juga menguji Owner membuat NAS opt-in → snapshot normal → pengulangan tanpa duplikasi → gagal sambil mempertahankan histori → kedaluwarsa → nol yang berasal dari snapshot berhasil. Ringkasan unknown vs nol diperiksa. NOC dapat membaca tanpa konfigurasi atau nominal finansial. List/detail/form diuji 360/768/1366/1920 tanpa overflow seluruh halaman; empat screenshot baru tidak menyimpan faktor MFA. Restore membandingkan konfigurasi dan payload snapshot. Semua memakai server/database sementara. security_smoke.py dijalankan ulang dan lulus untuk MFA/akses/billing massal serta billing browser.

Satu assertion regresi awal mendeteksi teks peringatan data router yang berubah. Pesan dashboard disesuaikan agar tetap menyatakan data sesi router nyata belum tersedia, lalu seluruh 187 tes lulus; assertion lama dipertahankan. Migrasi 0006 diuji pada restore salinan sebelum database lokal; jumlah dan total ledger cocok. System/model drift/pip check, collectstatic, PDF 14 halaman dan restore drill lulus. Collector kosong lokal tidak membuat NAS/snapshot. Data pengguna tetap 33 pelanggan/40 faktur dan tanpa NAS, poll atau perangkat MFA uji. Dependensi runtime tidak berubah, audit package pada tabel tetap hasil checkpoint akses. Polling perangkat nyata, trafik, PostgreSQL multi-process, retensi dan CI remote belum diverifikasi.

## Checkpoint PostgreSQL dan transaksi lintas proses

195 tes lulus pada SQLite dan 195 pada PostgreSQL 16.15, tanpa fail/skip/expected-failure. Delapan tes konfigurasi baru memeriksa default SQLite, pilihan eksplisit PG, field/port/nama/password wajib, file password, tanpa fallback, TLS verify-full dan larangan disable di DEBUG=0. Model drift/system/pip check lulus, migrasi 0001–0006 dari kosong di PG berhasil. Role aplikasi lab non-superuser dan memiliki CREATEDB khusus tes.

verify_postgres.py menjalankan delapan skenario dua proses Python berbeda pada database baru: faktur bulanan sama; payment replay; UUID yang sama pada dua faktur/pelanggan; worker bulk melawan penerbitan satuan dengan overlap lock yang dipaksakan; pembayaran melawan job jaringan dan rekonsiliasi; worker reminder ganda; aktivasi PSB ganda; deadlock buatan yang menguji retry serta rollback. Skenario bulk setelah perbaikan memeriksa counter deadlock PostgreSQL tetap nol; fixture deadlock terakhir sengaja membuat satu deadlock, berhasil dengan satu retry, dan tepat dua audit yang committed. Total ini terpisah dari 195 tes, bukan 195 tambahan.

Temuan awal: UUID payment pada dua invoice dapat menghasilkan IntegrityError 23505; diganti get_or_create dengan savepoint dan pemeriksaan payload pemenang, menghasilkan satu diterima/satu ValidationError. Penguncian joined actor/tenant pada worker bulk menghasilkan satu deadlock terukur; lock dibatasi ke self dan pengunci konfigurasi tenant menggunakan NO KEY UPDATE. Retry memakai SQLSTATE, bukan pencocokan substring error. Retest proses dan kedua suite lulus.

Verifier membuat pg_dump format custom dengan mode 0600, restore ke database kosong kedua, lalu membandingkan jumlah/hash payload seluruh tabel aplikasi/auth (termasuk through table) sebelum menghapus kedua database. Ini uji backup logis lokal, bukan enkripsi/offsite/PITR/failover. Volume lab dipertahankan saat container terhenti dihapus/dibuat ulang; startup dan migrate --check lulus. Tidak membuktikan snapshot cloud memulihkan volume Docker.

Kedua browser smoke lulus, termasuk saat dipanggil dari launcher PostgreSQL: skrip secara eksplisit memakai SQLite sementara. Pengujian browser bukan Chromium terhadap PostgreSQL; tes HTTP/domain pada PG termasuk dalam 195 tes. PDF/restore SQLite tetap diuji terpisah. Data demo pengguna tetap 33 pelanggan/40 faktur, tanpa fixture/MFA baru dan backend aktif tetap SQLite. Dependensi opsional psycopg/psycopg-binary 3.3.6 serta typing_extensions 4.16.0 dikunci. pip-audit terhadap requirements-postgres.txt (14 package Python termasuk base) tidak menemukan advisory saat scan; tidak mencakup OS atau library native yang dibundel. Job CI PostgreSQL didaftarkan; hasil remote belum diverifikasi.

## Checkpoint impor pelanggan — 1 November 2026 WIB

30 tes baru, **225 tes pada SQLite dan 225 pada PostgreSQL**, lulus tanpa fail/skip/expected-failure. Cakupan: UTF-8/BOM/header reorder/quoted multiline, batas 128 KiB/200 record/kolom, reject encoding/control/header/extension/UUID, form field validation, plan tenant, duplikat file/database/case variant, normalisasi, preview tanpa efek, atomic pending create/replay, rollback saat konflik, fingerprint tamper, harga/data/izin berubah, expiry/MFA/sesi, cancellation/quota, creator+tenant scope termasuk report dan direct POST, CSRF/review, formula-safe CSV/plaintext, summary pagination dan tiga concurrency test. Index username case-insensitive diuji pada direct DB insert.

Browser operations_smoke.py mengunggah CSV invalid (konfirmasi tidak tersedia), lalu CSV dua pelanggan valid; sebelum konfirmasi jumlah pelanggan tetap, sesudah MFA/review tepat dua customer pending ditambah. Replay tetap dua; invoice/job/membership tidak bertambah. Teknisi/pelanggan ditolak dari batch. Upload/list/detail diperiksa pada empat viewport dan bukti baru customer-import-*.png. Kolom hasil sticky mobile dipersempit setelah inspeksi visual dan retest browser lulus. security_smoke.py juga lulus untuk regresi MFA/akses/billing.

PostgreSQL verify_postgres.py menambah dua kasus proses terpisah: dua konfirmasi batch sama menghasilkan satu impor; dua preview bertumpang tindih menghasilkan completed/stale dan satu pelanggan. Total sepuluh skenario proses, ditambah pg_dump/restore yang merekonsiliasi seluruh tabel aplikasi/auth, termasuk batch/row impor. Chromium tetap memakai SQLite sementara; PG HTTP/domain diuji pada backend suite. Tidak ada klaim CI remote selesai.

Migrasi 0007 diuji pada salinan restore dengan probe benturan username: penolakan mempertahankan semua customer dan tidak mencatat migrasi selesai. Setelah probe dibuang pada salinan, migrasi/rekonsiliasi berhasil. Penerapan lokal setelah backup mempertahankan ledger/jumlah/MFA. System/model drift/pip check, static collection, PDF A4 14 halaman dan restore SQLite lulus. Data demo pengguna tidak diisi import/MFA uji. Dependensi tidak berubah dari checkpoint PG; audit 14 package yang dicatat sebelumnya bukan scan baru tanggal ini. Retensi otomatis, impor update/upsert, file spreadsheet/biner dan cutover produksi tetap belum tersedia.

## Checkpoint backup terenkripsi — 1 November 2026 WIB

20 tes baru, **245 pada SQLite dan 245 pada PostgreSQL**, lulus tanpa fail/skip/expected-failure. Cakupan: keygen private/random/no-clobber, pemisahan format kunci MFA, symlink/public/malformed/wrong-length key rejection; multi-chunk AES-GCM roundtrip/nonce unik; wrong key (termasuk ID cocok/material salah), cipher/tag/header tamper, versi/algoritma/tipe/header malformed/duplicate JSON/truncation/limit; publikasi interrupted/parallel/existing/broken symlink; SQLite WAL snapshot, integrity/format/backend checks, no plaintext publication on auth failure, sidecar rejection dan CLI redaction/exit statuses.

verify_encrypted_backup.py membuat seluruh fixture/keys/source/restore sementara. Fingerprint semua tabel identik setelah restore; wrong backup key dan target terisi ditolak. Kunci MFA salah gagal mendekripsi terpisah. Setelah kunci sesi/MFA source disediakan terpisah, password+TOTP login dengan CSRF berhasil; OTP dan recovery yang telah digunakan ditolak, saldo Rp149.750 tetap cocok. Fixture source tidak berubah dan seluruh temporary data dibersihkan. Tidak enroll/reset akun pengguna.

Sepuluh race proses PG tetap lulus. Verifier menambah source MFA encrypted payload, encrypted pg_dump/verify/restore dengan guard endpoint container, pemilik/target kosong dan target berbeda. Wrong key/source/occupied target ditolak; seluruh tabel/hash/MFA plaintext hasil decrypt sesuai setelah restore. Authenticated envelope dengan payload PGDMP malformed menghasilkan pg_restore nonzero dan target tetap tanpa objek publik. Database sumber/original tidak diisi fixture; semua DB sementara dihapus. Single-transaction restore bukan benchmark atau bukti failover/PITR.

Kedua browser regresi tetap lulus, termasuk restore kompatibilitas SQLite. PDF14 halaman, system/model drift/pip checks lulus. Satu backup demo terenkripsi dipertahankan lokal, diverifikasi dan dipulihkan ke direktori sementara; counts/ledger cocok dan source tetap 33 pelanggan/40 faktur tanpa fixture MFA/impor baru. Kunci/arsip mode0600, diabaikan Git; kunci development satu workspace bukan vault. Arsip plaintext lama tetap ada. Tidak ada schema/dependency baru; audit package tetap hasil checkpoint PG. Job backend CI menambah encrypted/MFA drill; remote CI belum diverifikasi. Offsite/retensi/PITR/RPO/RTO/secure erase belum diuji.

## Checkpoint Midtrans Sandbox — 4 November 2026 WIB

41 tes baru, **286 tes pada SQLite dan 286 pada PostgreSQL 16.15**, lulus tanpa fail/skip/expected-failure. Cakupan meliputi nominal/scope server, saldo parsial/lunas, opt-in, kredensial sandbox per-tenant, merchant reuse/switch, MFA/CSRF/peran/portal, GET tanpa efek, signature palsu, callback signed tetapi status API pending, merchant/order/currency/amount/transaction ID mismatch, capture/fraud, replay/out-of-order, refund/overpay review, larangan reversal manual gateway, timeout dengan order ID yang sama, disable yang tetap menerima callback, router gagal, parsing JSON/limit/malformed response, host redirect dan transport Basic Auth/3DS sandbox.

`verify_postgres.py` lulus **12 skenario dua proses**, termasuk dua skenario gateway baru: checkout bersamaan menghasilkan satu UUID order; settlement bersamaan menghasilkan satu payment/event/network job. pg_dump/pg_restore dan encrypted restore membandingkan seluruh tabel, kini termasuk konfigurasi/order/event gateway. Seluruh database verifikasi sementara dihapus; database demo SQLite tidak dipindahkan.

`gateway_smoke.py` lulus dengan Chromium/HTTP sungguhan, database/server sementara dan transport Midtrans tiruan: login + OTP, owner mengaktifkan gateway melalui UI, checkout pending, status refresh, penolakan signature palsu, settlement + replay callback, faktur lunas dan tidak ada reversal manual gateway. Empat viewport 360/768/1366/1920 tanpa overflow horizontal seluruh halaman; **0 browser errors**. Pending dan settlement menjadi dua event, satu payment dan satu job. Screenshot `gateway-mobile.png` dan `gateway-desktop.png` diperiksa; tidak memuat secret/OTP. Assertion awal smoke salah mengharapkan satu event meski pending dan settlement berbeda; diperbaiki agar memeriksa dua status tersebut secara eksplisit, kemudian smoke lulus.

`verify_encrypted_backup.py` lulus untuk pemulihan konfigurasi/order settled + pembayaran gateway dan reservasi order lain yang belum dibayar, fingerprint seluruh tabel, saldo ledger, password+TOTP/CSRF serta replay OTP/recovery. System check, migration drift check, pip check dan diff whitespace check lulus. Instalasi runtime/dev/PostgreSQL terpin juga diuji pada `.venv` workspace. Tidak ada dependensi runtime baru.

Kontrak client dicek terhadap SDK resmi Midtrans/midtrans-python-client dan artefak midtransclient 1.4.2. Kredensial sandbox asli dan URL HTTPS publik belum tersedia; tidak ada request transaksi ke merchant, uji metode pembayaran sandbox nyata, pengiriman webhook publik, refund provider, atau rilis produksi. GitHub Actions ditambah dengan gateway_smoke; keberhasilan CI remote belum dikonfirmasi. Panduan lengkap: PAYMENT-GATEWAY.md.

## Checkpoint rekonsiliasi gateway — 11 November 2026 WIB

Delapan tes tambahan, **294 tes pada SQLite dan 294 pada PostgreSQL 16.15**, lulus tanpa fail/skip/expected-failure. Dua regresi finansial direproduksi sebelum perbaikan: refund/partial-refund/chargeback/partial-chargeback yang lebih dahulu terverifikasi disusul sukses lama dapat mencatat payment; settlement yang tertahan oleh saldo tidak pulih sesudah koreksi karena receipt dideduplikasi terlalu awal. Sesudah perbaikan, seluruh status sengketa menahan kredit sukses terlambat; saldo yang dikoreksi dapat menerima settlement terverifikasi tepat sekali, dengan receipt review asli tetap tersimpan. Kasus diperiksa melalui API status tiruan dan transaksi database nyata.

Enam tes tambahan mencakup batas tenant dan pelanggan pada daftar/ringkasan order, GET tanpa network call, URL checkout/key tidak bocor melalui daftar, pengguna anonim/NOC/method, pencarian ID dan status, ukuran halaman dan output escaping, filter tenant CLI, validasi argumen dan deteksi refund saat `--include-settled`. Scope pelanggan satu ISP tetap diuji terpisah dari lintas ISP.

`verify_postgres.py` lulus **14 skenario dua proses**, termasuk pemulihan settlement bersamaan setelah reversal koreksi: satu payment/event/network job baru, receipt awal outcome review tidak diubah. Skenario kedua memaksa refund commit lebih dahulu sebelum respons settlement lama diterapkan di proses lain: status tetap review, tanpa payment/job. Dump/restore logis dan terenkripsi membandingkan seluruh tabel, termasuk order yang pulih dan order sengketa. Database verifikasi sementara dibersihkan.

`gateway_smoke.py` lulus dengan Chromium dan HTTP nyata serta transport Midtrans tiruan: login/MFA/opt-in, checkout, pencarian/filter pending/review, callback palsu/replay, settlement, refund, dan sukses terlambat yang tetap review. List/detail/setelan/faktur diperiksa pada 360/768/1366/1920 px tanpa overflow horizontal seluruh halaman; tabel memiliki gulir lokal; **0 browser errors**. Screenshot riwayat gateway mobile/desktop diperiksa. Satu payment, tiga status verifikasi (pending/settlement/refund), satu job, dan saldo tetap konsisten.

Encrypted SQLite restore, system check, migration drift, pip check dan diff whitespace check lulus. Tidak ada migrasi skema atau dependensi baru. Kredensial sandbox privat belum ada pada process dan domain Midtrans masih baru berada dalam draft, belum di kebijakan runtime; transaksi merchant dan webhook HTTPS publik belum diuji. Domain publik belum ditentukan. Bukti lokal bukan bukti akun Midtrans atau CI remote siap.

## Checkpoint pengakhiran pembayaran sandbox — 11 November 2026 WIB

22 tes tambahan, **316 tes pada SQLite dan 316 pada PostgreSQL 16.15**, lulus tanpa fail/skip/expected-failure. Cakupan: owner/tenant/MFA/session/CSRF/POST/review/reason, intent UUID persisten, replay/alasan berbeda, timeout sebelum/sesudah write, respons POST tanpa konfirmasi GET, unknown/404, mismatch nominal, pencabutan sesi setelah preflight, gateway default-off/live-mode, pembayaran yang menang race, refund, GET tanpa efek, alasan ter-escape, retry dengan alasan/UUID awal serta endpoint POST expire sandbox tetap.

Suite penuh SQLite pertama menemukan audit attempt di luar transaksi retry dapat gagal dengan database table is locked ketika dua request berjalan bersamaan. Audit dipindahkan ke transaksi persiapan intent; relasi order di-cache sebelum keluar transaksi dan pembacaan otorisasi dapat diulang saat lock. Tes fault injection menimbulkan lock pada audit, memeriksa rollback/retry menghasilkan satu intent/audit request dan satu network write. Suite penuh kedua lulus 316/316. Tidak ada HTTP di dalam transaksi yang dapat diulang.

`verify_postgres.py` lulus **16 skenario dua proses**. Tambahan expiry: dua proses dengan UUID permintaan sama menghasilkan satu intent confirmed tanpa payment/job; skenario terpisah memaksa payment commit setelah preflight namun sebelum expire selesai, menghasilkan satu payment/job, saldo lunas dan expiry blocked. Restore logis/terenkripsi PostgreSQL membandingkan seluruh tabel termasuk intent confirmed/blocked. Database verifikasi sementara dibersihkan.

`gateway_smoke.py` lulus dengan Chromium/HTTP sungguhan, transport Midtrans tiruan dan data sementara: dua faktur, transaksi pertama settlement/refund, checkout kedua diakhiri lewat form alasan/review owner, status expire terverifikasi, replay dengan CSRF tidak menambah intent, lalu checkout pengganti memakai order baru. Empat viewport 360/768/1366/1920, tanpa overflow horizontal seluruh halaman dan 0 browser errors. Satu payment/job untuk faktur pertama, tiga order, lima status verifikasi dan satu expiry confirmed; saldo faktur kedua tetap Rp249.750. Tangkapan layar memakai animasi disabled agar viewport resize tidak merekam sidebar saat transisi.

Encrypted SQLite restore drill kini mencakup permintaan expiry confirmed selain payment settled dan reservasi lain pending; seluruh tabel dan MFA login/replay cocok. Backup AES-256-GCM database development dibuat dan verify lulus sebelum migrasi 0009 diterapkan. System check, model drift, pip check serta diff whitespace check lulus. Tidak ada dependensi runtime baru. Akun merchant sandbox asli, notifikasi HTTPS publik dan produksi tetap belum diuji karena kredensial/domain belum tersedia pada runtime. Bukti transport tiruan tidak membuktikan layanan Midtrans siap.
