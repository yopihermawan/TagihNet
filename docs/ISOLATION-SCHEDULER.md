# Jadwal isolir simulasi

Buka **Jaringan → Jadwal isolir simulasi**. Centang aktivasi, pilih **Tinjau jadwal**, baca tanggal mulai dan cakupannya, lalu konfirmasi dengan MFA yang diverifikasi dalam lima menit terakhir. Owner memiliki izin ini. Peran kustom memerlukan izin `manage_isolation_schedule` dan `network`.

Jadwal dimulai pada tanggal konfirmasi WIB. Worker mencari layanan aktif dengan faktur bersaldo yang mencapai batas isolir sejak tanggal tersebut. Batasnya adalah jatuh tempo ditambah tenggang yang tersimpan pada faktur dan satu hari. Faktur yang batasnya sudah lewat sebelum tanggal mulai berada di luar cakupan. Tagihan jatuh tempo 29 Februari dengan tenggang tiga hari mencapai batas 4 Maret. Selama pembayaran masih tersisa, faktur itu bisa memicu job mulai tanggal 4 Maret atau setelahnya, jika tanggal tersebut tercakup jadwal.

Worker memeriksa paling banyak empat ISP per putaran dan sekali per menit per ISP. Setiap pemeriksaan mengantrekan maksimal 25 job, satu per pelanggan pada putaran itu. Faktur pemicu dan versi persetujuan dicatat agar restart atau dua worker tidak menggandakan job. Pembayaran sebagian tetap memenuhi syarat bila ada sisa yang sah. Jika sudah ada job jaringan yang menunggu atau masih dapat dicoba ulang untuk pelanggan, planner menunggu putaran berikutnya.

Pada eksekusi, worker memeriksa lagi versi persetujuan, akun pemberi izin, revisi MFA, kondisi layanan, tanggal dan saldo faktur pemicu. Job yang tidak memenuhi syarat tampil sebagai **Dilewati**. Jika tiga percobaan gagal, operator berizin dapat meninjau pemulihan dari **Jaringan**. Job pemulihan membawa faktur pemicu dan persetujuan jadwal yang sama. Job pembayaran, reversal dan PSB merupakan rekonsiliasi tersendiri. [Panduan pemulihan](NETWORK-RECOVERY.md)

Untuk menghentikan jadwal, hapus centang dan konfirmasi melalui langkah yang sama. Job jadwal yang belum selesai akan dilewati saat diperiksa. Perubahan izin, sandi atau MFA pemberi persetujuan dapat menahan jadwal. Persetujuan baru mulai dari tanggal konfirmasi baru dan job versi sebelumnya tidak dieksekusi. Layanan yang sudah diisolir diproses sesuai rekonsiliasi berikutnya, misalnya setelah pembayaran yang menutup seluruh utang yang memenuhi syarat.

Migrasi `0017_isolation_scheduler` menambah dua tabel. Semua ISP mulai dengan jadwal nonaktif. Saat pemulihan staging, worker tetap ditahan sampai operator meninjau transaksi, persetujuan dan kondisi setelah snapshot. Adapter saat ini bekerja pada status layanan simulasi.
