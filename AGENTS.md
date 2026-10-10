# Aturan Wajib SOL/ASTRA — Feature Freeze (Perintah Pemilik)

Berlaku untuk semua AI coding agent, kontributor, perencanaan, branch, PR, build, dan rilis dalam repository ini. Berlaku sejak 11 Oktober 2026 (WIB).

## Baseline fitur dikunci — DILARANG menambah fitur baru

Fitur, fungsi, alur kerja, perilaku, output, integrasi, dan desain UI yang telah secara eksplisit disepakati pemilik merupakan **baseline tetap**. Tanpa instruksi baru yang **spesifik dan eksplisit dari pemilik**, dilarang menambah fitur, mode, provider, menu, tombol, opsi, panel, preset, otomasi, atau menghapus/mengganti/memperluas/mendesain ulang fitur/UI yang sudah disetujui.

## Pekerjaan yang diperbolehkan

- Menyelesaikan implementasi fitur yang **sudah disepakati** tetapi belum berfungsi sesuai spesifikasi.
- Mencari penyebab dan memperbaiki bug, kegagalan integrasi, regresi, serta masalah keamanan.
- Meningkatkan stabilitas, performa, kompatibilitas, pengujian, CI, build, packaging, dan dokumentasi tanpa mengubah pengalaman atau kontrak input-output yang disepakati.
- Menyesuaikan UI **hanya** agar cocok dengan gambar/referensi final yang sudah disetujui; tidak membuat desain atau gambar baru sendiri.
- Refactor seperlunya jika perilaku tetap identik; buktikan dengan pengujian relevan.

## Persetujuan perubahan fitur

1. Ide fitur baru cukup dicatat sebagai usulan; **jangan implementasikan** sebelum pemilik secara spesifik memintanya.
2. Perintah **"LANJUTKAN"**, izin kerja otonom, usulan SOL, atau lulusnya tes/CI **bukan** izin menambah/mengubah fitur.
3. Jika kode berbeda dari baseline, perbaiki kode supaya sesuai baseline — jangan mengubah baseline agar cocok dengan kode.
4. Sebelum coding/PR, periksa spesifikasi, keputusan, gate, dan referensi UI resmi repository. Aturan keselamatan dan gate proyek yang lebih ketat tetap berlaku. Jika membutuhkan aset gambar baru, ikuti stop/gate gambar proyek; jangan generate sendiri.
5. Setiap PR harus menyatakan bahwa perubahan hanya menyelesaikan baseline atau memperbaiki bug/stabilitas, dengan bukti tes/regresi yang tersedia.

**Prinsip utama: pertahankan fitur yang sudah disetujui, tuntaskan yang belum selesai, lalu perbaiki dan stabilkan. Fitur baru hanya atas perintah eksplisit pemilik.**
