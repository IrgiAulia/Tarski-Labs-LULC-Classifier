# Tarski Labs - LULC Classifier

Plugin QGIS (≥ 3.34, diuji di 3.44.15 macOS) untuk klasifikasi tutupan/penggunaan lahan.
Hanya memakai GDAL, NumPy, SciPy, dan scikit-learn yang sudah ada di QGIS — tanpa instalasi tambahan.

## Alur kerja (dock: Raster › Tarski Labs › LULC Classifier)

1. **① Bands** – centang raster/band yang akan diklasifikasi (multiband dipakai utuh, urutan bisa di-drag).
2. **② ROI** – klik **New** untuk membuat layer ROI, isi ID + nama + warna kelas, klik **✎ Draw ROI**,
   gambar poligon di peta (klik kanan untuk menutup poligon). **+ New class** untuk kelas berikutnya.
   **Compute signatures** menampilkan grafik signature spektral per kelas.
3. **③ Classify** – pilih algoritma → **① Train** (akurasi hold-out langsung tampil) →
   **② Preview view** (hanya area peta yang terlihat, cepat) → **③ Classify full image**.
4. **④ Results** – majority filter, sieve, accuracy assessment (confusion matrix, OA, kappa),
   area report (ha, %), export CSV.

## Algoritma

| Supervised | Unsupervised | Post-processing |
|---|---|---|
| Minimum Distance, Mahalanobis, Maximum Likelihood, Spectral Angle Mapper, Parallelepiped (dari SAGA `imagery_classification`) | K-Means, ISODATA (SAGA `imagery_isocluster`) | Majority filter (SAGA `grid_filter`), Sieve |
| Random Forest, SVM, Multi-layer Perceptron (dari SCP) | | Accuracy, Area report (SCP) |

Semua tool juga tersedia di **Processing Toolbox › Tarski Labs - LULC** (bisa dipakai di Model Builder/batch).

Model terlatih bisa disimpan (`.tlc`) dan dimuat ulang. Hanya muat file `.tlc` yang kamu buat sendiri.

License: GPL-3.0-or-later.
