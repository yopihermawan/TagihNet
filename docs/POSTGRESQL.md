# PostgreSQL opsional dan hardening transaksi

Checkpoint `feature/tagihnet-postgresql-hardening` menambah PostgreSQL tanpa memindahkan database demo. SQLite tetap default; seluruh data/kunci lokal yang sudah ada dipertahankan. Migrasi skema yang sama (0001–0007) dapat diterapkan dari kosong pada PostgreSQL. Belum ada cutover atau migrasi data produksi.

## Lab yang telah diuji

Linux, Python 3.12.14, Django 5.2.18, psycopg/psycopg-binary 3.3.6, PostgreSQL 16.15 dalam Docker. Image server dikunci digest di postgres_lab.py dan CI. Image aplikasi Docker/Windows, TLS remote dan server PostgreSQL versi lain belum diuji pada checkpoint ini.

```bash
cd /workspace/TagihNet
.venv/bin/python -m pip install -r requirements-postgres.txt
.venv/bin/python scripts/postgres_lab.py start
.venv/bin/python scripts/postgres_lab.py status
.venv/bin/python scripts/postgres_lab.py run manage.py migrate --noinput
.venv/bin/python scripts/postgres_lab.py run manage.py test --verbosity 1 --noinput
.venv/bin/python scripts/postgres_lab.py run scripts/verify_postgres.py
# Hentikan server lab, pertahankan volume:
.venv/bin/python scripts/postgres_lab.py stop
```

Helper Linux menggunakan daemon lokal `unix:///var/run/docker.sock`, mengabaikan pemilih host/context/TLS Docker yang diwariskan, dan mempertahankan proxy/registry configuration. Di managed cloud, periksa daemon serta ikuti skill runtime untuk proxy/CA; jangan menjalankan daemon saingan atau melewati kebijakan jaringan. Helper membutuhkan akses socket Docker yang memang disediakan lingkungan.

Lab menyimpan manifest dan dua password acak terpisah mode 0600 pada direktori privat `data/postgres-lab/` yang diabaikan Git. Port hanya loopback dan dipilih saat awal. Role aplikasi `tagihnet_app` adalah non-superuser, tanpa CREATEROLE/REPLICATION, dengan CREATEDB khusus tes. Role bootstrap terpisah. Jangan menyalin hak CREATEDB ke role runtime produksi.

`run` menerapkan konfigurasi hanya pada subprocess dan menyimpan kunci sesi/MFA lab pada data directory tersendiri. Ia tidak mengekspor database PG ke web/worker SQLite yang sedang berjalan. Membuat schema lab tidak membuat akun demo atau melakukan enrollment MFA. Seed bukan bagian startup lab.

`start` dapat memulai container terhenti atau membuat ulang container yang hilang dengan volume milik lab. Label container/volume diperiksa; volume dengan identitas berbeda ditolak. Container terhenti telah dihapus lalu dibuat ulang dengan schema/role masih cocok. Tidak ada perintah hapus volume dalam helper. Error daemon/metadata tidak diperlakukan sebagai izin mereset data.

Snapshot cloud tidak menjamin container, image atau volume Docker ikut dipulihkan. Jika volume tidak ada, `start` membuat lab kosong menggunakan kredensial privat yang dipertahankan; jalankan migrasi lagi. Data lab bersifat disposable. Pemulihan filesystem pada task baru belum diverifikasi secara independen. Backup PostgreSQL yang penting harus dikelola di luar volume Docker beserta kunci terpisah.

## Konfigurasi aplikasi tanpa helper

Instal requirements-postgres.txt, lalu atur environment proses melalui pengelolaan secret yang sesuai. `.env` tetap tidak dimuat otomatis.

| Variabel | Aturan |
| --- | --- |
| TAGIHNET_DB_ENGINE | `sqlite` default; `postgresql` eksplisit |
| TAGIHNET_DB_NAME / USER | Wajib; identifier ASCII dimulai huruf/underscore, maksimal 63 karakter |
| TAGIHNET_DB_HOST | Wajib; tujuan database yang dikonfigurasi administrator |
| TAGIHNET_DB_PORT | 1–65535, default 5432 |
| TAGIHNET_DB_PASSWORD | Nilai dari secret process, atau gunakan file di bawah |
| TAGIHNET_DB_PASSWORD_FILE | File privat yang dapat dibaca aplikasi; tidak boleh bersamaan dengan PASSWORD |
| TAGIHNET_DB_SSLMODE | `verify-full` default; `disable` hanya diizinkan saat DEBUG=1 untuk lab lokal |
| TAGIHNET_DB_SSLROOTCERT | Opsional, path CA untuk verifikasi server |

Engine tidak dikenal, field kosong, password ambigu/tidak terbaca atau port salah menghentikan startup; tidak fallback ke SQLite. Konfigurasi DEBUG=0 tetap memerlukan secret sesi dan kunci MFA terpisah. Parser menolak TLS disable di DEBUG=0. Pengujian parser ini tidak membuktikan TLS/CA/hostname deployment remote telah benar.

Connect timeout 5 detik; lock timeout 5 detik; statement timeout 30 detik. Koneksi ditutup pada akhir request (`CONN_MAX_AGE=0`); belum ada pooling atau tuning kapasitas. Gunakan role migrasi dan runtime produksi dengan hak yang ditinjau terpisah; helper lab bukan provisioning produksi.

## Temuan dan perbaikan

1. **UUID payment lintas invoice.** Dua pelanggan mempunyai row lock berbeda. Kedua proses dapat melewati pre-read UUID sebelum salah satu insert menang; insert kedua sebelumnya menjadi IntegrityError 23505. Unique constraint tetap melindungi ledger. Kini get_or_create memakai savepoint untuk membaca pemenang lalu memeriksa invoice/amount. Hasil: satu pembayaran/job, satu penolakan validasi biasa. Replay yang identik tetap mengembalikan pembayaran lama.
2. **Lock relasi pada worker bulk.** FOR UPDATE tanpa OF ikut mengunci joined actor/tenant. Overlap yang dipaksakan dengan penerbitan satuan menghasilkan satu deadlock terukur. Lock kini dibatasi ke self. Pengunci tenant untuk akses/konfigurasi memakai NO KEY UPDATE agar insert FK biasa tidak ikut tertahan; serialisasi konfigurasi/owner tetap ada. Retest domain ini memeriksa counter deadlock nol.
3. **Retry database yang eksplisit.** Retry membaca SQLSTATE 40001 (serialization), 40P01 (deadlock), 55P03 (lock unavailable), atau SQLite BUSY/LOCKED. Maksimal lima percobaan dengan jeda bertingkat. Koneksi putus, SQL salah dan error lain tidak diulang secara umum. Fixture deadlock terkontrol menguji satu retry dengan rollback sehingga tidak ada audit yang committed dua kali.

Pembatasan lock juga diterapkan pada query join terkait pembayaran, pengingat, akun dan reset MFA. SQLite mengabaikan row-lock flags; suite terbaru 536 tes lulus di kedua backend. Tidak ada perubahan izin web atau mutasi ledger lama dari pemilihan backend.

## Verifikasi lintas proses dan restore

`verify_postgres.py` hanya berjalan pada PostgreSQL DEBUG/SIMULATION dengan role non-superuser yang mempunyai CREATEDB. Ia membuat database bernama acak, menerapkan schema dan fixture fiktif, lalu menjalankan dua interpreter Python berbeda pada setiap skenario:

- Penerbitan bulan yang sama menghasilkan satu invoice.
- Payment replay menghasilkan satu ledger entry.
- UUID yang sama pada dua invoice menghasilkan satu pembayaran/job dan satu ValidationError.
- Penerbitan massal/satuan dengan overlap lock menghasilkan satu invoice per pelanggan tanpa deadlock domain.
- Pembayaran/job jaringan direkonsiliasi sehingga saldo dan status simulasi sesuai.
- Dua worker pengingat menghasilkan satu receipt dan satu attempt.
- Aktivasi PSB menghasilkan satu customer/invoice/job.
- Deadlock buatan menghasilkan satu retry; dua transaksi yang berhasil meninggalkan tepat dua audit.
- Dua konfirmasi impor pelanggan yang sama menghasilkan satu set pelanggan.
- Dua preview impor bertumpang tindih menghasilkan satu completed dan satu stale tanpa penambahan parsial.
- Checkout gateway bersamaan menghasilkan satu order; settlement replay menghasilkan satu payment/event/job.
- Settlement setelah koreksi saldo menghasilkan satu payment; refund sebelum settlement menahan kredit baru.
- Pengakhiran checkout replay menyimpan satu intent; pembayaran yang menang race tetap tercatat dan menghalangi expiry.
- Lease pengakhiran menggabungkan request bersamaan pada intent yang sama/berbeda menjadi satu POST selama lease aktif; takeover mempertahankan order/intent dan menolak penulisan hasil dari token lama.
- Dua worker polling memakai satu lease/status request; pencabutan persetujuan selama GET mempertahankan hasil sah dan menghentikan order berikutnya.
- Pencabutan persetujuan sebelum dua worker melakukan claim menghasilkan satu hasil/audit blocked dan satu skipped, tanpa GET status, pembayaran atau job baru.
- Dua pencatatan refund penuh menghasilkan satu reversal/resolution/job; settlement terlambat mempertahankan refund serta saldo terbuka.
- Laporan saat transaksi refund belum committed melihat snapshot sebelumnya tanpa koreksi parsial; pembacaan sesudah commit merekonsiliasi refund lengkap.
- Dua webhook yang selesai tidak berurutan mempertahankan hasil kegagalan pengiriman terbaru, sambil mencatat tepat satu pembayaran/job dari verifikasi sah request lama.
- Pembuatan checkout bersamaan mengirim satu POST; takeover lease menolak respons lama; settlement selama pembuatan tetap lunas tanpa diterbitkan URL terlambat.

Gate file hanya menyelaraskan proses/lock; tidak mengganti operasi database dengan hasil palsu. Fixture deadlock terpisah sengaja menghasilkan deadlock, terpisah dari assertion nol deadlock pada race domain. Ini uji dua proses terbatas, bukan benchmark, jaminan fairness, klaim tidak pernah deadlock, atau pembuktian exactly-once integrasi eksternal.

Setelah race, verifier menjalankan pg_dump custom ke file sementara 0600, restore ke database kosong kedua dengan pg_restore, lalu membandingkan jumlah/hash payload seluruh tabel aplikasi/auth beserta through table. Kedua database kemudian dihapus; database asal dan SQLite pengguna tidak diisi fixture. Tools PG berasal dari container yang dikonfigurasi helper/CI atau client lokal yang sesuai server. Drill kompatibilitas ini memakai dump sementara tanpa enkripsi; verifier juga menguji format AES-256-GCM ke target database baru. Offsite, PITR, RPO/RTO dan failover belum diverifikasi. Pemulihan authenticator masih memerlukan kunci MFA terpisah.

`backup.py` mendukung PostgreSQL saat --key-file diberikan; restore memerlukan --target-database kosong yang berbeda dari source. verify_restore.py dan mode backup.py tanpa kunci tetap khusus SQLite. Panduan di BACKUPS.md. Kedua browser smoke secara eksplisit memakai SQLite sementara walaupun environment induk PG. Chromium belum diuji terhadap PG; tes HTTP/domain pada PG termasuk dalam 536 tes. CI mendaftarkan backend PG dan proses/restore, tanpa klaim remote CI sudah lulus.

## Cutover berikutnya

Jangan mengubah DB_ENGINE pada instalasi berisi data lalu menganggap data otomatis berpindah. Siapkan backup konsisten, migrasi ID/relasi/sequence dan data terenkripsi, rekonsiliasi saldo/jumlah/ledger, maintenance window, kunci MFA, rollback dan validasi staging. Prosedur migrasi data ini belum diimplementasikan.

Kesiapan produksi masih memerlukan TLS target, role minimum, beban/queue/pooling, retensi/monitoring, pengelolaan key/offsite/PITR, pemulihan target produksi dan integrasi gateway/router yang terverifikasi. Detail hasil lokal ada di [TEST-REPORT](TEST-REPORT.md).

Checkpoint impor menambah migrasi 0007, dua model dan index LOWER(username)/tenant. Preflight menolak benturan data lama; tidak menggabungkan/menghapus customer. Verifier kini memiliki sepuluh race dua proses dan restore juga mencakup batch/row impor. Batas kapasitas/cutover produksi di atas tetap berlaku.

Checkpoint enkripsi menambah encrypted pg_dump/restore dengan verifikasi endpoint tools memakai advisory marker, autentikasi penuh sebelum menyentuh target, pemeriksaan ownership/empty target dan single transaction. Target salah/sumber/berisi data serta dump malformed ditolak; seluruh tabel dan encrypted MFA payload cocok setelah restore. Tidak membuat database target atau menghapus objek secara otomatis. Gunakan contoh perintah di BACKUPS.md; legacy plaintext drill dipertahankan terpisah.
