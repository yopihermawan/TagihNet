# Midtrans Sandbox — checkpoint pengakhiran checkout 11 November 2026 WIB

Checkout Snap redirect, notifikasi terverifikasi, dan rekonsiliasi kini diimplementasikan. Integrasi **nonaktif secara bawaan**, khusus sandbox dan `TAGIHNET_SIMULATION=1`. Uang nyata, endpoint/kunci produksi, refund otomatis, serta aktivasi router nyata belum didukung. Kredensial merchant dan URL HTTPS publik belum tersedia pada lingkungan pengembangan; transport Midtrans diuji dengan respons tiruan, bukan transaksi pada akun sandbox sebenarnya.

## Konfigurasi merchant

Administrator menyiapkan environment process privat `TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS` berisi objek JSON berikut. Contoh ini skema placeholder, bukan kredensial yang dapat dipakai:

```json
{
  "<ID tenant ISP>": {
    "merchant_id": "<merchant ID sandbox ISP ini>",
    "server_key": "<server key sandbox ISP ini>"
  }
}
```

Gunakan server key sandbox dari dashboard Midtrans, dengan awalan `SB-Mid-server-`. Satu merchant untuk satu tenant; pemakaian merchant yang sama pada dua ISP ditolak. ID tenant berasal dari database aplikasi, bukan input browser. Simpan nilai melalui pengelola secret/process environment privat; jangan commit nilainya, mencantumkannya di chat atau memasukkannya ke screenshot. Aplikasi tidak membaca `.env` otomatis. Ini konfigurasi JSON yang harus tersedia utuh pada process, bukan placeholder proxy-secret untuk keseluruhan JSON. Rotasi server key dilakukan di konfigurasi privat; merchant ID tidak boleh berganti setelah memiliki order. Kunci lama dan akun merchant harus dipertahankan sampai seluruh transaksi lama selesai direkonsiliasi.

Izinkan HTTPS keluar hanya ke:

- `app.sandbox.midtrans.com` — Snap `POST /snap/v1/transactions` dan halaman pembayaran.
- `api.sandbox.midtrans.com` — `GET /v2/<order_id>/status`.

Client menggunakan proxy/CA bawaan lingkungan, Basic Auth server-side, timeout 10 detik, respons maksimal 64 KiB, dan penolakan redirect HTTP. Browser membuka halaman checkout di tab baru tanpa SDK atau script eksternal; CSP aplikasi tetap ketat. Client key tidak diperlukan untuk checkout redirect ini. Detail identitas pelanggan tidak dikirim; payload hanya membawa ID order acak, nominal, dan deskripsi tagihan generik. Permintaan kartu mengaktifkan `credit_card.secure=true`.

Jalankan migrasi sampai `0009` setelah mencadangkan database yang sudah berisi data (`0008`: gateway dan payment source; `0009`: permintaan pengakhiran):

```bash
.venv/bin/python manage.py migrate --noinput
```

Pemilik ISP membuka **Payment gateway**, meninjau mode sandbox, dan mengaktifkan dengan MFA terbaru. Operator lain tidak dapat mengubah konfigurasi. Menonaktifkan gateway menghentikan checkout baru; callback dan rekonsiliasi order lama tetap diproses jika kredensial dan mode simulasi masih tersedia. Menghapus kredensial atau mematikan mode simulasi membuat verifikasi gagal tertutup, dengan HTTP 503 agar notifikasi bisa dicoba ulang.

## Alur pembayaran

1. Pelanggan portal membuka faktur miliknya, atau operator berizin `pay` membuka faktur ISP-nya. Operator memerlukan MFA terbaru.
2. Klik **Buka checkout sandbox**. Server mengunci faktur/pelanggan, menghitung seluruh sisa tagihan, dan menyimpan order UUID sebelum menghubungi Midtrans. Nominal, tenant, dan order ID dari browser tidak digunakan.
3. Satu order aktif digunakan kembali pada retry. Pembayaran manual pada faktur dengan order aktif atau order belum dibayar yang perlu rekonsiliasi ditolak. Gangguan/timeout tidak membuat ID baru atau melepaskan reservasi.
4. Buka checkout Midtrans dari halaman order. Gunakan alat pembayaran uji resmi Midtrans. Kembali dari checkout atau menambahkan query `transaction_status=settlement` ke URL aplikasi tidak melunasi faktur.
5. Aplikasi mencatat pembayaran hanya setelah status API cocok dengan order, merchant, mata uang IDR, nominal rupiah bulat, dan transaction ID. `settlement` dengan status_code `200` dan fraud accept/absent, atau `capture` dengan `fraud_status=accept` dan status_code `200`, dapat dikreditkan.
6. Satu pembayaran bersumber `midtrans_sandbox`, satu event per status terverifikasi, audit, serta satu job rekonsiliasi jaringan simulasi disimpan atomik. Router gagal tidak membatalkan pembayaran.

Menu **Pembayaran gateway** menampilkan daftar order ISP sendiri (atau hanya order pelanggan portal itu sendiri), pencarian nama/ID order, filter status, pagination 10/25/50 dan ringkasan status seluruh cakupan sebelum filter. Daftar tidak membocorkan URL checkout privat. GET daftar/detail tidak menghubungi Midtrans. Detail order menampilkan maksimal 20 riwayat verifikasi, beserta hasil saat pertama diterima, tanpa payload mentah/signature. Riwayat order juga tersedia pada detail faktur. Halaman order privat menampilkan status, nominal dan waktu pembaruan WIB. Pembaca faktur tanpa izin pembayaran dapat membaca riwayat, tetapi tidak membuat checkout atau meminta pemeriksaan status. Pelanggan tidak dapat mengakses order pelanggan lain, termasuk pelanggan dalam ISP yang sama. Checkout dan refresh memerlukan POST+CSRF dan pembatasan permintaan persisten; GET tidak menghubungi Midtrans.

## Webhook dan pemulihan gangguan

Atur **Payment Notification URL** pada dashboard sandbox merchant:

```text
https://<domain-pengujian-HTTPS>/payments/midtrans/notifications/
```

Endpoint publik ini menerima POST JSON maksimal 16 KiB. Hanya endpoint notifikasi dikecualikan dari CSRF karena bukan request sesi browser. Signature diperiksa menggunakan SHA-512 atas `order_id + status_code + gross_amount + server_key` dan perbandingan waktu konstan. Signature valid masih harus diikuti pemeriksaan GET status server-to-server; status/fraud/merchant yang hanya disampaikan callback tidak dipercaya. JSON duplikat, nominal pecahan, signature salah, merchant/mata uang/order tidak cocok, atau transaksi berganti tidak dapat menulis ledger. Payload mentah, signature, dan secret tidak disimpan pada audit/event. URL checkout privat tersimpan di database dan harus dilindungi bersama backup.

Callback valid yang terverifikasi mengembalikan 200, termasuk replay. Signature salah mendapat 403; input salah 400; order tidak dikenal 404; kegagalan verifikasi/koneksi mendapat 503. Callback berulang atau status terlambat tidak menggandakan pembayaran dan tidak menurunkan pembayaran tercatat menjadi pending/expire. Refund/chargeback menjadi **Perlu rekonsiliasi** dengan ledger asli tetap ada. Jika refund/chargeback sudah terverifikasi sebelum respons status sukses yang terlambat, respons sukses itu tidak boleh membuat pembayaran baru. Riwayat sengketa menahan kredit otomatis dan membutuhkan rekonsiliasi, termasuk bila callback datang berulang. Reversal manual untuk pembayaran gateway ditolak karena bukan refund di provider.

Tombol **Periksa status pembayaran** mengambil status langsung. Rekonsiliasi tanpa membuat checkout baru juga dapat dijadwalkan melalui:

```bash
.venv/bin/python manage.py reconcile_gateway_payments --limit 100
# Periksa juga transaksi tercatat untuk mendeteksi notifikasi refund/chargeback yang terlewat:
.venv/bin/python manage.py reconcile_gateway_payments --include-settled --tenant 1 --limit 100
```

Perintah ini satu batch, bukan daemon; limit 1–500, ID tenant positif opsional, output hanya jumlah berhasil/gagal tanpa data kredensial. Secara default hanya order belum selesai/perlu rekonsiliasi diperiksa. `--include-settled` juga memeriksa pembayaran tercatat untuk menemukan refund/chargeback yang tidak pernah diterima melalui webhook. `--tenant` membatasi satu ISP. Pemeriksaan berhasil maupun gagal memutar urutan antrean agar satu order bermasalah tidak menghalangi order lain. Untuk order yang status API-nya belum tersedia, termasuk Snap yang belum memilih metode pembayaran, 404 tetap dianggap belum terverifikasi. Jangan menganggapnya lunas, menghapus order, atau membuka reservasi secara lokal. Snap diminta dengan expiry 24 jam, tetapi aplikasi melepaskan reservasi hanya setelah menerima status terminal yang terverifikasi. Timeout setelah Snap berhasil membuat token tetapi sebelum respons diterima dapat memerlukan pemeriksaan merchant/dashboard karena token redirect lama tidak dapat dipulihkan melalui Status API. Pengakhiran jarak jauh kini dapat diminta owner untuk transaksi pending yang dikenali, melalui alur di bawah. Resolusi manual order tidak dikenal/404, pemulihan Snap token yang responsnya hilang, dan refund provider belum disediakan; ini batas cakupan sandbox, bukan prosedur operasional produksi.

Jika provider melaporkan pembayaran melebihi sisa faktur, order diberi status rekonsiliasi dan tidak membuat saldo negatif. Setelah pencatatan manual yang keliru dikoreksi melalui reversal berizin, periksa status lagi: settlement yang sama dapat dinilai ulang terhadap saldo terkini dan mencatat tepat satu pembayaran gateway. Bukti verifikasi pertama tetap berstatus review, dengan audit terpisah untuk pemulihan; replay tidak menggandakan payment/event/job. Pemulihan otomatis ini ditolak bila order pernah memiliki refund/chargeback terverifikasi. Periksa merchant dan ledger sebelum refund/penyesuaian; penyesuaian keuangan otomatis belum diimplementasikan.

## Mengakhiri pembayaran sandbox

Owner membuka detail order yang masih Menyiapkan checkout atau Menunggu pembayaran, mengisi alasan 10–250 karakter dan konfirmasi peninjauan, lalu memilih **Minta pengakhiran pembayaran**. Aksi memakai POST+CSRF, pembatasan permintaan, scope ISP, status akun aktif, session version dan MFA terbaru lima menit. Kasir, keuangan, operasional, NOC dan pelanggan tidak berwenang. GET hanya menampilkan form; tidak menghubungi gateway. Pengakhiran ini tidak melunasi faktur, tidak menghapus ledger, dan bukan refund.

Aplikasi menyimpan `GatewayExpiry` beserta UUID permintaan, pemohon, alasan asli, waktu, status dan audit **sebelum** operasi jaringan. Jika formulir dikirim ulang dengan UUID sama, intent dipakai kembali; alasan berbeda dengan UUID yang sudah digunakan ditolak. Permintaan yang belum pasti ditampilkan dengan UUID/alasan awal untuk dicoba ulang. Hasil terminal tidak mengirim POST baru saat direplay. Dua pengiriman bersamaan dapat mencapai gateway dengan order ID yang sama, tetapi satu intent dan hasil terminal tersimpan; tidak ada penghapusan atau penambahan pembayaran karena replay.

Urutan pemeriksaan:

1. Ambil GET status melalui kredensial merchant milik ISP, dan validasi order/merchant/amount/IDR/transaction ID seperti saat rekonsiliasi.
2. Bila pembayaran sudah selesai, terjadi refund/chargeback atau status tidak dikenal, jangan kirim expire. Settlement yang sah tetap masuk ledger sesuai aturan biasa.
3. Hanya untuk status pending, periksa lagi izin dan MFA, lalu kirim POST ke host tetap `https://api.sandbox.midtrans.com/v2/<order_id>/expire`.
4. Ambil GET status lagi, termasuk jika POST timeout atau ditolak. Respons POST sendiri tidak pernah melepaskan reservasi.
5. Hanya status terminal expire/cancel/deny yang sudah terverifikasi dapat menghasilkan **Dikonfirmasi berakhir**. Pembayaran yang menang dalam race tetap tercatat, dan permintaan berstatus **Tidak dapat diakhiri**. Gangguan atau status yang masih pending menghasilkan **Belum dapat dipastikan**, dengan reservasi tetap ada.

Status awal **Diminta** juga dapat dipakai ulang jika proses berhenti sebelum hasil tersimpan. Tidak ada umur lokal yang otomatis membebaskan order. Setelah pengakhiran terkonfirmasi, checkout pengganti memakai UUID order baru dan sisa tagihan dari server; faktur lama tetap belum lunas. Menonaktifkan checkout baru pada setelan tidak menghalangi pengakhiran order lama, selama kredensial sandbox tetap tersedia dan SIMULATION=1. Pencabutan izin/sesi atau lewatnya batas MFA setelah preflight mencegah pengiriman write baru.

Riwayat maksimal sepuluh permintaan ditampilkan hanya kepada owner, termasuk alasan plaintext dan waktu WIB. Seluruh intent masuk backup/restore terenkripsi. Audit dan penyiapan intent berada di transaksi yang dapat dicoba ulang, sedangkan operasi HTTP berada di luar transaksi; retry lock database tidak mengulangi POST jaringan. Jika layanan gagal setelah remote request, ulangi UUID yang sama untuk memeriksa kebenaran status terlebih dahulu.

## Bukti pengujian dan batasnya

Suite domain/HTTP mencakup autentikasi, MFA/CSRF/izin/tenant/customer, nominal server, signature palsu, pengambilan status, replay, fraud, status terlambat, penolakan lebih bayar, timeout, disable, merchant reuse, serta race checkout/settlement. Verifier PostgreSQL menjalankan dua proses independen untuk reservasi dan pencatatan settlement. Restore terenkripsi SQLite/PostgreSQL membandingkan tabel gateway dan ledger selain data lama. `scripts/gateway_smoke.py` memakai Chromium sesungguhnya pada server/database sementara, transport Midtrans tiruan, dan empat viewport 360/768/1366/1920; tidak membuka situs pembayaran eksternal atau mengirim uang.

Kontrak endpoint dan bentuk payload diperiksa terhadap [SDK Python resmi Midtrans](https://github.com/Midtrans/midtrans-python-client), juga artefak `midtransclient` 1.4.2. SDK tidak ditambahkan sebagai dependensi runtime. Keberhasilan tes lokal tidak membuktikan kredensial, konfigurasi merchant, pengiriman notifikasi HTTPS publik, metode pembayaran, atau CI remote sudah bekerja. Jalankan satu checkout, pending, settlement, replay notifikasi, dan rekonsiliasi terhadap akun sandbox nyata setelah konfigurasi tersedia. Gateway produksi memerlukan pekerjaan dan verifikasi tersendiri.
