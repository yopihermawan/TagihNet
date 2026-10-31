# Audit keamanan TagihNet — 31 Oktober 2026

Lingkungan: lokal Linux, Python 3.12.14, Django 5.2.18, SQLite, Chromium; semua data fiktif. Audit hanya pada kode TagihNet yang ditulis untuk checkpoint ini. Observasi KelolaNet dalam brief adalah referensi UI, bukan temuan teknis terkonfirmasi. Tidak ada sertifikasi ASVS atau klaim production-ready.

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
| Dependensi terkunci | `pip-audit 2.10.1` via PyPI terhadap enam paket runtime di requirements.txt: no known vulnerabilities found | pass · sesuai basis advisory saat eksekusi; bukan jaminan bebas bug |

## Temuan yang diperbaiki selama implementasi

| ID / severity lokal | Bukti, dampak, perbaikan, retest |
| --- | --- |
| TGN-001 · sedang / availability | Eksekusi pertama settings mencoba chmod pada file object, sehingga bootstrap gagal dan file key kosong tertinggal. Diganti pembuatan file eksklusif mode 0600 melalui `os.open`, validasi panjang kunci; artefak kosong milik eksekusi ini dihapus. Migrasi/seed/check berhasil sesudahnya. Tidak ada deployment produksi terdampak |
| TGN-002 · rendah / integritas demo | Seed awal bergantung pada nama ISP yang dapat diubah. Diperbaiki dengan mengambil tenant dari membership owner yang ada dan menolak konflik akun. Uji seed ulang setelah rename menjaga jumlah record dan password |
| TGN-003 · rendah / aksesibilitas | Sidebar mobile yang bergeser keluar layar masih dapat menerima fokus. Ditambah `inert` saat tertutup; dibuka melalui tombol, Escape mengembalikan fokus. Browser retest mengikuti perbaikan |

## Risiko terbuka / gate sebelum produksi

| ID | Severity kontekstual | Bukti / tindakan |
| --- | --- | --- |
| TGN-P01 | Tinggi bila dipublikasikan | MFA owner/privilege belum dibuat. Terapkan enrollment/recovery/step-up dan uji; hanya lingkungan lokal saat ini |
| TGN-P02 | Tinggi bila data nyata dipakai | Backup masih plaintext lokal, audit bisa diubah administrator DB, belum retensi/monitoring/offsite. Terapkan enkripsi/key terpisah, kontrol storage dan restore drill produksi |
| TGN-P03 | Sedang | SQLite tidak memberi row lock `select_for_update`; pengujian race hanya lokal dua thread. Pindah dan uji transaksi/queue/locking PostgreSQL serta multiworker sebelum skala produksi |
| TGN-P04 | Sedang | Throttle DB memakai count lalu insert; burst paralel dapat melampaui batas. NAT bisa membatasi beberapa operator bersama. Gunakan limiter atomic di edge/shared store dan uji concurrency |
| TGN-P05 | Sedang | Akun demo lokal tersedia dengan password yang didokumentasikan; seed dibatasi development. Dilarang mempublikasikan database seed sebagai layanan produksi |
| TGN-P06 | Belum dinilai | TLS end-to-end, SMTP produksi, provisioning/rotation secret, Docker runtime, penetrasi aplikasi, dan beban belum diuji. Tidak ada klaim lulus untuk kontrol tersebut |

Gateway/webhook signature, SSRF pada router, kredensial RADIUS, upload malware, dan autentikasi realtime: **belum applicable pada kode aktif**, karena endpoint/adapternya tidak tersedia. Kontrol tersebut wajib dievaluasi ketika modul ditambahkan; jangan mengartikan ketiadaan implementasi sebagai lulus integrasi.

Audit ini tidak memperbaiki seluruh target ASVS L2. [ASVS-CHECKLIST](ASVS-CHECKLIST.md) adalah daftar subset kontrol yang relevan dan status bukti, bukan checklist lengkap standar.
