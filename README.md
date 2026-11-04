# Tarski Labs - LULC Classifier Plugin for QGIS

A plugin for landuse and landcover classification, comprising from both unsupervised and supervised classification methods. Runs rapid fast and efficient on latest QGIS LTR version.

![QGIS 3.34+](https://img.shields.io/badge/QGIS-3.34%2B-589632) ![Tested on 3.44.15](https://img.shields.io/badge/tested-3.44.15%20macOS-blue) ![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

## Overview

One dock panel that takes you from raw bands to a classified map, an accuracy report and an area table. The classification workflow is modelled on the Semi-Automatic Classification Plugin (SCP) and the image-classification tools of SAGA GIS. It is a re-implementation in plain Python on top of the libraries already bundled with QGIS, so there is nothing else to install: no Remotior Sensus, no SAGA binaries, no pip packages.

- **Lightweight.** GDAL, NumPy, SciPy and scikit-learn only.
- **Fast on large rasters.** Block-wise reads and writes, background jobs (`QgsTask`) so QGIS stays responsive, and a preview mode that classifies only the visible map extent.
- **Interactive.** Draw ROIs directly on the map, see spectral signatures per class, get hold-out accuracy right after training.
- **Scriptable.** Every tool is also a Processing algorithm, so it works in the Processing Toolbox, Model Builder and batch mode.

## Requirements

- QGIS 3.34 or newer (developed and tested on QGIS 3.44.15, macOS).
- Nothing else. The plugin only uses libraries shipped with QGIS.

## Installation

1. Download [`TarskiLabs_LULC_Classifier_1.0.2.zip`](TarskiLabs_LULC_Classifier_1.0.2.zip) from this repository.
2. In QGIS: **Plugins › Manage and Install Plugins › Install from ZIP**, choose the file and click **Install Plugin**.
3. Open the panel from **Raster › Tarski Labs › LULC Classifier** or the toolbar icon.

To install from source instead, copy the [`tarski_lulc_classifier`](tarski_lulc_classifier) folder into your QGIS profile's `python/plugins` directory and enable it in the Plugin Manager. On macOS that directory is `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins`.

## Workflow

The panel has four tabs.

| Tab | What you do |
|---|---|
| **① Bands** | Tick the rasters or bands to classify. A multiband raster is used whole; the band order can be changed by dragging. |
| **② ROI** | Click **New** to create a ROI layer (a temporary polygon layer with `class_id`, `class_name` and `color` fields), set the class ID, name and colour, click **Draw ROI** and draw polygons on the map (left-click to add vertices, right-click to close). **+ New class** starts the next class. **Compute signatures** plots the spectral signature of each class. |
| **③ Classify** | Choose an algorithm, then **① Train** (hold-out accuracy is shown immediately), **② Preview view** (classifies only what you see) and **③ Classify full image**. |
| **④ Results** | Majority filter, sieve, accuracy assessment (confusion matrix, overall accuracy, kappa), area report (hectares and %) and CSV export. |

### Using your own ROI layer

Any polygon layer works. It needs an integer class ID field (and optionally a text name field):

1. **Layer › Create Layer › New GeoPackage Layer**, geometry type *Polygon*.
2. Add `class_id` (Integer) and `class_name` (Text).
3. Toggle editing, draw polygons and fill in the attributes.
4. Select the layer as the ROI layer in the panel, or as *Training ROIs* in the Processing dialog.

Features with an empty or non-positive class ID are ignored. ROIs in a different CRS from the raster are reprojected automatically.

## Algorithms

| Supervised | Unsupervised | Post-processing |
|---|---|---|
| Minimum Distance | K-Means | Majority filter |
| Mahalanobis Distance | ISODATA | Sieve |
| Maximum Likelihood | | Accuracy assessment (confusion matrix, OA, kappa) |
| Spectral Angle Mapper | | Area report |
| Parallelepiped | | |
| Random Forest | | |
| Support Vector Machine | | |
| Multi-layer Perceptron | | |

A trained model can be saved as a `.tlc` file and loaded again later. `.tlc` files are Python pickles, so only load files you created yourself.

## Processing Toolbox

Under **Processing Toolbox › Tarski Labs - LULC**:

- **Supervised classification**
- **Unsupervised classification (K-Means / ISODATA)**
- **Majority filter**
- **Accuracy assessment**

Outputs are loaded with the class legend applied, showing only the classes that are actually present.

## Limitations

- This is an independent re-implementation, not a port of the SAGA or SCP source code. Algorithm names and parameters follow those tools, but numerical results are **not guaranteed to be identical**.
- In particular, K-Means here uses scikit-learn's MiniBatchKMeans on a pixel sample, with clusters ordered by brightness. SAGA's K-Means (Forgy / Rubin / combined, all pixels, unordered cluster IDs) can give different cluster assignments.
- SCP features outside classification (band-set tools, pre-processing, download tools, and similar) are not included.

## Development

The plugin is a standard QGIS Python plugin:

```
tarski_lulc_classifier/
├── __init__.py      # classFactory
├── plugin.py        # toolbar / menu action, dock, provider registration
├── dock.py          # four-tab UI
├── provider.py      # Processing provider and algorithms
├── engine.py        # GDAL / NumPy classification core, no QGIS dependency
├── qgis_utils.py    # ROI collection and layer styling helpers
├── metadata.txt
└── icon.png
```

To rebuild the installable zip, zip the `tarski_lulc_classifier` folder so that the folder itself is the top level of the archive:

```bash
zip -r TarskiLabs_LULC_Classifier_1.0.2.zip tarski_lulc_classifier -x "*/__pycache__/*"
```

## Changelog

- **1.0.2** Processing Toolbox outputs get the class legend applied when loaded (fixes a 0–255 legend).
- **1.0.1** Legend shows only the classes present; `.qml` style written next to every output.
- **1.0.0** First release.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Author

Tarski Labs
