# Profil saya

Checkpoint 18 November 2026 WIB. Semua akun yang sudah masuk dapat membuka **Profil saya** dari sidebar atau Keamanan akun. Operator mengikuti kebijakan MFA yang sama dengan halaman lain; pelanggan portal dapat memakai profil tanpa mengaktifkan MFA opsional.

## Memperbarui nama tampilan

Isi **Nama depan atau panggilan** dan **Nama belakang**, lalu pilih **Simpan nama tampilan**. Nama depan muncul pada ringkasan akun di sidebar. Keduanya boleh dikosongkan untuk memakai username sebagai nama tampilan. Masing-masing maksimal150 karakter; spasi luar dibuang dan Unicode dinormalisasi NFC, sedangkan spasi di dalam nama dipertahankan. Karakter kontrol, surrogate serta pengatur arah teks tersembunyi ditolak. Markup ditampilkan sebagai teks dengan output encoding.

Username, email pemulihan, ISP dan peran ditampilkan sebagai informasi akun. Form ini hanya menyimpan nama depan/belakang aktor login; ID akun/tenant, username, email, password, role dan flag admin yang disisipkan pada POST tidak diterapkan. Data pelanggan, nama snapshot faktur, pembayaran, penugasan dan profil ISP tetap dikelola melalui alur masing-masing.

Sukses tidak mengakhiri sesi atau mengubah sandi, MFA dan versi akses. Sesi lain tetap bisa dipakai dan melihat nama baru pada request berikutnya. Persetujuan pekerjaan billing tetap berlaku selama akses, MFA dan kebijakannya masih sesuai. Untuk mengganti sandi, gunakan [Ubah sandi mandiri](AUTHENTICATION.md); alur tersebut mencabut sesi dan menahan persetujuan lama.

## Formulir yang sudah berubah

Form berlaku15 menit dan terikat ke akun, tenant, versi akses, sesi serta fingerprint nama/username/email yang ditampilkan. Token hanya berisi metadata dan digest bertanda tangan, tanpa nama/email atau session key mentah. Jika data saat menyimpan berbeda dari snapshot form atau sesi berganti, respons409 meminta **Muat ulang profil**. Nilai isian lama tidak otomatis diberi token baru. Tinjau ulang data terbaru sebelum mengirim kembali.

Token salah/rusak/kedaluwarsa, format asing, field hilang dan batas ukuran ditolak. Token akun lain menghasilkan403; keduanya tidak menulis nama. Replay sesudah perubahan tidak menggandakan audit. Penyimpanan tanpa perubahan nama mengembalikan status berhasil tanpa event audit baru. Fingerprint membandingkan keadaan saat ini; bukan riwayat versi profil atau rekam setiap perpindahan nama di antara dua request.

Service mengunci membership lalu user dan memeriksa ulang status aktif, versi akses, revisi MFA serta hash autentikasi sesi sebelum menyimpan dua field yang diizinkan. Perubahan akses/sandi setelah pemeriksaan middleware tetap ditolak. Update nama dan audit berada dalam satu transaksi; audit gagal membatalkan update. Audit memuat identitas actor/tenant/target dan aksi `account.profile_updated`, tanpa salinan nama atau email. Seluruh respons akun memakai `Cache-Control: no-store`, POST memerlukan CSRF.

## Verifikasi dan batas

Tes mencakup role operator/portal, allowlist field, isolasi akun/sesi, token15menit/format/no-clobber, Unicode/control/XSS, audit rollback, sesi/MFA tetap dan consent billing. Dua koneksi DB menguji form nama yang sama secara bersamaan serta perubahan nama melawan perubahan sandi. Browser memakai database/server sementara pada360/768/1366/1920 dan dark mode; HTTPS verifier memeriksa CSRF, persistensi nama, akses tetap berlaku dan form replay ditolak. Hasil aktual ada di [TEST-REPORT](TEST-REPORT.md).

Profil belum menyediakan perubahan username/email, konfirmasi email, foto avatar, verifikasi identitas, atau perubahan profil ISP. Tidak ada migrasi model/dependency baru, pengiriman email/pesan, atau perubahan akun staging pengguna untuk demonstrasi. Kesiapan produksi tetap mengikuti [FEATURE-MATRIX](FEATURE-MATRIX.md) dan [RELEASE-RUNBOOK](RELEASE-RUNBOOK.md).
