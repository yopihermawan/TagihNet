# Audit keamanan TagihNet — 31 Oktober 2026

Lingkungan: lokal Linux, Python 3.12.14, Django 5.2.18, SQLite, Chromium; semua data fiktif. Audit hanya pada kode TagihNet yang ditulis untuk checkpoint ini. Observasi KelolaNet dalam brief adalah referensi UI, bukan temuan teknis terkonfirmasi. Checkpoint kini mencakup MFA/editor akses dan penerbitan massal dengan persetujuan snapshot. Tidak ada sertifikasi ASVS atau klaim production-ready.

## Model ancaman dan batas kepercayaan

Aset utama: identitas pelanggan, ledger rupiah, sesi operator, PDF/CSV, pengaturan billing, serta antrean perubahan layanan. Ancaman prioritas: pembacaan lintas ISP/IDOR, kasir menaikkan privilege, manipulasi nominal browser, payment replay, XSS footer, CSV formula injection, serta indikator router palsu. Internet, input browser, dan ID pada URL tidak dipercaya. Administrator database/host masih berada dalam batas kepercayaan. Tidak ada koneksi router/gateway, upload, atau URL-fetch API pada implementasi ini.

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
| TGN-P02 | Tinggi bila data nyata dipakai | Backup masih plaintext lokal, audit bisa diubah administrator DB, belum retensi/monitoring/offsite. Terapkan enkripsi/key terpisah, kontrol storage dan restore drill produksi |
| TGN-P03 | Sedang | SQLite tidak memberi row lock `select_for_update`; pengujian race hanya lokal dua thread. Pindah dan uji transaksi/queue/locking PostgreSQL serta multiworker sebelum skala produksi |
| TGN-P04 | Sedang | Count-then-insert telah diganti update counter bersyarat atomic dan diuji paralel. Risiko tersisa: batas lima percobaan/IP dapat memengaruhi operator di NAT sama; uji beban/edge produksi belum |
| TGN-P05 | Sedang | Akun demo lokal tersedia dengan password yang didokumentasikan; seed dibatasi development. Dilarang mempublikasikan database seed sebagai layanan produksi |
| TGN-P06 | Belum dinilai | TLS end-to-end, SMTP produksi, provisioning/rotation secret, Docker runtime, penetrasi aplikasi, dan beban belum diuji. Tidak ada klaim lulus untuk kontrol tersebut |

Gateway/webhook signature, SSRF pada router, kredensial RADIUS, upload malware, dan autentikasi realtime: **belum applicable pada kode aktif**, karena endpoint/adapternya tidak tersedia. Kontrol tersebut wajib dievaluasi ketika modul ditambahkan; jangan mengartikan ketiadaan implementasi sebagai lulus integrasi.

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
