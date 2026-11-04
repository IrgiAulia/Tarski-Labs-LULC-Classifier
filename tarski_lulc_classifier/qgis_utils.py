"""Small QGIS helpers shared by the dock and the Processing provider."""
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    QgsPalettedRasterRenderer, QgsRasterLayer,
)
from qgis.PyQt.QtGui import QColor


def raster_crs(wkt):
    return QgsCoordinateReferenceSystem.fromWkt(wkt) if wkt else \
        QgsCoordinateReferenceSystem()


def collect_rois(source, class_field, dest_crs, name_field=None,
                 color_field=None, context=None):
    """Read ROI features into plain data for worker threads.

    Returns (rois, names, colors): rois is a list of (class_id, wkb) with
    geometries transformed to dest_crs.
    """
    src_crs = source.sourceCrs() if hasattr(source, 'sourceCrs') else source.crs()
    xform = None
    if dest_crs.isValid() and src_crs.isValid() and src_crs != dest_crs:
        tc = context.transformContext() if context else \
            QgsProject.instance().transformContext()
        xform = QgsCoordinateTransform(src_crs, dest_crs, tc)
    rois, names, colors = [], {}, {}
    for f in source.getFeatures():
        g = f.geometry()
        cid = f[class_field]
        if g is None or g.isEmpty() or cid in (None, '') or \
                str(cid).upper() == 'NULL':
            continue
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        if xform:
            g.transform(xform)
        rois.append((cid, bytes(g.asWkb())))
        if name_field and f[name_field] not in (None, '') and cid not in names:
            names[cid] = str(f[name_field])
        if color_field and f[color_field] and cid not in colors:
            colors[cid] = str(f[color_field])
    return rois, names, colors


def style_classified(layer, names=None, colors=None):
    """Paletted renderer with class names as legend labels."""
    from . import engine
    prov = layer.dataProvider()
    stats_names = names or {}
    if not stats_names:
        return  # the .qml sidecar written by the engine is already applied
    classes = []
    for cid in sorted(stats_names):
        col = (colors or {}).get(cid) or engine.PALETTE[(cid - 1) % len(engine.PALETTE)]
        classes.append(QgsPalettedRasterRenderer.Class(
            cid, QColor(col), '%d · %s' % (cid, stats_names[cid])))
    layer.setRenderer(QgsPalettedRasterRenderer(prov, 1, classes))
    layer.triggerRepaint()


def add_classified(path, title, names=None, colors=None, replace_id=None):
    if replace_id:
        old = QgsProject.instance().mapLayer(replace_id)
        if old:
            QgsProject.instance().removeMapLayer(old)
    lyr = QgsRasterLayer(path, title)
    if not lyr.isValid():
        return None
    style_classified(lyr, names, colors)
    QgsProject.instance().addMapLayer(lyr)
    return lyr
