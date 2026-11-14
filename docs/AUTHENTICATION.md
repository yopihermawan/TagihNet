# Autentikasi, MFA, dan pemulihan

Checkpoint akses menambahkan MFA TOTP melalui aplikasi authenticator, peran kustom, dan manajemen akun satu ISP. Tidak ada kredensial authenticator bawaan. Akun demo pada database pengguna tidak didaftarkan otomatis oleh agent.

## Login pertama operator

1. Masukkan username dan kata sandi. Akun owner/operasional/keuangan/kasir/NOC wajib MFA; pelanggan portal boleh mengaktifkannya secara sukarela melalui **Keamanan akun**.
2. Jika belum terdaftar, pindai QR lokal atau masukkan kunci manual ke authenticator TOTP: SHA-1, 6 digit, periode 30 detik. QR/kunci adalah secret; jangan bagikan atau unggah sebagai bukti visual.
3. Masukkan OTP valid. Login penuh baru dibuat setelah kode berhasil diverifikasi.
4. Simpan sepuluh kode pemulihan pada password manager/tempat terpisah. Kode lengkap hanya tampil pada respons pembuatan dan tidak disimpan sebagai teks biasa di database.

Login berikutnya meminta password kemudian OTP atau satu kode pemulihan. Kode TOTP hanya diterima sekali per time step; toleransi waktu ±1 step. Tunggu kode baru jika kode saat ini sudah dipakai. Pending autentikasi kedaluwarsa setelah lima menit, terikat ke hash password dan versi akses akun; reset password, deaktivasi, atau perubahan akses membatalkannya.

Database menyimpan secret TOTP terenkripsi Fernet dan digest SHA-256 dari kode pemulihan acak 128-bit. Digest kode bukan hash password; entropi acak yang tinggi mencegah dictionary attack praktis. Secret pending enrollment juga terenkripsi dalam session server-side. QR digambar lokal tanpa layanan pihak ketiga. Cache halaman MFA/security dinonaktifkan; kode/password dikecualikan dari laporan error produksi dan audit.

## Aksi sensitif

Halaman **Akun & peran** hanya tersedia untuk owner ISP. Perubahan membutuhkan verifikasi faktor kedua dalam lima menit terakhir. Jika verifikasi kedaluwarsa, request belum disimpan; buka **Keamanan akun**, masukkan password dan OTP/kode pemulihan, lalu ulangi perubahan. Tidak ada redirect eksternal atau request POST sensitif yang diputar ulang otomatis.

Pengelolaan paket/harga juga memerlukan `manage_plans` dan MFA terbaru saat konfirmasi setelah tinjauan. Owner memiliki izin ini; delegasi peran kustom memerlukan `customers`. Petunjuk lengkap: [PLANS](PLANS.md).

Owner dapat membuat akun, memilih preset, membuat/mengubah peran kustom, menghubungkan akun portal ke pelanggan, dan mengaktifkan/menonaktifkan akun dalam ISP sendiri. Izin bisnis dinyatakan eksplisit; permission mengelola akses tidak bisa didelegasikan lewat peran kustom. Aksi menulis memerlukan izin membaca terkait. Owner, pelanggan dan teknisi memakai preset khusus dan tidak menerima peran kustom. Penugasan teknisi pada tiket/PSB kini tersedia; scope wilayah geografis masih belum tersedia.

Perubahan role, custom permissions, aktivasi akun, MFA enrollment/reset, atau pembuatan ulang kode meningkatkan versi akses/revisi MFA. Sesi lama dicabut pada request berikutnya. Owner aktif terakhir tidak boleh diturunkan/dinonaktifkan; transaksi menserialisasi pemeriksaan melalui tenant, dan kasus dua owner bersamaan diuji di SQLite lokal. Semua kontrol diberlakukan di endpoint/backend, termasuk PDF dan form yang mengirim ID dari tenant lain.

## Pemulihan

Lupa password: gunakan alur reset yang sama. Reset password mencabut sesi tetapi **tidak menghapus MFA**. Kehilangan perangkat: gunakan satu kode pemulihan di tantangan login. Setelah masuk, **Keamanan akun → Buat ulang kode pemulihan** memerlukan password dan faktor kedua, membatalkan semua kode lama serta sesi lain. MFA tidak dapat dimatikan via web.

Jika perangkat dan seluruh kode hilang, administrator host tepercaya wajib memverifikasi identitas pemilik melalui prosedur organisasi di luar aplikasi, mengonfirmasi username+ID, lalu menjalankan command offline berikut. Jangan menjalankan reset hanya berdasarkan permintaan akun yang belum diverifikasi.

```bash
python manage.py reset_mfa --username NAMA_AKUN --confirm-user-id ID_AKUN --reason 'Alasan dan referensi verifikasi identitas, minimal 20 karakter'
```

Command mencatat audit actor sistem, mencabut sesi, menghapus perangkat MFA akun yang tepat, dan mewajibkan enrollment ulang untuk operator. Tidak mengubah password. Jangan memasukkan secret/dokumen identitas ke alasan audit. Administrator host/database tetap merupakan trust boundary; command ini bukan endpoint publik atau jalur pemulihan otomatis lewat email.

## Kunci enkripsi

Development membuat `data/.mfa_key` acak dengan mode 0600. Produksi (`DEBUG=0`) wajib memberikan `TAGIHNET_MFA_KEY` terpisah dari database dan `TAGIHNET_SECRET_KEY`. Bentuknya kunci Fernet base64 yang merepresentasikan 32 byte acak, dibuat melalui `Fernet.generate_key()` di lingkungan pengelolaan secret. Jangan menyalin hasil ke repo, chat, Docker image, atau setup script.

Backup database saja tidak cukup: simpan kunci MFA di vault/offsite terenkripsi dengan kontrol akses dan retensi terpisah. Memulihkan tanpa kunci yang cocok membuat perangkat TOTP tidak dapat diverifikasi. **Mengganti variabel kunci begitu saja bukan rotasi**; diperlukan prosedur decrypt/re-encrypt offline teruji, yang belum diimplementasikan. Snapshot development mempertahankan `data/.mfa_key`; jangan membuat ulang ketika database terenkripsi masih dipakai. Enkripsi secret MFA terpisah dari enkripsi arsip database. CLI AES-256-GCM dan restore login sandi/TOTP/recovery telah diuji pada fixture; panduan serta batas rollback snapshot ada di [BACKUPS](BACKUPS.md). Kunci MFA tidak dimasukkan ke arsip.

## Batas pengujian dan mitigasi berikutnya

Limiting memakai update counter bersyarat dalam transaksi, lima percobaan per identitas dan IP dalam 15 menit untuk tiap scope login/MFA/step-up/reset. Header forwarded tidak dipercaya. Limiter fail-closed bila database sibuk. Batas IP dapat memengaruhi banyak operator di NAT yang sama; kebijakan edge dan uji beban produksi masih diperlukan.

Verifikasi lokal mencakup replay OTP/recovery paralel, revocation saat pending login, last-owner race, password reset, CSRF, dan browser nyata. Belum ada audit kriptografi independen, uji phishing, WebAuthn/passkeys, SMTP/TLS deployment, multi-process PostgreSQL, atau proses verifikasi identitas organisasi yang tersertifikasi. MFA TOTP tidak menghapus seluruh risiko phishing dan tidak menjadikan aplikasi otomatis siap produksi.
