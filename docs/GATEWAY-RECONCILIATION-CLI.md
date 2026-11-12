# Rekonsiliasi gateway melalui CLI

`reconcile_gateway_payments` memeriksa status order yang sudah ada. Mode normal dapat mencatat pembayaran terverifikasi pada ledger; tidak membuat checkout, meminta expire/refund, atau menjalankan daemon. Akses host/database dan konfigurasi merchant privat diperlukan; command ini bukan endpoint untuk pelanggan. Sakelar checkout baru dan kebijakan worker tidak membatasi rekonsiliasi CLI order lama.

```bash
# Ganti 1 dengan ID ISP yang sudah terdaftar.
# Pratinjau lokal tanpa request Midtrans atau perubahan tabel:
.venv/bin/python manage.py reconcile_gateway_payments --tenant 1 --limit 100 --dry-run --json

# Verifikasi nyata; dapat mencatat pembayaran yang terbukti sah:
.venv/bin/python manage.py reconcile_gateway_payments --tenant 1 --limit 100 --json

# Sertakan pembayaran tercatat untuk mendeteksi refund/chargeback yang terlewat:
.venv/bin/python manage.py reconcile_gateway_payments --tenant 1 --include-settled --json
```

## Kode keluar dan hasil

| Kode | Arti |
| --- | --- |
| 0 | Batch terpilih selesai diverifikasi, tidak ada order yang dipilih, atau pratinjau lokal berhasil. Periksa `outcome` untuk membedakannya. |
| 1 | Ada verifikasi gagal, batch hanya selesai sebagian, tenant tidak dikenal, atau pemilihan order gagal karena database. Hasil yang sudah tersimpan tidak dibatalkan. |

Perubahan kompatibilitas: sebelumnya kegagalan verifikasi tetap menghasilkan exit 0; kini satu kegagalan pun menghasilkan exit 1 setelah batch selesai. Scheduler harus memeriksa kode keluar dan laporan, bukan menganggap jumlah nol sebagai bukti merchant siap. Argumen salah juga ditolak; argparse dapat menghasilkan exit 2. Kegagalan startup/settings Django dapat terjadi sebelum laporan tersedia.

Mode `--json` hanya menulis laporan ke stdout. CommandError umum berada di stderr; jangan menggabungkan stderr dengan stdout jika laporan akan di-parse. Tanpa flag JSON, ringkasan manusia tetap menyebut verified/unavailable; pratinjau menyatakan tidak ada request atau perubahan data.

| Field JSON | Arti |
| --- | --- |
| `schema_version` | Versi format laporan, saat ini 1 |
| `outcome` | `planned`, `empty`, `completed`, `partial`, `failed`, `unknown_tenant`, atau `database_unavailable` |
| `dry_run`, `tenant_id`, `include_settled`, `limit` | Cakupan invocation; tenant null berarti seluruh ISP |
| `selected` | Jumlah order pada batch; null jika pemilihan belum dapat ditentukan, bukan batch kosong |
| `attempted` | Jumlah order yang dicoba melalui alur pemeriksaan; kegagalan kredensial dapat terjadi sebelum HTTP |
| `verified`, `unavailable` | Verifikasi berhasil atau belum berhasil; verified tidak berarti seluruh order lunas |
| `results` | ID order/ISP, hasil per order, dan status lokal sesudah verifikasi atau saat pratinjau; status null jika verifikasi gagal |
| `generated_at` | Waktu pembuatan laporan dalam WIB, offset `+07:00` |

`planned` berarti pratinjau berhasil, termasuk jika selected nol; attempted/verified/unavailable tetap nol. `empty` adalah mode normal tanpa order terpilih, bukan pemeriksaan akun Midtrans. `completed` dapat berisi status pending atau perlu rekonsiliasi yang berhasil diverifikasi; bukan seluruhnya pembayaran baru. `partial` mempertahankan pembayaran sah dari order lain yang berhasil, sedangkan `failed` berarti tidak ada pemeriksaan pada batch yang berhasil. Keduanya exit 1. `unavailable` mencakup kegagalan transport, identitas/nominal, kredensial atau penulisan database; detail exception privat tidak dimasukkan laporan.

## Seleksi, pratinjau dan retry

Limit 1–500, default 100. `--tenant` opsional tetapi harus positif dan sudah terdaftar; tanpa tenant, seluruh ISP dapat dipilih. Default status creating/pending/review; `--include-settled` menambah settled/refunded untuk mendeteksi perubahan yang notifikasinya terlewat. Terminal deny/cancel/expire tidak dipilih.

Order yang belum pernah dicoba didahulukan, lalu waktu percobaan terlama, created_at dan UUID. Daftar dibekukan sebelum request pertama; order yang muncul sesudahnya menunggu invocation berikutnya. Keberhasilan maupun kegagalan memperbarui urutan percobaan bila database dapat ditulis. Kegagalan tidak memperbarui bukti verifikasi sukses. Satu order gagal tidak menghentikan pemeriksaan order berikutnya.

Pratinjau hanya membaca state lokal dan tidak memerlukan kredensial, mode simulasi atau aktivasi checkout. Tidak ada timestamp, konfigurasi, audit atau ledger yang diubah. Pratinjau bukan rencana tersimpan: invocation normal memilih ulang sehingga daftar dapat berbeda bila ada perubahan setelah pratinjau. Startup development Django dapat menyiapkan direktori/kunci lokal sesuai perilaku settings yang sudah ada.

Mode normal menggunakan client sandbox dan writer ledger yang sama dengan webhook/worker. Retry memakai order ID yang sama dan tidak menggandakan payment/event/job. Batch tidak mengunci seluruh antrean selama HTTP; invocation lain atau worker dapat memeriksa order yang sama, dengan idempotensi ledger tetap berlaku. Command tidak membalik ledger otomatis untuk refund/chargeback. Ikuti alur owner/MFA untuk pencatatan refund penuh yang sudah terjadi.

Laporan tidak memuat server key, merchant ID, checkout URL/token, nominal/nama pelanggan, payload/signature webhook atau detail koneksi database. Jika skema atau koneksi database belum siap, `selected=null` dan exit 1; periksa `manage.py migrate --check` dan konfigurasi privat. Panduan merchant dan penanganan status: [PAYMENT-GATEWAY](PAYMENT-GATEWAY.md).

## Verifikasi tanpa akun merchant

```bash
.venv/bin/python manage.py test core.tests.test_gateway_reconciliation_cli --verbosity 1
.venv/bin/python scripts/verify_gateway_reconciliation_cli.py
```

Drill memakai CLI subprocess, database sementara dan transport status tiruan. Seluruh jumlah/hash tabel cocok sebelum/sesudah pratinjau. Batch gagal/sebagian exit 1 dengan JSON valid, pembayaran sah bertahan pada hasil parsial, lalu complete/replay exit 0 tanpa pembayaran/job ganda. Tidak ada request ke akun Midtrans sebenarnya atau perubahan data pengguna.
