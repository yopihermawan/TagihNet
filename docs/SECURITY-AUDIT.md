# Audit keamanan TagihNet — 1 November 2026

Lingkungan: lokal Linux, Python 3.12.14, Django 5.2.18, SQLite, Chromium; semua data fiktif. Audit hanya pada kode TagihNet yang ditulis untuk checkpoint ini. Observasi KelolaNet dalam brief adalah referensi UI, bukan temuan teknis terkonfirmasi. Checkpoint kini mencakup MFA/editor akses dan penerbitan massal dengan persetujuan snapshot. Tidak ada sertifikasi ASVS atau klaim production-ready.

## Model ancaman dan batas kepercayaan

Aset utama: identitas pelanggan, ledger rupiah, sesi operator, PDF/CSV, pengaturan billing, serta antrean perubahan layanan. Ancaman prioritas: pembacaan lintas ISP/IDOR, kasir menaikkan privilege, manipulasi nominal browser, payment replay, XSS footer, CSV formula injection, serta indikator router palsu. Internet, input browser, dan ID pada URL tidak dipercaya. Administrator database/host masih berada dalam batas kepercayaan. Unggahan hanya CSV pelanggan terbatasi. Tidak ada koneksi router nyata atau upload lampiran biner. Adapter Midtrans Sandbox memakai dua host HTTPS tetap; input browser tidak dapat menentukan URL fetch.

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

Webhook Midtrans Sandbox kini tersedia dengan signature dan GET status, diuji memakai transport tiruan; rincian pada PAYMENT-GATEWAY.md. SSRF pada router, kredensial RADIUS, upload lampiran biner/malware, dan autentikasi realtime: **belum applicable pada kode aktif**, karena endpoint/adapternya tidak tersedia. Kontrol tersebut wajib dievaluasi ketika modul ditambahkan; jangan mengartikan ketiadaan implementasi sebagai lulus integrasi.

Audit ini tidak memperbaiki seluruh target ASVS L2. [ASVS-CHECKLIST](ASVS-CHECKLIST.md) adalah daftar subset kontrol yang relevan dan status bukti, bukan checklist lengkap standar.

## Verifikasi checkpoint akses

66 tes backend mencakup MFA terenkripsi, OTP/recovery replay paralel, expiry pending login, revocation di antara faktor dan pembuatan session, limiter atomic, role tenant, direct PDF, step-up, dan last-owner race. Browser terisolasi menguji QR enrollment, custom role assignment, pencabutan sesi, regenerasi recovery dan penolakan kode lama; tidak ada QR/secret/kode disimpan ke bukti visual. Scope teknisi/wilayah, WebAuthn, dan rotasi key enkripsi terotomasi belum tersedia.

Audit dependency awal checkpoint ini menemukan advisory `PYSEC-2026-3552` pada cryptography 49.0.0. Paket diperbarui menjadi 50.0.0 sebelum commit; retest suite/browser dan pemindaian runtime dilakukan pada versi perbaikan. Advisory yang terduplikasi dalam output awal dihitung sebagai satu identitas advisory, bukan dua temuan independen.

## Retest penerbitan massal

95 tes total dan browser lokal memverifikasi endpoint bulk_issue terpisah, scope pembuat+tenant, POST/CSRF, review checkbox, MFA terbaru dan safe return sesudah step-up, snapshot server-side, deteksi perubahan sebelum konfirmasi, worker authorization version, rollback savepoint, cancellation serta concurrency/deduplikasi. Konfirmasi hanya mengantrekan; GET/preview tidak menerbitkan. Nominal/tenant/pelanggan dari POST konfirmasi tidak dipakai. Tidak ada renderer HTML baru atau akses jaringan eksternal. Bulk audit menggunakan correlation ID batch.

Snapshot yang telah disetujui tetap berlaku bila setelan berubah sesudah konfirmasi; kebijakan ini ditampilkan dan diuji. Data yang berubah sebelum konfirmasi mewajibkan pratinjau baru. Risiko yang tetap terbuka: database administrator dapat memodifikasi snapshot/audit, SQLite hanya diverifikasi lokal dengan thread, belum ada load test/admission control global atau PostgreSQL lintas proses. Batas 500 pelanggan per job dan 1–500 baris per putaran bukan jaminan kapasitas produksi.

## Retest tiket/PSB dan assignment scope

129 tes total dan browser terisolasi memverifikasi scope tenant/customer/assigned-technician, filtrasi komentar internal sebelum pagination, plaintext encoding, state machine+optimistic version+request UUID, serta fail-closed 404 bila assignment berubah selama request. Preset teknisi tidak dapat menerima custom role global. Pembuatan tiket customer mengambil customer dari membership, memaksakan prioritas normal, dan mengabaikan assignee/priority/customer palsu. Flag internal eksplisit ditolak untuk pelanggan.

Aktivasi memerlukan activate_installations, MFA terbaru, signed quote terikat actor/tenant/PSB/periode/identitas/harga/setelan/versi dan expiry. Transaksi membuat customer/invoice/job bersama; replay dan race menghasilkan satu set, kegagalan invoice rollback customer. Provisioning pending terpisah dan kegagalan router tidak dipalsukan aktif. Endpoint lampiran dan produksi tidak ditambahkan. Batas terbuka tetap database/admin trust, SQLite thread-only, belum ada deployment/performance/pen-test/ASVS lengkap.

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

Retensi batch/PII dan enkripsi database/backup belum otomatis. Tidak ada dukungan Excel/XLSX, lampiran biner, merge/update pelanggan atau importer nominal ledger. Source CSV tetap input tidak dipercaya dan data admin database masih batas kepercayaan. Tidak ada perluasan klaim menjadi seluruh ASVS L2 atau kesiapan produksi.

## Backup AES-256-GCM dan restore

245 tes pada dua backend dan drill SQLite/PG memverifikasi AEAD streaming standar cryptography, nonce96-bit acak/key256-bit, metadata sebagai AAD, header/size/tipe ketat, no-clobber dan verifikasi tag sebelum publikasi SQLite atau koneksi restore PG. Key ID saja tidak mengautentikasi material kunci salah. Invalid archive/key tidak menimpa DB; PG target terisi/sumber/sistem/role bukan owner ditolak dan pg_restore tidak memakai --clean. Advisory marker mengikat tools container ke endpoint yang dikonfigurasi; helper restore memakai advisory lock untuk serialisasi antarhelper. Tidak ada endpoint web/global backup bypass.

Snapshot/payload plaintext hanya staging privat0600/0700 dan normal cleanup diuji; crash artifacts/secure erase tidak dijamin. Host admin adalah batas kepercayaan; dump dari pemegang kunci dipercaya dan bukan sandbox SQL. Kunci arsip terpisah dari Fernet MFA dan sesi, tidak disertakan dalam archive. Drill menguji MFA setelah restore dengan keys terpisah, password/TOTP/CSRF serta penolakan replay. Tidak ada key/factor/user content di stdout/argv; untuk local PG password berada pada child environment, bukan argv. Kunci development serta archive pada workspace bukan vault/offsite. Legacy plaintext dipertahankan untuk kompatibilitas; retensi, produksi TLS, backup offsite/PITR dan audit anti-tamper tetap terbuka.

Replay drill membuktikan penolakan di database hasil restore. Snapshot lama dapat memulihkan state akses/counter/recovery sebelum perubahan pascabackup; rekonsiliasi transaksi, pencabutan sesi dan tinjauan akses/recovery tetap diperlukan sebelum cutover. Tidak ada klaim replay protection lintas rollback snapshot.

## Checkpoint Midtrans Sandbox — 4 November 2026 WIB

Gateway default-off dan hanya SIMULATION=1. Merchant/kredensial terikat tenant melalui konfigurasi host privat; pemilik mengaktifkan dengan MFA dan version check. Payload checkout minimal, amount dari saldo server, 3DS diminta untuk kartu, host sandbox tetap, HTTP redirect ditolak, timeout/size limit, serta checkout URL tervalidasi. Hanya notifikasi JSON terbatas dikecualikan CSRF; signature SHA-512/constant-time diikuti GET status yang mengecek order/merchant/currency/amount/transaction ID/fraud sebelum ledger atomik. Reservation, constraint dan lock mencegah pembayaran manual bersamaan/replay. Refund/chargeback dan lebih bayar ditahan untuk rekonsiliasi; reversal gateway manual ditolak. Raw webhook/secret tidak disimpan; URL checkout privat berada dalam database/backup. Pengujian lokal/Chromium/proses PostgreSQL memakai fixture, bukan bukti akun merchant atau callback publik bekerja. Pembatalan jarak jauh, resolusi order tak dikenal, refund provider dan produksi belum tersedia. Panduan dan batas expiry/timeout di PAYMENT-GATEWAY.md.

## Rekonsiliasi gateway — 11 November 2026 WIB

Dua regresi direproduksi sebelum perbaikan: refund/chargeback yang lebih dahulu committed dapat diikuti respons sukses lama yang membuat payment; settlement yang ditahan akibat konflik saldo tidak dapat dinilai ulang setelah koreksi karena receipt dideduplikasi terlalu awal. Riwayat dispute kini menahan kredit dari respons sukses lama. Settlement belum dibayar tanpa dispute dapat dinilai ulang di bawah lock invoice/customer/order, mempertahankan receipt pertama dan menambah audit saat pulih, tanpa duplikasi ledger/job. Daftar, ringkasan, pencarian dan detail riwayat memakai scope tenant dan customer yang sama; tidak mengekspos checkout URL/key pada daftar. Perintah rekonsiliasi dapat memasukkan settled untuk mendeteksi notifikasi refund yang hilang. Penanganan refund keuangan tetap memerlukan rekonsiliasi; tidak ada refund otomatis atau klaim produksi.

## Pengakhiran checkout sandbox — 11 November 2026 WIB

POST expire dibatasi owner aktif/tenant yang sama, MFA lima menit, session version, CSRF, reason/review dan throttling. Intent/idempotency key tersimpan sebelum HTTP. Preflight GET harus valid dan pending; izin diperiksa ulang sebelum write ke host sandbox tetap. GET sesudah POST juga dijalankan pada timeout/rejection. Hanya status terminal yang terverifikasi melepaskan reservasi; payment yang menang race tetap dicatat dan expiry blocked. Unknown/404 tetap tertahan. Tidak ada refund/reversal/production endpoint baru. Alasan disimpan sebagai plaintext dan dirender dengan escaping; histori intent hanya owner. Error audit SQLite yang muncul saat concurrency diperbaiki dengan transaksi retry yang membungkus intent+audit, tanpa menempatkan HTTP dalam retry transaction. Bukti dua proses dan restore meliputi intent confirmed/blocked beserta ledger.

## Worker polling opt-in — 11 November 2026 WIB

Kebijakan default-off memerlukan owner/MFA/review, versi dan kredensial merchant tenant. Approval menyimpan user/session version; worker memeriksa ulang akses dan mode sebelum request. Lease UUID/version dengan TTL60 dan pembaruan tiap order mengoordinasi proses; HTTP di luar transaksi, writer ledger tetap idempoten. Policy edit/revocation mencegah request berikutnya, tetapi tidak membatalkan pencatatan hasil yang sudah terverifikasi saat in-flight. Backoff capped1jam dan urutan berdasarkan waktu percobaan mencegah kegagalan order tunggal menghalangi pekerjaan lain. API worker hanya GET status; tidak membuat checkout/refund/expiry/pesan. Audit hanya metadata jumlah/approval, tidak menyimpan secret atau response mentah. Waktu pemeriksaan gagal dan verifikasi sukses dipisah; backfill hanya dari event terverifikasi. Uji restore mempertahankan consent/lease dan takeover setelah kedaluwarsa. Ini bukti lokal/transport tiruan, bukan pengujian merchant atau kapasitas produksi.

## Pencatatan refund penuh — 11 November 2026 WIB

Aksi owner berscope tenant, POST/CSRF/review/rate-limit, MFA lima menit dan current access version; owner/akun/sesi dicek ulang setelah jaringan. GET status autentik baru wajib memverifikasi order/merchant/IDR/gross/transaction ID, status refund, code200 dan refund_amount penuh. Data browser/webhook/event lama tidak menjadi otorisasi reversal. Partial/missing amount/riwayat chargeback ditolak. Tidak ditambahkan endpoint refund pada client provider.

Penulisan invoice/customer/order/payment terkunci dan atomik dengan PaymentReversal, resolution satu-ke-satu, audit dan job. Retry transaksi tidak mengulang HTTP; replay tidak mengoreksi dua kali. Digest metadata SHA256 bukan bukti signature atau ledger anti-tamper; administrator database tetap dipercaya. Payload mentah/kunci tidak disimpan pada bukti. Catatan owner privat, reversal publik memakai teks generik dan output tetap di-escape.

362 tes pada masing-masing backend, dua race refund lintas proses, browser portal/CSRF/MFA dan restore terenkripsi lulus. Pemanggilan API merchant nyata, refund sebagian, pengiriman refund provider, sengketa, load dan audit anti-tamper tetap belum diverifikasi/diimplementasikan.

## Tujuan kembali dari checkout — 11 November 2026 WIB

Alamat callbacks.finish berasal dari origin process yang divalidasi dan UUID order tersimpan. Request Host/forwarded host, next dan field browser tidak memilih tujuan. Origin menolak userinfo/path/query/fragment dan mewajibkan HTTPS kecuali HTTP loopback pada DEBUG=1. Konfigurasi salah gagal startup tanpa menyalin nilainya ke pesan validasi.

GET kembali tidak menerima bukti pembayaran, tidak memanggil provider dan tidak mengubah ledger. Anonim diarahkan ke login tanpa lookup objek. Hanya UUID+timestamp tervalidasi yang bertahan di antara flush/rotasi session, maksimal sepuluh menit tanpa memperpanjang deadline. Kredensial/faktor dan data pre-auth lain tidak disalin. MFA/enrollment/rotasi/revocation tetap berlaku, kemudian akses tenant/customer diperiksa ulang. Tujuan dipakai sekali; next umum/eksternal diabaikan. Kembalinya pengguna tidak melakukan POST sensitif secara otomatis.

385 tes pada masing-masing backend, browser gateway dan regresi security/MFA serta restore terenkripsi lulus. Tidak ada endpoint write provider baru atau perubahan signature webhook. Account takeover/session abuse pada deployment publik dan redirect merchant sesungguhnya belum diuji.

## Laporan ledger gateway — 12 November 2026 WIB

`gateway_reports` terpisah dari ekspor pelanggan dan memerlukan `invoices`. Owner/Finance mendapat preset, peran kustom tetap eksplisit; customer/ops/cashier/NOC/technician ditolak. Domain dan endpoint memeriksa hak, sesi dan scope ISP. Order, invoice serta payment difilter tenant dan relasi invoice; parameter tenant browser diabaikan. Riwayat tetap dapat dibaca saat kredensial tidak ada, tanpa request provider.

Periode <=31 hari dan maksimal 5.000 entri membatasi materialisasi. Satu UNION membaca pembayaran/koreksi konsisten; seluruh respons ditolak bila melebihi batas, tanpa total/CSV parsial. Report tidak mengunci atau mengubah ledger; ekspor hanya menambah audit periode/jumlah. Tidak ada klaim snapshot lintas request atau kapasitas produksi. Data status saat pembacaan dibedakan dari tanggal ledger.

CSV UTF-8/BOM menjaga angka integer negatif, menormalkan kontrol teks dan mengawali potensi formula dengan apostrof. HTML di-escape. URL checkout, key, private UUID pembayaran, digest bukti dan catatan owner tidak diekspor. Cache-control no-store berasal dari middleware. File yang sudah diunduh tetap berada di perangkat pengguna; retensi/enkripsi perangkat berada di luar cakupan.

412 tes pada kedua backend, uji proses saat refund belum committed, Chromium laporan/CSV/privacy dan restore terenkripsi lulus. Data ledger lokal bukan bukti kelengkapan transaksi merchant; koneksi akun sandbox/HTTPS publik tetap belum diverifikasi.

## Metadata notifikasi terautentikasi — 12 November 2026 WIB

Hanya callback dengan order/nominal/signature valid yang memasuki pencatatan metadata. Kredensial/mode yang tidak dapat memverifikasi signature, body invalid dan order tidak dikenal tidak memperbarui catatan. Satu row per order berisi jumlah pengiriman, waktu penerimaan dan outcome/waktu hasil terbaru; tidak menyimpan body, signature, server key, IP atau exception. Owner-only UI dan ringkasan dibatasi tenant serta invoice tenant; finance dan portal tidak melihat diagnostik.

Begin/finish memakai transaksi pendek dengan lock order serta urutan penerimaan. HTTP di luar transaksi; completion lama/berulang tidak mengganti hasil pengiriman baru. GET status dan writer ledger tetap authoritative. Sukses pending/refund review tidak diklaim lunas. Jika metadata gagal sesudah ledger commit, 503 dan replay tetap aman tanpa pembayaran/job ganda. Metadata bukan bukti pembayaran dan bukan audit provider yang tidak dapat diubah; administrator database tetap dipercaya. Model ini tidak mengarsipkan seluruh percobaan atau request yang tidak terautentikasi.

434 tes pada kedua backend, thread/dua proses HTTP overlap, browser privacy dan restore terenkripsi lulus. Tidak ditambah retry worker atau respons 200 sebelum proses sinkron selesai. Verifikasi merchant asli, pengiriman HTTPS publik, kapasitas dan audit anti-tamper tetap belum dilakukan.

## Lease pembuatan checkout — 12 November 2026 WIB

Reservasi/hak/config dan lease persisten disiapkan dalam transaksi singkat sebelum POST sandbox. Selama lease aktif, request lain tidak mengirim create baru. HTTP di luar transaksi; retry penulisan URL tidak mengulang POST. Token yang sudah digantikan tidak dapat melepas lease baru atau menyimpan respons lama. Settlement/terminal/review tidak dibuka kembali oleh URL yang tiba terlambat. Kedaluwarsa lease tidak membebaskan invoice; order ID tetap menjadi batas deduplikasi remote.

Resume terikat UUID endpoint, dengan POST/CSRF, throttle, izin pay/portal-own-customer dan MFA operator. Field token/deadline/order/nominal dari browser tidak mengubah claim. Formulir lama sesudah order terminal tidak membuat replacement; bila order pengganti sudah ada, formulir lama tidak mengklaimnya. Lease token tidak ditampilkan ke pengguna atau ditambahkan ke CSV. Schema/backup menyimpan pasangan token/deadline; restore tidak memulihkan proses HTTP.

457 tes kedua backend, tiga skenario proses baru, Chromium dan restore lease lulus. Batas tetap: lease adalah koordinasi berbatas waktu, bukan exactly-once provider; request yang masih hidup setelah deadline dapat overlap dengan takeover pada ID yang sama. Kehilangan respons token Snap tetap memerlukan rekonsiliasi merchant. Belum ada uji akun/HTTPS publik atau kapasitas produksi.

## Diagnostik kesiapan konfigurasi — 12 November 2026 WIB

Panel tetap khusus owner dengan tenant dari sesi; CLI memerlukan akses administrator dan ID ISP eksplisit. Hasil hanya membaca skema/config/riwayat merchant dan memvalidasi format origin/host, tanpa SDK request, penulisan ledger/config/audit atau aktivasi otomatis. Nilai server key, merchant, DSN, origin mentah, data pelanggan dan exception tidak masuk laporan. Fungsi diagnostik menandai local variables sensitif bagi halaman error Django. Kesalahan database yang ditangani mengganti hasil parsial dengan status belum dapat diperiksa.

Kredensial yang lolos format belum dianggap terautentikasi pada merchant. Mismatch merchant riwayat memerlukan pemulihan akun asal; mismatch konfigurasi yang belum dipakai dapat ditinjau/disimpan ulang owner dengan MFA. Loopback HTTP/HTTPS tidak dianggap alamat publik; origin lain hanya dinilai format dan kecocokan `ALLOWED_HOSTS`, tanpa klaim DNS/TLS/reachability. Semua hasil menyatakan `remote_verification=not_performed`. Settings Django harus dapat dimuat sebelum CLI tersedia.

479 tes kedua backend, drill subprocess CLI dengan fingerprint seluruh tabel, browser owner/privacy dan regresi proses/restore lulus. Tidak ada pembatasan checkout baru yang berasal dari hasil diagnostik; kontrol pembayaran yang ada tetap berlaku. Kredensial merchant/HTTPS publik dan audit produksi belum diverifikasi. Panduan: [GATEWAY-READINESS](GATEWAY-READINESS.md).

## Snapshot halaman pembayaran — 12 November 2026 WIB

GET snapshot memakai scope tenant/customer/izin halaman order, mengembalikan hanya revision opaque/status/label/watch dan cache no-store dengan Vary Cookie. Tanpa sesi, known/unknown order sama-sama 401. Pembacaan tidak memanggil provider atau menulis tabel; tidak menampilkan token/URL checkout, merchant, nominal/customer atau metadata webhook. Revision memotret state aplikasi, bukan bukti status provider baru. Pembacaan bukti lama tidak memerlukan kunci merchant atau izin melakukan pembayaran.

Idle/absolute expiry dan revocation tetap diperiksa pada setiap request. GET snapshot pasif yang tidak memodifikasi sesi tidak memperbarui aktivitas, cookie atau database sesi walaupun save-every-request aktif untuk request biasa. Logout/rotation/mutasi nyata tetap disimpan melalui middleware Django. Client memakai same-origin GET, no-store, redirect error, timeout/batas dua menit, membatalkan saat hidden/navigasi dan berhenti pada kegagalan. Label dirender sebagai teks, CSP tetap ketat; tidak ada auto-POST atau reload formulir.

494 tes kedua backend, SQL no-write assertions, Chromium virtual clock/error/timeout, browser webhook/form preservation, security smoke dan regresi proses/restore lulus. Endpoint pembacaan masih memerlukan kapasitas HTTP/database sesuai jumlah tab aktif; batas client bukan rate limiter keamanan. Akun merchant/HTTPS publik, beban dan audit produksi belum diverifikasi. [Panduan](GATEWAY-PAGE-UPDATES.md).


## Hasil rekonsiliasi CLI — 12 November 2026 WIB

Command kini mengembalikan exit 1 jika satu pemeriksaan gagal, termasuk batch parsial; hasil sah yang sudah commit tetap tersimpan. Tenant tidak dikenal atau pemilihan database gagal tidak dianggap batch kosong. Report JSON per order hanya membawa ID order/ISP, hasil dan status lokal; kredensial, merchant, URL checkout, nominal/customer, payload/signature serta detail exception tidak dicetak. Failed status null tidak mengklaim snapshot lama sebagai hasil terbaru. Stdout JSON terpisah dari CommandError umum di stderr, dan waktu laporan memakai WIB.

Dry-run tidak memanggil provider, mengubah timestamp/config/ledger/audit, atau mengaktifkan checkout. Limit <=500 dan urutan batch dibekukan sebelum I/O. Mode normal tetap memakai client/writer yang ada; HTTP tidak dibungkus transaksi batch, callback/replay memakai idempotensi ledger yang sama. Tidak ada endpoint publik atau izin baru; host administrator dapat memilih seluruh ISP seperti sebelumnya. Pratinjau adalah snapshot lokal yang tidak disimpan sebagai persetujuan; invocation berikutnya memilih ulang.

512 tes kedua backend dan drill CLI subprocess/fingerprint lulus, termasuk batch parsial dengan satu pembayaran tetap tersimpan serta replay tanpa payment/job ganda. Tidak ada perubahan worker/policy, schema atau UI. CI remote tidak dapat diverifikasi melalui API (403), dan merchant/HTTPS publik belum tersedia. [Panduan](GATEWAY-RECONCILIATION-CLI.md).


## Pelaporan persetujuan worker — 12 November 2026 WIB

Claim yang menonaktifkan kebijakan karena persetujuan tidak berlaku kini mengembalikan hasil blocked yang sudah commit, sehingga run_cycle/--once tidak menganggapnya putaran kosong. process_policy memeriksa enabled sebelum memakai token: hasil terblokir tidak memperoleh lease, tidak masuk alur GET/finish, dan tidak menyimpan timestamp sukses baru. Caller internal claim_policy dapat menerima kebijakan leased, kebijakan baru blocked, atau None jika dilewati; semua caller yang ada diperiksa. Lock transaksi/audit idempoten tetap dipakai, tanpa HTTP dalam transaksi.

CLI --once exit 1 untuk kebijakan yang baru blocked dan menjelaskan kebutuhan persetujuan ulang owner/MFA. Jumlah putaran lama tetap disimpan tetapi tidak dicetak sebagai pemeriksaan baru; hasil sah yang kembali sesudah pencabutan in-flight tetap memakai writer ledger lama. Kebijakan ISP lain tetap diproses dan daemon tetap hidup. Kebijakan yang sudah nonaktif/belum jatuh tempo/berlease aktif dilewati, sehingga exit 0/hasil kosong bukan pemeriksaan kesehatan semua kebijakan.

521 tes kedua backend, subprocess exit/reauthorization/fingerprint, 26 skenario proses dan restore SQLite/PostgreSQL lulus. Dua proses tanpa persetujuan menghasilkan satu blocked/audit tanpa I/O provider atau ledger baru. Tidak ada schema, credential atau izin baru, dan tanpa perubahan UI. Akun Midtrans/HTTPS publik dan CI remote belum diverifikasi.


## Lease pengakhiran — 12 November 2026 WIB

Semua intent pengakhiran pada satu order berbagi satu lease aktif melalui lock order, termasuk key berbeda/row legacy. Token/deadline diperoleh dalam transaksi singkat sebelum preflight, diperbarui sebelum POST dan tidak dikirim ke browser. Pemeriksaan owner/tenant/MFA/CSRF/throttle yang ada tetap berlaku; field lease dari browser diabaikan. HTTP berada di luar transaksi/retry database. Finish harus memiliki token yang masih sama; takeover membatalkan token intent lama sehingga completion lama tidak dapat melepas lease baru atau mengganti hasil.

Kegagalan yang diketahui menyelesaikan claim miliknya; proses terputus meninggalkan lease sampai deadline. Kedaluwarsa hanya mengizinkan retry, tidak melepas invoice atau membuktikan expire. Preflight/postflight tetap mengandalkan GET terautentikasi, dan status/payment sah dari request lama tetap memakai writer idempoten. Lease bukan exactly-once provider: request lama yang hidup melampaui deadline dapat overlap dengan takeover pada order ID sama. Tidak ada pengiriman refund atau reversal otomatis.

536 tes kedua backend, tiga skenario proses expiry baru (29 total), Chromium, restore SQLite/PG dan constraint pasangan lease lulus. Migrasi 0015 diterapkan pada development sesudah encrypted backup/verify; payload lama 40 tabel dan integritas tetap sesuai. Owner melihat status proses/deadline/muat ulang, tanpa token atau auto-POST. Tidak ada izin, dependency atau layanan baru. Merchant/HTTPS publik dan kapasitas produksi belum diverifikasi.
