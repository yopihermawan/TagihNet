# Audit keamanan TagihNet — 14 November 2026

Lingkungan: lokal Linux, Python 3.12.14, Django 5.2.18, SQLite, Chromium; semua data fiktif. Audit hanya pada kode TagihNet yang ditulis untuk checkpoint ini. Observasi KelolaNet dalam brief adalah referensi UI, bukan temuan teknis terkonfirmasi. Checkpoint kini mencakup MFA/editor akses dan penerbitan massal dengan persetujuan snapshot. Tidak ada sertifikasi ASVS atau klaim production-ready.

## Model ancaman dan batas kepercayaan

Aset utama: identitas pelanggan, ledger rupiah, sesi operator, PDF/CSV, pengaturan billing, serta antrean perubahan layanan. Ancaman prioritas: pembacaan lintas ISP/IDOR, kasir menaikkan privilege, manipulasi nominal browser, payment replay, XSS footer, CSV formula injection, serta indikator router palsu. Internet, input browser, dan ID pada URL tidak dipercaya. Administrator database/host masih berada dalam batas kepercayaan. Unggahan mencakup CSV pelanggan terbatasi dan foto JPG/PNG pekerjaan yang diverifikasi/dinormalisasi serta disimpan sebagai binary privat. Tidak ada koneksi router/gateway atau URL-fetch API. Lampiran umum selain foto belum diterima.

## Kontrol yang diverifikasi

| Risiko / lokasi | Reproduksi aman / hasil | Status |
| --- | --- | --- |
| IDOR tenant/customer · `views.allowed_invoice`, query tenant | Akun tenant A meminta ID/detail/edit/PDF/payment tenant B; 404, tidak ada perubahan. Portal juga menolak faktur pelanggan lain di tenant sama | pass · tes HTTP |
| Eskalasi kasir/NOC · `permissions.py` | POST langsung issue/settings/reversal sebagai kasir ditolak 403; role diubah saat sesi aktif langsung berlaku | pass |
| Nominal/status palsu · `billing.totals`, `issue_invoice`, `record_payment` | Kirim `total=1`, `status=paid`, tenant palsu; snapshot harga server tetap benar. Negatif/nol/lebih bayar ditolak | pass |
| Replay / race · constraint model + transaksi/retry | Dua thread issue periode sama dan payment event sama menghasilkan satu record | pass · SQLite lokal, belum multiworker produksi |
| CSRF / cookie auth | POST tanpa token untuk pembayaran/settings/logout ditolak 403 | pass |
| Login/session/reset | Throttle persisten, generic reset, token sekali pakai, rotasi session login, idle/absolute expiry, reset mencabut sesi lama | pass |
| Footer XSS · template/PDF | Markup uji tampil sebagai teks ber-encoding; ReportLab tidak menggunakan renderer HTML/file/network | pass · web/PDF lokal |
| CSV formula · `customer_export` | Nama `=1+1` diberi prefix apostrof pada ekspor yang berizin | pass |
| Router gagal / isolir vs bayar · `network.run_job` | Pembayaran bertahan saat adapter gagal; job cek ulang utang, hasil pending/failed/confirmed terpisah; dua urutan race direkonsiliasi | simulated |
| CSP dan cache · middleware | `default-src self`, tanpa unsafe-inline, frame/object off, nosniff, no-store halaman terautentikasi | pass · HTTP lokal |
| Dependensi terkunci | `pip-audit 2.10.1` via PyPI terhadap sebelas paket runtime di requirements.txt: no known vulnerabilities found | pass · sesuai basis advisory saat eksekusi; bukan jaminan bebas bug |

## Temuan yang diperbaiki selama implementasi

| ID / severity lokal | Bukti, dampak, perbaikan, retest |
| --- | --- |
| TGN-001 · sedang / availability | Eksekusi pertama settings mencoba chmod pada file object, sehingga bootstrap gagal dan file key kosong tertinggal. Diganti pembuatan file eksklusif mode 0600 melalui `os.open`, validasi panjang kunci; artefak kosong milik eksekusi ini dihapus. Migrasi/seed/check berhasil sesudahnya. Tidak ada deployment produksi terdampak |
| TGN-002 · rendah / integritas demo | Seed awal bergantung pada nama ISP yang dapat diubah. Diperbaiki dengan mengambil tenant dari membership owner yang ada dan menolak konflik akun. Uji seed ulang setelah rename menjaga jumlah record dan password |
| TGN-003 · rendah / aksesibilitas | Sidebar mobile yang bergeser keluar layar masih dapat menerima fokus. Ditambah `inert` saat tertutup; dibuka melalui tombol, Escape mengembalikan fokus. Browser retest mengikuti perbaikan |

## Risiko terbuka / gate sebelum produksi

| ID | Severity kontekstual | Bukti / tindakan |
| --- | --- | --- |
| TGN-P01 · resolved lokal | Retest pass | MFA operator, enrollment, recovery, step-up, replay dan session revocation diuji. Audit independen dan deployment masih belum |
| TGN-P02 | Tinggi bila data nyata dipakai | Arsip AES-GCM tersedia lokal dengan kunci terpisah; mode kompatibilitas/arsip lama masih plaintext. Audit bisa diubah admin DB; vault/retensi/monitoring/offsite dan restore target produksi belum diverifikasi |
| TGN-P03 | Sedang | SQLite tidak memberi row lock `select_for_update`; PostgreSQL kini diuji dengan suite dan dua proses pada lab. Throughput, load/queue terdistribusi dan konfigurasi produksi tetap harus diverifikasi |
| TGN-P04 | Sedang | Count-then-insert telah diganti update counter bersyarat atomic dan diuji paralel. Risiko tersisa: batas lima percobaan/IP dapat memengaruhi operator di NAT sama; uji beban/edge produksi belum |
| TGN-P05 | Sedang | Akun demo lokal tersedia dengan password yang didokumentasikan; seed dibatasi development. Dilarang mempublikasikan database seed sebagai layanan produksi |
| TGN-P06 | Belum dinilai | TLS end-to-end, SMTP produksi, provisioning/rotation secret, Docker runtime, penetrasi aplikasi, dan beban belum diuji. Tidak ada klaim lulus untuk kontrol tersebut |

Gateway/webhook signature, SSRF pada router, kredensial RADIUS, lampiran umum selain raster/malware scanner, dan autentikasi realtime: **belum applicable pada kode aktif**, karena endpoint/adapternya tidak tersedia. Kontrol tersebut wajib dievaluasi ketika modul ditambahkan; jangan mengartikan ketiadaan implementasi sebagai lulus integrasi.

Audit ini tidak memperbaiki seluruh target ASVS L2. [ASVS-CHECKLIST](ASVS-CHECKLIST.md) adalah daftar subset kontrol yang relevan dan status bukti, bukan checklist lengkap standar.

## Verifikasi checkpoint akses

66 tes backend mencakup MFA terenkripsi, OTP/recovery replay paralel, expiry pending login, revocation di antara faktor dan pembuatan session, limiter atomic, role tenant, direct PDF, step-up, dan last-owner race. Browser terisolasi menguji QR enrollment, custom role assignment, pencabutan sesi, regenerasi recovery dan penolakan kode lama; tidak ada QR/secret/kode disimpan ke bukti visual. Scope teknisi/wilayah, WebAuthn, dan rotasi key enkripsi terotomasi belum tersedia.

Audit dependency awal checkpoint ini menemukan advisory `PYSEC-2026-3552` pada cryptography 49.0.0. Paket diperbarui menjadi 50.0.0 sebelum commit; retest suite/browser dan pemindaian runtime dilakukan pada versi perbaikan. Advisory yang terduplikasi dalam output awal dihitung sebagai satu identitas advisory, bukan dua temuan independen.

## Retest penerbitan massal

95 tes total dan browser lokal memverifikasi endpoint bulk_issue terpisah, scope pembuat+tenant, POST/CSRF, review checkbox, MFA terbaru dan safe return sesudah step-up, snapshot server-side, deteksi perubahan sebelum konfirmasi, worker authorization version, rollback savepoint, cancellation serta concurrency/deduplikasi. Konfirmasi hanya mengantrekan; GET/preview tidak menerbitkan. Nominal/tenant/pelanggan dari POST konfirmasi tidak dipakai. Tidak ada renderer HTML baru atau akses jaringan eksternal. Bulk audit menggunakan correlation ID batch.

Snapshot yang telah disetujui tetap berlaku bila setelan berubah sesudah konfirmasi; kebijakan ini ditampilkan dan diuji. Data yang berubah sebelum konfirmasi mewajibkan pratinjau baru. Risiko yang tetap terbuka: database administrator dapat memodifikasi snapshot/audit, SQLite hanya diverifikasi lokal dengan thread, belum ada load test/admission control global atau PostgreSQL lintas proses. Batas 500 pelanggan per job dan 1–500 baris per putaran bukan jaminan kapasitas produksi.

## Retest tiket/PSB dan assignment scope

129 tes total dan browser terisolasi memverifikasi scope tenant/customer/assigned-technician, filtrasi komentar internal sebelum pagination, plaintext encoding, state machine+optimistic version+request UUID, serta fail-closed 404 bila assignment berubah selama request. Preset teknisi tidak dapat menerima custom role global. Pembuatan tiket customer mengambil customer dari membership, memaksakan prioritas normal, dan mengabaikan assignee/priority/customer palsu. Flag internal eksplisit ditolak untuk pelanggan.

Aktivasi memerlukan activate_installations, MFA terbaru, signed quote terikat actor/tenant/PSB/periode/identitas/harga/setelan/versi dan expiry. Transaksi membuat customer/invoice/job bersama; replay dan race menghasilkan satu set, kegagalan invoice rollback customer. Provisioning pending terpisah dan kegagalan router tidak dipalsukan aktif. Pada checkpoint tiket/PSB awal, endpoint lampiran belum ditambahkan; foto privat tersedia sejak checkpoint14 November. Adapter produksi tetap belum tersedia. Batas terbuka tetap database/admin trust, SQLite thread-only, belum ada deployment/performance/pen-test/ASVS lengkap.

## Pengingat opt-in dan perubahan PSB

162 tes total memverifikasi capability reminders dengan prasyarat tickets/invoices/customers, scope tenant, CSRF/review/MFA terbaru dan versi kebijakan. Planner dan worker ditolak saat SIMULATION=0. Adapter tidak melakukan I/O; status sukses disebut Tercatat simulasi. Worker memeriksa pelunasan/resolusi/deadline/siklus terbaru dan hak/versi pemberi persetujuan. Perubahan akses menahan antrean; persetujuan baru hanya melanjutkan blocked, tidak mengulang receipt sukses/terminal. Deduplikasi disimpan dalam constraint, race lokal diuji. Audit hanya metadata ID/status/alasan, tanpa isi reminder.

Edit/reschedule PSB memakai installations; teknisi tidak boleh melakukannya. Semua mutasi berscope tenant, versi dan UUID; alasan serta before/after plaintext disimpan internal. Perubahan scheduled membatalkan jadwal dan kembali survey; setelah installing identitas/paket terkunci. Tidak ada mutasi finansial dari edit/reschedule.

Isi receipt mengandung informasi billing/operasional; akses terbatas pada pengelola dengan seluruh prasyarat. Retensi/enkripsi database, audit anti-tamper dan penyerahan pesan nyata belum tersedia. Tidak ada klaim exactly-once eksternal atau database multi-process. Pengujian browser terisolasi memverifikasi dedupe, receipt, pembayaran sebelum proses dan pembatasan akses tanpa faktor MFA pada screenshot.

## Monitoring NAS simulasi

187 tes total dan browser terisolasi memverifikasi tenant/object scope, NOC read/collect vs konfigurasi terpisah, dependensi custom role, MFA/CSRF/review/version dan pencabutan persetujuan saat akses berubah. Kolektor memakai unique device/version/minute dan transaksi/retry; dua collector lokal menghasilkan satu snapshot. Konfigurasi baru mengecualikan snapshot lama. Kegagalan/stale tidak dipalsukan menjadi nol atau data segar.

Tidak ada URL/IP/port/password/secret input pada konfigurasi. Adapter tidak menggunakan I/O jaringan, sehingga belum menambah permukaan SSRF router nyata. Data sesi sintetis dibatasi 100, field/status/durasi/duplikat/timestamp divalidasi sebelum disimpan; error adapter tidak menyalin detail exception. Histori tidak memiliki endpoint edit/delete; admin database tetap dipercaya. Audit tidak mencatat daftar username sesi. Retensi otomatis, enkripsi backup, multi-process PostgreSQL dan adapter perangkat nyata masih terbuka.

## PostgreSQL dan temuan transaksi

Verifikasi memakai PostgreSQL 16.15 lokal, role aplikasi non-superuser/CREATEDB, data fiktif dalam database disposable, dan dua proses OS per skenario. 195 tes lulus pada masing-masing backend. Temuan TGN-004: penggunaan UUID pembayaran yang sama bersamaan pada dua invoice dapat lolos pre-read lalu memicu IntegrityError unique (23505), alih-alih validasi biasa. Ledger tetap dilindungi constraint; perbaikan memakai get_or_create/savepoint dan pemeriksaan payload pemenang. Retest: satu diterima, satu ValidationError, satu Payment/job.

Temuan TGN-005: worker bulk ikut mengunci joined actor/tenant, menyebabkan satu deadlock terukur pada overlap dengan penerbitan satuan. Scope FOR UPDATE dibatasi ke self; NO KEY UPDATE pada konfigurasi tenant mempertahankan serialisasi tanpa menghalangi FK insert biasa. Retest skenario domain menunjukkan nol deadlock. Fixture deadlock terpisah sengaja menguji retry SQLSTATE dan rollback; berhasil satu retry tanpa audit ganda. Retry tetap maksimal lima dan tidak mengulang error koneksi/SQL lain secara umum.

Konfigurasi PG eksplisit, tidak fallback diam-diam ke SQLite; password variable/file privat, TLS verify-full default dan disable ditolak pada DEBUG=0. Lab hanya loopback dengan secret acak 0600; credential CI fiktif hanya untuk service ephemeral. Parameter timeout dibatasi. Kedua browser smoke tetap memakai SQLite disposable walaupun environment induknya PG; skrip backup SQLite menolak backend PG. Pemindaian 14 paket Python base+PG tidak menemukan advisory saat scan. Kontrol load/capacity, TLS remote, pooling/failover, runtime role least-privilege produksi, backup terenkripsi/offsite dan cutover belum terverifikasi.

## Impor CSV dan username case-insensitive

225 tes pada kedua backend, dua race proses impor tambahan, serta browser memverifikasi upload CSV <=128 KiB/200 record, UTF-8/header ketat dan kontrol karakter; extension diperiksa, MIME tidak dipercaya sebagai validasi isi. Tidak ada lokasi/filename storage yang dipilih pengguna. Byte asli tidak diarsipkan; hanya kolom yang dikenal (dibatasi untuk row invalid) dan snapshot/error privat di database. Kolom tenant, status, password dan nominal tidak diterima. Preview/konfirmasi/report memerlukan capability+creator+tenant; konfirmasi memakai CSRF/review/MFA/current access version, fingerprint, expiry dan revalidasi server.

Semua baris harus valid, konflik saat penyimpanan rollback seluruh impor. Constraint username LOWER per tenant mencegah benturan case di seluruh jalur penambahan; preflight migrasi berhenti jika legacy collision ditemukan tanpa koreksi otomatis. GET tidak membuat customer; replay tidak menggandakan. Hasil pending tidak memalsukan router aktif dan tidak menambah invoice, akun portal atau job. Laporan CSV meng-escape spreadsheet formula; web memakai autoescape. Audit hanya jumlah/status/alasan, tidak mencatat isi CSV/telepon/nama baris.

Retensi batch/PII dan enkripsi database/backup belum otomatis. Tidak ada dukungan Excel/XLSX, lampiran selain JPG/PNG, merge/update pelanggan atau importer nominal ledger. Source CSV tetap input tidak dipercaya dan data admin database masih batas kepercayaan. Tidak ada perluasan klaim menjadi seluruh ASVS L2 atau kesiapan produksi.

## Backup AES-256-GCM dan restore

245 tes pada dua backend dan drill SQLite/PG memverifikasi AEAD streaming standar cryptography, nonce96-bit acak/key256-bit, metadata sebagai AAD, header/size/tipe ketat, no-clobber dan verifikasi tag sebelum publikasi SQLite atau koneksi restore PG. Key ID saja tidak mengautentikasi material kunci salah. Invalid archive/key tidak menimpa DB; PG target terisi/sumber/sistem/role bukan owner ditolak dan pg_restore tidak memakai --clean. Advisory marker mengikat tools container ke endpoint yang dikonfigurasi; helper restore memakai advisory lock untuk serialisasi antarhelper. Tidak ada endpoint web/global backup bypass.

Snapshot/payload plaintext hanya staging privat0600/0700 dan normal cleanup diuji; crash artifacts/secure erase tidak dijamin. Host admin adalah batas kepercayaan; dump dari pemegang kunci dipercaya dan bukan sandbox SQL. Kunci arsip terpisah dari Fernet MFA dan sesi, tidak disertakan dalam archive. Drill menguji MFA setelah restore dengan keys terpisah, password/TOTP/CSRF serta penolakan replay. Tidak ada key/factor/user content di stdout/argv; untuk local PG password berada pada child environment, bukan argv. Kunci development serta archive pada workspace bukan vault/offsite. Legacy plaintext dipertahankan untuk kompatibilitas; retensi, produksi TLS, backup offsite/PITR dan audit anti-tamper tetap terbuka.

Replay drill membuktikan penolakan di database hasil restore. Snapshot lama dapat memulihkan state akses/counter/recovery sebelum perubahan pascabackup; rekonsiliasi transaksi, pencabutan sesi dan tinjauan akses/recovery tetap diperlukan sebelum cutover. Tidak ada klaim replay protection lintas rollback snapshot.


## Foto pekerjaan — 14 November 2026 WIB

Kontrol binary foto kini applicable dan diuji: PNG/JPEG decode penuh, ekstensi/format/frame/piksel/metadata limit, re-encode tanpa EXIF dan nama asli, transport single-file2MiB di memori, cleanup abort, kuota/replay serial, pemisahan internal/portal/assignment dan unduhan auth+checksum+attachment+sandbox+nosniff+no-store. Content disimpan dalam database dan dipulihkan melalui backup terenkripsi dengan pembandingan binary; tidak ada folder eksekusi/media publik. CSP middleware mempertahankan kebijakan respons unduhan yang lebih ketat.30 tes baru/382 total per backend dan15 skenario PG lulus. Data demo tidak diisi fixture.

Audit PyPI version JSON14 paket runtime/PG pada14 November mencantumkan0 advisory aktif; bukan audit OS/native/dev atau bukti bebas malware. Original gambar tidak diarsipkan (hanya hasil normalisasi dan digest sumber), tidak ada penghapusan/retensi/vault otomatis, storage eksternal atau klaim kapasitas produksi. Catatan endpoint lampiran yang belum tersedia pada checkpoint bertanggal sebelumnya bersifat historis. Lihat [WORK-PHOTOS](WORK-PHOTOS.md).

## Kuitansi — 14 November 2026 WIB

Endpoint baca HTML/PDF memerlukan login dan scope faktur saat request. Payment juga dicocokkan dengan invoice/tenant pada URL. Portal dibatasi pelanggan yang tertaut; ID swap lintas ISP/faktur/pelanggan, NOC/teknisi dan sesi dicabut/user nonaktif diuji. Tidak ada token publik, endpoint gateway atau mutasi ledger pada cetak. PDF hanya teks ter-escape dan font Vera bawaan ReportLab yang disematkan; input markup/URL tetap teks. HTML autoescape; respons private/no-store/nosniff. Middleware cache kini menambahkan no-store sambil mempertahankan directive private dari view.

31 tes kuitansi menambah total menjadi413 per backend. Pembayaran sebagian tetap satu transaksi; reversal menampilkan status terbaru/nominal asli dan label di tiap halaman PDF. Label simulasi tidak tergantung toggle konfigurasi setelah transaksi. Snapshot pelanggan berasal dari faktur, sedangkan identitas penerbit/akun pencatat dari data saat cetak, dijelaskan di dokumen. Catatan pembayaran tampil pada kuitansi termasuk portal dan form menjelaskannya. Dokumen tersimpan lama tidak bisa dicabut/diperbarui; tidak ada tanda tangan, verifikasi publik, snapshot kuitansi fiskal atau klaim settlement. Lihat [PAYMENT-RECEIPTS](PAYMENT-RECEIPTS.md).

## Jadwal bulanan — 14 November 2026 WIB

Opt-in memerlukan capability baru dengan dependensi billing, signed review/MFA/CSRF/versi actor+tenant+config, fingerprint contoh dan replay guard. Generasi memakai lock tenant/jadwal + constraint satu cycle/month, snapshot/batch/item/audit atomik, maksimal500 dan semua invalid menahan seluruh generasi. Worker memeriksa revisi MFA/access/useractive/scope pada generasi dan tiap baris. Perubahan/nonaktif membatalkan sisa batch otomatis, bukan ledger lama. SIMULATIONfalse menolak pengaktifan/generasi dan menahan queue otomatis; tidak ada gateway/router/message I/O. Calendar WIB/clamp serta current-month-only dijelaskan bersama consent dinamis. Jadwal/riwayat tenant-scoped dan rincian batch creator-private tetap.31 tes baru/444 per backend,17 skenario proses dan restore schema0011 lulus. Tidak ada klaim scheduler produksi/failover/backfill/capacity; lihat [BILLING-SCHEDULER](BILLING-SCHEDULER.md).

## Staging lokal — 15 November 2026 WIB

Profil config.staging_settings memvalidasi konfigurasi sebelum base settings: DEBUG0/SIMULATION1/PG verify-full/CA/host allowlist/cookie Secure, file secret eksklusif/bounded tanpa mutasi env. Gunicorn TLS native mengabaikan forwarded scheme/host, WhiteNoise hashed assets, container app non-root/read-only/no-new-privileges/cap-drop dan secret runtime/migration terpisah. Runtime PG tidak dapat DDL/CREATEDB/CREATEROLE/superuser; jaringan plaintext serta CA/hostname salah ditolak. Bootstrap hanya DB kosong, password file/validator, audit atomic/advisory lock/MFA tanpa superuser/demo account.455 tes per backend dan container TLS/login/MFA/CSRF/DDL/restart/log secrecy lulus. Guard initialized-volume mencegah silent replacement setelah snapshot kehilangan volume. HSTS1jam host lokal menampilkan warning W005/W021 yang dijelaskan, tanpa suppression. Pengiriman email dummy; real integrations tetap disabled. Dua dependency staging baru metadata advisory0, bukan audit OS/image. VPS/domain/SMTP/load/offsite/DR/independent pen-test tetap belum diverifikasi. Lihat [STAGING](STAGING.md).

## Recovery staging — 15 November 2026 WIB

GCM/type validation selesai sebelum Docker/target/key changes. Container/project/service/image/volume, migrator owner dan target catalog empty diperiksa; default ACL provisioning yang sah diizinkan. Restore hanya fresh prepared project tanpa existing volume dan memakai single transaction/no-clean. Shared filesystem lock menserialkan restore/up/resume; metadata atomic/fsync menjaga fail-closed preparing/failed state. Original MFA key diperlukan dan semua cipher device diuji; session key target tetap baru sehingga old cookies tidak berlaku. Worker otomatis ditahan sampai explicit host reconciliation/resume. Dump dianggap SQL terpercaya dari pemegang archive key, bukan sandbox.16 tes baru/471 per backend dan17 proses PG lulus; container restore membandingkan semua table/sequence/ledger/photo/MFA, HTTP auth/replay/CSRF/download, error guards dan source unchanged. Tidak ada klaim replay lintas snapshot rollback/offsite/PITR/RPO/RTO. [STAGING-RECOVERY](STAGING-RECOVERY.md).

## Operasi staging — 15 November 2026 WIB

Pemantauan/backup berkala berada pada host administrator; socket Docker tidak dipasang pada aplikasi. Heartbeat hanya metadata loop pada tmpfs privat, ops file0600/direktori baru0700, laporan/log tidak memuat payload pelanggan atau diagnostic exception. HTTPS tetap memverifikasi CA/hostname; query metrik memakai role runtime, transaksi READ ONLY, statement/lock timeout5detik dan batas proses15detik. Ketidaktersediaan metrik/status lama dilaporkan unknown, bukan nol/sehat. Lock mencegah backup paralel; PID/starttime/argv/token/proyek diperiksa sebelum menandai/menghentikan watcher. Backup opt-in diautentikasi sebelum sukses dicatat, recovery hold tetap berlaku. SIGSTOP loop/container pause/missing archive dan lifecycle daemon diuji pada fixture terpisah. Ini bukan alert eksternal, sandbox host admin, pengawasan host independen atau audit dependensi/OS/image baru. [STAGING-OPERATIONS](STAGING-OPERATIONS.md).

## Beban dan query baca — 15 November 2026 WIB

Harness tidak menerima endpoint/direktori staging pengguna, hanya membuat target sementara/loopback baru. Concurrency/durasi/response size dibatasi, TLS CA/hostname tetap diverifikasi, redirect login dianggap gagal, dan cookie/OTP/body tidak masuk laporan. Input laporan existing/symlink ditolak tanpa provisioning. Worker/load/canary/saldo/replay serta cleanup diuji pada data fiktif. Optimasi saldo adalah query tenant-scoped baca, bukan cache pada model yang dipakai mutasi uang; batas query, reversal/status/tenant dan browser regresi lulus. P95/resource yang teramati tidak menetapkan kapasitas atau ketersediaan produksi. [STAGING-LOAD](STAGING-LOAD.md).

## Paket pemulihan — 15 November 2026 WIB

Manifest metadata v1 memakai HMAC-SHA256 dengan HKDF key terpisah tujuan dari archive key, salt UUID paket dan domain info tetap; canonical JSON tanpa mac, compare_digest, exact fields/bounds/duplicate rejection. Manifest mengikat hash/ukuran ciphertext dan fingerprint MFA; seluruh GCM/type/PGDMP tetap wajib. Direktori/key terpisah/private, file reguler/no-follow/size-bounded, output no-clobber dan manifest terakhir. Restore membekukan ciphertext/MFA sebelum operasi target sehingga tidak reread key input yang berubah; normal cleanup diuji. Provenance HEAD exporter bukan bukti kompatibilitas dump. HMAC bukan identitas pihak ketiga, holder archive key tetap pihak tepercaya.22 tes baru dan source-unavailable Docker restore/auth/photo/worker-hold lulus. Paket/key satu workspace belum custody/offsite independen; tidak ada penilaian crypto/audit pihak ketiga atau jaminan crash/secure erase baru. [RECOVERY-HANDOFF](RECOVERY-HANDOFF.md).

## Preflight rilis staging — 15 November 2026 WIB

Pin image memvalidasi ID lokal penuh di bawah operation lock, menyimpan pilihan proyek secara atomic dan mempertahankan restore hold. Helper Compose menimpa selector image dari environment dengan pilihan manifest; startup image pin yang hilang ditolak sebelum fallback/pull. Preflight mencocokkan ID, byte aplikasi dan16 versi dependency, migrasi pending/unknown/berubah, Docker network/volume/port/user/cap/rootfs/resource/log serta setting/TLS/least-privilege runtime. Probe Django berjalan READ ONLY dengan statement/lock timeout5detik; hanya ID check/digest/boolean/version yang diperiksa, tanpa message body/secret. Backup dinilai dari timestamp job dan header GCM, paket harus cocok ciphertext terbaru/MFA. Laporan private/no-clobber dan production_ready selalu false; tidak ada jalur auto deploy/migrate/queue cancellation/resume. Ini bukan supply-chain signature/advisory audit, autentikasi pihak ketiga, atau bukti produksi; batas dan prosedur ada di [RELEASE-RUNBOOK](RELEASE-RUNBOOK.md).

## Latihan rollback image — 15 November 2026 UTC

Harness membuat sendiri proyek/volume/image turunan unik, tanpa argumen target pengguna. Image normal dan CMD exit37 mempunyai source/schema identik; pin dan image container aktual diperiksa. Kegagalan kesiapan tidak mengubah hold/pin atau memperbaiki data. Setelah pergantian dan rollback eksplisit pada fixture, seluruh model/sequence/recorder migrasi, sesi target dan secret file tetap sesuai; preflight recovery lulus dan tiga worker ditahan. Recorder dihash tersendiri, bukan diasumsikan tercakup daftar model. Cleanup menolak shared/base ID/foreign token/label, memeriksa identitas alias base, menangani tag dari build parsial dan membedakan daemon failure dari tag hilang. Tidak ada force/prune atau cleanup data staging pengguna. Laporan0600/no-clobber tanpa faktor/exception body; cleanup gagal menggagalkan hasil.

15 tes baru/588 per backend dan run Docker akhir lulus. Scope tetap same-schema disposable simulation, production_ready false; tidak ada klaim schema rollback umum, cross-snapshot replay, cutover/RTO atau audit independen. SIGKILL/host crash bisa menyisakan fixture dan memerlukan inspeksi kepemilikan. Panduan/evidence: [STAGING-ROLLOUT](STAGING-ROLLOUT.md).

## Ubah sandi mandiri — 16 November 2026 WIB

Endpoint hanya memakai aktor login, tanpa pilihan target dari POST. Sandi lama, kebijakan sandi baru, MFA aktual serta status aktif/hash sesi/versi akses/revisi perangkat diperiksa ulang di bawah lock membership→user→device. Verifikasi MFA lima menit sebelumnya tidak menggantikan faktor pada form ini. Password hash, versi akses, konsumsi OTP/recovery dan audit sukses berada dalam satu transaksi; audit gagal membatalkan semuanya. Regenerasi recovery kini mengunci membership sebelum device agar urutannya konsisten. Sesi saat ini diakhiri, sesi lain/pending login/token reset lama dicabut; secret MFA dan recovery lain tidak diganti. Billing consent lama ditahan tanpa perubahan ledger.

CSRF, batas panjang, validator Django, pelestarian Unicode/spasi dan rate limit lima percobaan per identitas autentikasi/IP diterapkan. ID/role/tenant spoof diabaikan; akun portal tanpa MFA dapat membuktikan sandi sendiri, operator tetap wajib MFA. Semua input password/kode tidak diisi ulang atau dicatat pada audit/error produksi. Perubahan serentak dan rollback audit diuji di kedua backend. Harness browser memakai fixture terpisah untuk kelompok password agar assertion replay tetap berjalan di dalam limiter yang sebenarnya; error browser hanya menyebut fase dan tipe exception, tanpa snapshot form MFA.21 tes baru/609 per backend, browser dan HTTPS/backup/source-loss restore lulus. Ini bukan fitur edit identitas, password history, reset akun lain, WebAuthn, SMTP atau audit keamanan independen. [AUTHENTICATION](AUTHENTICATION.md).


## Profil saya — 18 November 2026 WIB

Allowlist hanya menulis first_name/last_name aktor login, tanpa mass assignment target/tenant/username/email/role/password/admin. Token bertimestamp15menit mengikat user/tenant/akses/sesi/snapshot; digest tidak membawa nama/email/session key mentah. Stale state409 tidak diperbarui diam-diam, token asing403, invalid/expired400, no-op tanpa audit dan replay setelah perubahan tidak menggandakan event. Fingerprint membandingkan state kini, bukan rekam versi historis. Lock membership→user, pemeriksaan aktif/hash sesi/versi/MFA terbaru, update dua field + audit atomic menjaga password/akses/MFA/sesi/consent; audit gagal rollback. Nama dibatasi150/NFC/control+bidi+surrogate ditolak/HTML di-escape; token/nama dan locals ditandai sensitif pada error produksi. Audit hanya metadata actor/tenant/target/aksi.

25 tes baru/634 per backend, dua race DB, browser4viewports/dark dan HTTPS/backup/source-loss restore lulus. Profil tidak mengubah identitas login, email, data pelanggan, consent atau secret MFA. Source/clone/default data dan kunci tetap; clone workers held. Tidak ada verifikasi identitas/avatar/email delivery atau perluasan klaim ASVS/produksi. [ACCOUNT-PROFILE](ACCOUNT-PROFILE.md).

## Pengarsipan paket — 18 November 2026 WIB

Arsip/aktifkan kembali memakai izin `customers`+`manage_plans`, tenant server, CSRF, persetujuan eksplisit dan MFA terkini. Token15menit terikat aktor/tenant/versi akses/revisi MFA/versi serta snapshot paket/tujuan status. Konfirmasi mengunci tenant→membership→paket, membaca ulang hak akses dan faktor, memeriksa stale serta menulis flag/version+audit atomik. Harga dan arsip memakai versi yang sama. Replay setelah perubahan ditolak; no-op tanpa audit. Tidak ada password/faktor di token; parameter token disamarkan pada laporan error.

Paket arsip tidak boleh digunakan untuk pelanggan baru, impor create-only, PSB baru atau penggantian ke paket arsip lain. Pemeriksaan form diulang di transaksi dan penugasan diserialisasi terhadap arsip melalui kunci tenant. Pelanggan/PSB yang sudah memakai paket itu boleh mempertahankannya; PSB lama tetap dapat diaktivasi dan billing lama/berikutnya tetap berjalan. Ini kebijakan katalog, bukan penghentian layanan. Admin SQL langsung tidak dibatasi aturan form aplikasi.

Migrasi0012 menambah flagTrue berindeks tanpa mengubah nilai lama. Operasi SQLite menjaga high-water sequence saat table rebuild; uji migrasi nonkosong mencakup ID tinggi yang pernah dihapus. Semua data/kunci lama harus direkonsiliasi dan backup diuji sebelum penulis dimulai. Tidak ada auto-reverse migration; image0011 tidak kompatibel dengan gate recorder0012. Backup lama tetap disimpan, bukan ditimpa atau dihapus. Rincian bukti operasional ada di [TEST-REPORT](TEST-REPORT.md) dan [PLANS](PLANS.md).

30 tes domain/HTTP/konkurensi/migrasi baru serta664 tes pada masing-masing backend lulus. Browser arsip dan regresi operasional menggunakan akun/database sementara, termasuk penolakan replay dan fingerprint pelanggan/ledger yang tetap. Pemilihan pelanggan pembanding pada regresi kuitansi diperbaiki agar benar-benar berbeda dari pemilik portal; ini perbaikan fixture uji. Tidak ada advisory/OS/supply-chain/penetration audit baru atau klaim kesiapan produksi.
