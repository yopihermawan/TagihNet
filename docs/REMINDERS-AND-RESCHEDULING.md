# Pengingat simulasi dan jadwal ulang PSB

Checkpoint `feature/tagihnet-reminders-rescheduling` melanjutkan tiket/PSB. Fitur pengingat hanya menyimpan hasil di aplikasi: tidak mengirim WhatsApp, email, HTTP atau pesan ke pihak luar. `SIMULATION=0` menolak planner/worker. Tidak ada scheduler penerbitan faktur bulanan tanpa konfirmasi.

## Mengaktifkan pengingat

1. Masuk sebagai Owner atau akun dengan role kustom `reminders`, `tickets`, `invoices`, `customers`.
2. Buka **Pengingat simulasi → Atur kebijakan simulasi**. Billing dan SLA nonaktif secara bawaan; membuka halaman tidak membuat kebijakan atau antrean.
3. Pilih jenis pengingat, jendela 0–14 hari sebelum jatuh tempo, jam mulai billing 0–23 WIB dan skenario Normal/Gangguan simulasi. Tinjau checkbox dan simpan dengan MFA terverifikasi <=5 menit. Form lama ditolak melalui versi kebijakan.
4. Jalankan worker terpisah dari web, worker jaringan dan worker faktur massal:

```bash
python manage.py run_reminder_worker
# Satu putaran, maksimal 100 kandidat per kebijakan dan 100 pemrosesan:
python manage.py run_reminder_worker --once --limit 100
```

Loop menunggu lima detik. Limit 1–500; maksimal sepuluh kebijakan per putaran dipilih berdasarkan waktu pemeriksaan terlama, termasuk kebijakan yang izinnya sudah tidak berlaku. SLA diprioritaskan sebelum billing di setiap kebijakan. Batas ini untuk pengembangan satu mesin, tanpa klaim latensi/kapasitas produksi. Restart seluruh proses setelah perubahan kode atau pemulihan snapshot.

## Aturan dan deduplikasi

| Jenis | Objek yang memenuhi syarat | Kunci deduplikasi persisten |
| --- | --- | --- |
| Billing | Faktur belum lunas, due_date <= hari WIB + lead_days, setelah jam mulai | ISP + faktur + tanggal bisnis WIB |
| SLA | Tiket belum resolved/closed, resolved_at kosong dan deadline terlewati | ISP + tiket + deadline + siklus penanganan |

Billing mencakup faktur overdue seluruh periode; setiap hari baru dapat menghasilkan satu pengingat baru selama belum lunas. Baris hari lama yang belum diproses akan dilewati. Baris yang sudah berhasil/dilewati/gagal maksimal tidak dibuat ulang untuk kunci yang sama, termasuk bila pembayaran dibalik pada hari yang sama. Tidak ada tombol kirim ulang atau penghapusan histori.

SLA memakai waktu terus berjalan, tanpa jeda jam kerja. Reprioritas memakai snapshot kebijakan SLA awal dan dapat mengubah deadline. Reopen dari resolved ke in_progress menambah siklus tanpa mengatur ulang jam; komentar tidak menambah siklus. Antrean dengan deadline/siklus lama dilewati. Satu receipt per deadline/siklus bukan eskalasi ke orang berikutnya.

Saat memproses, worker memeriksa kembali invoice balance setelah reversal/pembayaran parsial/pelunasan; isi menggunakan saldo terbaru. Tiket dicek ulang untuk resolusi, deadline dan siklus. Billing yang jam kebijakannya bergeser ke waktu mendatang dijadwalkan ulang tanpa mengonsumsi percobaan. Scope objek dan izin persetujuan juga dicek ulang sebelum adapter simulasi dipanggil.

## Status dan pemulihan

| Status | Makna / langkah berikutnya |
| --- | --- |
| Menunggu | Belum waktunya atau menunggu worker; muat ulang status |
| Tercatat simulasi | Receipt tersimpan lokal; tidak berarti pesan terkirim/diterima |
| Dilewati | Sudah lunas/selesai, fitur dimatikan, tanggal/deadline/siklus tidak berlaku; alasan tersimpan |
| Tertahan | Akun/izin/versi persetujuan berubah; pengelola sah menyimpan ulang kebijakan dengan MFA terbaru |
| Gagal | Skenario gangguan simulasi; retry otomatis setelah 30 lalu 60 detik, maksimal tiga percobaan |

Mengganti skenario menjadi Normal memungkinkan retry yang masih tersisa; tiga percobaan tetap terminal untuk kunci tersebut. `next_attempt_at` pada kegagalan ketiga tidak membuat percobaan keempat. `--once` keluar nonzero jika ada kegagalan pemrosesan. Receipt sukses tidak diulang oleh worker atau persetujuan baru.

Persetujuan kebijakan terikat akun, ISP dan session_version saat disimpan. Worker memeriksa akun aktif dan semua capability. Sesi operator dapat habis setelah persetujuan tanpa mematikan kebijakan; yang mencabut persetujuan adalah perubahan hak/versi/keaktifan akun. Simpan ulang kebijakan hanya mengantrekan kembali baris blocked, lalu semua kondisi objek tetap diperiksa. Jangan memaksa status/versi melalui SQL.

Isi receipt bersifat privat dan plaintext; tidak memasukkan komentar internal tiket. Akses memerlukan seluruh izin pengelola, termasuk baca faktur/pelanggan. Teknisi dan pelanggan portal tidak dapat membacanya. Audit menyimpan metadata/alasan, bukan isi receipt. Retensi dan enkripsi database/backup belum diterapkan.

## Edit dan jadwal ulang PSB

Owner/Ops atau role kustom dengan `installations` dapat mengubah nama, email, telepon, username, alamat, wilayah dan paket pada Prospek, Survei atau Terjadwal. Paket wajib milik ISP; telepon/username yang dipakai customer ditolak. Perubahan wajib berbeda dari data sebelumnya, beralasan 10–2000 karakter, dan menyertakan versi serta UUID request.

Perubahan apa pun pada PSB Terjadwal mengembalikan tahap ke Survei dan mengosongkan jadwal. Teknisi perlu meninjau ulang serta menjadwalkan waktu mendatang. Pada Pengerjaan/Terpasang/Teraktivasi/Cancelled, identitas/paket terkunci.

Jadwal ulang hanya pada Terjadwal dengan teknisi aktif, waktu baru mendatang dan berbeda, serta alasan. Timeline internal menyimpan nilai sebelum/sesudah dan jadwal lama/baru dalam WIB. Versi mencegah form lama menimpa perubahan; replay UUID dengan payload sama tidak membuat event ganda. Teknisi tidak boleh mengedit identitas atau menjadwalkan ulang; ia melihat hasil perubahan saat membuka ulang tugas.

Edit/reschedule tidak membuat customer, invoice atau provisioning job. Aktivasi tetap memakai quote bertanda tangan/MFA setelah Terpasang dan transaksi atomik yang telah ada.

## Migrasi dan bukti

Migrasi 0005 menambah Ticket.sla_cycle dan tabel kebijakan/outbox beserta constraint. Backup privat konsisten dibuat sebelum migrasi. Ledger lama tidak diubah. Restore drill mencocokkan outbox, kebijakan/versi, siklus tiket dan jadwal/versi PSB; pemulihan authenticator tetap membutuhkan kunci MFA terpisah.

33 tes baru (162 total), `scripts/operations_smoke.py`, `scripts/security_smoke.py`, PDF dan restore drill memberi bukti lokal. Semua browser memakai database/server sementara; akun/data demo pengguna tidak diubah. Lampiran, kanal pesan nyata, business-hour calendar, PostgreSQL multi-process dan kesiapan produksi belum diverifikasi. Detail hasil di [TEST-REPORT](TEST-REPORT.md).
