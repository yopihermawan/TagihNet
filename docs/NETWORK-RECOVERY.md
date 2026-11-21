# Pemulihan job jaringan simulasi

Di **Jaringan**, pilih **Gagal · tiga percobaan habis** untuk menemukan job yang memerlukan tindak lanjut. Pencarian menerima nama pelanggan, kode pelanggan, atau ID job. Buka baris untuk membaca hasil terakhir, jumlah percobaan, asal job, dan status layanan saat ini. Entri lama tetap tampil di riwayat.

Jika router pelanggan ditandai tidak dapat dijangkau, buka **Ubah skenario router simulasi**. Owner dan peran kustom dengan `network` serta `manage_router_simulation` dapat memilih kondisi baru, memberi alasan 10–160 karakter, meninjau perubahannya, lalu menyimpan dengan MFA dalam lima menit terakhir. Audit mencatat pelanggan, operator, kondisi sebelum dan sesudah, serta alasan. Perubahan terbaru berlaku saat worker menjalankan job pelanggan yang sudah mengantre. Job yang sudah mencapai tiga percobaan memerlukan pemulihan tersendiri.

Owner dan peran kustom dengan izin `network` serta `manage_network_retries` dapat meminta pemulihan untuk job terbaru pelanggan yang sudah gagal tiga kali. NOC dapat melihat antrean dan detail. Jika pelanggan sudah memiliki job yang lebih baru, gunakan detail job tersebut untuk melihat kelanjutan rekonsiliasi.

1. Periksa penyebab kegagalan, lalu isi alasan pemulihan 10–160 karakter pada detail job.
2. Pilih **Tinjau pemulihan**, periksa kembali job dan alasan, lalu tandai persetujuan.
3. Pilih **Konfirmasi dan antrekan** dengan MFA dalam lima menit terakhir. Tinjauan berlaku 15 menit.

Konfirmasi menambahkan satu job dan tautan ke riwayat lama. Pengiriman ulang konfirmasi yang sama membuka job yang sudah dibuat. Status dan ketiga percobaan job gagal dipertahankan. Job baru mengikuti batas tiga percobaan dan jeda yang sama. Dashboard dan monitor membedakan pekerjaan yang masih menunggu, percobaan ulang otomatis, dan kegagalan terminal yang masih menjadi job terakhir pelanggan.

Worker menilai kembali saldo dan izin operator saat eksekusi. Pemulihan job dari jadwal isolir juga memeriksa versi jadwal, persetujuan MFA, layanan aktif, batas tenggang dan saldo faktur pemicu. Job dilewati jika kondisi itu sudah berubah. Pemulihan memakai adapter simulasi dan tidak mencatat transaksi pembayaran.

Schema `0018_network_recovery` menambah tabel `NetworkJobRetry` untuk tautan, alasan dan persetujuan. Baris baru dibuat saat operator mengonfirmasi pemulihan.
