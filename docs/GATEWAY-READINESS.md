# Pemeriksaan kesiapan Midtrans Sandbox

Owner membuka **Payment gateway → Kesiapan konfigurasi** untuk melihat hasil pemeriksaan lokal ISP yang sedang login. Administrator dapat memakai CLI pada checkout dan environment process aplikasi yang sama:

```bash
# Ganti 1 dengan ID tenant ISP yang sudah terdaftar.
.venv/bin/python manage.py check_payment_gateway --tenant 1
.venv/bin/python manage.py check_payment_gateway --tenant 1 --json
```

ID tenant wajib bilangan positif. CLI tidak memilih ISP pertama secara otomatis; ID tidak dikenal menghasilkan laporan belum siap. Akses CLI memerlukan akses administrator ke proses/database. Halaman web khusus owner dan selalu mengambil tenant dari sesi, tanpa menerima pilihan tenant dari query string.

## Arti hasil

| Field JSON | Arti |
| --- | --- |
| `configuration_ready` | Seluruh pemeriksaan konfigurasi lokal sesuai untuk memulai pengujian |
| `checkout_configured` | Skema, mode sandbox, format kredensial, kecocokan merchant dan opt-in checkout sesuai |
| `public_urls_configured` | Origin HTTPS non-loopback valid dan hostname diterima `ALLOWED_HOSTS` |
| `remote_verification` | Selalu `not_performed`; tidak ada request Midtrans, DNS atau akses HTTPS publik |
| `checked_at` | Waktu pemeriksaan ber-offset `+07:00` (WIB) |
| `checks` | Kode, label, status, penjelasan dan tindakan per pemeriksaan, tanpa nilai konfigurasi privat |

Exit **0** berarti `configuration_ready=true`; exit **1** berarti belum lengkap/belum dapat diperiksa. Dalam mode `--json`, laporan berada di stdout dan pesan kegagalan umum berada di stderr sehingga stdout tetap dapat di-parse. Argumen yang hilang/tidak sah ditolak sebelum laporan dibuat; kegagalan startup Django juga dapat terjadi sebelum laporan tersedia.

Status `ready` berarti sesuai pemeriksaan lokal, `blocked` perlu diperbaiki, `unknown` belum dapat diperiksa, dan `local` hanya alamat pengujian lokal. Flag checkout/public URL bernilai `null` bila pemeriksaan berhenti sebelum seluruh konfigurasi dapat dinilai, misalnya migrasi tertunda, tenant tidak ditemukan atau database tidak tersedia. `configuration_ready` tetap `false`.

## Pemeriksaan dan tindakan

| Pemeriksaan | Yang dinilai dan tindakan bila belum sesuai |
| --- | --- |
| Skema database | Riwayat konsisten dan tidak ada migrasi tertunda. Cadangkan database berisi data sebelum menerapkan migrasi. CLI dapat membaca status database yang belum dimigrasikan, tanpa membuat tabel aplikasi atau menjalankan migrasi. |
| Mode sandbox | `TAGIHNET_SIMULATION=1`. Saat mode nonaktif, kredensial tidak dianggap telah diperiksa. |
| Kredensial ISP | Konfigurasi privat `TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS` tersedia, format sandbox sesuai dan merchant unik antar-ISP. Ini tidak memvalidasi server key pada akun Midtrans. Masukkan nilai melalui pengelola konfigurasi privat. |
| Kecocokan merchant | Merchant environment sesuai konfigurasi tersimpan dan seluruh order ISP ini. Jika hanya konfigurasi yang belum pernah dipakai berbeda, owner dapat meninjau/simpan ulang dengan MFA. Jika riwayat order berbeda, pulihkan akun merchant asal; jangan menggantinya untuk melewati order lama. |
| Checkout baru | Owner sudah mengaktifkan checkout dengan MFA. Pemeriksaan tidak mengaktifkannya otomatis. |
| Alamat aplikasi HTTPS | `TAGIHNET_PUBLIC_ORIGIN` adalah origin HTTPS valid tanpa path, kredensial, query atau fragment. HTTP loopback hanya diterima saat DEBUG aktif; HTTP maupun HTTPS loopback berstatus lokal saja. |
| Host aplikasi | Hostname origin diterima oleh `TAGIHNET_ALLOWED_HOSTS`, termasuk aturan domain induk Django, IDNA dan IPv6 berkurung. Port origin bukan bagian entri hostname. Restart proses setelah mengubah environment. |

Django harus dapat memuat settings sebelum CLI berjalan. JSON environment yang tidak dapat di-parse atau origin yang ditolak saat startup perlu diperbaiki melalui konfigurasi privat terlebih dahulu. Kesalahan koneksi database yang ditangani diagnostik hanya menghasilkan pesan umum; DSN dan detail exception tidak ditampilkan.

Origin non-loopback yang lolos pemeriksaan format masih mungkin menunjuk jaringan privat atau tidak dapat dijangkau. Pemeriksaan tidak membuktikan DNS, TLS, reverse proxy, akses keluar sandbox, akun merchant, pengiriman webhook, status pembayaran atau pencairan bank. Lanjutkan uji akun dan endpoint HTTPS sesuai [PAYMENT-GATEWAY](PAYMENT-GATEWAY.md).

## Batas dan verifikasi

Diagnostik tidak membuat checkout, mengambil status, menjalankan worker, mengubah konfigurasi/ledger/audit atau menyimpan payload webhook. Startup Django development dapat menyiapkan direktori/kunci lokal sesuai perilaku settings yang sudah ada. Origin tetap opsional bagi alur checkout yang sudah ada; hasil belum lengkap tidak menambah pembatasan baru pada checkout/rekonsiliasi. Simpan/aktivasi dan tindakan pembayaran tetap memakai kontrol izin/MFA masing-masing.

Laporan tidak memuat server key, merchant ID, DSN, origin mentah atau data pelanggan. Jangan menyertakan nilai privat saat membagikan konfigurasi untuk troubleshooting.

Verifikasi tanpa kredensial merchant:

```bash
.venv/bin/python manage.py test core.tests.test_gateway_readiness --verbosity 1
.venv/bin/python scripts/verify_gateway_readiness.py
.venv/bin/python scripts/gateway_smoke.py
```

Drill CLI memakai database sementara, menguji exit/JSON pada database belum dimigrasikan serta konfigurasi kurang/lengkap, lalu membandingkan jumlah dan hash payload seluruh tabel sebelum/sesudah diagnostik. Browser memakai Chromium dan transport Midtrans tiruan; loopback tetap dilaporkan lokal saja. Data pengguna tidak diisi fixture.
