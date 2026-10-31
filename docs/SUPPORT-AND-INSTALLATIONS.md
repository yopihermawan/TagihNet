# Tiket gangguan dan pasang sambungan baru

Checkpoint operasional menambah tiket bantuan, penugasan teknisi, SLA, komentar publik/internal, dan PSB dari prospek sampai aktivasi administrasi. Data dan adapter jaringan masih simulasi. Tidak ada pengiriman pesan, gateway pembayaran, atau perubahan router nyata.

## Akses

- Owner dan preset Admin operasional dapat mengelola tiket dan PSB dalam ISP sendiri. Peran kustom memerlukan capability `tickets` atau `installations` secara eksplisit.
- Preset **Teknisi** hanya memiliki `field_work`: daftar/detail/status/komentar pada tiket dan PSB yang **ditugaskan kepadanya**. Ia tidak menerima seluruh daftar pelanggan, nominal billing, invoice/PDF, editor role, penugasan ulang, pembatalan PSB, atau aktivasi administrasi.
- Teknisi memakai preset khusus dan tidak dapat diberi peran kustom yang memperluas scope. Owner tetap dapat mengubah preset akun lewat proses pengelolaan akses; perubahan mencabut sesi. Scope wilayah geografis belum dibuat.
- Pelanggan portal hanya membuat/membaca/mengomentari tiket untuk customer yang terikat pada akunnya. Pilihan customer/priority/assignee dari browser tidak dipercaya. Catatan internal tidak masuk query riwayat/pagination pelanggan.
- Capability `activate_installations` terpisah dari pengelolaan PSB, dimiliki Owner secara bawaan. Peran kustom harus juga memiliki `installations`, `edit_customers`, `issue`, `invoices`, dan `customers`.

Semua pemeriksaan diulang pada backend/service; menyembunyikan menu bukan batas keamanan. Reassignment segera mencabut akses objek dari teknisi lama. Object yang tidak berada pada scope menjadi 404, termasuk bila scope berubah di tengah request. Technician/customer asing tidak dapat dipilih lewat form atau parameter ID.

## Menyiapkan teknisi

Buka **Akun & peran → Tambah akun**, pilih preset **Teknisi**, dan gunakan password awal yang unik. Akun operator wajib mendaftar MFA saat login. Seed tidak otomatis membuat/mendaftarkan perangkat teknisi. Penugasan hanya menerima akun Teknisi aktif dari ISP yang sama.

## Tiket

Tiket mencatat pelanggan, judul, uraian teks, kategori (koneksi/penagihan/pemasangan/lainnya), prioritas, teknisi, status, versi data, target SLA dan timeline. Pencarian/filter/pagination server-side menggunakan GET/URL. Status berubah melalui:

| Dari | Ke |
| --- | --- |
| Terbuka | Dalam penanganan / Terselesaikan |
| Dalam penanganan | Menunggu pelanggan / Terselesaikan |
| Menunggu pelanggan | Dalam penanganan / Terselesaikan |
| Terselesaikan | Dalam penanganan (buka lagi) / Ditutup |
| Ditutup | Terminal, triase/status tidak dapat diubah |

Catatan status wajib 10–2.000 karakter dan **terlihat pelanggan**. Teknisi boleh menyelesaikan penanganan tetapi tidak menutup tiket. Staff berizin menutup setelah resolusi. Komentar terpisah dapat publik atau internal; pelanggan tidak mempunyai flag internal dan tidak dapat mengubah status/penugasan. Komentar boleh ditambahkan setelah tiket ditutup tanpa mengubah status/SLA.

Form mutasi membawa request UUID untuk idempotensi. Perubahan status/triase juga membawa versi; form lama ditolak tanpa menimpa update baru. Retry identik tidak menggandakan aktivitas. Riwayat dipaginasi, teks di-escape, dan audit umum hanya memuat metadata tindakan; isi komentar disimpan dalam WorkEvent yang scope-nya mengikuti tiket.

## SLA

**Target SLA tiket** menerima 1–720 jam per prioritas. Nilai awal development: rendah 72, normal 24, tinggi 8, mendesak 2 jam. Ini target demo yang harus disesuaikan ISP, bukan kontrak SLA produksi yang telah diverifikasi.

Saat tiket dibuat, seluruh kebijakan SLA disalin ke tiket. Batas waktu dihitung dari `created_at + jam prioritas`. Mengubah prioritas menggunakan snapshot kebijakan awal itu, bukan konfigurasi terbaru. Konfigurasi baru hanya berlaku pada tiket berikutnya. Waktu tetap berjalan saat Menunggu pelanggan; membuka kembali tiket tidak mereset batas. Resolusi mencatat waktu selesai sehingga indikator tepat/melewati SLA dapat ditelusuri. Semua timestamp disimpan aware/UTC dan ditampilkan WIB.

Belum ada jam kerja/hari libur, pause SLA, eskalasi, alarm otomatis, atau notifikasi keluar. Lampiran/file upload belum tersedia; jangan menganggap modul upload sudah lolos audit.

## PSB

Pengajuan PSB menyimpan calon pelanggan, email/telepon, username layanan, lokasi/alamat/wilayah, paket, penugasan, jadwal, status dan timeline. Telepon dinormalisasi seperti form pelanggan. Customer dengan telepon/username yang sudah dipakai ditolak; tidak ada penggabungan otomatis dengan akun pelanggan lama.

Alur: **Prospek → Survei → Terjadwal → Pengerjaan → Terpasang → Teraktivasi administrasi**. Menjadwalkan perlu teknisi aktif dan waktu mendatang dalam WIB. Setiap perpindahan memerlukan catatan minimal 10 karakter dan versi form yang masih berlaku. Perubahan penugasan hanya dilakukan staff berizin, bukan teknisi. Pembatalan sebelum aktivasi memerlukan alasan dan menjadi terminal; tidak membuat/menghapus customer atau invoice.

Catatan lapangan PSB hanya internal untuk staff berizin/teknisi yang ditugaskan. Tidak ada portal prospek publik. Akun berizin installations dapat mengubah identitas/paket pada Prospek, Survei atau Terjadwal. Perubahan saat Terjadwal membatalkan jadwal lama dan kembali ke Survei. Penjadwalan ulang tersedia pada Terjadwal dengan teknisi aktif dan waktu mendatang. Alasan, versi dan riwayat sebelum/sesudah wajib; tidak mengubah ledger. Identitas/paket terkunci mulai Pengerjaan. Panduan: [REMINDERS-AND-RESCHEDULING](REMINDERS-AND-RESCHEDULING.md).

## Aktivasi administrasi

Hanya tersedia saat **Terpasang** dan untuk akun dengan hak aktivasi. Halaman pratinjau memungkinkan memilih bulan layanan awal dan memperlihatkan identitas, paket, harga, pajak, total, jatuh tempo dan footer. Faktur sebulan penuh tanpa prorata. Quote bertanda tangan terikat aktor, tenant, PSB, periode, identitas serta snapshot harga/setelan/versi; berlaku 15 menit.

Konfirmasi perlu checkbox peninjauan dan MFA <=5 menit. Jika MFA kedaluwarsa, verifikasi ulang lalu kembali ke pratinjau; POST tidak diputar ulang otomatis. Jika harga/setelan/PSB berubah, muat ulang dan setujui quote baru. Harga/status lunas/tenant dari POST tidak dipakai.

Satu transaksi atomik membuat **satu Customer**, **satu Invoice awal**, **satu NetworkJob**, serta tautan PSB dan histori. Customer/Invoice memakai relasi one-to-one dari PSB; constraint pelanggan dan faktur bulanan tetap berlaku. Pengiriman ulang atau dua aktivasi bersamaan menghasilkan tautan yang sama, bukan customer/invoice baru. Duplicate telepon/username yang muncul setelah prospek dibuat menghentikan aktivasi; kegagalan invoice me-rollback customer dan job baru.

Status awal customer adalah **Menunggu provisioning**. PSB `Teraktivasi administrasi` bukan bukti router online atau pembayaran lunas. Job jaringan diproses worker simulasi yang sudah ada. Jika gagal, customer tetap pending dan invoice tetap tercatat; UI tidak mengklaim aktif. Keberhasilan simulasi diberi label simulasi. Simulator masih memakai aturan tunggakan/jatuh tempo saat ini, belum aturan paket prabayar yang menunggu pembayaran sebelum provisioning.

## Operasi dan bukti

Migrasi `0004` menambah SLA, role teknisi, status layanan pending, Ticket/Installation/WorkEvent tanpa mengubah ledger lama. Backup lokal konsisten dibuat sebelum migrasi. Web perlu restart; tidak ada worker baru khusus tiket/PSB. Provisioning memakai `run_simulation_worker`; `run_billing_worker` tetap untuk batch invoice yang sudah disetujui.

`python manage.py test` kini mencakup **129 tes**, termasuk 34 tes operasional baru: actor/tenant/customer/assignment scope, komentar internal, SLA snapshot, optimistic version/replay, signed quote/expiry, batas aktivasi MFA, rollback/duplicate, concurrency dan router gagal. `python scripts/operations_smoke.py` memakai database/server sementara untuk alur owner → teknisi → portal → PSB → invoice, provisioning simulasi, empat viewport dan restore state operasional. Tidak ada enrollment, tiket, atau PSB uji yang ditambahkan ke database demo pengguna.

Bukti visual: [teknisi menangani tiket](evidence/technician-ticket-desktop.png), [tiket pelanggan mobile](evidence/customer-ticket-mobile.png), [tiket ditutup](evidence/ticket-closed-desktop.png), [persetujuan aktivasi](evidence/psb-activation-preview.png), [PSB teraktivasi](evidence/psb-activated-desktop.png). Semua data fiktif dan tidak merekam QR/secret/kode pemulihan.

Masih belum terverifikasi: runtime Windows/Docker, PostgreSQL lintas proses, kapasitas produksi, integrasi router/RADIUS/payment nyata, upload, notifikasi otomatis, dan audit aksesibilitas lengkap. Modul ini tidak membuat seluruh aplikasi production-ready.

## Tambahan pengingat simulasi

Migrasi 0005 menambah siklus SLA dan outbox pengingat. Reopen dari Terselesaikan meningkatkan siklus tanpa mengatur ulang waktu SLA; pergantian prioritas menghasilkan deadline baru. Keduanya memberi kunci deduplikasi SLA baru. Komentar tidak membuat pengingat baru. Kebijakan opt-in/MFA dan worker terpisah dijelaskan di REMINDERS-AND-RESCHEDULING.md. Tidak ada kanal notifikasi nyata atau eskalasi otomatis ke pihak lain.

Hasil 129 tes di atas adalah checkpoint operasional sebelumnya. Suite terbaru berisi 162 tes; browser operations_smoke.py juga menguji edit/reschedule/reset Survei, opt-in pengingat, deduplikasi, pelunasan sebelum proses dan restore outbox pada database sementara.
