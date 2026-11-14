# Penerbitan faktur massal

Checkpoint ini menambahkan alur operasional **pratinjau → konfirmasi → antrean → hasil per pelanggan**. Tidak ada faktur massal yang dibuat hanya karena membuka halaman, membuat pratinjau, memulai server, atau menjalankan seed. Tidak ada scheduler bulanan otomatis, pengingat WhatsApp, webhook gateway, atau perintah router nyata pada fitur ini.

## Menggunakan alur

1. Masuk dengan MFA. Buka **Penagihan → Faktur massal → Buat pratinjau**.
2. Pilih bulan layanan, status layanan (bawaan **Aktif**), paket/wilayah opsional, dan diskon rupiah seragam per faktur baru. Maksimal **500 pelanggan** per batch. Hasil yang melebihi batas ditolak, tidak dipotong diam-diam. Satu pelanggan masih berarti satu layanan/paket.
3. Periksa seluruh ringkasan dan rincian berpaginasi: pelanggan, harga, diskon, pajak, total, jatuh tempo, dan faktur yang sudah ada. Faktur lama ditampilkan dengan nominal/diskon aslinya dan akan dilewati. Diskon baru tidak boleh mencapai/melebihi harga paket yang akan ditagihkan.
4. Centang peninjauan dan konfirmasikan. Konfirmasi memerlukan MFA terverifikasi dalam **5 menit terakhir**. Jika kedaluwarsa, verifikasi ulang lalu kembali ke batch yang sama; request belum disimpan dan tidak diputar ulang otomatis. Pengiriman ulang konfirmasi tidak membuat batch baru atau menghidupkan batch terminal.
5. Worker billing menerbitkan bertahap. Muat ulang status untuk melihat hasil dan membuka faktur/PDF. **Menunggu worker** berarti konfirmasi tersimpan, bukan faktur sudah terbit.

## Masa berlaku dan snapshot

Pratinjau berlaku **15 menit**. Saat konfirmasi server menghitung ulang fingerprint pilihan pelanggan dan snapshot relevan: nama/alamat, paket/harga, status layanan, diskon, pajak, jatuh tempo, footer, dan faktur yang sudah ada. Pelanggan baru yang cocok dengan filter juga menyebabkan pratinjau berubah. Jika data/relasi berubah atau pelanggan tidak lagi sesuai, batch menjadi `stale`; jika habis waktu, `expired`. Buat pratinjau baru. Nilai harga, total, daftar pelanggan, tenant, dan status yang dikirim browser tidak dipakai sebagai snapshot.

Setelah konfirmasi, snapshot yang disetujui menjadi dasar penerbitan. Perubahan harga, pajak, identitas pelanggan, atau footer sesudah konfirmasi tidak mengubah angka batch tersebut. Ini disengaja dan ditampilkan sebelum pengguna menyetujui. Jika persetujuan tidak lagi sesuai, batalkan **sisa** pekerjaan dan buat pratinjau baru. Faktur yang sudah terbit tidak dihapus, diperbarui, atau digandakan.

Pajak dalam basis poin, pembulatan half-up integer rupiah, tanggal bisnis WIB. Jatuh tempo 29–31 mengikuti hari terakhir bulan, termasuk Februari tahun kabisat, sama seperti faktur satuan. Prorata, invoice nol, lebih dari satu layanan per pelanggan, dan pembatalan faktur terbit belum diimplementasikan.

## Izin dan batas data

Capability baru `bulk_issue` dimiliki preset **Owner**. Preset operasional yang hanya memiliki `issue` tidak otomatis mendapat hak massal. Owner dapat memberi hak melalui peran kustom yang juga memiliki `issue`, `invoices`, dan `customers`. Pemeriksaan berlaku pada form, detail/list, konfirmasi, pembatalan, service, dan worker.

Batch hanya dapat dilihat/dikonfirmasi/dibatalkan oleh **pembuatnya dalam ISP yang sama**, termasuk ketika ada owner lain pada ISP itu. Ini tidak mengubah hak baca faktur yang sudah terbit menurut peran biasa. Worker membaca tenant dari batch persisten dan memeriksa izin, status akun aktif, serta versi akses pembuat sebelum setiap baris. Jika izin berubah/dicabut, sisa pekerjaan menjadi `blocked`; invoice yang sudah selesai tetap tersimpan. Aktor yang memperoleh hak kembali harus meninjau pratinjau baru.

Tidak ada API untuk mengedit JSON snapshot, mengganti pembuat/tenant, atau memutar ulang POST sensitif melalui URL eksternal. Semua mutasi memakai POST+CSRF. Audit menyimpan pembuat/aktor worker, tenant, target, status, waktu, dan correlation ID batch; tidak ada secret MFA atau cookie dalam audit.

## Worker dan pemulihan

Jalankan proses terpisah dari web dan worker jaringan:

```bash
python manage.py run_billing_worker             # loop; Ctrl+C untuk berhenti
python manage.py run_billing_worker --once      # satu putaran
python manage.py run_billing_worker --once --limit 25
```

Satu putaran memilih maksimal 10 batch antrean/pemrosesan, paling lama dibuat lebih dulu. `--limit` menerima 1–500 baris per batch per putaran (bawaan 100). Hanya batch yang telah dikonfirmasi yang diproses. Command tidak membuat batch atau mengkonfirmasi pratinjau sendiri.

Setiap pelanggan diproses dalam transaksi tersendiri. Invoice, item, dan audit baris berhasil commit bersama. Jika proses berhenti setelah beberapa pelanggan, jalankan ulang worker untuk melanjutkan baris pending. Snapshot tidak dihitung ulang setelah persetujuan. Jika invoice periode tersebut dibuat oleh jalur satuan/batch lain sebelum worker memprosesnya, invoice yang ada dihubungkan dan baris menjadi `skipped`. Constraint database satu pelanggan/tenant/periode adalah pengaman terakhir terhadap duplikasi.

Kegagalan validasi snapshot/relasi ditandai `blocked`; savepoint membatalkan invoice/audit baris yang gagal tanpa membatalkan baris yang sudah selesai. Konflik SQLite dicoba ulang secara terbatas; jika tetap sibuk, progres tetap antre/processing, `--once` mengembalikan exit status gagal dan dapat dijalankan lagi setelah penyebab selesai. Batch blocked tidak otomatis dipaksakan retry; tinjau dan buat pratinjau baru.

Pembatalan menandai batch `cancelled` dan menghentikan sisa baris. Transaksi baris yang sudah commit tetap terbit. Jika pembatalan beradu dengan transaksi worker, kunci batch menserialisasikan keputusan sehingga transaksi yang lebih dulu selesai dapat tetap menghasilkan invoice; tidak ada pembatalan/rollback finansial tersembunyi.

| Dari | Ke | Pemicu |
| --- | --- | --- |
| preview | queued | Checkbox review, MFA terbaru, fingerprint tetap, belum kedaluwarsa |
| preview | stale / expired | Data berubah / melewati 15 menit |
| queued | processing | Sebagian baris selesai |
| queued / processing | completed | Semua baris created/skipped, termasuk batch tanpa invoice baru |
| queued / processing | blocked | Izin/versi akun berubah atau snapshot/relasi tidak valid |
| preview / queued / processing / blocked | cancelled | Pembuat menghentikan sisa pekerjaan |

## Bukti dan batas kesiapan

29 tes backend baru mencakup preview tanpa penerbitan, stale/expiry, MFA/CSRF, batas 500, diskon, snapshot, retry/resume, cancellation, actor revocation, rollback baris, scope tenant/pembuat, dua worker, batch bertumpang tindih, dan race dengan penerbitan satuan. Pengujian browser terisolasi menerbitkan 28 invoice senilai Rp6.310.000 setelah diskon, memproses satu baris lalu melanjutkan, mengulang konfirmasi/worker, melewati 28 invoice lama, membatalkan batch parsial, membuka PDF berizin, serta memeriksa empat viewport.

Backup/restore SQL pada database sementara yang sedang memiliki batch `processing` membandingkan jumlah batch, status, jumlah item per status, pelanggan, invoice, pembayaran dan piutang. Itu memverifikasi state persisten, bukan pemulihan authenticator tanpa kunci. Simpan kunci MFA terpisah sebagaimana [AUTHENTICATION](AUTHENTICATION.md).

Bukti visual: [preview desktop](evidence/bulk-preview-desktop.png), [preview mobile](evidence/bulk-preview-mobile.png), [hasil selesai](evidence/bulk-completed-desktop.png). Semua memakai data fiktif. Runtime tetap SQLite/satu node pengembangan; lock/queue multi-process PostgreSQL, kapasitas produksi, scheduler otomatis dan integrasi nyata belum diverifikasi. Tidak ada klaim production-ready.

## Batch dari jadwal bulanan

Scheduler opt-in menggunakan batch/items yang sama. Persetujuan diberikan pada kebijakan jadwal (review/MFA), bukan konfirmasi tiap batch otomatis. Label sumber/versi jadwal tampil pada detail batch. Worker memeriksa revisi kebijakan/akses/MFA sebelum setiap baris; mengubah/nonaktifkan jadwal membatalkan sisa batch otomatis dan tidak menyentuh batch manual. Rincian batch tetap creator-private; riwayat jadwal tenant-scoped dapat dibaca pengelola jadwal berwenang. Lihat [BILLING-SCHEDULER](BILLING-SCHEDULER.md).
