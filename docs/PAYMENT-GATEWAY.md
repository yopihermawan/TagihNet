# Midtrans Sandbox — checkpoint kembali dari checkout 11 November 2026 WIB

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

Jalankan migrasi sampai `0012` setelah mencadangkan database yang sudah berisi data (`0008`: gateway/payment source; `0009`: pengakhiran; `0010`: kebijakan polling; `0011`: waktu pemeriksaan/verifikasi; `0012`: bukti pencatatan refund penuh). Migrasi 0012 tidak membalik pembayaran lama secara otomatis:

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

Menu **Pembayaran gateway** menampilkan daftar order ISP sendiri (atau hanya order pelanggan portal itu sendiri), pencarian nama/ID order, filter status, pagination 10/25/50 dan ringkasan status seluruh cakupan sebelum filter. Daftar tidak membocorkan URL checkout privat. GET daftar/detail tidak menghubungi Midtrans. Detail order menampilkan maksimal 20 riwayat verifikasi, beserta hasil saat pertama diterima, tanpa payload mentah/signature. Riwayat order juga tersedia pada detail faktur. Halaman order privat menampilkan status, nominal dan waktu pembuatan serta verifikasi terakhir WIB. Pembaca faktur tanpa izin pembayaran dapat membaca riwayat, tetapi tidak membuat checkout atau meminta pemeriksaan status. Pelanggan tidak dapat mengakses order pelanggan lain, termasuk pelanggan dalam ISP yang sama. Checkout dan refresh memerlukan POST+CSRF dan pembatasan permintaan persisten; GET tidak menghubungi Midtrans.

## Kembali dari checkout dan login ulang

Administrator dapat mengatur `TAGIHNET_PUBLIC_ORIGIN=https://billing.example.id` pada environment process dan menambahkan hostname itu ke `TAGIHNET_ALLOWED_HOSTS`. Gunakan origin saja, tanpa path, kredensial, query atau fragment. Nilai tidak valid menghentikan startup dengan pesan generik. HTTPS wajib; untuk pengujian lokal dengan DEBUG=1, HTTP localhost/127.0.0.1/[::1] dengan port eksplisit juga diterima. Alamat loopback bukan endpoint yang dapat dijangkau Midtrans. Perubahan konfigurasi memerlukan restart proses aplikasi.

Jika origin tersedia, setiap checkout Snap **baru** menyertakan `callbacks.finish` menuju `/payments/orders/<UUID>/return/` pada origin tersebut. Host browser, forwarded host dan field POST tidak menentukan alamat ini. Callback per-transaksi diperiksa terhadap [tipe Callbacks pada SDK Go resmi](https://github.com/Midtrans/midtrans-go/blob/bb1a9d781c69e48a956b2ed129328ffe18d7689e/snap/request.go#L183). Checkout yang URL/token-nya sudah disimpan tetap memakai konfigurasi saat dibuat. Jika origin belum diatur, payload Snap tetap tanpa override callbacks; dashboard merchant menentukan perilaku kembali.

Endpoint kembali hanya menerima GET. Semua parameter URL, termasuk order_id, transaction_status, status_code dan next, diabaikan. GET tidak mengambil status gateway atau mengubah ledger. Pengguna yang masih login diarahkan ke detail order setelah pembatasan tenant/customer diperiksa. Halaman menjelaskan bahwa kembali dari checkout belum membuktikan pembayaran berhasil; tombol **Periksa status pembayaran** tetap memakai POST/CSRF dan verifikasi server-ke-server. Webhook dan worker tetap bekerja secara terpisah.

Jika sesi telah berakhir, endpoint mengarahkan ke login tanpa membaca atau mengungkap keberadaan order. Session menyimpan UUID order serta waktu awal selama maksimal sepuluh menit. Metadata yang tervalidasi ini dipertahankan saat session dihapus/dirotasi untuk password, MFA dan enrollment pertama; data session lain tidak dipertahankan. Password, MFA wajib operator, pemeriksaan pencabutan akses dan rotasi session tetap berlaku. Setelah login, tujuan dipakai sekali dan hak tenant/customer diperiksa lagi. Login akun yang tidak berhak tidak dapat melihat order; tujuan kedaluwarsa kembali ke halaman awal akun. Enrollment pertama tetap menampilkan kode pemulihan sebelum tombol lanjut mengarah ke order. Tidak ada URL eksternal atau nilai next umum yang disimpan sebagai tujuan login.

Setelan owner menampilkan origin dan **Payment Notification URL** berdasarkan konfigurasi yang sama, tanpa secret. Tempel alamat notifikasi itu ke dashboard sandbox merchant. Origin aplikasi, URL kembali dan endpoint notifikasi memiliki peran berbeda: kembali dari browser bukan bukti pembayaran. Pengiriman browser Midtrans sesungguhnya dan akses HTTPS publik masih perlu diuji setelah domain/kredensial tersedia.

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

## Mencatat refund penuh yang sudah selesai

Owner membuka order dengan pembayaran gateway tercatat, mengisi alasan 10–250 karakter, meninjau dampaknya pada sisa tagihan dan memilih **Catat refund penuh yang sudah terjadi**. POST memerlukan CSRF, scope ISP, pembatasan permintaan, akun aktif, session version dan MFA terbaru lima menit. Izin diperiksa ulang setelah GET status kembali. GET halaman hanya menampilkan informasi. Kasir, keuangan, NOC, operasional dan pelanggan tidak dapat mencatat koreksi ini.

Aplikasi mengambil GET status baru menggunakan kredensial merchant ISP. Identitas order/merchant/transaction ID, gross_amount dan IDR wajib sesuai. Hanya `transaction_status=refund`, `status_code=200` dan `refund_amount` rupiah bulat yang sama dengan seluruh nominal pembayaran gateway dapat diterima. Nominal dari browser, isi webhook atau event lama tidak mengizinkan reversal. Respons tanpa refund_amount, refund sebagian, nominal salah dan riwayat chargeback/partial_chargeback tetap perlu ditinjau. Riwayat partial_refund dapat diselesaikan bila GET terbaru menyatakan seluruh nominal telah direfund. Full refund mengacu pada pembayaran gateway tersebut; bila faktur sebelumnya dibayar sebagian secara manual, hanya alokasi gateway yang dibuka kembali.

Dalam satu transaksi terkunci, aplikasi mempertahankan Payment asli, menambah satu PaymentReversal dan `GatewayRefundResolution`, menetapkan **Refund penuh tercatat**, membuka kembali sisa tagihan dan mengantrekan rekonsiliasi jaringan simulasi. Worker jaringan menghitung utang lagi saat dijalankan. Faktur tidak dibatalkan. Aplikasi tidak mengirim permintaan refund ke provider atau memindahkan dana. Operator harus menyelesaikan refund di Midtrans terlebih dahulu. Menonaktifkan checkout baru tidak menghalangi koreksi order lama bila kredensial dan SIMULATION=1 tetap tersedia.

Satu order hanya mempunyai satu resolusi dan satu koreksi. Replay UUID/alasan yang sama mengembalikan hasil lama tanpa GET atau koreksi kedua; UUID/alasan yang berbeda setelah selesai ditolak. HTTP berada di luar transaksi, sehingga retry lock hanya mengulang penulisan database. Respons settlement/pending yang terlambat tidak membatalkan koreksi atau membuat pembayaran baru. Refund penuh yang sama mempertahankan status selesai; laporan sengketa atau nominal refund yang bertentangan ditandai perlu rekonsiliasi tanpa reversal kedua. Opsi pemeriksaan transaksi tercatat pada worker maupun CLI juga mencakup order refunded.

Bukti menyimpan actor, waktu verifikasi, nominal, transaction ID dan digest SHA-256 metadata yang dinormalisasi; digest ini bukan signature provider. Payload mentah dan kredensial tidak disimpan. Event verifikasi pertama tetap menunjukkan hasil saat diterima, sementara resolusi menunjukkan koreksi sesudah persetujuan owner. Alasan owner hanya ditampilkan kepada owner dan audit internal; portal melihat ringkasan dan alasan koreksi generik. Pembayaran, reversal, bukti serta saldo yang terbuka kembali ikut diverifikasi dalam restore terenkripsi.

Kontrak `refund_amount` diperiksa terhadap tipe GET status pada [SDK Go resmi, TransactionStatusResponse](https://github.com/Midtrans/midtrans-go/blob/bb1a9d781c69e48a956b2ed129328ffe18d7689e/coreapi/response.go#L84) dan [CheckTransaction](https://github.com/Midtrans/midtrans-go/blob/bb1a9d781c69e48a956b2ed129328ffe18d7689e/coreapi/clienttransaction.go). Respons merchant sandbox asli belum diuji. Bila metode pembayaran tidak menyertakan total refund pada GET status, pencatatan ditolak sampai kontraknya dapat diverifikasi. Refund sebagian, chargeback, refund provider dari aplikasi dan koreksi pembayaran gateway tanpa bukti ini belum didukung.

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

## Pemeriksaan status otomatis

Pada **Payment gateway → Pemeriksaan status otomatis**, owner dapat mengaktifkan pemeriksaan berkala dengan MFA terbaru dan persetujuan eksplisit bahwa pembayaran terverifikasi akan dicatat. Default nonaktif, terpisah dari sakelar checkout baru. Interval yang tersedia 1/5/15 menit; pemeriksaan pembayaran yang sudah tercatat untuk mencari refund/chargeback juga opt-in. Gateway checkout boleh dinonaktifkan sementara pemeriksaan order lama tetap berjalan. Kunci merchant tetap berada di konfigurasi privat; database kebijakan hanya menyimpan ID merchant dan bukti persetujuan.

```bash
.venv/bin/python manage.py run_gateway_worker --once --limit 20
# Layanan kontinu, dihentikan dengan Ctrl+C:
.venv/bin/python manage.py run_gateway_worker --limit 20
```

Worker memeriksa maksimal 10 kebijakan yang jatuh tempo per putaran dan maksimal 20 order per kebijakan secara bawaan (limit 1–100). Jadwal adalah jeda minimum antarputaran; pemeriksaan setiap order bergantung backlog dan jumlah worker. Order yang belum pernah diperiksa didahulukan, kemudian yang waktu pemeriksaannya paling lama. Keberhasilan maupun kegagalan memutar urutan sehingga satu order bermasalah tidak menghalangi yang lain. `--once` mengembalikan gagal jika batch yang dilaporkan masih mempunyai status gagal/sebagian; progres dan jadwal tetap tersimpan. Mode di luar SIMULATION ditolak. Worker tidak membuat checkout, tidak mengirim expire, tidak meminta refund, dan tidak mengirim pesan.

Setiap kebijakan memakai UUID lease selama 60 detik yang diperbarui sebelum tiap request. Tidak ada transaksi database yang dibiarkan terbuka selama HTTP. Selama lease valid hanya satu worker mengerjakan kebijakan; lease kedaluwarsa dapat diambil ulang setelah proses terhenti. Worker lama tidak dapat menimpa hasil/jadwal dari token atau versi baru. Jika proses berhenti saat request sedang berlangsung, status yang kembali tetap melewati writer ledger idempoten; lease tidak membatalkan request yang sudah dikirim. Simpan ulang atau menonaktifkan kebijakan membatalkan lease, dan worker berhenti sebelum request berikutnya. Hasil yang sudah terverifikasi saat perubahan berlangsung tetap dicatat.

Akses owner pemberi persetujuan, status akun, tenant, session version dan mode simulasi diperiksa ulang sebelum batch dan setiap order. Persetujuan yang tidak berlaku menonaktifkan kebijakan dan menampilkan **Perlu persetujuan ulang**; owner harus meninjau dan menyimpan ulang dengan MFA. Kredensial yang sementara hilang menghasilkan kegagalan verifikasi dengan jadwal ulang, bukan pemakaian merchant ISP lain.

Jika seluruh pemeriksaan gagal, interval berkembang dua kali lipat pada kegagalan berikutnya, maksimal satu jam. Keberhasilan atau putaran kosong mereset rangkaian kegagalan. Batch dengan hasil campuran tetap memakai interval normal agar kegagalan satu order tidak memperlambat order lain. Status **Pemeriksaan selesai** berarti pemeriksaan API pada batch berhasil; bukan berarti semua order lunas atau masalah keuangan sudah diselesaikan.

Halaman setelan menampilkan putaran terakhir, verifikasi sukses terakhir, jumlah hasil, status dan jadwal berikutnya. Order mempunyai waktu percobaan dan waktu verifikasi sukses yang terpisah. Kegagalan tidak memperbarui waktu verifikasi, dan daftar/detail menandai pemeriksaan terakhir gagal. Migrasi 0011 memakai waktu event verifikasi sebagai bukti untuk data lama; updated_at lama yang juga pernah mencatat kegagalan tidak dipercaya untuk backfill. Order tanpa bukti event tetap belum terverifikasi.

Kebijakan, persetujuan, jadwal, backoff dan lease ikut backup/restore. Restore tidak menjalankan proses worker secara otomatis; jalankan lagi layanan dan biarkan lease lama kedaluwarsa sebelum mengambil alih. Penjadwalan/lease telah diuji pada SQLite dan PostgreSQL lokal, bukan bukti kapasitas produksi atau akses akun Midtrans asli.

## Bukti pengujian dan batasnya

Suite domain/HTTP mencakup autentikasi, MFA/CSRF/izin/tenant/customer, nominal server, signature palsu, pengambilan status, replay, fraud, status terlambat, penolakan lebih bayar, timeout, disable, merchant reuse, serta race checkout/settlement. Verifier PostgreSQL menjalankan dua proses independen untuk reservasi dan pencatatan settlement. Restore terenkripsi SQLite/PostgreSQL membandingkan tabel gateway dan ledger selain data lama. `scripts/gateway_smoke.py` memakai Chromium sesungguhnya pada server/database sementara, transport Midtrans tiruan, dan empat viewport 360/768/1366/1920; tidak membuka situs pembayaran eksternal atau mengirim uang.

Kontrak endpoint dan bentuk payload diperiksa terhadap [SDK Python resmi Midtrans](https://github.com/Midtrans/midtrans-python-client), juga artefak `midtransclient` 1.4.2. SDK tidak ditambahkan sebagai dependensi runtime. Keberhasilan tes lokal tidak membuktikan kredensial, konfigurasi merchant, pengiriman notifikasi HTTPS publik, metode pembayaran, atau CI remote sudah bekerja. Jalankan satu checkout, pending, settlement, replay notifikasi, dan rekonsiliasi terhadap akun sandbox nyata setelah konfigurasi tersedia. Gateway produksi memerlukan pekerjaan dan verifikasi tersendiri.
