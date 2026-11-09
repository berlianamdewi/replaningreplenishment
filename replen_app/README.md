# Automated WMS Replenishment Generator

## Menjalankan
```
pip install -r requirements.txt
streamlit run app.py
```
Upload: Item & Location, Inventory Balance, SO/TO (boleh beberapa file), RR13 (kolom RR13 atau Forecast), template WMS (opsional).
Hasil: file WMS siap upload, laporan replenishment, laporan exception & validasi.
Uji: `python -m pytest tests`

## Aturan yang dipakai (disepakati)
- Total demand = SO + TO + RR13 (Qty SO dari status yang dipilih di aplikasi; bawaan: status yang sudah di-pick/pack/kirim/loaded tidak dihitung; RR13 dibaca apa adanya, tidak dihitung ulang). Ditampilkan di laporan.
- Active BIN = lokasi bertipe PICK (Item & Location). Lebih dari satu PICK = anomali, task tidak dibuat.
- Rasio = Stock Active / Max Cap. < 50% Prioritas 1, < 80% Prioritas 2, selain itu Prioritas 3.
- Kebutuhan (bawaan, sama dengan rencana PPIC 4 Nov) = Max - Active, dibatasi stok Reserve yang tersedia.
  Opsi di sidebar: tambahkan SO+TO+RR13 ke kebutuhan; Active < demand otomatis Prioritas 1; hanya SKU yang ada di file RR13.
- Sumber Reserve: lokasi tipe OTHER, Status OK, Available > 0, LPN terisi. Urut Date Updated paling lama dulu.
- SKU dilewati jika Reserve < batas minimum (default 5, bisa diubah).

## Asumsi sementara (belum dikonfirmasi, ubah di Settings / kode)
- Kebutuhan pecahan (karena RR13) dibulatkan ke atas.
- Tanggal Date Updated sama: urut berdasarkan Location lalu LPN.
- Qty task = kebutuhan (bukan satu LPN utuh).
- PRIORITY WMS = 5 untuk semua prioritas; TOID dan USERKEYOVERRIDE dikosongkan.
- Stock Active = On Hand lokasi PICK dari Item & Location.
