"""Tarski Labs - LULC Classifier processing core.

Pure GDAL + NumPy (+ scikit-learn for ML models). No QGIS imports, so every
function here is safe to run inside a QgsTask worker thread.

Algorithms re-implemented from:
  * SAGA imagery_classification (Grid_Classify_Supervised): minimum distance,
    Mahalanobis distance, maximum likelihood, spectral angle mapping,
    parallelepiped; imagery_isocluster (ISODATA); grid_filter (Majority).
  * Semi-Automatic Classification Plugin: random forest, SVM, multi-layer
    perceptron, accuracy assessment, classification report, sieve.
"""
import math
import os
import pickle
from xml.sax.saxutils import quoteattr

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

BLOCK_ROWS = 256
MAX_SAMPLES_PER_CLASS = 20000

SUPERVISED = {
    'mindist': 'Minimum Distance',
    'mahalanobis': 'Mahalanobis Distance',
    'maxlike': 'Maximum Likelihood',
    'sam': 'Spectral Angle Mapper',
    'parallelepiped': 'Parallelepiped',
    'rf': 'Random Forest',
    'svm': 'Support Vector Machine',
    'mlp': 'Multi-layer Perceptron',
}
UNSUPERVISED = {
    'kmeans': 'K-Means',
    'isodata': 'ISODATA',
}
STATISTICAL = ('mindist', 'mahalanobis', 'maxlike', 'sam', 'parallelepiped')

PALETTE = [
    '#1a9850', '#d73027', '#4575b4', '#fee08b', '#8c510a', '#66bd63',
    '#f46d43', '#74add1', '#bababa', '#762a83', '#a6d96a', '#fdae61',
    '#313695', '#e7298a', '#35978f', '#bf812d',
]


class Cancelled(Exception):
    pass


def _noop(*_):
    return False


# --------------------------------------------------------------------- input

def build_stack(paths, out_vrt):
    """Stack rasters into one virtual multiband raster (no pixel copy)."""
    if len(paths) == 1:
        return paths[0]
    gdal.BuildVRT(out_vrt, list(paths), separate=True, resolution='highest')
    return out_vrt


def stack_info(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    return {
        'bands': ds.RasterCount, 'width': ds.RasterXSize,
        'height': ds.RasterYSize, 'res': (abs(gt[1]), abs(gt[5])),
        'wkt': ds.GetProjection(), 'geotransform': gt,
    }


def _nodata(ds):
    return [ds.GetRasterBand(i + 1).GetNoDataValue()
            for i in range(ds.RasterCount)]


def _read(ds, xoff, yoff, w, h):
    """Read a window as (pixels, bands) float32 plus a valid-pixel mask."""
    a = ds.ReadAsArray(xoff, yoff, w, h)
    if a.ndim == 2:
        a = a[None]
    a = a.reshape(a.shape[0], -1).T.astype(np.float32, copy=False)
    valid = np.isfinite(a).all(1)
    for i, nd in enumerate(_nodata(ds)):
        if nd is not None:
            valid &= a[:, i] != nd
    return a, valid


# ------------------------------------------------------------------ training

def extract_samples(path, rois, max_per_class=MAX_SAMPLES_PER_CLASS,
                    seed=0, cancel=_noop):
    """Pixel values under ROI geometries.

    rois: iterable of (class_id, wkb) with geometries already in the raster
    CRS. Each geometry is rasterised in its own small window, so memory stays
    proportional to ROI size, not image size.
    Returns X (n, bands) float32 and y (n,) int32.
    """
    ds = gdal.Open(path) if isinstance(path, str) else path
    gt = ds.GetGeoTransform()
    inv = gdal.InvGeoTransform(gt)
    nx, ny = ds.RasterXSize, ds.RasterYSize
    srs = osr.SpatialReference()
    if ds.GetProjection():
        srs.ImportFromWkt(ds.GetProjection())
    xs, ys = [], []
    for cid, wkb in rois:
        if cancel():
            raise Cancelled()
        geom = ogr.CreateGeometryFromWkb(bytes(wkb))
        minx, maxx, miny, maxy = geom.GetEnvelope()
        cols, rows = zip(*[gdal.ApplyGeoTransform(inv, x, y)
                           for x in (minx, maxx) for y in (miny, maxy)])
        c0, r0 = max(int(math.floor(min(cols))), 0), max(int(math.floor(min(rows))), 0)
        c1, r1 = min(int(math.ceil(max(cols))), nx), min(int(math.ceil(max(rows))), ny)
        if c0 >= nx or r0 >= ny:
            continue
        c1, r1 = max(c1, c0 + 1), max(r1, r0 + 1)
        w, h = c1 - c0, r1 - r0
        mem = gdal.GetDriverByName('MEM').Create('', w, h, 1, gdal.GDT_Byte)
        ox, oy = gdal.ApplyGeoTransform(gt, c0, r0)
        mem.SetGeoTransform((ox, gt[1], gt[2], oy, gt[4], gt[5]))
        vds = ogr.GetDriverByName('Memory').CreateDataSource('')
        lyr = vds.CreateLayer('roi', srs)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(geom)
        lyr.CreateFeature(feat)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
        m = mem.ReadAsArray().astype(bool).ravel()
        if not m.any():  # tiny polygons / points / lines
            gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1],
                                options=['ALL_TOUCHED=TRUE'])
            m = mem.ReadAsArray().astype(bool).ravel()
        if not m.any():
            continue
        a, valid = _read(ds, c0, r0, w, h)
        a = a[m & valid]
        xs.append(a)
        ys.append(np.full(len(a), int(cid), np.int32))
    if not xs:
        raise ValueError('No training pixels found: check that ROIs overlap '
                         'the selected bands.')
    X, y = np.concatenate(xs), np.concatenate(ys)
    rng = np.random.default_rng(seed)
    keep = []
    for c in np.unique(y):
        idx = np.flatnonzero(y == c)
        if len(idx) > max_per_class:
            idx = rng.choice(idx, max_per_class, replace=False)
        keep.append(idx)
    keep = np.concatenate(keep)
    return X[keep], y[keep]


def class_signatures(X, y):
    """Per-class spectral statistics (SCP-style spectral signatures)."""
    out = {}
    for c in np.unique(y):
        v = X[y == c]
        out[int(c)] = {'n': len(v), 'mean': v.mean(0), 'std': v.std(0),
                       'min': v.min(0), 'max': v.max(0)}
    return out


# -------------------------------------------------------------------- models

class Model:
    """One interface for statistical, machine-learning and cluster models."""

    def __init__(self, method, **params):
        self.method = method
        self.params = params
        self.classes = None
        self.names = {}
        self.colors = {}
        self.bands = None

    # -- fitting
    def fit(self, X, y):
        self.bands = X.shape[1]
        self.classes = np.unique(y).astype(np.int32)
        m = self.method
        if m in STATISTICAL:
            self._fit_stats(X, y)
        else:
            from sklearn.preprocessing import StandardScaler
            self.scaler = StandardScaler().fit(X)
            Xs = self.scaler.transform(X)
            if m == 'rf':
                from sklearn.ensemble import RandomForestClassifier
                clf = RandomForestClassifier(
                    n_estimators=int(self.params.get('trees', 100)),
                    n_jobs=-1, class_weight='balanced', random_state=0)
            elif m == 'svm':
                from sklearn.svm import SVC
                clf = SVC(C=float(self.params.get('C', 10.0)), gamma='scale',
                          kernel='rbf', class_weight='balanced')
            elif m == 'mlp':
                from sklearn.neural_network import MLPClassifier
                hidden = tuple(int(v) for v in str(
                    self.params.get('hidden', '100')).replace(' ', '').split(',') if v)
                clf = MLPClassifier(hidden_layer_sizes=hidden or (100,),
                                    max_iter=int(self.params.get('max_iter', 300)),
                                    early_stopping=True, random_state=0)
            else:
                raise ValueError('Unknown method %s' % m)
            # Threads only: process-based joblib backends would relaunch the
            # QGIS executable on macOS.
            from joblib import parallel_backend
            with parallel_backend('threading'):
                clf.fit(Xs, y)
            self.clf = clf
        return self

    def _fit_stats(self, X, y):
        b = X.shape[1]
        self.mu = np.stack([X[y == c].mean(0) for c in self.classes])
        self.sd = np.stack([X[y == c].std(0) for c in self.classes])
        if self.method in ('mahalanobis', 'maxlike'):
            inv, logdet = [], []
            for c in self.classes:
                v = X[y == c].astype(np.float64)
                cov = np.cov(v, rowvar=False).reshape(b, b) if len(v) > 1 \
                    else np.eye(b)
                cov += np.eye(b) * (1e-6 * max(np.trace(cov) / b, 1e-6))
                inv.append(np.linalg.inv(cov))
                logdet.append(np.linalg.slogdet(cov)[1])
            self.icov = np.stack(inv)
            self.logdet = np.array(logdet)

    @classmethod
    def from_centroids(cls, centroids, method):
        m = cls('mindist', source=method)
        m.mu = np.asarray(centroids, np.float32)
        m.classes = np.arange(1, len(m.mu) + 1, dtype=np.int32)
        m.bands = m.mu.shape[1]
        return m

    # -- prediction
    def predict(self, X):
        """Return (labels int32, confidence float32 in 0..1). 0 = unclassified."""
        m = self.method
        thr = self.params.get('threshold')
        X = X.astype(np.float32, copy=False)
        if m == 'mindist':
            d = (X * X).sum(1)[:, None] - 2 * X @ self.mu.T + (self.mu ** 2).sum(1)
            d = np.sqrt(np.maximum(d, 0))
            return self._from_score(-d, thr and (d.min(1) > thr), dist=d)
        if m in ('mahalanobis', 'maxlike'):
            maha = np.empty((len(X), len(self.classes)), np.float32)
            for k in range(len(self.classes)):
                D = X - self.mu[k]
                maha[:, k] = ((D @ self.icov[k]) * D).sum(1)
            if m == 'mahalanobis':
                d = np.sqrt(np.maximum(maha, 0))
                return self._from_score(-d, thr and (d.min(1) > thr), dist=d)
            g = -0.5 * (maha + self.logdet)
            p = np.exp(g - g.max(1, keepdims=True))
            p /= p.sum(1, keepdims=True)
            return self._from_proba(p, thr)
        if m == 'sam':
            nx = np.linalg.norm(X, axis=1)[:, None] + 1e-12
            nm = np.linalg.norm(self.mu, axis=1) + 1e-12
            ang = np.degrees(np.arccos(np.clip((X @ self.mu.T) / nx / nm, -1, 1)))
            return self._from_score(-ang, thr and (ang.min(1) > thr), dist=ang)
        if m == 'parallelepiped':
            k = float(self.params.get('sigma', 2.0))
            lo, hi = self.mu - k * self.sd, self.mu + k * self.sd
            d = np.full((len(X), len(self.classes)), np.inf, np.float32)
            for i in range(len(self.classes)):
                inside = ((X >= lo[i]) & (X <= hi[i])).all(1)
                d[inside, i] = np.sqrt(((X[inside] - self.mu[i]) ** 2).sum(1))
            lab, conf = self._from_score(-d, None, dist=np.where(np.isinf(d), 1e30, d))
            lab[np.isinf(d).all(1)] = 0
            return lab, conf
        Xs = self.scaler.transform(X)
        if m in ('rf', 'mlp'):
            return self._from_proba(self.clf.predict_proba(Xs), thr)
        s = self.clf.decision_function(Xs)
        if s.ndim == 1:
            s = np.stack([-s, s], 1)
        return self._from_score(s, None)

    def _from_score(self, score, reject, dist=None):
        idx = score.argmax(1)
        lab = self.classes[idx].copy()
        if dist is not None:
            w = 1.0 / (dist + 1e-6)
            conf = w.max(1) / w.sum(1)
        else:
            e = np.exp(score - score.max(1, keepdims=True))
            conf = e.max(1) / e.sum(1)
        if isinstance(reject, np.ndarray):
            lab[reject] = 0
        return lab.astype(np.int32), conf.astype(np.float32)

    def _from_proba(self, p, thr):
        lab = self.classes[p.argmax(1)].astype(np.int32)
        conf = p.max(1).astype(np.float32)
        if thr:
            lab[conf < thr] = 0
        return lab, conf

    # -- persistence
    def save(self, path):
        with open(path, 'wb') as f:
            pickle.dump(self, f, protocol=4)

    @staticmethod
    def load(path):
        with open(path, 'rb') as f:
            return pickle.load(f)


def holdout_score(method, X, y, params, test=0.3, seed=0):
    """Stratified pixel hold-out. Optimistic (pixels from the same polygons)."""
    rng = np.random.default_rng(seed)
    tr, te = [], []
    for c in np.unique(y):
        idx = rng.permutation(np.flatnonzero(y == c))
        n = max(1, int(len(idx) * test)) if len(idx) > 1 else 0
        te.append(idx[:n])
        tr.append(idx[n:])
    tr, te = np.concatenate(tr), np.concatenate(te)
    if len(te) == 0:
        return None
    pred, _ = Model(method, **params).fit(X[tr], y[tr]).predict(X[te])
    return accuracy_from_pairs(y[te], pred)


# --------------------------------------------------------------- clustering

def sample_pixels(path, n=200000, seed=0):
    """Random valid pixels read from a decimated overview of the stack."""
    ds = gdal.Open(path)
    nx, ny = ds.RasterXSize, ds.RasterYSize
    f = max(1.0, math.sqrt(nx * ny / (n * 4.0)))
    w, h = max(1, int(nx / f)), max(1, int(ny / f))
    a = ds.ReadAsArray(0, 0, nx, ny, buf_xsize=w, buf_ysize=h)
    if a.ndim == 2:
        a = a[None]
    a = a.reshape(a.shape[0], -1).T.astype(np.float32)
    valid = np.isfinite(a).all(1)
    for i, nd in enumerate(_nodata(ds)):
        if nd is not None:
            valid &= a[:, i] != nd
    a = a[valid]
    if len(a) > n:
        a = a[np.random.default_rng(seed).choice(len(a), n, replace=False)]
    return a


def kmeans(X, k, max_iter=100, seed=0):
    from sklearn.cluster import MiniBatchKMeans
    km = MiniBatchKMeans(n_clusters=int(k), n_init=3, max_iter=int(max_iter),
                         batch_size=4096, random_state=seed).fit(X)
    return _order_by_brightness(km.cluster_centers_)


def isodata(X, k_init=8, k_max=16, max_iter=20, min_samples=None,
            split_std=None, merge_dist=None, seed=0):
    """Compact ISODATA (Ball & Hall 1965), as in SAGA imagery_isocluster."""
    rng = np.random.default_rng(seed)
    n, b = X.shape
    min_samples = min_samples or max(20, n // (k_max * 20))
    scale = X.std(0).mean() + 1e-9
    split_std = split_std or 0.6 * scale
    merge_dist = merge_dist or 0.4 * scale
    C = X[rng.choice(n, min(int(k_init), n), replace=False)].astype(np.float64)
    for it in range(int(max_iter)):
        d = (X * X).sum(1)[:, None] - 2 * X @ C.T + (C ** 2).sum(1)
        lab = d.argmin(1)
        counts = np.bincount(lab, minlength=len(C))
        keep = counts >= min_samples
        if keep.sum() == 0:
            keep[counts.argmax()] = True
        C = np.stack([X[lab == i].mean(0) for i in np.flatnonzero(keep)])
        d = (X * X).sum(1)[:, None] - 2 * X @ C.T + (C ** 2).sum(1)
        lab = d.argmin(1)
        if it % 2 == 0 and len(C) < k_max:  # split
            new = []
            for i, c in enumerate(C):
                v = X[lab == i]
                s = v.std(0) if len(v) else np.zeros(b)
                j = s.argmax()
                if s[j] > split_std and len(v) > 2 * min_samples and \
                        len(C) + len(new) // 2 < k_max:
                    e = np.zeros(b)
                    e[j] = s[j] * 0.5
                    new += [c + e, c - e]
                else:
                    new.append(c)
            C = np.array(new)
        else:  # merge closest pair below merge_dist
            dd = np.sqrt(((C[:, None] - C[None]) ** 2).sum(-1))
            np.fill_diagonal(dd, np.inf)
            i, j = np.unravel_index(dd.argmin(), dd.shape)
            if dd[i, j] < merge_dist and len(C) > 2:
                C = np.vstack([np.delete(C, [i, j], 0), (C[i] + C[j]) / 2])
    return _order_by_brightness(C)


def _order_by_brightness(C):
    C = np.asarray(C, np.float32)
    return C[np.argsort(C.mean(1))]


# ---------------------------------------------------------- classification

def _create_like(ds, path, dtype, xsize=None, ysize=None, gt=None):
    drv = gdal.GetDriverByName('GTiff')
    out = drv.Create(path, xsize or ds.RasterXSize, ysize or ds.RasterYSize, 1,
                     dtype, ['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'])
    out.SetGeoTransform(gt or ds.GetGeoTransform())
    out.SetProjection(ds.GetProjection())
    return out


def _hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def apply_legend(band, names, colors):
    if not names:
        return
    top = max(names)
    ct = gdal.ColorTable()
    ct.SetColorEntry(0, (0, 0, 0, 0))
    cats = [''] * (top + 1)
    for cid, name in names.items():
        col = colors.get(cid) or PALETTE[(cid - 1) % len(PALETTE)]
        ct.SetColorEntry(int(cid), _hex_rgb(col) + (255,))
        cats[cid] = name
    if top < 256:
        band.SetRasterColorTable(ct)
        band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    band.SetCategoryNames(cats)


def write_style(path, present=None):
    """Write <raster>.qml so QGIS shows only real classes.

    GeoTIFF always stores a 256-entry colour table; without this sidecar
    QGIS lists every entry (0..255) in the legend.
    """
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    ct = band.GetRasterColorTable()
    cats = band.GetCategoryNames() or []
    if present is None:
        present = np.flatnonzero(_histogram(band))
    entries = []
    for v in sorted(int(c) for c in present if c > 0):
        if ct and v < ct.GetCount():
            r, g, b = ct.GetColorEntry(v)[:3]
        else:
            r, g, b = _hex_rgb(PALETTE[(v - 1) % len(PALETTE)])
        name = cats[v] if v < len(cats) and cats[v] else 'Class %d' % v
        entries.append('      <paletteEntry value="%d" color="#%02x%02x%02x" '
                       'alpha="255" label=%s/>' % (v, r, g, b,
                                                   quoteattr('%d · %s' % (v, name))))
    ds = None
    with open(os.path.splitext(path)[0] + '.qml', 'w', encoding='utf-8') as f:
        f.write("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                '<qgis styleCategories="Symbology" version="3.44">\n'
                '  <pipe>\n'
                '    <rasterrenderer type="paletted" band="1" opacity="1" '
                'alphaBand="-1" nodataColor="">\n'
                '      <rasterTransparency/>\n'
                '      <colorPalette>\n%s\n      </colorPalette>\n'
                '    </rasterrenderer>\n  </pipe>\n</qgis>\n' % '\n'.join(entries))


def _histogram(band, cancel=_noop):
    counts = np.zeros(1, np.int64)
    ny, nx = band.YSize, band.XSize
    for r0 in range(0, ny, BLOCK_ROWS):
        if cancel():
            raise Cancelled()
        a = band.ReadAsArray(0, r0, nx, min(BLOCK_ROWS, ny - r0)).ravel()
        c = np.bincount(a[(a > 0) & (a < 65536)].astype(np.int64))
        if len(c) > len(counts):
            counts = np.pad(counts, (0, len(c) - len(counts)))
        counts[:len(c)] += c
    return counts


def classify(src, model, out_path, conf_path=None, src_win=None,
             out_size=None, progress=_noop, cancel=_noop):
    """Classify a stack block by block.

    src_win/out_size give a fast resampled preview of a sub-window
    (cols, rows, width, height) / (width, height).
    """
    ds = gdal.Open(src) if isinstance(src, str) else src
    if ds.RasterCount != model.bands:
        raise ValueError('Model expects %d bands, stack has %d.'
                         % (model.bands, ds.RasterCount))
    if src_win or out_size:
        opts = {'format': 'MEM'}
        if src_win:
            opts['srcWin'] = list(src_win)
        if out_size:
            opts['width'], opts['height'] = out_size
        ds = gdal.Translate('', ds, **opts)
    top = int(model.classes.max()) if len(model.classes) else 1
    dtype = gdal.GDT_Byte if top < 256 else gdal.GDT_UInt16
    out = _create_like(ds, out_path, dtype)
    ob = out.GetRasterBand(1)
    ob.SetNoDataValue(0)
    apply_legend(ob, model.names, model.colors)  # must precede pixel writes
    cb = None
    seen = set()
    if conf_path:
        cds = _create_like(ds, conf_path, gdal.GDT_Float32)
        cb = cds.GetRasterBand(1)
        cb.SetNoDataValue(-1)
    nx, ny = ds.RasterXSize, ds.RasterYSize
    for r0 in range(0, ny, BLOCK_ROWS):
        if cancel():
            raise Cancelled()
        h = min(BLOCK_ROWS, ny - r0)
        X, valid = _read(ds, 0, r0, nx, h)
        lab = np.zeros(len(X), np.int32)
        conf = np.full(len(X), -1, np.float32)
        if valid.any():
            lab[valid], conf[valid] = model.predict(X[valid])
        ob.WriteArray(lab.reshape(h, nx), 0, r0)
        seen.update(np.unique(lab).tolist())
        if cb:
            cb.WriteArray(conf.reshape(h, nx), 0, r0)
        progress(100.0 * (r0 + h) / ny)
    out.FlushCache()
    out = None
    if cb:
        cds.FlushCache()
        cds = None
    write_style(out_path, seen)
    return out_path


# ------------------------------------------------------------ post-process

def majority_filter(src_path, out_path, size=3, progress=_noop, cancel=_noop):
    """Moving-window majority (SAGA grid_filter Filter_Majority)."""
    from scipy.ndimage import uniform_filter
    ds = gdal.Open(src_path)
    band = ds.GetRasterBand(1)
    nx, ny = ds.RasterXSize, ds.RasterYSize
    out = _create_like(ds, out_path, band.DataType)
    ob = out.GetRasterBand(1)
    nd = band.GetNoDataValue()
    if nd is not None:
        ob.SetNoDataValue(nd)
    _copy_legend(band, ob)
    halo = size // 2
    classes = None
    for r0 in range(0, ny, BLOCK_ROWS):
        if cancel():
            raise Cancelled()
        h = min(BLOCK_ROWS, ny - r0)
        a0, a1 = max(0, r0 - halo), min(ny, r0 + h + halo)
        a = band.ReadAsArray(0, a0, nx, a1 - a0)
        if classes is None:
            classes = np.unique(a)
        cls = np.union1d(classes, np.unique(a))
        cls = cls[cls != (nd if nd is not None else -1)]
        best = np.full(a.shape, -1.0, np.float32)
        res = a.copy()
        for c in cls:
            f = uniform_filter((a == c).astype(np.float32), size, mode='nearest')
            upd = f > best
            best[upd] = f[upd]
            res[upd] = c
        if nd is not None:
            res[a == nd] = nd
        ob.WriteArray(res[r0 - a0:r0 - a0 + h], 0, r0)
        progress(100.0 * (r0 + h) / ny)
    out.FlushCache()
    out = None
    write_style(out_path)
    return out_path


def sieve(src_path, out_path, threshold=10, connected8=True):
    """Remove patches smaller than `threshold` pixels (GDAL sieve)."""
    ds = gdal.Open(src_path)
    out = gdal.Translate(out_path, ds, creationOptions=[
        'COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'])
    b = out.GetRasterBand(1)
    gdal.SieveFilter(b, b.GetMaskBand(), b, int(threshold),
                     8 if connected8 else 4)
    out.FlushCache()
    out = None
    write_style(out_path)
    return out_path


def _copy_legend(src_band, dst_band):
    ct = src_band.GetRasterColorTable()
    if ct:
        dst_band.SetRasterColorTable(ct)
        dst_band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    cats = src_band.GetCategoryNames()
    if cats:
        dst_band.SetCategoryNames(cats)


# ---------------------------------------------------------------- reports

def accuracy_from_pairs(ref, pred):
    labels = np.union1d(np.unique(ref), np.unique(pred[pred > 0]))
    k = len(labels)
    cm = np.zeros((k, k), np.int64)  # rows = classified, cols = reference
    m = pred > 0
    if m.any():
        np.add.at(cm, (np.searchsorted(labels, pred[m]),
                       np.searchsorted(labels, ref[m])), 1)
    n = cm.sum()
    oa = np.trace(cm) / n if n else 0.0
    pe = (cm.sum(0) * cm.sum(1)).sum() / (n * n) if n else 0.0
    kappa = (oa - pe) / (1 - pe) if pe < 1 else 1.0
    with np.errstate(invalid='ignore', divide='ignore'):
        ua = np.diag(cm) / cm.sum(1)
        pa = np.diag(cm) / cm.sum(0)
        f1 = 2 * ua * pa / (ua + pa)
    return {'labels': labels.tolist(), 'matrix': cm, 'overall': float(oa),
            'kappa': float(kappa), 'users': np.nan_to_num(ua).tolist(),
            'producers': np.nan_to_num(pa).tolist(),
            'f1': np.nan_to_num(f1).tolist(), 'samples': int(n),
            'unclassified': int((~m).sum())}


def accuracy(classified_path, rois, cancel=_noop):
    """Confusion matrix of a classified raster against reference ROIs."""
    X, y = extract_samples(classified_path, rois, max_per_class=10 ** 9,
                           cancel=cancel)
    return accuracy_from_pairs(y, X[:, 0].astype(np.int64))


def area_report(path, cancel=_noop):
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    gt = ds.GetGeoTransform()
    px = abs(gt[1] * gt[5] - gt[2] * gt[4])
    counts = _histogram(band, cancel)
    srs = osr.SpatialReference(wkt=ds.GetProjection()) if ds.GetProjection() else None
    geographic = bool(srs and srs.IsGeographic())
    unit = srs.GetLinearUnits() if srs and not geographic else 1.0
    names = band.GetCategoryNames() or []
    total = counts.sum()
    rows = []
    for cid in np.flatnonzero(counts):
        m2 = counts[cid] * px * unit * unit
        rows.append({'class': int(cid),
                     'name': names[cid] if cid < len(names) else '',
                     'pixels': int(counts[cid]), 'area_m2': float(m2),
                     'area_ha': float(m2 / 1e4),
                     'percent': float(100.0 * counts[cid] / total)})
    return {'rows': rows, 'geographic': geographic}
