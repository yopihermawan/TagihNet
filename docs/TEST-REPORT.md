# Laporan pengujian — 19 November 2026, WIB

Target histori awal: backup/SQL recovery staging, staging lokal HTTPS/PostgreSQL TLS, jadwal penagihan bulanan, kuitansi pembayaran, foto pekerjaan privat, laporan penagihan historis, workspace pelanggan bertab, NAS/filter jatuh tempo pelanggan, paket/harga, billing, operasional, pengingat, PSB, monitoring NAS simulasi dan backup terenkripsi TagihNet, Linux cloud, Python 3.12.14, Django 5.2.18, SQLite dan PostgreSQL 16.15 (psycopg 3.3.6), Chromium melalui Playwright 1.62.0. Data seluruhnya fiktif, dua tenant. Test database terpisah dari demo. Tidak ada transaksi gateway atau router nyata.

Checkpoint terbaru adalah [navigasi/drawer workspace](WORKSPACE-UX.md): **781 tes per backend**, ditambah verifikasi browser, proses PostgreSQL, migrasi dan pemulihan yang dijelaskan pada bagian terakhir. Tabel awal berikut mempertahankan histori checkpoint 15 November.

## Eksekusi dan bukti historis

| Perintah | Hasil yang diamati |
| --- | --- |
| `python manage.py migrate --noinput` | pass · skema auth/session dan core diterapkan dari checkout awal kosong |
| `python manage.py seed_demo` | pass · dua ISP, paket/pelanggan/faktur/pembayaran fiktif; idempotensi/rename/password diverifikasi melalui tes |
| `python manage.py check` | pass · no issues |
| `python manage.py makemigrations --check --dry-run` | pass · no changes detected |
| `python -m pip check` | pass · no broken requirements |
| `python manage.py test --verbosity 1` | pass · **471 tes dieksekusi pada SQLite dan 471 pada PostgreSQL**, tanpa fail/skip/expected-failure |
| `python manage.py collectstatic --noinput` | pass · tiga aset lokal terkumpul |
| `python scripts/browser_smoke.py` | pass · Chromium sungguhan: login/password toggle, tambah/reload pelanggan, faktur, pembayaran penuh, reversal, PDF, filter/URL kembali, logout, dark mode, menu mobile; **0 browser errors** |
| `python scripts/security_smoke.py` (checkpoint scheduler; tidak diulang pada staging) | pass · server/database sementara, MFA enrollment/challenge, custom role assignment/edit, session revocation, recovery regeneration/rejection, penerbitan 28 faktur massal dengan diskon, replay, worker resume/duplikat/cancel parsial/restore antrean, lalu seluruh alur billing; tanpa mengubah akun demo pengguna |
| `python scripts/operations_smoke.py` (checkpoint scheduler; tidak diulang pada staging) | pass · jadwal/review/MFA/worker/restart/dedupe/nonaktif/restore, kuitansi/partial/terbilang/PDF/scope portal/reversal, foto privat/internal/portal/PSB/unduhan/backup binary, laporan/roll-forward/SQL sumber/CSV/scope, 7 tab pelanggan/izin/ledger/tiket/tautan tanpa JavaScript/kembali URL+scroll400px, pemetaan NAS, filter/piutang/ekspor/kembali daftar/pelunasan/reversal, paket buat/tinjau/konfirmasi/replay/edit harga/ledger tetap, pencarian/scope; owner/teknisi/pelanggan, catatan internal tersembunyi, scope tugas, tiket sampai Ditutup, PSB sampai aktivasi/replay, satu customer/invoice/job, provisioning simulasi, edit/reschedule/reset Survei, opt-in pengingat, dedupe, pelunasan sebelum proses, monitoring NAS normal/gagal/stale/nol, NOC scope, impor CSV invalid/preview/MFA/atomic/replay, 4 viewport dan restore outbox/snapshot/impor |
| `python scripts/verify_postgres.py` | pass · 17 skenario lintas proses termasuk scheduler/worker jadwal serentak, snapshot laporan dan unggahan/kuota foto serentak, dump/restore biasa dan terenkripsi, seluruh tabel/ledger/MFA/paket cocok |
| `python scripts/verify_staging_backup.py` | pass · seluruh TLS/runtime checks ditambah encrypted backup/SQL restore/fingerprint tabel+sequence, wrong keys/tamper/invalid/occupied/hold/MFA/session/photo/source preserved |
| `python scripts/verify_staging.py` (disertakan dalam verifier recovery) | pass · container aktual/verified TLS/static/host/bootstrap/MFA/CSRF/cookie/SQL privilege/CA/hostname/non-root/read-only/volume guard/worker crash/web restart/log secrecy |
| Browser responsive checks | pass terbatas · 360/768/1366/1920 px, dashboard/pelanggan/faktur/jaringan/setelan tanpa scroll horizontal seluruh halaman; tabel punya scroll lokal |
| `python scripts/verify_receipt.py` (checkpoint kuitansi; tidak diulang pada scheduler) | pass · A4, normal satu halaman/reversal empat halaman, nominal/terbilang/WIB/marker akhir/footer, font lokal tertanam; fixture tanpa database |
| `python scripts/verify_pdf.py` (checkpoint terdahulu; tidak diulang pada kuitansi) | pass · A4 **14 halaman**, 100 baris teks panjang, total Rp100.000, label simulasi, markup footer tampil sebagai teks; fixture di-rollback |
| `python scripts/verify_encrypted_backup.py` (checkpoint scheduler; tidak diulang pada staging) | pass · arsip AES-GCM, semua tabel cocok, wrong key/target lama ditolak, restored password+TOTP/CSRF dan replay OTP/recovery diverifikasi |
| `python scripts/verify_restore.py` | pass · backup konsisten dan restore ke direktori kosong, integrity_check ok; pelanggan/faktur/total/pembayaran/piutang, state batch/items/tiket/PSB/outbox, versi kebijakan/siklus SLA/jadwal PSB, konfigurasi dan payload snapshot NAS sama |
| `python manage.py run_simulation_worker --once` | simulated · batch pertama 18 confirmed dan satu router seeded gagal; pembayaran tetap tersimpan, kegagalan dijadwalkan backoff |
| `pip-audit 2.10.1 -r requirements.txt --no-deps --disable-pip --vulnerability-service pypi` (checkpoint awal; tidak diulang 14 November) | pass saat scan · no known vulnerabilities found untuk sebelas paket runtime; tidak mencakup OS, dev dependencies, atau bug logika |
| `manage.py check --deploy` dengan DEBUG off dan secret sesi/MFA/host sementara (checkpoint awal; tidak diulang 14 November) | warning konfigurasi · W021: HSTS preload false. Ini disengaja sampai domain/HTTPS siap, tidak ada klaim deployment HTTPS sudah diverifikasi |

Prefix `python` berarti `.venv/bin/python` proyek. Instalasi saat ini memakai `.venv` di checkout dan Chromium `/usr/bin/chromium`. Smoke security/operations membuat database/server sementara, lalu membersihkannya; demo pengguna tidak memperoleh record uji atau pendaftaran MFA. Bukti terbaru paket ada di [PLANS](PLANS.md); gambar modul sebelumnya dipertahankan sebagai bukti checkpoint masing-masing. Bagian checkpoint bertanggal di bawah merupakan histori pengujian; data privat instance lama tidak diasumsikan tersedia di mesin baru.

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


## Checkpoint paket/harga dan instance baru — 14 November 2026 WIB

27 tes tambahan; **272 tes SQLite dan 272 PostgreSQL**, tanpa fail/skip/expected-failure. Cakupan: GET/pratinjau tanpa efek, validasi domain, nama duplikat, idempotensi paket baru, replay setelah edit tanpa rollback harga, versi/no-op, perubahan di luar UI, token tamper/expiry/account/tenant/access binding, izin preset/custom/revocation, MFA kedaluwarsa/masa depan, CSRF/POST/checkbox, audit atomik sebelum/sesudah, encoding teks, snapshot faktur lama/baru, batch stale versus batch disetujui, serta pratinjau impor/aktivasi PSB stale. Semua hasil melalui database uji terpisah.

`verify_postgres.py` lulus **12 skenario lintas proses**: sepuluh sebelumnya ditambah konfirmasi paket baru yang sama (satu paket/audit) dan dua edit dari versi sama (satu berhasil/satu stale). Dump/restore biasa dan terenkripsi membandingkan seluruh tabel aplikasi/akun termasuk versi/creation UUID paket serta ciphertext MFA. Database uji dibuang, base lab tidak diisi fixture.

Browser `operations_smoke.py` lulus untuk paket dan seluruh regresi operasional, 360/768/1366/1920 px, tanpa galat JavaScript. Tangkapan mobile menggunakan reduced motion dan memastikan sidebar tersembunyi setelah resize; pemeriksaan awal menemukan tangkapan saat transisi navigasi, lalu diperbaiki pada skrip bukti. Tiga bukti baru `plans-*` hanya memuat data fiktif. Skrip security juga lulus untuk MFA/akses/billing/massal dan empat viewport. Keduanya memakai server/database SQLite sementara; bukan uji Chromium pada PostgreSQL. Paket/faktor demo pengguna tidak diubah oleh tes.

Instance baru semula hanya berisi metadata Git dari snapshot awal. Kode dipulihkan dari commit backup terakhir; database, kunci dan arsip privat sesi 1 November tidak tersedia di sini. Demo **dibuat baru**, bukan dipulihkan dari data lama: 30 pelanggan, 37 faktur, 19 pembayaran, 0 reversal dan 0 perangkat MFA. Tidak ada impor/paket uji di database demo. Worker jaringan memproses 19 antrean seed: 18 terkonfirmasi simulasi, satu gagal setelah tiga percobaan; ini bukan koneksi router nyata. Tiga worker dan web dimulai, `/health/` status ok/simulation, login 200, halaman paket anonim menuju login.

Migrasi0008 diuji terlebih dahulu pada salinan backup SQLite, lalu diterapkan pada demo dan base lab PG. Jumlah pelanggan/faktur/pembayaran/reversal/MFA serta nama/kecepatan/harga paket lama cocok; versi awal1/creation UUID null. Backup sebelum migrasi disimpan privat lokal. Arsip development baru `data/backups/encrypted/TagihNet-20261114-ready.tnbk` dan kunci independen `data/backup-keys/cloud-development-20261114.json` dibuat0600, diabaikan Git, dibaca ulang/di-restore ke direktori sementara; seluruh payload app/auth cocok. Perbandingan pertama saat worker masih retry menunjukkan perbedaan metadata antrean/audit sesudah snapshot; arsip terbaru diverifikasi setelah antrean selesai. Kunci baru tidak menggantikan kunci backup lama yang tidak tersedia, dan penyimpanan satu workspace bukan offsite/vault.

PDF14 halaman, restore kompatibilitas SQLite (kini juga membandingkan paket/versi/UUID), encrypted SQLite/MFA drill, system/model-drift/static/pip checks lulus. Dependensi tidak berubah; tidak mengklaim scan advisory baru pada tanggal ini. CI mencakup tes tambahan lewat runner yang sudah ada; hasil remote CI belum diperiksa. Pengarsipan/jadwal harga/profil router nyata dan modul brief tersisa belum diimplementasikan. Save draft bukan Publish; restorasi snapshot baru ke task berikutnya belum diverifikasi.


## Checkpoint NAS pelanggan/filter jatuh tempo — 14 November 2026 WIB

28 tes tambahan; **300 tes SQLite dan300 PostgreSQL**, tanpa fail/skip/expected-failure. Perhitungan piutang/faktur bersisa menggunakan subquery agregat pembayaran sah per faktur, tanpa fanout beberapa faktur/pembayaran. Filter overdue/today/next7/unpaid/clear memakai tanggal WIB dan tidak disamakan dengan status layanan. Batas tanggal0/1/7/8 serta17:00UTC, pelunasan/reversal, beberapa faktur, NAS disabled/null/tenant asing/invalid, query-count konstan pada ukuran halaman10/50, filter gabungan/URL/pagination/CSV, CSRF/role, audit dan teks aman diuji. Lihat [CUSTOMER-FILTERS](CUSTOMER-FILTERS.md).

Migrasi0009 diuji pada salinan restore arsip AES-GCM sebelum diterapkan ke demo dan base lab PostgreSQL. Semua payload app/auth lama cocok setelah meniadakan kolom baru pada pembandingan; seluruh nas_id awal NULL. Demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS, tanpa pemetaan atau fixture tes persisten. Empat proses pengembangan sebelumnya dihentikan sebelum perubahan schema, lalu web/tiga worker dimulai kembali. Tidak ada dependency, secret atau tujuan jaringan baru.

12 skenario transaksi PostgreSQL tetap lulus. Fingerprint dump/restore biasa/terenkripsi kini mencakup satu pemetaan NAS terisi yang dibuat melalui CustomerForm. Seluruh tabel cocok dan database uji dibuang. Restore SQLite juga menambah pemeriksaan id/tenant/nas_id; operasi browser menjalankannya sesudah pemetaan terisi. Arsip development schema0009 baru `data/backups/encrypted/TagihNet-20261114-customer-filters.tnbk`, memakai kunci development independen yang sudah ada, dibaca ulang dan di-restore ke direktori sementara; seluruh payload app/auth cocok dan source tetap. Arsip lama tidak ditimpa.

Browser security/billing/MFA dan operasi lulus. Alur baru: simpan NAS tanpa tambahan faktur/pembayaran/job/poll, filter NAS+overdue+pencarian, ekspor CSV, kembali detail ke URL lengkap, pelunasan menghilangkan hasil overdue, reversal memunculkannya lagi, role scope serta360/768/1366/1920px. ID paket lama yang sangat panjang juga dibatasi sebelum ORM agar tidak menimbulkan galat konversi atau memperluas hasil CSV. Kolom identitas diperlebar minimum agar nama tidak terpecah per huruf pada mobile; scroll tetap lokal dan tinggi baris hasil diuji<170px. Bukti desktop/mobile baru hanya data fiktif; tidak ada faktor MFA. Browser memakai SQLite terisolasi, bukan Chromium pada PostgreSQL. Dua pemilihan elemen awal pada skrip diperjelas untuk teks NAS beserta catatan dan dua tautan pelanggan yang sama; tidak mengubah izin/assertion bisnis.

System/model-drift/pip/static, PDF14 halaman, restore kompatibilitas SQLite dan encrypted/MFA recovery drill lulus. Dependensi tidak berubah; scan advisory baru dan CI remote tidak diklaim. Pemetaan ini tidak menetapkan sesi online atau menghubungkan router nyata. Drawer bertab dan fitur matriks tersisa masih pending. Draft konfigurasi bukan publikasi snapshot.


## Checkpoint workspace pelanggan — 14 November 2026 WIB

26 tes baru, **326 tes SQLite dan326 PostgreSQL**, tanpa fail/skip/expected-failure. Tujuh tab server-rendered dengan whitelist tab, izin tambahan invoices/tickets/audit dan batas customer+tenant. Pengujian mencakup GET-only/tanpa efek bisnis, unknown/path-like tab404, preset/custom/revocation, saldo dan status sebagian/lunas/reversal, snapshot harga, pembayaran/reversal historis, ringkasan seluruh halaman, pagination, query count tetap ketika jumlah baris faktur/pembayaran meningkat, teks aman, empty state, scope tiket tanpa komentar internal dan whitelist audit mencegah tabrakan ID antar-jenis objek. Piutang ringkasan tetap mengikuti izin customers sebelumnya; izin tab tidak diklaim menyembunyikan seluruh agregat keuangan dari izin pelanggan.

Browser operasi lulus untuk tujuh tab/nav aktif, record tepat, permintaan role lain ditolak, navigasi faktur dengan JavaScript dinonaktifkan, posisi kembali URL filter lengkap dan scroll400px, empat viewport dan target sentuh44px. Counts bisnis sebelum/sesudah pembacaan workspace tetap. Bukti baru customer-workspace-* memuat data fiktif tanpa OTP/recovery/secret. Browser security/billing/MFA regresi juga lulus. Kedua browser memakai server/database SQLite sementara; bukan Chromium di PostgreSQL. Restore SQLite dijalankan oleh browser operasi sebagai regresi dan cocok.

Tidak ada migrasi atau dependency baru; model drift/check/pip/static lulus. Skema0009, proses worker dan arsip/kunci development sebelumnya tetap dipertahankan. Default demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS; tidak ditambahkan fixture workspace ke database persisten. Web dimuat ulang untuk memakai kode baru; tiga worker tidak perlu diubah. Pengujian12 proses PG, encrypted/MFA drill, PDF14 halaman dan advisory scan merujuk tahap terdahulu, tidak diulang pada tahap halaman baca ini. CI remote belum diverifikasi. Drawer overlay, koordinat/topologi dan modul matriks tersisa belum diselesaikan. Lihat [CUSTOMER-WORKSPACE](CUSTOMER-WORKSPACE.md).


## Checkpoint laporan penagihan — 14 November 2026 WIB

26 tes tambahan, **352 SQLite dan352 PostgreSQL**, tanpa fail/skip/expected-failure. Tiga mode laporan berdasarkan waktu transaksi WIB: penerimaan/koreksi, faktur terbit, piutang akhir historis. Unit/integrasi memeriksa opening + issued − received + reversal = closing terhadap saldo tiap faktur; koreksi beda bulan tidak mengubah penerimaan periode lampau; as-of memperhatikan tanggal pembayaran/reversal/issued_at, bukan periode layanan atau saldo sekarang. Batas awal/akhir WIB, tahun kabisat366 hari, nilai neto negatif, snapshot nama/harga, scope/izin/dependensi/revocation, input invalid, CSV formula/angka negatif/no-truncation/audit dan pagination diuji.

Laporan memakai transaksi baca tersendiri, dengan REPEATABLE READ/READ ONLY pada PG. Read-only mode diperiksa aktif di dalam scope dan kembali off setelahnya. TestCase biasa tidak dipakai untuk menyembunyikan batas transaksi;26 tes laporan memakai TransactionTestCase. Tidak ada perubahan model/dependency; skema tetap0009 dan ATOMIC_REQUESTS false.

`verify_postgres.py` lulus13 skenario lintas proses:12 sebelumnya ditambah laporan yang tetap konsisten ketika dua proses pembayaran commit idempoten; pembacaan baru menampilkan tambahan1234 rupiah. Dump/restore biasa dan terenkripsi seluruh tabel/MFA/NAS tetap cocok. Database uji dibuang, base lab tidak diisi fixture. Tidak ada klaim benchmark atau failover.

Browser operasi lulus: ringkasan dibandingkan dengan SQL sumber independen, saldo cocok, pergantian mode, CSV semua peristiwa/total bertanda, input invalid, role lain ditolak dan4 viewport. Browser security/billing/MFA regresi lulus. Bukti billing-report-* hanya data fiktif, tanpa faktor MFA. Browser memakai SQLite sementara; pengujian PG ada di backend/proses. Restore SQLite dijalankan browser dan cocok. System/model-drift/pip/static lulus. PDF14 halaman, encrypted SQLite/MFA drill dan scan advisory tetap merujuk checkpoint terdahulu, tidak diulang.

Default demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS; tes/ekspor tidak menyentuh database persisten. Web dimuat ulang, tiga worker tetap. Tidak ada secret/tujuan jaringan/migrasi/worker baru. CI remote dan kapasitas produksi belum diverifikasi. Laporan ini mencakup ledger simulasi; mutasi bank/pengeluaran/pendapatan akrual/tutup buku dan modul matriks lain tetap pending. Lihat [BILLING-REPORTS](BILLING-REPORTS.md).


## Checkpoint foto pekerjaan privat — 14 November 2026 WIB

30 tes baru, **382 SQLite dan382 PostgreSQL**, tanpa fail/skip/expected-failure. Cakupan photo decoding/format/extensions/mime, EXIF-orientation/metadata strip/PNG alpha, corrupt/animation/pixel bombs/metadata compression/source/output limits, quota/dedupe/version/assignment/tenant/portal/internal/revocation/CSRF/rollback/audit, deferred binary, integrity/download headers dan GET tanpa efek keuangan. Handler upload global single-file2MiB tidak melakukan spool disk; penolakan oversize diuji menutup buffer. CSV128KiB/200 baris tetap lulus. Perbaikan cleanup diuji ulang bersama seluruh backend setelah ditemukan buffer pada error parser belum ditutup otomatis.

Migrasi0010 menambah WorkPhoto. Backup schema0009 dibuat/dibaca ulang, lalu copy-migration awal menandai perbedaan auth_permission karena4 permission model baru. Shell sempat melanjutkan migrasi aktif setelah assertion tersebut. Verifikasi kemudian diperbaiki dan diulang pada salinan serta database aktif: tidak ada row app/auth lama yang hilang/berubah, hanya tabel/content type/4 permission baru. Foto awal kosong; base PG juga bermigrasi. Gunakan set-e/&& untuk menghentikan langkah setelah pemeriksaan gagal. Demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto; tidak ada pengisian fixture ke data persisten.

**15 skenario proses PG** lulus:13 sebelumnya ditambah request foto sama menghasilkan satu binary/timeline, serta dua request untuk slot ke-20 menghasilkan satu diterima/satu quota rejection. Snapshot/dump/restore biasa/terenkripsi membandingkan20 binary foto dan seluruh app/auth/MFA/NAS. SQLite encrypted/MFA drill menambah binary dan mengunduh foto dengan autentikasi, checksum dan header sandbox setelah restore. Verifier kompatibilitas SQLite menambah jumlah/digest metadata+binary tanpa mencetak isi foto. Arsip development0010 baru `data/backups/encrypted/TagihNet-20261114-work-photos.tnbk` dibaca ulang/restore sementara, seluruh app/auth payload cocok dan source tetap; arsip/key lama tidak ditimpa.

Browser operasi dan security/billing/MFA regresi lulus. Foto internal/publik, public upload portal, private/PSB download denied, assigned-tech read, MIME/attachment/sandbox, source data dan4 viewport terverifikasi di database SQLite sementara. Dua gambar work-photos-* hanya fiksi, tanpa faktor MFA. System/model-drift/pip/static lulus. Tidak ada dependency baru. PyPI version metadata14 runtime/PG dicek ulang melalui HTTPS dengan TLS verification: tidak ada advisory aktif yang tercantum. OS/native/dev/fuzzing/malware/kapasitas belum tercakup. PDF14-page stress tidak diulang; PDF authorization tetap ada dalam browser. CI remote tidak diklaim.

Web/tiga worker dimulai kembali. Foto ada di BLOB/bytea database, bukan media publik. Penghapusan/penggantian/retensi, lampiran non-raster dan kanal nyata tetap pending. Lihat [WORK-PHOTOS](WORK-PHOTOS.md); tidak ada klaim seluruh ASVS atau produksi siap.

## Checkpoint kuitansi pembayaran — 14 November 2026 WIB

31 tes baru, **413 SQLite dan 413 PostgreSQL**, tanpa fail/skip/expected-failure. Kuitansi baca HTML/PDF per payment memakai scope faktur + pasangan tenant/invoice/payment. Kasir/owner/keuangan/operasional dan portal sendiri diuji; custom role membutuhkan invoices. ID swap lintas ISP/faktur/pelanggan, NOC/teknisi, sesi dicabut/user nonaktif dan metode selain GET ditolak. Cetak ulang tidak mengubah ledger/audit/jobs. Nomor stabil memakai tanggal transaksi WIB dan ID; partial tidak diklaim lunas, customer snapshot tetap, identitas ISP/pencatat saat cetak dijelaskan, reversal mempertahankan nominal/nomor asli dan status terkini. Label simulasi tetap saat toggle SIMULATION dinonaktifkan.

HTML autoescape dan PDF markup/URL-as-text, cache private/no-store/nosniff, judul/filename/A4, terbilang hingga signed BIGINT, font Latin tertanam/aksen dan teks panjang diuji. Middleware sebelumnya menimpa private dari view; kini menambahkan no-store sambil mempertahankan directive lain. Inspeksi visual menemukan substitusi font sistem pada PDF standar; kuitansi kini menyematkan Vera yang sudah termasuk ReportLab. Verifier standalone tanpa DB lulus: normal satu halaman dan reversal empat halaman, marker akhir tiap field/nominal/WIB/terbilang/nomor/A4/footer terbaca dan reversal pada semua halaman. Hasil normal dan halaman terakhir stress diperiksa visual.

Satu run PG menemukan false-positive pada tes NOC lama: token CSRF acak dalam atribut HTML berisi substring Rp. Assertion kini memeriksa teks setelah tag/atribut dibuang, tetap melarang nominal yang tampil. Tes terkait SQLite dan suite PG413 diulang dan lulus. Hasil final tercatat pada log lokal data/evidence/payment-receipts-{sqlite,postgres}-tests.log. Tidak ada perbaikan kode monitoring atau pengurangan pembatasan akses.

Browser operasi lulus pada SQLite sementara: pembayaran sebagian10.000 rupiah, tautan operator/portal sendiri, terbilang/PDF/no-store/private, reversal dan scope pelanggan lain/NOC/teknisi. Kuitansi normal/reversal diuji360/768/1366/1920px tanpa overflow seluruh halaman. Dua screenshot payment-receipt-* hanya data fiktif, tanpa faktor MFA. Browser security/billing/MFA/bulk regresi dan restore SQLite dalam browser lulus. Source screenshot terdahulu yang dibuat ulang oleh pengujian dikembalikan. System/model-drift/pip/static/diff checks lulus. CI kini memasang requirements-test.txt (pypdf sudah ada di dev) untuk backend; dependensi runtime tidak berubah, CI remote belum diverifikasi.

Tidak ada migrasi; schema0010/arsip terenkripsi checkpoint foto dipertahankan.15 skenario PG lintas proses, drill encrypted SQLite/PG, PDF faktur14 halaman dan scan advisory14 paket merujuk checkpoint terdahulu, tidak diulang untuk fitur baca ini. Web versi akhir dimulai, tiga worker yang ada dipertahankan; HTTP health simulation/login dan kedua endpoint kuitansi anonim diverifikasi. Jumlah/hash seluruh tabel aplikasi/auth sebelum/sesudah startup cocok:30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/6 paket. Tidak ada seed/MFA/perubahan data demo.

Kuitansi merupakan pembacaan ledger simulasi dengan identitas penerbit saat cetak, tanpa tanda tangan/snapshot fiskal/verifikasi publik. Salinan PDF lama tidak diperbarui otomatis setelah reversal. Gateway/settlement serta modul matriks lainnya tetap pending. Panduan: [PAYMENT-RECEIPTS](PAYMENT-RECEIPTS.md).

## Checkpoint jadwal penagihan — 14 November 2026 WIB

31 tes jadwal, **444 SQLite dan444 PostgreSQL**, tanpa fail/skip/expected-failure. Default nonaktif/GET dan review tanpa efek; konfirmasi bertanda tangan15 menit/MFA5 menit/actor/tenant/access_version/config version/stale data/replay; range/type/control/plan lintas tenant; owner/custom dependencies/preset denied/CSRF/step-up/plaintext; clamp29–31/Februari kabisat/WIB/late current-month-only; snapshot dinamis saat generasi lalu tetap; filter/empty/over500/invalidprice; dedupe/manual overlap/worker restart; pencabutan akses/MFA/userinactive, nonaktif/cancelremaining/SIMULATIONfalse, rollback audit dan constraint diuji. Dua subtest tipe/karakter kontrol serta label UI diperiksa ulang setelah suite penuh, tetap31 tes lulus.

**17 skenario PG dua proses** lulus:15 terdahulu ditambah dua scheduler bulan sama menghasilkan satu siklus/batch + satu replay, lalu dua worker menghabiskan snapshot tersebut dengan tepat satu faktur per pelanggan/periode. Normal/encrypted PG dump/restore membandingkan setiap tabel aplikasi/auth termasuk jadwal/run/batch/MFA dan20 foto. Database uji dihapus, base lab tanpa fixture. Encrypted SQLite drill juga memuat konfigurasi dan run antre nonkosong; semua fingerprint cocok, password/TOTP/CSRF/replay/recovery dan unduhan foto sesudah restore lulus. Tidak ada klaim kapasitas/queue produksi.

Browser operasi sementara lulus: fixture pelanggan baru hanya untuk tes, review tanpa persist, konfirmasi MFA, worker--once dua kali menghasilkan satu faktur/siklus, nonaktif, riwayat dan scope teknisi/portal;360/768/1366/1920px tanpa overflow halaman. Dua screenshot billing-schedule-* tanpa faktor MFA. Label review diterjemahkan menjadi nama status/paket dan bulan; screenshot diperbarui. Semua operasi/receipt/report/photo/filter/import regresi dan restore SQLite juga lulus. Browser security/MFA/role/bulk/billing lulus; system/model drift/pip/static/diff checks lulus. Runtime dependencies tidak berubah. Scan advisory dan PDF stress mandiri tetap hasil checkpoint sebelumnya; tidak diulang.

Empat proses development dihentikan, archive terenkripsi schema0010 pre-scheduler dibuat dan dibaca ulang. Restore sementara diberi salinan kunci aplikasi terpisah, migrasi0011 dijalankan lalu semua digest baris aplikasi/auth lama cocok. Dengan set-e, hanya sesudah preflight lulus migrasi aktif dijalankan dan dibandingkan lagi: dua tabel kosong/delapan permission baru saja, tanpa row lama berubah/hilang. PG base bermigrasi0011. Archive baru schema0011 `TagihNet-20261114-billing-scheduler.tnbk` dibuat/verify/restore sementara dan seluruh app/auth cocok. Semua kunci/arsip lama dipertahankan.

Data demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/6 paket,0 jadwal dan0 run. Tidak ada seed atau persetujuan otomatis yang dibuat pada database persisten. Tiga worker dan web dimulai kembali pada source baru; health simulation/login serta redirect endpoint jadwal anonim diperiksa. Migrasi ini tidak mengubah aturan nominal/faktur manual, tidak menambah worker atau domain/secret, dan tidak mengaktifkan integrasi nyata. Panduan: [BILLING-SCHEDULER](BILLING-SCHEDULER.md). CI remote/produksi belum diverifikasi.

## Checkpoint staging lokal — 15 November 2026 WIB

User memilih staging lokal karena belum ada VPS/domain.11 tes baru, **455 SQLite dan455 PostgreSQL**, tanpa fail/skip/expected-failure. Profil fail-closed mewajibkan DEBUG0/SIMULATION1/PG verify-full, host eksplisit/cookie Secure/CA, sumber file secret eksklusif/bounded/invalid/UTF8 tanpa kebocoran, serta bootstrap database kosong/non-superuser/MFA/ulang ditolak/password lemah/audit rollback diuji. Tidak ada migrasi model; skema tetap0011.

Image Python3.12-slim-bookworm dipatok digest, Gunicorn27.0.1/WhiteNoise6.12.0 dan PG16.15 dijalankan nyata lewat Compose. Verifier stack disposable lulus: guard volume initialized hilang; TLS dengan CA/hostname diverifikasi dan CA asing ditolak; Host invalid400, forwarded header tidak mengambil alih; HTML security headers/no-store/HSTS, hashed static immutable/private path404; bootstrap/password/MFA/CSRF/cookie Secure; role runtime PG tanpa SUPERUSER/CREATEDB/CREATEROLE/CREATE schema/tabel dan SQLSTATE42501, TLS plaintext/wrong CA/wrong hostname ditolak; non-root/read-only/cap-drop/loopback/private DB; crash worker memicu Docker restart dan session tetap setelah restart web; semua nilai faktor/secret tidak ada pada log. Stack sementara dibuang, staging persisten/demo/lab dipertahankan. Ini tes HTTP/TLS/container sebenarnya, bukan browser Chromium staging; browser screenshot/17 race PG/encrypted full restore/PDF stress tetap checkpoint sebelumnya dan tidak diulang.

check--deploy profil staging menghasilkan W005/W021 saja, tidak disembunyikan: HSTS1jam untuk host lokal, tanpa include-subdomains/preload. Tidak ada klaim HTTPS domain publik atau keamanan produksi lengkap. CI staging job build+verifier ditambahkan, hasil remote belum dikonfirmasi. Pip/model/system/diff checks lulus. Dua paket baru diperiksa lewat HTTPS metadata PyPI,0 advisory aktif tercantum; OS/image/native/dev/full16-package rescan belum dilakukan.

Build awal gagal CA proxy, diperbaiki dengan BuildKit secret pada Dockerfile sementara tanpa TLS bypass/layer CA. Format tmpfs YAML dibetulkan. Izin file host600 menghalangi PG membaca config; initializer kini menyalin config/init dengan owner999/mode0400 dan image code dibuat terbaca untuk UID10001. Cluster staging baru yang sempat initdb tanpa role/database aplikasi diperiksa kosong pada kedua bagian itu lalu init dilengkapi; tidak ada penghapusan data. Tes crash semula memakai Docker API kill yang dianggap administrative stop sehingga restart tidak terjadi; verifier diperbaiki untuk mematikan proses worker di container. Ulang final lulus. Docker ignore khusus staging menjaga konfigurasi development lama.

Staging persisten data/staging mempunyai1 ISP/1 owner/0 MFA/0 pelanggan/0 faktur, secret independen dan portloopback18443; web+3workers+PG sehat dan job prepare/migrate exit0. Owner belum didaftarkan MFA otomatis; tes memakai project sementara. Pg_dump staging diekspor ke archive AES-GCM independen TagihNet-staging-20261115.tnbk, authenticated/decrypted byte-for-byte; SQL restore archive khusus ini belum dijalankan. Volume/image tidak dijamin ikut snapshot cloud; initialized-volume guard menolak pengganti kosong bila volume lama hilang. Sumber/data default tetap cocok seluruh digest app/auth dengan baseline0011:30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/6 paket/0 jadwal/0 run. Kunci/archive development tidak diganti. Tidak ada pengiriman email/pembayaran/router nyata. Lihat [STAGING](STAGING.md) untuk panduan dan pekerjaan produksi berikutnya.

## Checkpoint backup dan SQL recovery staging — 15 November 2026 WIB

16 tes baru, **471 SQLite dan471 PostgreSQL**, tanpa fail/skip/expected-failure. Cakupan: pg_dump stream sukses/gagal/publication gate/non-clobber; GCM/tamper/type sebelum Docker; file MFA format/mode/symlink; target source/initialized/existing volume/error socket; container project/service/image/volume identity; lock restore/up/resume; failed-start; web-only recovery dan explicit resume. Satu helper streaming PG sekarang dibagi oleh backup generik dan adapter staging, tanpa perubahan format crypto.17 skenario proses PG plus normal/encrypted full restore dijalankan ulang dan lulus; database uji dibuang.

Verifier staging_backup menjalankan seluruh HTTPS/runtime/host/static/MFA/CSRF/privilege/restart checks terdahulu, lalu fixture dua tenant, faktur249.750, payment100.000+25.000/reversal25.000/saldo149.750, binary foto, MFA, pending schedule batch/outbox/network. Writer sumber fixture dihentikan saat membandingkan. Backup, decrypt/SQL restore dan seluruh tabel app/auth/framework serta sequence last_value/is_called cocok. Old session cookie ditolak dengan secret sesi target baru; restored password/TOTP/CSRF dan authorized photo checksum/download lulus, accepted target OTP replay ditolak. Cipher semua MFADevice diuji dengan original key; wrong-MFA target retained+blocked, invalid SQL dump rollback tetap empty. Wrong archive key/tamper/source target/used target ditolak; setelah sukses up hanya web dan semua worker tidak berjalan. Sumber fingerprint tetap. Semua project/volume sementara dibersihkan; source user/demo tidak diubah.

Dua kesalahan fixture awal diperbaiki: root import belum ditambahkan ke sys.path; versi akses fixture di-hardcode1, padahal enrollment MFA nyata telah menaikkannya. Fixture kini membaca versi aktual dan tidak melemahkan pemeriksaan izin. Benturan nama file wrong-mfa dengan direktori test juga diperbaiki. Ulang verifier final beserta assertion partial-failure/empty target lulus. Metadata rename/file+directory fsync dan shared lock menjaga urutan restore/start; targeted16 tes lulus sesudah perubahan akhir. Tidak ada migration/dependency baru, schema tetap0011. System/model-drift/pip/diff checks lulus, CI staging job kini memakai verifier gabungan; CI remote belum dikonfirmasi.

CLI backup staging nyata menghasilkan data/staging/backups/TagihNet-staging-20261115-managed.tnbk memakai archive key lama, tanpa overwrite. Archive awal TagihNet-staging-20261115.tnbk dari checkpoint sebelumnya dipulihkan ke proyek baru data/staging-recovery-20261115/port19443, dengan new session key/original MFA key. Seluruh payload app/auth cocok dengan source1ISP/1owner/0MFA/0pelanggan/0faktur. Web/database clone sehat, tiga worker tetap ditahan (recovery_pending); original staging dan private keys/archives tetap. Default development hash setiap baris app/auth cocok dengan baseline0011 yang sudah diverifikasi:30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/6 paket/0 schedule/run.

## Checkpoint operasi staging — 15 November 2026 WIB

27 tes baru; **498 SQLite dan498 PostgreSQL** lulus, tanpa fail/skip/expected-failure. Cakupan: heartbeat optional/private/atomic/I/O/stale/future/degraded/malformed; konfigurasi terikat proyek dan interval/key; HTTP/sertifikat/metrik unknown; backup jadwal/interval/retry/missing/hold/deduplikasi; log transisi/rotasi; status lama/failure marker; lock/start-idempotence/PID reuse/stop saat backup. Tes terarah27 dijalankan lagi setelah perbaikan akhir query. System check, model drift, pip check dan diff check lulus. Tidak ada dependensi/migrasi baru.

Verifier gabungan kini menambah fault injection pada source sementara: SIGSTOP proses billing sementara Docker tetap Running, alarm heartbeat stale, SIGCONT dan satu raised/resolved tanpa duplikasi; backup periodik pg_dump/GCM terautentikasi dan tanpa duplikat awal, missing archive, pause/unpause layanan, private log serta watcher yang benar-benar menerbitkan laporan baru/start idempoten/stop. Recovery berisi ledger/MFA/foto/batch/reminder tetap diperiksa dengan worker held dan backup default-off. Umur batch masa depan dibatasi minimal nol, bukan menyebabkan semua metrik unavailable. Seluruh validasi HTTPS/auth/SQL restore sebelumnya tetap dijalankan dalam verifier yang sama.

Run final verifier gabungan lulus sampai pemeriksaan old-session/password/TOTP/CSRF/photo/replay dan cleanup seluruh stack/volume sementara (exit0). Tidak ada fixture uji yang ditinggalkan pada source atau clone pengguna.

Percobaan pertama menemukan pemeriksaan health Docker terlalu cepat setelah web restore siap; verifier kini menunggu health juga. Percobaan berikutnya menemukan umur negatif pada batch fixture masa depan; perhitungan SQL diperbaiki dan assertion `billing_due=1,billing_age=0` ditambahkan pada recovery nonkosong. Ini bukan kegagalan restore data. Tidak ada penonaktifan pemeriksaan atau pengubahan fixture untuk melewati kasus tersebut.

Pada staging pengguna, image diperbarui tanpa regenerasi key/prepare/bootstrap. Source web/db/3worker sehat dengan heartbeat baru; clone recovery tetap web/db saja. Backup harian opt-in sumber memakai key yang sudah ada dan direktori `data/staging/backups/automated`; arsip pertama diautentikasi, watcher memeriksa setiap60detik. Seluruh tabel app/auth/framework dan sequence source/clone identik sebelum/sesudah pembaruan/monitor/backup. Default development app/auth per-row digest juga identik dengan baseline schema0011. Arsip/key lama tetap dipertahankan.

17 skenario race PG/normal+encrypted restore generik merupakan hasil checkpoint recovery sebelumnya, tidak dijalankan ulang pada perubahan ini. Tidak ada klaim CI remote, browser baru, advisory/OS/image audit, beban, restart host/fresh-task persistence, alert eksternal, systemd/cron, offsite, retensi, PITR atau SLA/RPO/RTO produksi. Lihat [STAGING-OPERATIONS](STAGING-OPERATIONS.md).

Tidak ada uji Chromium/UI baru, scan advisory baru, kapasitas, restore ke server lain atau offsite/RPO/RTO produksi. SMTP/gateway/router tetap simulasi. Snapshot rollback dapat mengembalikan faktor/counter/izin lama dan harus direkonsiliasi. Panduan [STAGING-RECOVERY](STAGING-RECOVERY.md).

## Checkpoint beban staging dan query ledger — 15 November 2026 WIB

22 tes baru; **520 SQLite dan520 PostgreSQL** lulus tanpa fail/skip/expected-failure.16 tes harness mencakup percentile/failure/null, redirect/body/type/canary/ukuran, TLS fixed-local, cookie/diagnostic privacy, batas opsi/no-clobber/symlink, resource parsing, gate health/restart, failure artifact/cleanup.6 tes query mencakup pertumbuhan data/jumlah query, page50, seluruh status/today/reversal, nominal/scope dan tenant kosong. Tes batas query gagal pada source lama: dashboard37→115, filter overdue23→72 saat25 faktur ditambahkan, page50 memakai105 query. Source baru mempertahankan jumlah query dashboard≤18/daftar≤12 dan seluruh makna ledger. Tidak mengubah properti/mutasi model uang atau schema/dependency.

Baseline beban lama gagal latency/sample/error budget; percobaan awal tanpa ANALYZE timeout pada pelanggan. Kontrol dengan ANALYZE masih lambat dan satu health HTTPS tertahan. Hasil gagal disimpan, cleanup lulus. Setelah agregasi SQL, filter sebelum pagination dan row baca tanpa query pembayaran per sel, run final default20detik pada1/4/8 klien menghasilkan288/436/447 request, total1171,0 gagal; p95 keseluruhan112,20/298,09/533,40ms, p95 endpoint terburuk151,56/352,45/558,04ms. Budget2000ms tidak dilonggarkan. Profil dataset500 pelanggan/1000 faktur awal/250 pembayaran/25 reversal, laporan/PDF/GET dengan cookie owner MFA nyata dan3 worker.

Sepuluh sampel health/resource sehat; tidak ada Docker restart/container ID berubah/OOM. Batch500 berjalan pada tahap8, selesai menjadi1500 faktur dengan pasangan pelanggan/periode unik. Nominal total337.500.000 minus pembayaran sah22.500.000 menghasilkan saldo aktual315.000.000, tenant pembanding tetap dan replay worker0 tambahan. JSON gagal/sukses tidak menyimpan credential/body. Verifier TLS/host/DDL/static/bootstrap/MFA/CSRF/restart terdahulu dijalankan sebagai setup pada stack fiktif. Run awal setelah optimasi juga lulus; run final memperkuat assertion saldo aktual dan tenant pembanding. Semua stack/volume fixture dibersihkan. [STAGING-LOAD](STAGING-LOAD.md) dan ringkasan JSON memuat profil, hasil, limit dan batas interpretasi.

Regresi browser operations_smoke lulus: jadwal, paket, foto, receipt, laporan, workspace/filter pelanggan, PSB/tiket/reminder/monitoring/impor, empat viewport dan restore kompatibilitas SQLite pada DB/server sementara. Screenshot lama yang dihasilkan ulang dikembalikan; tidak ada perubahan visual baru yang diklaim. Dua tes harness mula-mula membuka socket dari unit test di sandbox; pemilihan port dipisahkan/dimock, dan run SQLite penuh serta targeted22 terakhir lulus. System/model-drift/pip/diff checks lulus.

Image baru diterapkan pada source staging dan clone dengan volume/key lama. Seluruh tabel app/auth/framework dan sequence identik sebelum/sesudah; source watcher tetap sehat, clone tetap worker hold. Development web/3worker dimulai setelah antrean kosong dikonfirmasi, tanpa seed/MFA/jadwal baru; app/auth digest tetap baseline0011. Workflow beban manual/artifact ditambahkan, remote CI belum dijalankan.17 PG process races/full PG restore, scan advisory/OS/image dan load di server lain adalah hasil lama atau belum diulang; tidak ada klaim soak/failover/offsite/PITR/RPO/RTO atau siap produksi.

## Checkpoint paket pemulihan — 15 November 2026 WIB

22 tes baru; **542 SQLite dan542 PostgreSQL** lulus tanpa fail/skip/expected-failure. Cakupan export preserving source/private/separate outputs; wrong key/type/magic/tamper sebelum output; existing/nested/symlink/FIFO guards; metadata edit/same-ID wrong key/swap archive; valid manifest MAC tetapi GCM rusak; wrong MFA saat tidak ada perangkat DB; incomplete/extra/duplicate/version/type/oversize/mode; partial export; frozen restore inputs/normal cleanup dan verify offline. Tidak ada perubahan model/dependency/format arsip TNBK; metadata paket baru memakai HKDF-SHA256 + HMAC-SHA256 terpisah tujuan.

Verifier Docker gabungan lulus seluruh TLS/host/static/login/MFA/CSRF/runtime SQL permissions/worker crash/heartbeat/backup/recovery guards terdahulu. Akses restore diekstrak menjadi helper yang dipakai pada dua target. Sesudah restore biasa/source unchanged lulus, paket dan key custody disalin ke direktori penerima. Kunci MFA penerima yang salah ditolak tanpa menginisialisasi target. Lalu hanya sumber fiktif/volume database/arsip/key asal serta paket ekspor awal dihapus. Salinan penerima sendiri berhasil restore ke target baru dengan semua tabel app/auth/framework dan sequence cocok, ledger149.750/reversal/foto/scheduler/outbox tetap. Old cookie ditolak; password/TOTP/CSRF/authorized photo checksum dan replay target diuji ulang. Key sesi baru/CA baru dan worker hold tetap. Seluruh stack/volume sementara dibersihkan (exit0); source pengguna/default demo/clone persisten tidak dihapus atau dienroll MFA.

System/model-drift/pip/diff checks lulus. Image aplikasi yang sudah diuji dipakai ulang karena hanya helper host/tests/docs yang berubah; schema tetap0011. CI staging memakai verifier yang diperluas; remote CI belum diklaim. Browser, load,17 generic PG races/restore dan advisory/OS/image scan tidak diulang pada perubahan ini. Hasil beban sebelumnya tetap pengukuran lokal, bukan kapasitas produksi.

Pengujian masih pada host sama dan format paket baru memerlukan review operasional sendiri. Tidak ada offsite upload, media/akses custody independen, rotasi key, retensi, PITR/failover atau bukti RPO/RTO/restore server lain. [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md).

## Checkpoint preflight rilis — 15 November 2026 WIB

31 tes baru; **573 SQLite dan573 PostgreSQL** lulus tanpa fail/skip/expected-failure. Cakupan digest source/self-contained execution/tests+bytecode exclusion/missing/symlink/batas pins; mutable tag/image selection/availability/restore lock/manifest-only pin/legacy fallback; container privilege/volume/network/port/resource/cap/log; warning/schema/source/dependency/security/invalid-probe/privacy; unknown/pending/changing migration fingerprints; mode recovery/held worker; queue drain, backup rusak/header lama meski metadata baru/pasangan custody; report no-clobber. Targeted31 dan16 staging recovery unit lama juga lulus sesudah perubahan pin. System/model-drift/pip/diff checks lulus, tanpa migration/dependency baru.

Verifier gabungan Docker lulus dengan source dan target fiktif: pin tidak mengganti container; up benar-benar memakai ID pilihan, sementara seluruh tabel/sequence dan metadata backup tetap. Gate aktif lulus image/source/pins/schema/security/TLS/health/backup+custody. ID image salah ditolak. Pada target recovery, active mode ditolak; record migrasi0011 dihapus lalu record future9999 ditambahkan secara terpisah. Gate membaca keduanya sebagai gagal tanpa repair/migrate. Hanya record fixture dipulihkan dengan ID/time eksplisit; seluruh tabel/sequence cocok kembali dan worker tetap held. TLS/auth/MFA/CSRF/photo/SQL restore/source-unavailable guards terdahulu tetap dijalankan; semua stack/volume sementara dibersihkan (exit0).

Saat pengujian pin/up, web dapat siap HTTPS sebelum status health Docker selesai; assertion awal terlalu cepat. Verifier diperbaiki untuk menunggu kedua health, tanpa melonggarkan gate. Run final seluruh verifier lulus. Manual Compose config dengan selector image environment yang berbeda juga membuktikan source/clone tetap memakai pin masing-masing, tanpa start layanan.

Assert recorder diperkuat untuk memeriksa langsung bahwa record0011 tetap tidak ada dan record9999 tetap ada setelah gate gagal, selain fingerprint model/sequence. Latihan terarah tambahan pada source/target kosong milik tes menjalankan assertion tersebut dan lulus sampai cleanup; hanya fixture yang dipulihkan. Ini memastikan pemeriksaan tidak bergantung pada asumsi bahwa tabel metadata migrasi ikut daftar model aplikasi.

Source pengguna dan clone dipin ke image yang memang sudah berjalan, tanpa container restart/migrate; source watcher dimuat ulang. Digest aplikasi1c56ab9a4f710a336ef6bc49bb0785521e7734792ba6468dc6b885936cb06db4 cocok dengan image yang sudah diuji, sehingga tidak perlu build runtime baru. Paket lama valid tetapi berbeda dari backup terpantau dan ditolak oleh gate aktif; salinan baru release-20261115 dibuat pada dua folder privat, semua paket/key lama dipertahankan. Source/recovery app/auth/framework+sequence dan default demo app/auth tetap, serta mode SIMULATION/hold dipertahankan.

Laporan gate terbatas local_simulation dan production_ready selalu false. Pin/backup/health/source checks tidak membuktikan CI remote, supply-chain/advisory/OS audit, schema kompatibel untuk semua versi, rekonsiliasi pascasnapshot atau cutover/rollback produksi. Load/browser/17 generic PG races adalah hasil checkpoint sebelumnya dan tidak diulang pada perubahan host helper ini. [RELEASE-RUNBOOK](RELEASE-RUNBOOK.md) menjelaskan urutan serta batas operasional.

## Checkpoint latihan rollback image — 15 November 2026 UTC

**588 tes SQLite dan588 PostgreSQL** lulus tanpa fail/skip/expected-failure.15 tes baru memverifikasi budget HTTPS integer1–120 ditolak sebelum lock/Docker bila tidak valid, default tetap120, gagal kesiapan mempertahankan pin/hold/manifest, output existing/dangling symlink ditolak, scope tag/token/label image, partial-build cleanup, daemon error tidak dianggap missing tag, alias base berubah tidak dihapus, foreign container dan worker yang masih berjalan ditolak, serta exception body tidak dipublikasikan. Model-drift/system/pip/diff checks lulus; model/schema/dependency aplikasi tidak berubah.

Verifier baru membuat dua image turunan lokal dengan source/schema sama dan ID berbeda: satu normal, satu keluar37 saat web dimulai. Run awal dan run akhir lulus; run akhir mencakup cleanup yang diperkuat untuk tag hasil partial build. Source fiktif melewati TLS/hostname/static/bootstrap/password/MFA/CSRF, SQL verify-full/DDL denial, runtime restrictions dan worker restart guards. Fixture ledger dua tenant/faktur249.750/dua pembayaran/satu reversal/saldo149.750, foto dan antrean simulasi dipulihkan melalui paket terenkripsi ke target baru dengan sesi baru/MFA asli dan worker hold.

Kandidat normal benar-benar menjalankan image ID pilihan, HTTPS/health Docker dan recovery preflight lulus. Hash seluruh baris model app/auth/framework, seluruh sequence PG serta recorder django_migrations eksplisit cocok. Sesi login target tetap berlaku. Image gagal terdeteksi lewat gagal readiness serta exit37/container ownership, tanpa perubahan tabel/sequence/recorder atau pembukaan worker hold. Pemilihan kembali ke ID kandidat normal memulihkan HTTPS/preflight/sesi, menjaga seluruh file secret dan data, serta tidak mengubah source fixture/tag staging bersama. Request autentikasi memperbarui aktivitas sesi secara normal; baseline diambil ulang sesudah request, tanpa mengecualikan tabel sesi.

Seluruh stack/volume/turunan image/alias milik run dibersihkan; laporan akhir exit0/status passed/cleanup passed dan production_ready false. JSON privat0600/no-clobber disimpan di data/evidence; ringkasan tanpa credential/PII beserta hash laporan dan verifier ada di [evidence/staging-rollout-20261115.json](evidence/staging-rollout-20261115.json).

Sesudah latihan, fingerprint source pengguna/clone recovery cocok dengan baseline sebelum checkpoint preflight; default development app/auth cocok dengan baseline schema0011. Source web/db/tiga worker serta watcher sehat/fresh; clone web/db sehat dan ketiga worker tetap ditahan. Pin image, kode aplikasi, kunci dan arsip lama dipertahankan; tidak ada restart/switch image pada dua staging pengguna. Development health/login lulus melalui proses yang sudah ada, tanpa duplikasi.

Workflow manual staging-rollout ditambahkan, belum dijalankan di GitHub. Full verifier backup/source-loss/preflight,17 generic PG concurrency/restore, browser, load dan advisory/OS scan tetap bukti checkpoint sebelumnya; tidak diulang untuk perubahan helper ini. Latihan baru bukan pengujian migrasi lintas versi, reverse migration, traffic cutover, tulisan serentak, host lain, durasi gangguan atau RTO. [STAGING-ROLLOUT](STAGING-ROLLOUT.md) menjelaskan batas dan cara mengulang.

## Checkpoint ubah sandi mandiri — 16 November 2026 WIB

**609 tes SQLite dan609 PostgreSQL** lulus tanpa fail/skip/expected-failure, termasuk21 tes baru. Targeted50 tes password/security juga lulus. Cakupan baru: akses anonymous/password-only/GET/private-cache/CSRF; bukti sandi/MFA meski sesi fresh; validator/reuse/konfirmasi/batas panjang/Unicode+spasi; portal MFA opsional vs wajib operator; spoof target/tenant/role, limiter5percobaan/identitas+IP; pencabutan sesi/pending login/token reset; recovery sekali pakai, rollback hash/versi/faktor bila audit gagal; stale access/device/password setelah pemeriksaan sesi; billing batch lama blocked tanpa faktur baru; dua perubahan sandi bersamaan dan perubahan sandi vs regenerasi recovery memakai koneksi DB terpisah. System/model-drift/pip/diff checks lulus, schema0011 dan16 dependency tetap.

Browser Chromium akhir lulus MFA/access/role/revocation/recovery, penerbitan massal serta alur billing/payment/reversal/PDF/filter/logout lama. Password smoke tersendiri membuktikan sandi salah tidak mengonsumsi recovery, input tidak diisi ulang, perubahan sukses mengakhiri sesi lain, sandi baru tetap meminta MFA dan recovery yang baru dipakai ditolak lalu kode lain diterima. Halaman diperiksa di360/768/1366/1920 serta dark mode. Dua screenshot form kosong disimpan di [account-password-desktop.png](evidence/account-password-desktop.png) dan [account-password-mobile.png](evidence/account-password-mobile.png); reduced motion menstabilkan screenshot saat resize. Tidak ada QR/kode/sandi pada screenshot.

Percobaan awal menemukan pencari label yang tidak menyertakan titik dua, enrollment memakai kode step sebelumnya yang dapat melewati batas waktu, dan total login/MFA dari gabungan skenario melampaui limiter IP. Harness memperbaiki selector, memakai kode enrollment saat ini/recovery untuk login staff berikutnya dan memisahkan password smoke ke DB/server sementara sendiri. Assertion replay dan limiter tetap aktif. Run lengkap akhir exit0; targeted password smoke sesudah preferensi reduced motion juga exit0. Diagnostik kegagalan kini hanya fase+tipe, tanpa snapshot/faktor; log kegagalan sintetis yang memuat diagnostik MFA dari percobaan awal dihapus.

Image staging baru dibangun dengan trust CA proxy tetap aktif. Full Docker backup verifier akhir exit0: alur password baru melalui HTTPS nyata/CSRF/MFA, old cookie/sandi ditolak, MFA tetap wajib; semua SQL/TLS/privilege/container/worker/ops/backup guard lama tetap lulus. Identity privat yang dikembalikan memakai sandi baru untuk restore berikutnya. SQL restore, seluruh model/sequence/foto/ledger, target auth/replay, gate migrasi tanpa repair dan source-loss restore dari paket/kunci terpisah lulus sampai cleanup.

Image `sha256:a2bf84edb6512a712f0f7a477edbf1dac5c5bf0d5ca66cd4735c3ad4e7e804d4` diterapkan ke source staging dan clone setelah tes lulus. Digest aplikasi18199a322711c0ee275bfe3ea642387fdce50f6a69cd1c13bb43d34e9480e727. Snapshot sebelum/sesudah mencakup semua model, sequence, recorder migrasi dan hash seluruh secret host; semuanya cocok, web/db/worker source sehat dan clone tetap web/db/worker hold. Akun staging pengguna tidak diubah sandi atau dienroll MFA. Image/kunci/arsip lama dipertahankan; salinan custody terbaru berada di dua folder self-password-20261116. Development web/tiga worker dimuat ulang setelah data/queue diperiksa, tanpa seed/migrasi atau perubahan baris app/auth/kunci.

Ringkasan agregat serta hash source ada di [self-password-20261116.json](evidence/self-password-20261116.json). Schema rollback,17 generic PG process scenarios/load/operations browser/advisory scan tidak diulang pada checkpoint ini; remote CI, integrasi nyata, produksi/offsite/audit independen tetap belum diklaim. [AUTHENTICATION](AUTHENTICATION.md) menjelaskan perilaku ubah sandi dan dampak pada persetujuan pekerjaan.

## Checkpoint Profil saya — 18 November 2026 WIB

**634 tes SQLite dan634 PostgreSQL** lulus tanpa fail/skip/expected-failure pada versi akhir, termasuk25 tes profil baru. Targeted46 profil/password juga lulus. Cakupan: anonymous/password-only/private GET/CSRF; update dua nama saja dengan field target/tenant/username/email/password/admin palsu diabaikan; semua preset operator dan portal tanpa MFA; token terikat sesi/aktor/tenant/versi/snapshot15menit, expiry/tamper/ukuran/tipe/format asing, foreign token403, stale/replay409 tanpa overwrite/audit ganda; no-op/clear/NFC/spasi/control/bidi/surrogate/panjang/XSS; stale sandi/akses/MFA/deaktivasi; audit rollback; sesi/password/MFA/consent billing serta nama snapshot pelanggan tetap. Dua koneksi DB membuktikan satu pemenang pada form sama dan race nama/sandi tidak mengembalikan hash sandi lama. System/model-drift/pip/diff lulus, schema0011/16 dependency tetap.

Cloud restart mempertahankan checkout/perubahan, database/kunci dan source+clone Docker dengan pin a2bf84... . Lab PG berhenti, lalu dimulai memakai volume/credential lama sebelum suite dijalankan. Watcher source dimulai kembali dengan konfigurasi backup opt-in/kunci lama; salinan custody terbaru dibuat pada dua folder privat profile-20261118 tanpa menimpa pasangan sebelumnya. Ini observasi restart dalam percakapan ini, bukan bukti restore host independen.

Chromium akhir exit0 mencakup seluruh security/access/role/bulk/billing/password lama, profil persisten/sidebar, info akun hanya baca, form lama409 serta sesi tetap aktif. Profil diuji pada360/768/1366/1920 dan dark mode; [desktop](evidence/account-profile-desktop.png) dan [mobile](evidence/account-profile-mobile.png) hanya berisi akun fiktif tanpa QR/OTP/password/token terlihat. Token/nama/locals ditandai sensitif pada laporan error produksi; suite backend diulang sesudah perubahan tersebut. Browser gagal hanya melaporkan fase/tipe exception tanpa snapshot form rahasia.

Run Docker awal berhenti pada timeout polling HTTPS sebelum pemeriksaan fungsional. Stack tes dibersihkan. Stack diagnostik baru berhasil startup dengan web/db healthy dan batas polling HTTPS12detik; akar penyebab timeout awal belum dipastikan. Sesudah tes berat selesai, image final dengan masking nama dibangun dan verifier gabungan dijalankan terpisah: **exit0 sampai cleanup**, tanpa melemahkan gate. HTTPS nyata membuktikan CSRF403, nama tersimpan, sesi/akses tetap dan stale replay409. Password/MFA/revocation, SQL/TLS/privilege/runtime/worker/ops/backup lama, restore seluruh tabel/sequence/foto/ledger, preflight pending/future recorder tanpa repair dan source-loss restore dari paket/kunci terpisah semuanya lulus. Fixture enrollment memakai TOTP saat ini dan recovery berbeda untuk perubahan sandi/login berikutnya agar tidak bergantung pada batas time step kode sebelumnya.

Image `sha256:963185d3317bf5a0b319077a8b27f09577a5aed02cb51b7ce8a4d0df2e1dc7e1` dengan digest aplikasi40adaba627c004bbfb0b15cec242c331784286dd3c945bb613ff02b903a0fbde dipin dan dijalankan pada source/clone setelah verifier lulus. Semua model app/auth/framework, seluruh sequence, recorder migrasi eksplisit dan hash file secret cocok sebelum/sesudah. Source sehat; clone sehat dengan tiga worker tetap ditahan. Akun source/clone tidak diubah nama/sandi atau dienroll MFA. Image lama tetap tersedia. Development web/tiga worker dimulai tanpa duplikasi/seed, health/login/profile anonymous redirect lulus dan semua baris app/auth/kunci lama tetap cocok.

Ringkasan agregat serta hash source: [account-profile-20261118.json](evidence/account-profile-20261118.json). Tidak ada klaim username/email/avatar edit, schema rollback baru, remote CI,17 generic PG process scenarios/load/advisory/penetration rerun, offsite atau produksi. [ACCOUNT-PROFILE](ACCOUNT-PROFILE.md) menjelaskan batas fingerprint serta dampak profil terhadap sesi dan persetujuan pekerjaan.

## Checkpoint arsip paket — 18 November 2026 WIB

**664 tes SQLite dan664 PostgreSQL** lulus tanpa fail/skip/expected-failure, termasuk30 tes baru. Targeted30 pada masing-masing backend juga lulus sesudah penyesuaian akhir harness konkurensi. Cakupan baru: GET tanpa mutasi/private cache, tenant/izin, MFA/versi akses/revisi perangkat/deaktivasi, CSRF/checkbox, target status bertanda tangan, expiry/tamper/format, stale/replay/konflik harga, no-op dan rollback audit; pelanggan/impor/PSB baru ditolak saat arsip, pilihan lama/replay PSB dipertahankan, aktivasi PSB/tagihan tunggal/batch/jadwal berikutnya tetap berjalan, reaktivasi mengembalikan pilihan baru. Dua koneksi DB menguji satu pemenang konfirmasi dan arsip vs penugasan; SQLite boleh menolak write tumpang tindih dengan aman. Migrasi nonkosong menjaga seluruh nilai lama, foreign key dan high-water ID yang pernah dihapus pada kedua backend. System/model-drift/pip/diff checks lulus;16 dependency tetap.

Chromium arsip/aktifkan kembali memakai DB/server dan MFA sementara sendiri: review/checkbox/replay, pilihan pelanggan/PSB baru hilang saat arsip, pilihan pelanggan lama tetap, reaktivasi, fingerprint pelanggan/faktur/pembayaran/reversal identik,360/768/1366/1920 dan dark mode. Bukti [desktop](evidence/plan-archive-desktop.png) dan [mobile](evidence/plan-archive-mobile.png) hanya berisi data fiktif, tanpa faktor. Regresi operations_smoke akhir lulus seluruh paket/pelanggan/impor/PSB/tiket/foto/billing/laporan/kuitansi/reminder/monitoring lama hingga restore. Percobaan awal memilih pelanggan yang kebetulan merupakan pemilik portal untuk assertion kuitansi lintas pelanggan; fixture sekarang secara eksplisit mengecualikan pemilik portal. Aturan akses aplikasi tidak dilonggarkan. Harness menerima TAGIHNET_EVIDENCE_DIR agar pengulangan dapat menulis screenshot sementara; screenshot historis yang sempat dihasilkan ulang dikembalikan.

Image kandidat dibangun dan verifier Docker gabungan **exit0 hingga cleanup**. HTTPS nyata mencakup pembuatan paket, review arsip/aktifkan kembali, MFA/CSRF/checkbox, stale replay; profil/password/revocation dan seluruh guard TLS/SQL/runtime/container/worker/monitor/backup lama tetap lulus. Fixture ledger/MFA/foto/jadwal/outbox memuat paket arsip dan pelanggan lama; keduanya tetap arsip/terhubung setelah SQL restore serta source-loss restore dari paket dan kunci terpisah. Gate migrasi kini menghapus hanya leaf core aktual0012 untuk menguji pending, lalu mengembalikan ID/waktu recorder persis; future recorder juga ditolak tanpa repair. Semua stack/volume fixture dibersihkan.

Sebelum migrasi data tersimpan, development web/tiga worker dihentikan dan seluruh tabel/kolom/baris/sequence/kunci ditangkap privat. Backup0011 dienkripsi memakai kunci lama, dipulihkan ke direktori baru, dimigrasi0012 dan diperiksa. Migrasi development sebenarnya kemudian lulus proyeksi seluruh kolom lama, foreign-key/integrity check serta semua sequence selain recorder+1. Keenam paket lama is_active=True;30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/0 schedule/run tetap. Backup0012 baru dipulihkan identik. Web/tiga worker dimulai sesudah pemeriksaan antrean, health/login lulus, seluruh baris0012 dan kunci tetap.

Watcher source serta penulis source/clone PostgreSQL dihentikan. Backup0011 masing-masing diautentikasi memakai kunci lama; baseline stabil mencakup semua tabel public, kolom, hash tiap baris, seluruh sequence dan recorder. Image `sha256:83dda3f5f6b0f9cbb376aa4ede8f9b22f741c5308cf97c4b063d3f407111f424`, digest aplikasiabbd07069ebd81424f8619efb84111a76f21392a960d6ab2167d8d9b20349f64, diterapkan dengan migrasi0012. Semua nilai lama dan secret host cocok; hanya kolomTrue/indeks dan satu recorder baru ditambahkan, sequence recorder+1. Source web/db/tiga worker sehat; clone web/db sehat, recovery_pending/worker hold tetap. Owner persisten tidak diedit atau dienroll MFA. Lab PostgreSQL memakai volume/kunci lama dan base schema disamakan tanpa seed.

Backup manual source0012 baru benar-benar dibuat dan diautentikasi sebelum catatan sukses diberi waktu aktual; interval24jam tidak diubah dan tidak ada pemajuan timestamp. Pasangan custody baru archives/plan-archive-20261118 dan keys/plan-archive-20261118 diverifikasi, seluruh pasangan lama dipertahankan. Watcher dimulai dan menghasilkan tick sehat baru. Ini satu workspace, bukan offsite. Setup cloud baru memeriksa migrate --check pada database tersimpan dengan kunci asli wajib; migrate/seed hanya pada direktori data benar-benar kosong. Eksekusi pada instance ini menjaga seluruh baris0012.

Ringkasan agregat dan hash source: [plan-archive-20261118.json](evidence/plan-archive-20261118.json). [PLANS](PLANS.md) menjelaskan grandfather pelanggan/PSB dan migrasi. Schema0011 image lama tidak dipakai sebagai rollback otomatis setelah0012. Tidak ada bukti reverse migration produksi, remote CI,17 generic PG process scenarios/load/soak/rollback image/advisory rerun, independent-host restore, integrasi nyata atau kesiapan produksi.

## Checkpoint tinjauan setelan billing — 19 November 2026 WIB

**694 tes SQLite dan694 PostgreSQL** lulus tanpa fail/skip/expected-failure, termasuk30 tes baru. Targeted64 settings+operasional SQLite serta30 settings PostgreSQL pada versi akhir juga lulus. Cakupan: GET/edit POST tanpa mutasi, penolakan direct POST lama, CSRF/checkbox/target bertanda tangan, izin/tenant/peran kustom/MFA/status aktif/versi akses/revisi perangkat, token kind/tipe/expiry/tamper/batasUnicode,409 form/review stale, no-op/replay, validasi NFC/kontrol/batas/pajak nol/multiline/plaintext, kalender Februari, audit rollback dan database busy. Ledger/faktur lama serta batch disetujui tetap; preview/PSB belum disetujui menjadi stale; invoice baru dan siklus jadwal berikutnya memakai setelan terbaru. Dua race memakai koneksi terpisah membuktikan satu pemenang konfirmasi serta preservasi SLA dan billing melalui fungsi penyimpanan aktual.

Temuan TGN-006: ModelForm billing dan SLA sebelumnya dapat menyimpan kembali field tenant yang sudah berubah setelah form/request dibaca. Billing kini memakai reviewed snapshot dengan token awal, MFA dan update_fields; jalur SLA memakai whitelist empat field serta tenant→membership lock/fresh access. Tes HTTP menyisipkan konfirmasi billing sesudah validasi form SLA: tax_bp baru tetap0 dan SLA17, tanpa mengembalikan nilai pajak lama1100. Identitas penerbit terkini pada cetak ulang dijelaskan terpisah dari snapshot nominal. Tidak ada schema/dependency baru: core0012 dan16 dependency tetap.

Chromium akhir memakai DB/server/MFA sendiri. Edit POST hanya menampilkan tinjauan; perubahan SLA melalui HTTP ketika review terbuka bertahan setelah konfirmasi billing; checkbox wajib, form/review lama409, current issuer pada faktur lama, fingerprint pelanggan/paket/faktur/item/pembayaran/reversal/networkjob tetap. Layout diperiksa360/768/1366/1920 dan dark mode; [desktop](evidence/billing-settings-review-desktop.png) serta [mobile](evidence/billing-settings-review-mobile.png) hanya data fiktif tanpa faktor. Dua percobaan selector awal tidak cocok dengan label wajib yang memuat tanda bintang; selector akhir memakai label aktual. Uji unit awal memperbaiki argumen simulation pada harness PDF dan validasi kontrol sebelum trim. Final suites/targeted/browser di atas memakai versi perbaikan.

Suite awal692 lulus sebelum jalur SLA diperbaiki. Run Docker kandidat awal diinterupsi dengan SIGINT pada PID/cwd/argv verifier sementara yang diverifikasi agar perbaikan SLA masuk image final; finally cleanup selesai, tidak ada direktori/proyek verifier tersisa dan staging pengguna/lab dipertahankan. Run final berurutan sesudah tes berat selesai **exit0 hingga cleanup**. HTTPS nyata membuktikan settings review/CSRF/checkbox/MFA/save/stale form+review409, sambil mempertahankan profil/password/catalog/replay/TLS/SQL privilege/container/worker/monitor/backup guard lama. Restore membandingkan semua model/sequence/binary/ledger/jadwal/outbox, termasuk setelan/identitas yang ditinjau dan paket arsip beserta penugasan lama. Old-session/password/TOTP/CSRF/photo/replay serta gate pending/future migration tanpa repair dan source-loss restore dari pasangan paket/kunci terpisah lulus. Seluruh stack/volume fixture dibersihkan.

Fixture staging_load kini menentukan tax0/due20 secara eksplisit agar nominal workload historis tidak bergantung pada perubahan setelan pada setup HTTPS. Tidak ada pengukuran load baru. Full operations/security browser,17 generic PG process scenarios, same-schema rollback rehearsal, advisory/OS/penetration audit dan hasil CI remote tidak diulang/diklaim pada checkpoint ini. Targeted backend dan browser baru memeriksa jalur SLA yang berubah. Tidak ada klaim reverse migration, independent-host recovery, produksi atau penyelesaian seluruh brief. Kontrak dan batas: [BILLING-SETTINGS](BILLING-SETTINGS.md).

Image `sha256:723ffc72f7afbe2012b1605af336a945cbd38ee8086989375a2cc2b1766664b7`, digest aplikasi751b98a872c26a66fb4b801002151499ce106ab6fe6c9a75029f14c1f5d479f2, diterapkan pada source/clone setelah verifier final lulus. Seluruh model/auth/framework, sequence, recorder eksplisit dan secret host cocok dengan baseline; schema0012 tetap. Source web/db/tiga worker sehat; clone web/db sehat dan recovery_pending/worker hold tetap. Owner persisten tidak diubah atau dienroll MFA. Watcher source menghasilkan tick sehat baru. Backup0012 dan pasangan custody plan-archive-20261118 yang masih cocok dengan backup terpantau dipertahankan/diverifikasi tanpa timestamp palsu atau duplikasi arsip untuk nama checkpoint. Semua image/kunci/arsip lama tetap. Development satu web/tiga worker dimuat ulang sesudah pemeriksaan antrean, tanpa seed/migrasi; health/login/anonymous settings redirect lulus, seluruh baris dan kunci tetap.

Ringkasan agregat dan hash source: [billing-settings-20261119.json](evidence/billing-settings-20261119.json). System/model-drift/pip/diff checks lulus. Masih staging lokal simulasi; produksi dan modul brief yang tersisa belum dinyatakan siap.

## Checkpoint koordinat pelanggan/PSB — 19 November 2026 WIB

**725 tes SQLite dan725 PostgreSQL** lulus tanpa fail/skip/expected-failure, termasuk31 tes baru. Cakupan: finite/tipe/presisi/pasangan/rentang, NULL vs0,0, CHECK dua model, form private/GET tanpa mutasi/CSRF/stale409, aktor/tenant/objek/versi akses/revisi MFA, no-op dan rollback audit, koordinat vs identitas/status tersimpan bersamaan, peran teknisi/portal dan scope ditarik sebelum respons, PSB terminal, event rollback, quote stale/copy aktivasi/replay, dua penulis satu pemenang dan migrasi berisi data/FK/deleted-ID kedua model. Fixture migrasi0012 lama sekarang mengembalikan graph terbaru pada finally. Ketidakcocokan assertion kata empty state pada run awal disesuaikan ke “Koordinat belum diisi”; unknown tidak diubah menjadi titik palsu. Syntax check awal memperbaiki tanda kurung validasi token sebelum pengujian. System/model-drift/pip/diff checks lulus;16 dependency tetap.

Chromium akhir memakai DB/server/data/owner+teknisi/MFA sementara sendiri. Alur pelanggan memeriksa half-pair, simpan,409 form lama, tautan host tetap dan ledger tetap. Teknisi hanya mengubah PSB ditugaskan; PSB lain404 dan editor pelanggan global403. Quote owner sebelum edit lokasi ditolak, quote baru menampilkan koordinat dan aktivasi menyalinnya ke satu pelanggan/faktur/job. PSB terminal terkunci. Tidak ada request eksternal peta;360/768/1366/1920 dan dark mode lulus. Dua screenshot fiktif [desktop](evidence/customer-location-desktop.png) dan [mobile](evidence/customer-location-mobile.png) tanpa QR/faktor. Selector CSRF awal menemukan input logout+form; final memakai satu input yang sesuai.

Image kandidat dibangun dan verifier Docker gabungan **exit0 hingga cleanup**. HTTPS nyata membuat PSB fiktif tanpa pelanggan/faktur baru, menguji lokasi/CSRF/half-pair/stale409 serta link tetap. Seluruh guard billing/profile/password/catalog/TLS/SQL privilege/container/worker/ops/backup lama tetap lulus. Fixture restore mencakup koordinat customer dan PSB; seluruh model/sequence/binary/ledger/schedule/outbox cocok, koordinat terlihat melalui HTML berscope sesudah restore. Old-session/password/TOTP/CSRF/photo/replay, pending leaf0013/future recorder tanpa repair dan source-loss dari paket+kunci terpisah lulus. Semua stack/volume fixture dibersihkan, sumber/clone pengguna dipertahankan.

Development web/tiga worker dihentikan sebelum schema baru. Backup0012 dengan kunci lama diautentikasi/dipulihkan ke direktori baru, salinan dimigrasi0013 dan seluruh nilai lama/sequence/FK diperiksa. Migrasi retained SQLite kemudian lulus pemeriksaan yang sama:30 pelanggan tetap tanpa koordinat buatan,37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto/6 paket/0 schedule-run tetap. Hanya dua kolomNULL per customer/PSB, enam CHECK dan satu recorder/sequence+1 ditambahkan. Backup0013 dipulihkan identik. SQLite constraint rebuild mempertahankan high-water ID terhapus maupun tabel kosong tanpa sequence.

Watcher dan penulis source/clone PG dihentikan, backup0012 masing-masing diautentikasi dengan kunci lama dan baseline stabil ditangkap untuk semua tabel/kolom/hash baris/sequence/recorder/secret host. Image `sha256:55ee60b3f061b1005a90f46928ba243c1703d66e8b8b795509a0d435a58be4a1`, digest aplikasi44df7efa26391fdcf51bc4ba8895c6a3c618f290a78fee7d149a407aed6dac13, dipin dan dimigrasi0013. Semua proyeksi nilai lama, sequence selain recorder+1 dan secret tetap. Source web/db/tiga worker sehat; clone web/db sehat dengan recovery_pending/worker hold tetap. Akun persisten tidak diubah/enroll MFA. Lab memakai volume/credential lama, base schema disamakan tanpa seed.

Backup manual source0013 benar-benar dibuat dan diautentikasi sebelum catatan sukses memakai waktu aktual; interval24jam tetap. Custody baru archives/locations-20261119 dan keys/locations-20261119 diverifikasi; seluruh pasangan/arsip/kunci/image lama dipertahankan. Watcher menghasilkan tick sehat baru. Development web/tiga worker dimulai sesudah data/antrean/kunci diperiksa, health/login/anonymous location redirect lulus dan seluruh baris0013/kunci tetap.

Ringkasan agregat dan hash source: [locations-20261119.json](evidence/locations-20261119.json); kontrak: [LOCATIONS](LOCATIONS.md). Tidak ada reverse migration produksi atau auto-downgrade image setelah0013. Full legacy operations/security browser,17 generic PG process scenarios/load/soak/same-schema rollback/advisory/OS audit tidak diulang. CI dikonfigurasi, hasil remote tidak diklaim. Tidak ada klaim akurasi lokasi, geofencing/scope wilayah, independent-host restore, offsite atau kesiapan produksi.


## Operasional lokal lengkap — 19 November 2026 WIB

Branch kumulatif `feature/tagihnet-local-operations`, schema `0014_local_operations`.
Kas/dana/pengeluaran/transfer/koreksi/tutup buku, tim/payroll/slip sendiri,
Sales/prospek/komisi/kampanye, voucher hotspot simulasi dan topologi ODC/ODP/port
memiliki alur aplikasi yang dapat dijalankan. Kontrak/batas: [LOCAL-OPERATIONS](LOCAL-OPERATIONS.md).
Ringkasan publik tanpa data/kunci: [evidence](evidence/local-operations-20261119.json).

| Pemeriksaan | Hasil yang diamati |
| --- | --- |
| `manage.py test --noinput` SQLite | 771 tes, OK; 46 tes baru sejak checkpoint0013 |
| `postgres_lab.py run manage.py test --noinput` | 771 tes PostgreSQL, OK |
| Pengujian ulang `test_local_business` setelah validator NFC terakhir | 46 SQLite dan46 PostgreSQL, OK; bidi/surrogate/ekspansi normalisasi, alasan audit dan rollback posting+receipt tercakup |
| `local_business_smoke.py` | MFA nyata pada database sementara; enam modul, formulir/tinjauan/konfirmasi, kas/payroll/slip sendiri, sales→PSB/scope, voucher PDF/aktivasi/refund, topologi/port dan promo publik;360/768/1366/1920px/dark tanpa scroll halaman atau error browser |
| `verify_postgres.py` | 17 skenario dua proses, pg_dump/restore dan restore terenkripsi lulus; database lab asli tetap |
| `verify_encrypted_backup.py` | Seluruh18 tabel bisnis berisi fixture, semua tabel/sequence cocok, voucher dapat didekripsi dengan kunci MFA asli, login/TOTP/recovery/photo/ledger pulih; temporary database/keys dibersihkan |
| `verify_staging_backup.py` image final | Exit0 sampai cleanup; HTTPS/SQL TLS/role runtime/privilege/container/host/MFA/CSRF/worker/monitor/backup/custody gates, seluruh tabel, termasuk18 model bisnis berisi fixture, ledger/payroll/slip/komisi/promo/voucher terenkripsi/topologi/tutup buku pulih |
| Source-loss recovery | Paket+custody penerima memulihkan database setelah sumber/volume/arsip/key fixture asli dihapus; seluruh tabel/sequence cocok, password/TOTP/CSRF/photo berfungsi, worker tetap held |
| Salinan0013→0014 SQLite dan PG | Arsip kunci asli dipulihkan ke target baru, migrasi maju dan rekonsiliasi hash baris/kolom/sequence sebelum data yang dipertahankan dimigrasikan |
| Data lokal yang dipertahankan | SQLite/source staging/held clone pada0014; seluruh nilai lama/kunci tetap,18 tabel baru kosong. Tambahan yang diharapkan:18 content type/72 permission/1 recorder dan sequence metadata terkait |
| Gate kandidat aktif dan recovery | Keduanya passed/issues kosong pada image final; backup aktual pascamigrasi terautentikasi, custody baru cocok, watcher fresh/ok, clone tetap held |
| Development startup | Satu web8000 dan tiga worker, health simulation/login/new-route guard lulus, seluruh baris/kunci0014 tetap dan tidak ada seed |
| Check/drift/dependencies/diff | `check`, `makemigrations --check --dry-run`, `pip check`, `git diff --check` lulus; dependency tetap |

Image final `sha256:d443b72d46605e1bc528509997ece72bc73d7624d1064bd90d71663af977def6`;
application tree `44db2c9c171f45818ca7e5c744db546174c819fe730c76bf76472bf6cf64c7b3`.
Source dibekukan selama run final. Satu run sebelumnya menolak `source_mismatch`
ketika perbaikan audit/input ditambahkan sesudah image dibangun; image dibangun
ulang dan run final lulus. Harness proses shared-UUID kini rendezvous sebelum
kunci tenant, mengikuti serialisasi pembayaran/keputusan kas; barrier lama di
posisi insert menyebabkan timeout pada harness dan telah diperbaiki.

Source tetap1 ISP/1 owner staging tanpa MFA/plan/customer/invoice/PSB atau data bisnis
baru. Default tetap30 pelanggan/37 faktur/19 pembayaran, tanpa perubahan koordinat,
NAS, MFA, akun atau job lama. Backup pra0014 dan seluruh arsip lama tetap disimpan.
Backup source aktual `TagihNet-20261119-post-local-operations-0014.tnbk` memakai
kunci lama; waktu sukses manual dicatat sesuai operasi nyata. Paket baru
`local-operations-20261119` mempunyai folder arsip/kunci terpisah. Ini masih satu
workspace, bukan custody offsite. Clone tetap memakai sandi arsip/MFA asli dan
session/TLS/DB key targetnya; bootstrap-password clone tidak digunakan.

Bukti privat berawalan `data/evidence/local-operations-`: before-0014,
sqlite-proof-0014, development-0014/development-baseline-0014,
pg-stopped-before-0014/pg-restored-proof-0014/pg-after-0014,
monitor-0014/development-running-0014. Gate setelah commit bersih disimpan sebagai
`local-operations-preflight-active-20261119.json` dan
`local-operations-preflight-recovery-20261119.json`. File data/kunci/baseline privat
tersebut tidak masuk Git; jalankan ulang dengan nama output baru.

Ruang lingkup tetap simulasi lokal. Tidak ada gateway/webhook settlement/router/
RADIUS/SMTP nyata, VPS/domain/public TLS, offsite/retensi/PITR, uji kapasitas/soak
baru atau audit keamanan independen. Hasil CI remote belum diperiksa. Image0013
bukan rollback otomatis bagi schema0014; pemulihan memerlukan target baru dengan
arsip/image kompatibel dan rekonsiliasi pascasnapshot. Cakupan lanjut tetap pada
[FEATURE-MATRIX](FEATURE-MATRIX.md), tanpa persentase kesiapan produksi.


## Navigasi workspace dan drawer — checkpoint 19 November 2026 WIB

Branch `feature/tagihnet-workspace-navigation`, schema tetap0014. Tidak ada model,
migrasi, dependency atau aturan bisnis baru. Kelompok menu native/berizin,
pencarian grup tertutup, preferensi opsional, fokus menu mobile, dan drawer
pelanggan baca saja kini tersedia. Ringkasan publik dan gambar fiktif:
[WORKSPACE-UX](WORKSPACE-UX.md), [evidence](evidence/workspace-ux-20261119.json).

781 tes SQLite/781 PostgreSQL lulus, termasuk10 tambahan untuk pemilihan grup/current
page, scope role/custom role/tenant, parameter spoofing, escaping, cache, GET/POST
dan preservasi isi model. Assertion lama current-page pada tab pelanggan dibatasi
ke nav tab karena sidebar kini juga mengumumkan halaman aktif secara aksesibel.
Browser nyata memakai database/server sementara dan MFA fiktif: persistensi/grup
pencarian, fokus mobile, dialog Tab/ShiftTab/Escape/backdrop/focus return, respons
terlambat/error/sesi berakhir, link detail/URL kembali,4 viewport/dark/storage
terblokir/tanpa JavaScript. Ledger fixture tidak berubah. Perilaku ShiftTab awal
meninggalkan dialog diperbaiki lewat loop fokus eksplisit; final run lulus.
Browser enam modul bisnis juga lulus pada shell baru. Gambar diperiksa langsung;
header/footer dialog tetap terlihat dan kontennya bergulir di dalam panel.

Image final `sha256:e68410aa9b2219f928d79949f669bab6bbb088d21eb67a91c1121c83bf785800`,
application digest `7b605b7033bb8e40247c098172de2d945826f709a4fc21b4b752f4ee80cf44c2`.
Source dibekukan saat verifier HTTPS/backup/source-loss dijalankan sampai exit0 dan
cleanup. Pengujian lama TLS/role/container/password/MFA/CSRF/monitor/backup serta
seluruh fixture18 tabel bisnis tetap lulus; paket penerima pulih setelah sumber
fiktif/volume/arsip/key asli dihapus, dengan worker held.

Source dan clone yang dipertahankan diperbarui pada schema sama; hash seluruh
model, semua sequence, MigrationRecorder serta seluruh secret file cocok sebelum/
sesudah image switch. Clone tetap web/DB saja, recovery_pendingTrue, backupOFF.
Default cocok baseline baris/sequence/kunci yang ditangkap pada awal pekerjaan,
lalu satu web/tiga worker dimulai tanpa seed. Tidak ada akun pengguna yang
dienroll/reset untuk tes. Backup pra/pasca UI terautentikasi memakai kunci lama;
waktu sukses dicatat nyata dan pasangan custody baru `workspace-ux-20261119`
dipisahkan dari arsip. Semua image/arsip/custody lama dipertahankan.

## Identitas penerbit dokumen — 20 November 2026 WIB

Branch `feature/tagihnet-document-issuers`, schema `0015_document_issuers`.
Faktur baru menyimpan nama/alamat ISP saat diterbitkan; pembayaran baru menyimpan
identitasnya sendiri saat dicatat. HTML/PDF cetak ulang memakai snapshot masing-masing.
Dokumen lama tanpa snapshot menampilkan profil terkini dengan penjelasan riwayat
yang belum tersimpan. Tidak ada backfill dari migrasi, GET, replay atau reversal.
Snapshot nominal/footer/jatuh tempo batch yang disetujui tetap; identitas penerbit
diambil saat penerbitan aktual melalui writer bersama satuan/batch/scheduler/PSB.
Kontrak dan bukti visual: [DOCUMENT-ISSUERS](DOCUMENT-ISSUERS.md).

792 tes SQLite dan 792 PostgreSQL lulus, termasuk 11 tambahan untuk identitas pada
waktu faktur/pembayaran yang berbeda, HTML/PDF historis/legacy, GET tanpa perubahan
bisnis, replay tanpa backfill, snapshot approved money dengan tenant cache lama,
scope tenant, escaping serta rollback kegagalan snapshot/audit. Targeted 74 lulus.
Uji race yang ada juga memastikan tepat satu snapshot untuk faktur/pembayaran
serentak. Verifier PostgreSQL terpisah lulus 17 skenario lintas proses serta
pg_dump/pg_restore biasa dan terenkripsi, dengan database asli lab tidak berubah.

Chromium dengan database/server/kunci/MFA fiktif sementara memeriksa identitas
faktur A, kuitansi B dan profil C, empat lebar 360/768/1366/1920, penanda legacy,
HTML/PDF serta hash baris bisnis tetap. Browser setelan billing lulus tinjauan,
MFA, konflik form/review lama, preservasi SLA dan identitas dokumen setelah profil
berubah. Verifier PDF standalone tetap tanpa Django/database: A4 satu halaman
normal dan empat halaman reversal/stress, marker akhir teks terbaca.

Drill backup SQLite memulihkan seluruh tabel/sequence, dua tabel issuer nonkosong,
18 tabel bisnis nonkosong, MFA, foto dan ledger. Verifier Docker lengkap lulus
HTTPS/SQL TLS/role runtime/security/worker restart/heartbeat/backup+custody gates,
restore SQL dan login password/TOTP/CSRF/foto, serta paket portable setelah hanya
sumber fiktif beserta volume/arsip/kuncinya dihapus. Identitas dan waktu snapshot
serta PDF dibaca ulang dari database hasil restore. Seluruh stack sementara
dibersihkan; akun pemilik persisten tidak diubah atau dienroll.

Gate kandidat aktif/recovery lulus/issues kosong. Bukti privat berawalan
`data/evidence/workspace-ux-`: development-before/after, staging-before,
staging-recovery-20261115-before, staging-rollout, custody dan kandidat gates.
Gate setelah commit bersih memakai `workspace-ux-preflight-active-20261119.json`
dan `workspace-ux-preflight-recovery-20261119.json`, tanpa overwrite bukti lama.
Check/model drift/pip/diff lulus.17 skenario proses PostgreSQL sebelumnya tetap
histori; tidak dijalankan ulang untuk perubahan UI ini. Hasil CI remote, load/soak
baru, audit keamanan/WCAG independen atau deployment produksi tidak diklaim.


Migrasi schema0014 diuji terlebih dahulu dari arsip asli SQLite dan PostgreSQL
dengan kunci yang sudah ada, ke target baru yang terpisah. Setelah bukti salinan
lulus, database pengembangan, source staging dan clone yang dipertahankan dinaikkan
ke schema0015. Hash seluruh baris/kolom bisnis lama tetap; sequence lama tetap
kecuali metadata Django yang tepat: dua content type, delapan permission dan satu
migration recorder. Kedua tabel issuer baru kosong pada data yang dipertahankan.
Kunci sesi/MFA/TLS/database/bootstrap/backup asli tetap. Manifest proyek hanya
berubah pada image; clone tetap recovery_pending, web/DB saja, backup OFF.

Backup source aktual `TagihNet-20261120-post-document-issuers-0015.tnbk`
diautentikasi dengan kunci asli, dicatat dengan waktu keberhasilan nyata, dan
diekspor ke pasangan custody baru `document-issuers-20261120`. Watcher source
kembali segar/ok dengan backup opt-in tetap. Default satu web/tiga worker sehat,
baris/sequence/kunci pascamigrasi tetap dan tidak ada seed. Backup SQLite0015
dipulihkan identik ke salinan baru. Semua arsip, proof, custody dan image lama
dipertahankan; folder terpisah di workspace ini belum merupakan offsite.

Image lokal: `sha256:fcc69e39928afc614cdd385caab9fcc9d0400888d39e48af565aec383a885438`.
Digest aplikasi: `f292cbd0e326f33a9cad90ad5315b955b3b733d1534d3784b17bd2777c792767`.
Gate active/recovery lulus terhadap image dan isi aplikasi tersebut. Ringkasan
publik: [document-issuers-20261120.json](evidence/document-issuers-20261120.json).
Bukti privat memakai awalan `data/evidence/document-issuers-`: baseline sebelum
0014, proof salinan SQLite/PG, rekonsiliasi setelah0015, restart, monitor, serta
preflight active/recovery. Gate checkout bersih setelah commit disimpan sebagai
`document-issuers-preflight-active-20261120.json` dan
`document-issuers-preflight-recovery-20261120.json`; berkas candidate dipertahankan.

Pip/model drift/system/diff checks lulus; tidak ada dependency runtime baru.
Hasil ini untuk simulasi lokal. VPS/domain, gateway dan router nyata, SMTP,
custody offsite/retensi/PITR, kapasitas/soak serta audit independen masih tertunda.
Schema0015 tidak boleh diturunkan otomatis dengan image lama; gunakan pemulihan
arsip kompatibel ke target baru dan rekonsiliasi pascasnapshot.
