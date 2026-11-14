# Foto privat tiket dan PSB — 14 November 2026 WIB

Checkpoint `feature/tagihnet-private-work-photos` menambahkan **Lampiran foto** pada detail tiket/PSB. Pilih JPG/PNG, isi keterangan, periksa visibilitas, lalu simpan. Foto tersimpan sebagai bukti pekerjaan dan dapat diunduh oleh akun yang masih berhak. Penghapusan/penggantian belum tersedia; pengelolaan retensi masih pekerjaan tersisa.

## Akses dan visibilitas

- Operator dengan `tickets` dapat mengunggah/membaca foto tiket ISP sendiri; bawaan unggahan **Internal**, dan operator dapat memilih foto yang juga dibaca pemilik tiket.
- Pelanggan portal hanya melihat/mengunggah foto publik pada tiket miliknya. Foto internal tidak ditampilkan dan unduhannya ditolak404. Identitas staf pada daftar foto portal ditampilkan sebagai Tim dukungan.
- Teknisi mengikuti penugasan tiket/PSB yang sudah ada. Perubahan penugasan mencabut akses daftar/unduh/unggah pada permintaan berikutnya, termasuk replay kunci unggahan sebelumnya.
- Foto PSB selalu internal; scope mengikuti `installations` atau penugasan teknisi. Tidak ada akses portal PSB.
- Kasir/keuangan/NOC tanpa hak pekerjaan tidak memperoleh akses lewat izin invoice/network. Tenant ditentukan dari membership, bukan parameter request. Metadata asing, target tidak terlihat dan foto internal yang tidak berhak mendapat404.

Keterangan di-encode sebagai teks, UUID foto ditentukan server, dan nama file asli tidak disimpan. Unduhan memakai nama `tagihnet-photo-UUID.png/jpg` tanpa path dari browser. Tidak ada URL media publik, thumbnail inline atau fetch sumber gambar eksternal. Respons berupa attachment dengan MIME hasil verifikasi, nosniff, no-store dan CSP sandbox. Integritas ukuran/checksum diperiksa sebelum byte dikirim. Daftar hanya membaca metadata; binary didefer sampai unduhan berizin.

## Pemeriksaan dan batas

Ekstensi .jpg/.jpeg/.png harus cocok dengan format hasil decoder; header MIME browser tidak dipercaya. Server memverifikasi struktur, memuat gambar, menerapkan orientasi EXIF lalu membuat gambar baru tanpa metadata/EXIF. PNG mempertahankan alpha; JPEG dikodekan ulang. Original bytes tidak diarsipkan, hanya digest sumber dan hasil untuk identifikasi. Ini bukan pengarsipan original/forensik. Animasi, GIF/SVG/PDF/arsip, gambar rusak/truncated, metadata PNG berlebihan dan dimensi besar ditolak.

| Batas | Nilai |
| --- | --- |
| File masukan dan hasil normalisasi | Maksimal2 MiB masing-masing |
| Dimensi | Maksimal4096 piksel tiap sisi dan12 megapiksel |
| Metadata PNG terkompresi |64 KiB per chunk,256 KiB total |
| Jumlah file per request |1 |
| Foto per tiket/PSB |20 |
| Unggahan baru per akun |50 dalam24 jam berjalan |
| Binary foto per ISP |100 MiB |

`BoundedMemoryUploadHandler` menjadi satu-satunya handler upload HTTP: memeriksa Content-Length awal, membatasi byte yang benar-benar diterima dan tidak melakukan spool ke disk. Buffer ditutup saat batas aktual terlampaui. Field request tetap dibatasi256 KiB. Batas ini juga berlaku pada transport CSV; parser impor tetap lebih ketat128 KiB/200 baris. Endpoint unggah tetap memerlukan CSRF/session yang berlaku.

Unggahan menormalisasi sebelum transaksi simpan, lalu memeriksa ulang aktor/versi akses, target/versi dan kuota setelah mengunci tenant/pekerjaan. Kunci UUID terikat pembuat/tenant dan payload sumber/keterangan/visibilitas/target: replay identik menghasilkan foto yang sama; penggunaan berbeda ditolak. Pemeriksaan quota di dalam transaksi mencegah dua unggahan mengambil slot terakhir bersama. Foto, timeline yang mengikuti visibilitas dan audit tersimpan atomik. Tidak ada faktur/pembayaran/job router baru dari unggahan.

## Penyimpanan, migrasi dan backup

Migrasi0010 menambahkan `WorkPhoto` dengan binary privat di database, FK target PROTECT dan constraint satu target; PSB wajib internal. Tidak ada folder MEDIA atau credential baru. Foto otomatis masuk backup SQLite/pg_dump, termasuk enkripsi arsip yang sudah ada, sehingga restore tidak memerlukan penyalinan folder foto terpisah. Penyimpanan database utama tetap mengikuti perlindungan host yang sudah ada; fitur ini tidak menambah vault/rotasi/enkripsi primary database.

Web/tiga worker dihentikan, backup terenkripsi schema0009 dibuat dan migrasi dicoba pada salinan. Pemeriksaan awal membandingkan seluruh row dengan persamaan ketat dan menandai tambahan empat permission Django; urutan shell tetap melanjutkan migrasi aktif. Verifikasi diperbaiki dan diulang: setiap row aplikasi/auth lama tetap identik, hanya tabel/content type/empat permission model baru yang ditambahkan, tanpa foto seed. Database aktif dan lab PG cocok. Perintah prosedur berikutnya harus memakai `set -e`/penggabungan `&&` agar kegagalan pemeriksaan menghentikan langkah berikutnya.

Arsip development terbaru `data/backups/encrypted/TagihNet-20261114-work-photos.tnbk` memakai kunci privat independen yang sudah ada. Arsip dibaca ulang/dipulihkan ke direktori sementara, seluruh payload app/auth cocok dan source tetap; demo tetap30 pelanggan/37 faktur/19 pembayaran/0 reversal/0 MFA/0 NAS/0 foto. Arsip lama dipertahankan. Kunci dan arsip satu workspace bukan vault/offsite backup.

## Verifikasi dan batas bukti

30 tes baru; **382 pada masing-masing SQLite dan PostgreSQL**, tanpa fail/skip. Meliputi PNG/JPEG, EXIF/orientasi/alpha, input MIME palsu, ekstensi mismatch/active format/animasi/rusak/bomb/metadata/ukuran, source/output limit, buffer abort, CSRF, target/tenant/customer/internal/penugasan/revocation, replay/konflik/version/kuota, rollback audit, metadata tanpa binary, header unduhan, korupsi blob dan GET tanpa efek bisnis.

**15 skenario lintas proses PostgreSQL** lulus:13 sebelumnya serta unggahan UUID sama menghasilkan satu foto/timeline dan dua unggahan pada slot terakhir menghasilkan satu penerimaan/satu penolakan. Restore PG biasa/terenkripsi membandingkan binary foto beserta seluruh app/auth/MFA/NAS. Drill SQLite AES-GCM kini menyimpan foto, membandingkan binary, memulihkan password+TOTP dan mengunduh foto dengan izin/checksum tepat. Verifier kompatibilitas SQLite menghitung digest binary dan tidak mencetak byte foto.

Browser menguji foto internal/publik, unggahan portal, penolakan unduhan internal/PSB, akses teknisi ditugaskan, header unduhan, CSV regresi dan empat viewport. Bukti: [operator desktop](evidence/work-photos-owner-desktop.png), [portal mobile](evidence/work-photos-customer-mobile.png). Browser security/MFA/billing dan system/model-drift/pip/static lulus. Metadata versi PyPI14 dependensi runtime/PG diperiksa ulang pada14 November: tidak ada advisory aktif yang tercantum; ini tidak mencakup OS/native/dev dependencies atau menjamin ketiadaan bug parser. Tidak ada dependency baru.

Dokumen biner lain, antivirus umum, penghapusan/retensi otomatis, kapasitas upload produksi, storage eksternal/offsite dan kanal notifikasi nyata masih pending. PDF14-page stress test tidak diulang pada tahap ini. CI remote belum diverifikasi. Lihat [TEST-REPORT](TEST-REPORT.md).
