# TagihNet — Prompt Redesign, Audit, dan Hardening

Disusun 31 Oktober 2026 berdasarkan peninjauan langsung demo KelolaNet melalui akun demo.

## Cara menggunakan

Salin seluruh bagian **Prompt utama** ke AI coding agent, atau lampirkan file ini dan instruksikan: “Jalankan Prompt utama dalam dokumen ini untuk membangun TagihNet.” Sertakan repositori milik Anda bila ada. Tanpa repositori, pekerjaan menjadi pembangunan aplikasi baru; audit kode aplikasi referensi belum dapat dilakukan.

## Catatan audit awal dari demo

Referensi: https://demo.kelolanet.id/login

Login berhasil. Halaman yang ditinjau: login, dashboard, pelanggan beserta detail, faktur beserta rincian, setelan billing, Admin & Role, monitor jaringan, tiket, PSB, dan dasbor keuangan. Modul lain diinventarisasi dari navigasi; fungsi internalnya belum diperiksa. Akun menampilkan pemberitahuan demo hanya-lihat. Tidak dilakukan perubahan data bisnis, pengiriman pesan, pembayaran, pemutusan pelanggan, pemindaian kerentanan, atau percobaan eksploitasi.

Penilaian berikut merupakan observasi UI dan kandidat pemeriksaan lanjutan. Bukan bukti bahwa aplikasi referensi memiliki semua risiko keamanan yang dibahas dalam prompt.

| Area | Yang terlihat saat peninjauan | Implikasi untuk TagihNet |
| --- | --- | --- |
| Navigasi | Sidebar memuat banyak kelompok dan submenu, mencakup operasional, RADIUS, billing, marketing, keuangan, payroll, jaringan, dan sistem. | Ringkas menjadi kelompok tugas; gunakan menu yang dapat dilipat, pencarian menu, dan visibilitas sesuai peran. |
| Tabel pelanggan | Pada viewport desktop sekitar 1360 px, sebagian tindakan baris berada di luar area yang langsung terlihat. | Kolom aksi tetap terlihat, kolom dapat dipilih, dan detail sekunder dipindah ke drawer. |
| Pagination | Daftar pelanggan menampilkan pilihan “10” baris, tetapi rentang halaman “1–20” dan 20 baris data. | Gunakan satu sumber state untuk ukuran halaman, data, rentang, dan URL; uji pergantian ukuran serta filter. |
| Statistik koneksi | Dashboard memperlihatkan PPPoE online 0 dan hotspot online 0, sementara widget jaringan pada tampilan yang sama memperlihatkan 355 online: 331 PPPoE + 24 hotspot. | Tampilkan sumber, cakupan NAS, waktu pembaruan, dan apakah data merupakan simulasi. Penyebab perbedaan belum diketahui. |
| Statistik billing | Dashboard menunjukkan penghasilan bulanan Rp1.150.000 / 4 pembayaran, sedangkan halaman faktur menunjukkan “Lunas bulan ini” Rp150.000 / 23 faktur. | Jelaskan perbedaan pembayaran diterima dan faktur berstatus lunas, periodenya, serta tanggal yang dipakai. Angka berbeda ini belum membuktikan kesalahan akuntansi. |
| Aksi faktur | Beberapa tindakan penerbitan dan pembaruan status ditampilkan berdekatan, termasuk terbitkan faktur langganan, tagihan manual, terbitkan jatuh tempo, dan tandai overdue. | Jadikan “Buat faktur” tindakan utama; tempatkan otomatisasi dan operasi massal dalam kelompok terpisah dengan pratinjau dampak. |
| Footer faktur | Kolom teks footer berisi markup berbentuk tag script yang menunjuk sumber eksternal. Tidak diuji apakah markup dieksekusi di web, cetak, atau PDF. | Prioritas verifikasi tinggi: telusuri sumber-ke-renderer, gunakan output encoding, batasi format teks, serta isolasi renderer PDF. Ini kandidat risiko, bukan XSS terkonfirmasi; severity menunggu bukti. |
| Peta detail pelanggan | Iframe peta memperlihatkan pesan WebGL tidak didukung pada browser pemeriksaan; tautan peta eksternal tersedia. | Sediakan fallback peta 2D atau tautan lokasi. Ini keterbatasan yang teramati di lingkungan pemeriksaan, bukan bukti peta selalu gagal. |
| Data demo | Sejumlah baris menampilkan “tanpa paket”, identitas pelanggan kosong pada faktur, atau faktur Rp0. | Gunakan seed data yang konsisten dan label data contoh; audit relasi serta aturan faktur nol sebelum menyimpulkan data produksi bermasalah. |
| Batas audit keamanan | Tidak tersedia kode sumber, konfigurasi server, akses log, atau beberapa akun dengan peran berbeda. | Auth, tenant isolation, webhook pembayaran, enkripsi, backup, dan server-side demo guard belum terverifikasi. Audit teknis dilakukan pada kode TagihNet atau repositori yang sah diberikan. |

## Prompt utama

Anda bertindak sebagai product designer, UX engineer, full-stack engineer, dan application security engineer. Bangun ulang pengalaman aplikasi billing ISP dengan merek **TagihNet**, menggunakan KelolaNet di https://demo.kelolanet.id/login sebagai referensi cakupan fitur dan alur yang dapat dilihat. Hasilkan aplikasi operasional yang lebih fresh, mudah dipakai, konsisten, dan dapat diuji.

Gunakan catatan audit awal di dokumen ini sebagai input, bukan sebagai vonis keamanan terhadap aplikasi referensi. Bangun desain, kode, ikon/logo, dan aset orisinal. Jangan mengambil source code, aset berlisensi terbatas, atau data pelanggan dari layanan referensi. Gunakan data fiktif. Kredensial demo referensi tidak boleh menjadi akun bawaan TagihNet.

### 1. Hasil yang diminta dan cara bekerja

- Kerjakan desain ulang, implementasi, audit, perbaikan, dan verifikasi secara nyata. Jangan berhenti pada rekomendasi, mockup dashboard, atau tombol tanpa fungsi.
- Jika ada repositori pengguna: baca instruksi proyek, inventarisasi stack, route, skema, migrasi, integrasi, worker, dan tes; pertahankan perilaku bisnis serta kompatibilitas yang masih benar. Buat perubahan di branch terpisah. Utamakan refactor bertahap daripada rewrite tanpa kebutuhan terukur.
- Jika repositori belum tersedia: buat proyek TagihNet baru, pilih stack yang stabil dan dipelihara, jelaskan alasan singkat, lalu implementasikan. Jangan mengaku telah mengaudit backend KelolaNet. Pisahkan frontend, domain billing, adapter jaringan, dan background worker secara jelas.
- Hindari perubahan destruktif pada data dan produksi. Siapkan migrasi, cadangan, strategi rollback, dan validasi rekonsiliasi sebelum perpindahan data.
- Mulai dengan satu ISP dan data demo. Jika greenfield menggunakan model multi-tenant, tegakkan batas tenant sejak awal dan sediakan minimal dua tenant uji. Jangan menambahkan sistem langganan SaaS komersial sebagai penghalang penyelesaian billing inti.
- Lanjutkan keputusan implementasi rutin secara mandiri. Nyatakan asumsi dan blocker konkret; jangan mengarang keberhasilan integrasi yang belum dapat dibuktikan.

### 2. Identitas visual TagihNet

Nama: **TagihNet**. Tagline: **Billing rapi, jaringan terkendali.**

Gunakan karakter visual bersih, profesional, tenang, dan modern untuk pekerjaan harian operator ISP/RT RW Net. Dashboard harus membantu menemukan tindakan berikutnya dengan cepat.

- Light mode sebagai default, dark mode konsisten dengan kontras yang diuji.
- Palet awal: navy #0F172A untuk struktur dan teks utama, teal #0F766E untuk tindakan utama, latar #F8FAFC, permukaan putih, garis #E2E8F0. Gunakan hijau/amber/merah untuk status yang selalu disertai teks atau ikon. Sesuaikan token jika hasil uji kontras belum memenuhi target.
- Tipografi Inter atau Plus Jakarta Sans dengan fallback sistem; body sekitar 14–16 px. Angka nominal menggunakan tabular numerals dan rata kanan.
- Sistem jarak kelipatan 4/8 px, radius sekitar 10–14 px, border halus, bayangan ringan, dan ikon dengan gaya konsisten. Kurangi ornamen dan kartu warna penuh pada statistik rutin.
- Logo TagihNet orisinal yang sederhana, terbaca pada sidebar kecil, tersedia versi terang/gelap dan favicon. Ganti identitas pada login, metadata, portal, pesan, cetak, dan PDF. Domain serta identitas perusahaan tetap dapat dikonfigurasi.
- Responsif pada lebar 360, 768, 1366, dan 1920 px. Tidak ada scroll horizontal seluruh halaman; tabel kompleks boleh memiliki area gulir lokal dengan petunjuk yang jelas.
- Target aksesibilitas WCAG 2.2 AA: label nyata, navigasi keyboard, fokus terlihat, dialog mengelola fokus, pesan error terkait field, pembacaan status oleh screen reader, dan kontras memadai. Target sentuh praktis sekitar 44 px untuk aksi utama di mobile.
- Gunakan Bahasa Indonesia yang konsisten. Nominal berformat rupiah. Tampilkan waktu Asia/Jakarta/WIB; simpan timestamp dengan aturan timezone yang konsisten dan dokumentasikan tanggal bisnis.

### 3. Navigasi dan cakupan modul

Susun navigasi sebagai berikut; submenu yang jarang digunakan tetap dapat ditemukan melalui pencarian menu. Tampilkan menu berdasarkan hak akses, dengan pemeriksaan izin yang sama di backend.

| Kelompok | Fitur |
| --- | --- |
| Ringkasan | Dashboard pemilik/admin, billing, dan NOC sesuai peran; notifikasi dan daftar pekerjaan. |
| Pelanggan & Layanan | Daftar/detail pelanggan, paket/profil, siklus layanan, analisis pelanggan. |
| Penagihan | Faktur, pembayaran/transaksi, tunggakan, pengingat, setelan billing, laporan penagihan. |
| Jaringan | PPPoE/DHCP, sesi online, isolir, NAS/router, VPN, hotspot/voucher/pesanan, monitoring, diagnosis, peta ODC/ODP/pelanggan. |
| Operasional | Tiket gangguan, PSB, penugasan teknisi, aktivitas lapangan. |
| Keuangan & Tim | Kas, pengeluaran, sumber dana, tutup buku, payroll, karyawan, tim, bonus, komisi sales/referral. |
| Pemasaran | Paket per NAS, pengajuan upgrade, promo, halaman promosi, pengaturan portal pelanggan. |
| Pengaturan | Profil ISP, akun, admin dan role, WhatsApp/payment gateway, impor/ekspor, audit log, backup, panduan. |

Fitur yang belum diimplementasikan harus dicatat sebagai pekerjaan tersisa. Jangan menampilkan submenu seolah siap pakai jika hanya menuju halaman kosong.

### 4. Spesifikasi layar dan alur utama

**Login dan shell aplikasi**

Buat halaman login TagihNet yang ringkas, logo jelas, label input, tampil/sembunyikan sandi yang aksesibel, loading, error yang aman, dan alur pemulihan akun yang terkontrol. Sidebar dapat dilipat; topbar berisi breadcrumb, pencarian global, notifikasi, dan menu akun. Banner mode simulasi harus mudah dikenali tanpa memenuhi layar. Tampilkan empty, loading, error, dan offline state yang membantu pengguna bertindak.

**Dashboard**

Pisahkan ringkasan billing dan jaringan berdasarkan tab atau peran. Billing menampilkan pembayaran diterima, nilai faktur terbit, sisa piutang, jatuh tempo, dan tindakan yang perlu dikerjakan. NOC menampilkan perangkat bermasalah, sesi online, trafik, alarm aktif, dan status pengambilan data. Sediakan filter periode/NAS/wilayah, tautan menuju data rinci, serta aksi tambah pelanggan dan buat faktur.

Untuk setiap metrik tetapkan definisi, periode, sumber, cakupan, dan waktu update. Bedakan “pelanggan aktif”, “layanan diisolir”, “sesi sedang online”, dan “data belum tersedia”. Jangan mengubah kegagalan polling menjadi angka nol. Tunjukkan data kedaluwarsa dan alasan perbedaan widget. Pembayaran diterima dan faktur lunas merupakan metrik berbeda dan harus diberi definisi yang jelas.

**Pelanggan**

Daftar dengan pencarian nama/ID/username/telepon, filter layanan/status/paket/NAS/wilayah/jatuh tempo, sorting, pagination server-side, kolom pilihan, dan ringkasan hasil. Sinkronkan filter dan pagination ke URL. Kolom aksi tetap terjangkau; aksi sekunder ada dalam menu per baris.

Detail menggunakan halaman atau drawer bertab: Ringkasan, Layanan & Koneksi, Faktur, Pembayaran, Tiket, Lokasi, Riwayat. Tampilkan status finansial dan koneksi secara terpisah. Form pelanggan dibagi identitas, layanan, billing, serta lokasi dengan validasi inline dan deteksi data ganda. Detail tidak boleh menghilangkan posisi daftar saat ditutup. Peta memiliki fallback bila WebGL atau penyedia peta gagal.

**Faktur dan pembayaran**

Sediakan daftar dan detail faktur, item tagihan, periode layanan, tanggal terbit/jatuh tempo, diskon, pajak, pembulatan, pembayaran, sisa tagihan, catatan, dan histori. Tampilkan pratinjau sebelum penerbitan massal. Dukung cetak/PDF A4 yang rapi, logo, identitas ISP/pelanggan, nomor faktur, rincian, total, status, instruksi bayar, dan footer aman; uji multipage dan teks panjang.

Definisikan state machine faktur, pembayaran, dan layanan secara terpisah. Lunas ditetapkan dari pembayaran yang sah dan teralokasi. Overdue dihitung dari sisa tagihan dan tanggal bisnis. Terbit ulang/retry job tidak boleh menggandakan faktur. Koreksi pembayaran menggunakan reversal atau adjustment dengan jejak audit, bukan penghapusan histori. Tentukan aturan pembayaran sebagian/lebih bayar secara eksplisit sebelum mengaktifkannya.

Semua hitungan uang dilakukan server-side menggunakan integer unit terkecil atau decimal yang sesuai, bukan floating point. Simpan snapshot harga, diskon, dan aturan pajak pada faktur. Tarif pajak dapat dikonfigurasi sesuai kebutuhan bisnis yang diverifikasi; jangan mengasumsikan satu tarif berlaku untuk semua ISP.

**Setelan billing dan isolir**

Tampilkan timeline terbit → pengingat → jatuh tempo → tenggang → isolir, disertai contoh tanggal konkret yang berubah mengikuti pengaturan. Jelaskan perbedaan prabayar, pascabayar, tanggal tetap, dan perpanjangan. Tetapkan aturan tanggal 29–31, Februari, prorata, dan peralihan bulan. Pajak nol harus dapat dibedakan dari nilai yang belum diatur.

Tunjukkan pratinjau dampak perubahan pada pelanggan baru, layanan berjalan, dan faktur yang sudah terbit. Cegah isolir jika pembayaran sudah terverifikasi. Tangani pembayaran yang masuk bersamaan dengan job isolir menggunakan transaksi, pengecekan ulang kondisi, dan rekonsiliasi. Pisahkan status perintah “menunggu/dikirim/gagal/terkonfirmasi” dari status layanan aktual.

**Tiket, PSB, keuangan, dan modul lanjutan**

Tiket memiliki prioritas, kategori, pelanggan, penanggung jawab, SLA yang dikonfigurasi, lampiran, komentar, dan timeline. PSB mencakup prospek → survei → jadwal → pengerjaan → terpasang → aktivasi, serta pembatalan dengan alasan. Aktivasi tidak boleh menggandakan pelanggan atau faktur.

Keuangan memisahkan arus kas, pendapatan, dan piutang; tampilkan periode serta dasar hitung. Tutup buku harus dapat direkonsiliasi, dibatasi izin, dan memiliki prosedur koreksi. Payroll dan komisi hanya terlihat oleh peran yang berwenang. Hotspot mencakup voucher, profil, sesi, template cetak, penjualan, dan pesanan dengan aturan masa berlaku jelas. Portal pelanggan berfokus pada layanan, tagihan, pembayaran, bukti bayar, dan tiket milik pengguna sendiri.

### 5. Hak akses dan pemisahan data

Buat role yang dapat disunting izin dan cakupannya oleh pemilik berwenang. Sediakan preset berikut sebagai titik awal:

| Peran | Akses awal |
| --- | --- |
| Owner ISP | Pengaturan dan laporan milik ISP sendiri, pengelolaan role, persetujuan aksi sensitif. |
| Admin operasional | Pelanggan, layanan, faktur, tiket/PSB sesuai izin; secret dan role tidak otomatis dapat dikelola. |
| Keuangan | Pembayaran, rekonsiliasi, kas, laporan dan koreksi yang diizinkan; tanpa konfigurasi router. |
| Kasir | Mencatat pembayaran dan mencetak kuitansi; tanpa mengubah harga atau menghapus histori. |
| NOC | Monitoring dan diagnosis; isolir/pemutusan hanya bila izin khusus diberikan. |
| Teknisi | Tiket, PSB, lokasi, dan data minimum pelanggan pada wilayah/penugasan sendiri. |
| Sales | Prospek, pengajuan, dan komisi miliknya; tanpa seluruh keuangan atau kredensial layanan. |
| Pelanggan | Layanan, faktur, pembayaran, dan tiket milik akun sendiri. |

Terapkan deny-by-default. Periksa aksi, objek, field, tenant, dan wilayah/penugasan pada backend, termasuk API, ekspor, file, cetak/PDF, pencarian, worker, cache, dan kanal realtime. Tenant ditentukan dari konteks autentikasi tepercaya; jangan percaya tenant_id dari request. Perubahan hak berlaku pada sesi aktif. Lindungi agar admin tidak dapat memberi izin di atas kewenangannya atau menghapus pemilik terakhir. Jika ada administrator platform SaaS, jangan beri akses isi data pelanggan semua ISP secara otomatis.

### 6. Audit keamanan dan hardening

Gunakan **OWASP ASVS 5.0.0 Level 2 sebagai target verifikasi yang relevan**, bukan klaim sertifikasi. Buat daftar kontrol applicable, not applicable, pass, fail, dan not tested beserta bukti. Gunakan dokumentasi resmi stack yang benar-benar dipilih.

**Autentikasi dan sesi**

Gunakan password hashing modern yang didukung framework untuk akun web, tanpa password default produksi. Terapkan pembatasan percobaan login dan reset, error login generik, reset token acak/sekali pakai/kedaluwarsa, rotasi sesi saat login/perubahan privilege, pencabutan sesi saat logout/reset, serta idle dan absolute timeout yang terdokumentasi. Gunakan cookie HttpOnly, Secure pada HTTPS, SameSite sesuai alur, dan perlindungan CSRF untuk autentikasi cookie. Sediakan MFA untuk owner dan akun berprivilege; jangan mencatat sandi, OTP, token, atau cookie sesi.

**Validasi, XSS, file, dan renderer**

Validasi tipe, panjang, format, dan aturan bisnis di server. Gunakan query terparameterisasi/ORM yang aman, daftar field yang boleh diubah, dan encoding sesuai konteks output. Footer faktur diperlakukan sebagai teks biasa secara default; jika rich text dibutuhkan, gunakan sanitizer dengan allowlist yang sempit. Larang eksekusi script dan pemuatan sumber eksternal tidak tepercaya pada web, cetak, email, dan PDF. Renderer PDF diisolasi, akses file lokal dinonaktifkan, dan akses jaringan dibatasi ke aset yang diizinkan. CSP merupakan lapisan tambahan, bukan pengganti output encoding.

Batasi upload menurut tipe konten yang diverifikasi, ekstensi, ukuran, nama acak, serta izin akses. Simpan di lokasi non-eksekusi; file privat harus memerlukan otorisasi. Cegah path traversal, formula injection pada ekspor spreadsheet, dan proses impor tanpa pratinjau/validasi. Batasi jumlah baris/ukuran job dan sediakan laporan error tanpa secret.

**Pembayaran dan integritas bisnis**

Jangan mempercayai nominal, diskon, harga, status lunas, atau parameter redirect dari browser. Verifikasi notifikasi payment gateway sesuai dokumentasi resmi: keaslian/signature, merchant/order, nominal, mata uang, dan status server-to-server. Simpan identitas event secara persisten untuk deduplikasi, gunakan transaksi database/constraint dan idempotency, serta tangani callback bersamaan, retry, replay, terlambat, dan urutan event yang berbeda. Rekonsiliasi status pembayaran sebelum mengubah saldo dan aktivasi. Signature freshness dan kedaluwarsa checkout merupakan aturan berbeda. Pisahkan secret sandbox/produksi. Jangan menyimpan data kartu sensitif.

**Kredensial jaringan dan integrasi**

Secret router, RADIUS, gateway pembayaran, dan kanal pesan hanya tersedia di backend/worker yang membutuhkan. Gunakan secret manager atau enkripsi dengan key terpisah dari database, masking di UI/log, pembatasan akses, dan rotasi. Bedakan hashing akun web dengan kebutuhan autentikasi PPP/RADIUS; dokumentasikan protokol yang dipilih dan proteksi penyimpanan yang sesuai, jangan mengasumsikan hash password web dapat dipakai untuk semua metode RADIUS.

Tangani SSRF pada URL webhook, diagnosis, gambar/PDF, VPN, dan koneksi perangkat. Bedakan koneksi internal router yang memang diizinkan dari URL bebas. Gunakan allowlist target perangkat/subnet/protokol/port yang disetujui, cek hasil resolusi DNS dan redirect, serta blok endpoint metadata cloud, loopback, link-local, dan tujuan lain yang tidak diperlukan. Jangan menyediakan shell/terminal dengan perintah mentah dari input pengguna.

**Infrastruktur dan operasi**

Gunakan HTTPS dengan validasi sertifikat, security header termasuk CSP/frame-ancestors/nosniff serta HSTS setelah HTTPS siap, CORS allowlist yang tepat, database/cache di jaringan privat, akun database dengan izin minimum, container non-root jika sesuai, debug off produksi, pembatasan resource, dan dependency/secret scanning. Jangan menonaktifkan TLS, CSRF, autentikasi, atau rate limit demi meloloskan tes.

Audit log merekam pelaku, tenant, target, aksi, hasil, waktu, dan correlation ID dengan perubahan sebelum/sesudah yang sudah disamarkan. Batasi akses dan pengubahan log; tentukan retensi. Alarm untuk kegagalan job, autentikasi, rekonsiliasi, dan backup. Buat backup terenkripsi, restore drill, serta prosedur pemulihan dan rollback yang benar-benar diuji.

### 7. Integrasi MikroTik/RADIUS dan mode simulasi

- Mode default adalah SIMULASI dengan indikator yang jelas. Router, webhook, dan kanal notifikasi memakai adapter palsu atau sandbox. Tidak ada pengiriman pesan, transaksi gateway, atau perintah perangkat produksi secara otomatis.
- Sediakan skenario: sesi PPPoE online/offline, router tidak dapat dihubungi, data kedaluwarsa, webhook duplikat, job gagal, dan pembayaran bersamaan dengan isolir.
- Pisahkan adapter read-only monitoring dari adapter perubahan layanan. Koneksi produksi memerlukan konfigurasi eksplisit, konektivitas privat/VPN atau collector bila diperlukan, dan hak minimum.
- Jika menggunakan FreeRADIUS, dokumentasikan alur autentikasi/accounting, sumber data sesi, shared secret, firewall, timeout, dan dukungan CoA/Disconnect pada lab. Jangan menyebut pemutusan sesi berhasil hanya berdasarkan respons API aplikasi.
- Uji isolir pada lab dari ujung ke ujung: sesi lama, dial ulang, profil/filter, FastTrack bila relevan, IPv4/IPv6, dan akses portal pembayaran yang memang diizinkan. Catat bagian yang belum dapat diuji.
- Pembayaran sah menimbulkan proses aktivasi yang idempoten. Jika router gagal merespons, status pembayaran tetap benar dan pekerjaan jaringan ditandai tertunda/gagal dengan retry terkontrol.
- Gunakan queue, retry dengan backoff, deduplikasi, timeout, rate limit perangkat, dan daftar job gagal. Pemicu terjadwal tidak boleh menggandakan tagihan atau notifikasi setelah restart.

### 8. Pengujian penerimaan

Prioritaskan tes yang memeriksa risiko dan alur nyata. Minimal buktikan:

1. Login/logout, pemulihan akun, timeout sesi, dan pencabutan akses bekerja.
2. Pengguna tanpa izin gagal mengakses endpoint secara langsung; menyembunyikan tombol saja tidak dianggap lulus.
3. Dua tenant/pelanggan berbeda tidak dapat saling membaca atau mengubah data lewat ID, pencarian, ekspor, file, PDF, job, atau realtime.
4. Pencarian, filter, sorting dan pagination konsisten; ukuran 10/25/50 cocok dengan baris dan rentangnya, serta URL dapat dibuka ulang.
5. Tambah/ubah pelanggan menyimpan data dan detail langsung menampilkan hasil yang sama setelah reload.
6. Retry serta dua job penerbitan bersamaan tidak membuat faktur ganda untuk layanan/periode/jenis yang sama menurut aturan bisnis.
7. Total rupiah, diskon, pajak, pembulatan, periode, prorata, Februari, dan tanggal 29–31 sesuai spesifikasi.
8. Webhook tidak sah, nominal salah, mata uang/order tidak cocok, dan status tak terverifikasi ditolak tanpa membuat lunas.
9. Callback sah yang duplikat/bersamaan/terlambat hanya memengaruhi ledger satu kali; event berbeda urutan dapat direkonsiliasi.
10. Pembayaran bersamaan dengan isolir tidak meninggalkan pelanggan terbayar dalam status salah tanpa rekonsiliasi atau peringatan.
11. Kegagalan router tidak membatalkan catatan pembayaran dan tidak menghasilkan indikator aktivasi palsu.
12. Teks berisi markup uji yang aman tidak dieksekusi pada detail, portal, cetak, atau PDF. Tes dilakukan pada aplikasi sendiri di lokal/staging.
13. Upload, impor, dan ekspor menolak input yang tidak sah serta mematuhi hak akses.
14. Akun demo dibatasi di server dan background worker; tidak dapat mengirim pesan, membayar, atau mengubah router nyata.
15. Semua angka dashboard mempunyai definisi dan cakupan yang dapat ditelusuri; data gagal/kedaluwarsa tidak menjadi nol atau online palsu.
16. UI lolos pemeriksaan visual desktop/mobile, keyboard, modal, kontras, state error/loading/empty, dan fallback peta. Aksi baris tetap mudah dijangkau.
17. Faktur PDF A4 dan multipage terbaca, nominal sesuai database, footer aman, dan tidak membuka data milik pengguna lain.
18. Backup dapat dipulihkan ke lingkungan kosong dan hasil jumlah pelanggan, saldo, serta faktur dapat direkonsiliasi.

Laporkan perintah tes, lingkungan, tanggal, hasil, bukti yang sudah disamarkan, dan blocker. Labeli pass, fail, not tested, atau simulated dengan jujur. Jangan membuat persentase kelulusan tanpa daftar tes yang menjadi dasarnya.

### 9. Urutan pelaksanaan dan keluaran

Kerjakan bertahap dengan checkpoint yang dapat dijalankan:

1. **Inventarisasi dan audit awal:** peta fitur/route/data, baseline perilaku, ancaman utama, prioritas, dan matriks izin.
2. **Fondasi desain dan satu alur lengkap:** design tokens, login/shell, pelanggan → faktur → pembayaran simulasi → detail/riwayat/PDF, dengan backend dan otorisasi sejak awal.
3. **Billing dan jaringan:** siklus otomatis, pengingat sandbox, adapter router/RADIUS, isolir/aktivasi lab, monitoring dan rekonsiliasi.
4. **Cakupan lanjutan:** hotspot, tiket/PSB, peta, keuangan/payroll, marketing, portal, impor/ekspor sesuai prioritas dan fitur referensi.
5. **Audit akhir dan persiapan rilis:** perbaikan temuan, pengujian penerimaan, performa terukur, backup/restore, panduan pemasangan dan rollback.

Berikan source code lengkap yang runnable, migrasi dan seed fiktif, contoh konfigurasi tanpa secret, daftar dependensi terkunci sesuai ekosistem, serta langkah menjalankan di Windows/Docker Desktop bila lingkungan itu digunakan. Semua tombol harus berfungsi atau secara jelas menunjukkan dependensi/fitur yang belum siap.

Sertakan dokumen berikut di proyek:

- README: setup, perintah run/build/test, akun uji yang dibuat khusus lingkungan lokal, dan troubleshooting.
- FEATURE-MATRIX: cakupan referensi, implementasi TagihNet, fase, dan status nyata.
- UX-AUDIT: observasi, masalah, perubahan, dan bukti visual dengan data fiktif.
- SECURITY-AUDIT: severity berdasarkan bukti, lokasi kode/endpoint, reproduksi aman, dampak, perbaikan, dan hasil retest.
- ASVS-CHECKLIST dan PERMISSIONS: cakupan verifikasi serta matriks peran/aksi/data.
- TEST-REPORT: tes yang dijalankan, hasil dan yang belum dapat diuji.
- OPERATIONS: migrasi, backup/restore, pemantauan worker, pengelolaan secret, deployment, dan rollback.

Mulai dari inventarisasi proyek dan implementasi fondasi TagihNet. Pertahankan progres di checkpoint, lanjutkan pekerjaan yang sudah jelas, dan laporkan hasil yang bisa direview. Jangan menyatakan “production-ready” sebelum kriteria yang relevan terbukti; nyatakan risiko tersisa secara spesifik.

## Referensi resmi untuk verifikasi

Referensi berikut mendasari target kontrol; penilaian aplikasi tetap membutuhkan bukti implementasi dan hasil tes.

- OWASP ASVS 5.0.0: https://asvs.owasp.org/ dan https://asvs.owasp.org/downloads/
- Authorization: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- XSS Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- Payment Gateway Integration: https://cheatsheetseries.owasp.org/cheatsheets/Third_Party_Payment_Gateway_Integration_Cheat_Sheet.html
- SSRF Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- Session Management: https://github.com/OWASP/ASVS/blob/master/5.0/en/0x16-V7-Session-Management.md (gunakan rilis ASVS stabil yang dipin untuk checklist proyek).
- WCAG 2.2: https://www.w3.org/TR/WCAG22/

Dokumen ini adalah prompt pelaksanaan dan audit UI awal. Belum ada kode TagihNet yang dibuat atau di-hardening melalui penugasan ini.
