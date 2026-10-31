# Impor pelanggan CSV

Checkpoint `feature/tagihnet-customer-import`, 1 November 2026 WIB. Impor menambah identitas pelanggan baru dari CSV dengan pratinjau privat dan persetujuan MFA. Tidak memperbarui/menggabungkan pelanggan lama, menerbitkan invoice, membuat akun portal, atau mengirim job/perintah router. Data demo pengguna tetap terpisah dari pengujian.

## Alur penggunaan

1. Owner membuka **Impor pelanggan → Unggah CSV**. Role kustom dapat diberi `import_customers` dengan prasyarat `customers` dan `edit_customers`.
2. Unduh template dan lihat daftar paket_id/nama/harga milik ISP pada halaman unggah. Isi file UTF-8 dengan delimiter koma.
3. Buat pratinjau. Upload belum menambah pelanggan; hasil menampilkan normalisasi, paket, alamat dan kesalahan per record.
4. Jika ada satu kesalahan, perbaiki file dan unggah sebagai pratinjau baru. Semua baris harus valid; tidak ada pilihan mengabaikan row bermasalah.
5. Dalam 15 menit, tinjau semua baris, centang persetujuan dan konfirmasi dengan MFA terverifikasi <=5 menit. Bila verifikasi ulang diperlukan, kembali ke batch yang sama dan tinjau ulang.
6. Status Selesai berarti seluruh pelanggan baru tersimpan dengan **Menunggu provisioning**. Buka detail pelanggan untuk pekerjaan berikutnya sesuai izin Anda.

Penagihan terpisah. Untuk menerbitkan faktur massal atas pelanggan hasil impor yang masih pending, pilih filter **Semua layanan**; filter Aktif bawaan tidak memasukkan pelanggan tersebut. Status pending tidak membuktikan perangkat aktif. Rekonsiliasi jaringan tetap melalui alur yang sudah ada dan adapter simulasi.

## Format CSV

Maksimal **128 KiB (131072 byte)** dan **200 record data** per batch. UTF-8 dengan/tanpa BOM, header tepat tujuh kolom berikut; urutan kolom dapat berbeda. Baris kosong dilewati. Nomor pada preview dimulai dari record data kedua setelah header, bukan nomor baris fisik apabila cell memiliki newline di dalam tanda kutip.

```csv
nama,email,telepon,username,paket_id,wilayah,alamat
Pelanggan Fiktif,contoh@example.invalid,081299988877,contoh-pelanggan,1,Wilayah Contoh,"Jl. Fiktif, No. 1"
```

Nilai paket_id contoh `1` harus diganti dengan ID paket yang tersedia pada ISP Anda.

| Kolom | Aturan |
| --- | --- |
| nama | Wajib, maksimal 100 karakter |
| email | Nilai boleh kosong; jika diisi wajib email valid |
| telepon | Wajib, normalisasi +62/62 menjadi 0, hapus spasi/tanda hubung, 10–15 digit |
| username | Wajib, slug maksimal 64 karakter, dinormalisasi ke huruf kecil |
| paket_id | Wajib memilih ID paket yang tersedia pada ISP dari membership |
| wilayah | Wajib, maksimal 80 karakter |
| alamat | Wajib, maksimal 250 karakter; koma/newline di dalam cell memakai quoting CSV |

Header unknown/duplikat/hilang, jumlah kolom salah, encoding lain, karakter kontrol selain CR/LF/tab, ekstensi selain .csv, file kosong dan input melebihi batas ditolak sebelum batch disimpan. MIME dari browser tidak dijadikan bukti isi; parser/field tetap memvalidasi. Tidak menerima Excel/XLSX, ZIP, binary attachment, delimiter titik koma, sandi, tenant_id, nominal atau status layanan.

Username/telepon duplikat di file atau database ISP sendiri menjadi kesalahan. Username dibandingkan tanpa membedakan case dan juga dilindungi constraint database untuk semua jalur pembuatan pelanggan. Data ISP lain tidak dipakai sebagai duplicate global dan identitas/paketnya tidak ditampilkan pada error.

## Pratinjau, konfirmasi dan atomisitas

Maksimal 20 pratinjau aktif dan belum expired per pembuat. Batalkan preview yang tidak diperlukan untuk membuka slot. Kunci UUID upload yang sama dengan byte CSV sama mengembalikan batch yang sama; byte berbeda dengan UUID sama ditolak. Unduhan template dan GET halaman tidak membuat batch/customer.

File asli/nama file tidak diarsipkan. Database menyimpan digest sumber, nilai kolom yang dikenal, snapshot paket dan error per record. Nilai invalid dibatasi panjangnya untuk laporan. Karena itu laporan bukan salinan lengkap file sumber untuk semua input invalid. Setelah normalisasi, isi row/fingerprint pratinjau tidak dapat diedit melalui aplikasi; unggah ulang file untuk koreksi.

Harga/nama/kecepatan paket ditampilkan sebagai informasi saat preview. Jika paket berubah sebelum konfirmasi, buat preview baru. Impor tidak membuat snapshot harga untuk faktur masa depan; penerbitan invoice tetap memakai aturan billing pada saat diterbitkan.

Saat konfirmasi, aplikasi memeriksa kembali actor aktif, tenant, seluruh capability, MFA, session_version, versi akses saat preview, expiry, fingerprint, duplicate dan validasi paket/field. Jika satu row tidak valid atau data berubah, tidak ada row yang ditambahkan. Penyimpanan customer/link row berlangsung dalam satu transaksi dengan savepoint. Konflik database membatalkan seluruh set dan menandai preview stale. Proses tidak memerlukan worker tambahan dan tidak memiliki hasil parsial.

| Status batch | Makna |
| --- | --- |
| Pratinjau | Belum ada customer ditambahkan; berlaku 15 menit, semua error wajib diperbaiki |
| Selesai | Semua customer baru tersimpan; replay tidak menggandakan |
| Dibatalkan | Preview dihentikan tanpa customer baru |
| Perlu pratinjau baru | Data/izin/paket/fingerprint/constraint berubah; tidak ada customer ditambahkan |
| Kedaluwarsa | Konfirmasi dilakukan setelah masa berlaku; tidak ada customer ditambahkan |

GET tidak mengubah expired status; tombol konfirmasi hilang setelah masa berlaku. POST konfirmasi expired menyimpan status Kedaluwarsa. Semua status selain Pratinjau bersifat terminal; batal setelah Selesai tidak menghapus customer. Koreksi customer yang telah tercipta mengikuti form/izin pelanggan biasa.

## Privasi, laporan dan hak akses

Batch, row dan laporan privat untuk **pembuat + ISP**, termasuk ketika peminta adalah owner lain dalam ISP sama. Semua endpoint memakai capability yang sama dan tenant dari membership. Ops/Keuangan/NOC/Teknisi/Pelanggan tidak mendapatkan impor secara bawaan; custom role harus memenuhi prasyarat. Customer yang sudah dibuat mengikuti hak baca/edit pelanggan biasa.

Laporan CSV memuat row/status/data dikenal/paket/error dengan escaping formula spreadsheet. Halaman web memakai output encoding untuk teks markup. CSV tidak dieksekusi atau diambil sebagai URL. Audit hanya jumlah/status/alasan/UUID, bukan isi baris/telepon. Riwayat di database tetap mengandung PII; retensi/pruning/enkripsi data/backup dan batas storage jangka panjang perlu diatur. Tidak ada endpoint delete batch atau public file storage pada checkpoint ini.

## Migrasi 0007 dan pemulihan

Migrasi menambah CustomerImportBatch/Row dan unique index tenant + LOWER(username). Sebelum menerapkan pada data yang sudah ada, hentikan penulisan, buat backup konsisten dan uji pada salinan. Preflight menolak benturan username legacy tanpa menormalkan, menggabungkan atau menghapus data otomatis. Tinjau koreksi dengan pemilik data pada salinan; jangan melewati index.

Uji salinan dengan probe collision telah membuktikan penolakan tanpa kehilangan customer atau menandai migrasi selesai. Setelah probe uji dibuang hanya pada salinan, migrasi berhasil dan ledger cocok. Database lokal yang tidak memiliki collision lalu dimigrasikan setelah backup; pelanggan/faktur/pembayaran/reversal/MFA/tiket/PSB tetap sama. Web dan tiga worker dimulai kembali; tidak perlu worker impor.

Restore SQLite mencocokkan batch/fingerprint/versi dan row payload/status/customer links. PostgreSQL restore memeriksa seluruh tabel, termasuk model impor. Pemulihan MFA tetap membutuhkan kunci enkripsi terpisah; backup belum terenkripsi/offsite. Tidak ada migrasi data produksi yang dilakukan.

## Bukti

30 tes baru, 225 tes lulus pada masing-masing SQLite/PostgreSQL. Dua race PostgreSQL tambahan membuktikan konfirmasi batch sama idempoten serta impor overlap menghasilkan satu completed/satu stale. Verifier kini sepuluh skenario lintas proses ditambah pg_dump/restore.

Browser terisolasi menguji upload invalid/valid, preview tanpa efek, persetujuan, tepat dua customer pending, replay tanpa duplikasi, izin teknisi/portal, empat viewport dan restore state. Tidak menambah fixture atau perangkat MFA ke database demo pengguna. Lihat [TEST-REPORT](TEST-REPORT.md), [PERMISSIONS](PERMISSIONS.md) dan [UX-AUDIT](UX-AUDIT.md). Import update/upsert, spreadsheet biner, retensi otomatis dan cutover produksi belum tersedia.
