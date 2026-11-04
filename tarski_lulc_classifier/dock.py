"""Dock widget: Bands -> Training -> Classify -> Results."""
import csv
import os
import tempfile
import time

from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateTransform, QgsDefaultValue,
    QgsEditFormConfig, QgsExpression, QgsField, QgsMapLayerProxyModel,
    QgsProject, QgsRasterLayer, QgsTask, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import (
    QgsColorButton, QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox,
)
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QSizePolicy, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout,
    QWidget, QCheckBox,
)

from . import engine
from .qgis_utils import add_classified, collect_rois, raster_crs

ACCENT = '#0f766e'
CSS = """
QPushButton#primary { background:%s; color:white; border:none; border-radius:5px;
  padding:6px 12px; font-weight:600; }
QPushButton#primary:disabled { background:#9ca3af; }
QPushButton#primary:hover { background:#115e59; }
QLabel#hint { color:#6b7280; }
QLabel#metric { font-size:15px; font-weight:600; color:%s; }
""" % (ACCENT, ACCENT)


def _vlayout(parent=None, margin=6):
    lay = QVBoxLayout(parent)
    lay.setContentsMargins(margin, margin, margin, margin)
    lay.setSpacing(6)
    return lay


def _hint(text):
    lab = QLabel(text)
    lab.setObjectName('hint')
    lab.setWordWrap(True)
    return lab


def _primary(text):
    b = QPushButton(text)
    b.setObjectName('primary')
    return b


def _set_filters(combo, polygon_point=False, raster=False):
    try:  # QGIS >= 3.34
        f = Qgis.LayerFilter.RasterLayer if raster else \
            (Qgis.LayerFilter.PolygonLayer | Qgis.LayerFilter.PointLayer)
    except AttributeError:
        f = QgsMapLayerProxyModel.RasterLayer if raster else \
            (QgsMapLayerProxyModel.PolygonLayer | QgsMapLayerProxyModel.PointLayer)
    combo.setFilters(f)


class SignaturePlot(QWidget):
    """Tiny spectral-signature chart (mean per band per class)."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.sigs, self.names, self.colors = {}, {}, {}

    def set_data(self, sigs, names, colors):
        self.sigs, self.names, self.colors = sigs, names, colors
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(34, 8, -8, -20)
        p.setPen(QPen(QColor('#d1d5db')))
        p.drawRect(r)
        if not self.sigs:
            p.setPen(QColor('#9ca3af'))
            p.drawText(r, Qt.AlignCenter, 'Spectral signatures appear here\n'
                                          'after "Compute signatures"')
            return
        means = [s['mean'] for s in self.sigs.values()]
        lo = min(float(m.min()) for m in means)
        hi = max(float(m.max()) for m in means)
        hi = hi if hi > lo else lo + 1
        nb = len(means[0])
        xs = [r.left() + (r.width() * i / max(nb - 1, 1)) for i in range(nb)]
        f = QFont(self.font())
        f.setPointSize(max(f.pointSize() - 2, 7))
        p.setFont(f)
        p.setPen(QColor('#6b7280'))
        for i, x in enumerate(xs):
            p.drawText(int(x) - 8, r.bottom() + 15, 'B%d' % (i + 1))
        p.drawText(2, r.top() + 10, '%.4g' % hi)
        p.drawText(2, r.bottom(), '%.4g' % lo)
        for cid, s in self.sigs.items():
            col = QColor(self.colors.get(cid) or
                         engine.PALETTE[(cid - 1) % len(engine.PALETTE)])
            p.setPen(QPen(col, 2))
            pts = [(xs[i], r.bottom() - (float(v) - lo) / (hi - lo) * r.height())
                   for i, v in enumerate(s['mean'])]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(int(a[0]), int(a[1]), int(b[0]), int(b[1]))
            for x, y in pts:
                p.drawEllipse(int(x) - 2, int(y) - 2, 4, 4)


class TarskiDock(QDockWidget):

    def __init__(self, iface):
        super().__init__('Tarski Labs · LULC Classifier')
        self.setObjectName('TarskiLULCDock')
        self.iface = iface
        self.tmp = tempfile.mkdtemp(prefix='tarski_lulc_')
        self.model = None
        self.samples = None
        self.task = None
        self.preview_id = None
        self.last_report = None
        self._build()
        self.refresh_bands()
        prj = QgsProject.instance()
        prj.layersAdded.connect(self.refresh_bands)
        prj.layersRemoved.connect(self.refresh_bands)

    def disconnect_project(self):
        prj = QgsProject.instance()
        for sig in (prj.layersAdded, prj.layersRemoved):
            try:
                sig.disconnect(self.refresh_bands)
            except TypeError:
                pass

    # ================================================================ UI
    def _build(self):
        root = QWidget()
        root.setStyleSheet(CSS)
        lay = _vlayout(root)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_bands(), '① Bands')
        self.tabs.addTab(self._tab_training(), '② ROI')
        self.tabs.addTab(self._tab_classify(), '③ Classify')
        self.tabs.addTab(self._tab_results(), '④ Results')
        lay.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setMaximumHeight(16)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText('Cancel')
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_task)
        bar.addWidget(self.progress, 1)
        bar.addWidget(self.cancel_btn)
        lay.addLayout(bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        lay.addWidget(self.log)
        self.setWidget(root)

    # ---- tab 1
    def _tab_bands(self):
        w = QWidget()
        lay = _vlayout(w)
        lay.addWidget(_hint('Tick the raster bands to classify. Multiband '
                            'files are used whole. Drag to reorder.'))
        self.band_list = QListWidget()
        self.band_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.band_list.itemChanged.connect(self._bands_changed)
        lay.addWidget(self.band_list, 1)
        row = QHBoxLayout()
        b_add = QPushButton('Add files…')
        b_add.clicked.connect(self.add_files)
        b_all = QPushButton('Select all')
        b_all.clicked.connect(lambda: self._check_all(True))
        b_none = QPushButton('Clear')
        b_none.clicked.connect(lambda: self._check_all(False))
        for b in (b_add, b_all, b_none):
            row.addWidget(b)
        lay.addLayout(row)
        self.band_summary = QLabel('No bands selected')
        self.band_summary.setObjectName('metric')
        self.band_summary.setWordWrap(True)
        lay.addWidget(self.band_summary)
        return w

    # ---- tab 2
    def _tab_training(self):
        w = QWidget()
        lay = _vlayout(w)
        box = QGroupBox('ROI layer')
        f = QFormLayout(box)
        row = QHBoxLayout()
        self.roi_combo = QgsMapLayerComboBox()
        _set_filters(self.roi_combo, polygon_point=True)
        self.roi_combo.setAllowEmptyLayer(True)
        self.roi_combo.layerChanged.connect(self._roi_changed)
        b_new = QToolButton()
        b_new.setText('New')
        b_new.setToolTip('Create a new ROI polygon layer')
        b_new.clicked.connect(self.new_roi_layer)
        row.addWidget(self.roi_combo, 1)
        row.addWidget(b_new)
        f.addRow('Layer', row)
        self.id_field = QgsFieldComboBox()
        self.name_field = QgsFieldComboBox()
        self.name_field.setAllowEmptyFieldName(True)
        f.addRow('Class ID field', self.id_field)
        f.addRow('Class name field', self.name_field)
        lay.addWidget(box)

        box = QGroupBox('Draw ROIs')
        g = _vlayout(box, 4)
        row = QHBoxLayout()
        self.cur_id = QSpinBox()
        self.cur_id.setRange(1, 65535)
        self.cur_name = QLineEdit('Vegetation')
        self.cur_color = QgsColorButton()
        self.cur_color.setColor(QColor(engine.PALETTE[0]))
        self.cur_color.setMaximumWidth(40)
        self.cur_id.valueChanged.connect(self._class_id_changed)
        self.cur_name.editingFinished.connect(self._apply_defaults)
        self.cur_color.colorChanged.connect(self._apply_defaults)
        row.addWidget(QLabel('ID'))
        row.addWidget(self.cur_id)
        row.addWidget(self.cur_name, 1)
        row.addWidget(self.cur_color)
        g.addLayout(row)
        row = QHBoxLayout()
        self.draw_btn = _primary('✎  Draw ROI')
        self.draw_btn.setCheckable(True)
        self.draw_btn.toggled.connect(self.toggle_draw)
        b_next = QPushButton('+ New class')
        b_next.clicked.connect(self.next_class)
        row.addWidget(self.draw_btn, 1)
        row.addWidget(b_next)
        g.addLayout(row)
        g.addWidget(_hint('Each new polygon gets the class above. '
                          'Double-click a row below to switch class.'))
        lay.addWidget(box)

        self.class_table = QTableWidget(0, 4)
        self.class_table.setHorizontalHeaderLabels(['ID', 'Name', 'ROIs', 'Pixels'])
        self.class_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.class_table.verticalHeader().setVisible(False)
        self.class_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.class_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.class_table.cellDoubleClicked.connect(self._pick_class)
        lay.addWidget(self.class_table, 1)
        row = QHBoxLayout()
        b_ref = QPushButton('Refresh classes')
        b_ref.clicked.connect(self.refresh_classes)
        self.sig_btn = QPushButton('Compute signatures')
        self.sig_btn.clicked.connect(self.compute_signatures)
        row.addWidget(b_ref)
        row.addWidget(self.sig_btn)
        lay.addLayout(row)
        self.sig_plot = SignaturePlot()
        lay.addWidget(self.sig_plot)
        self._roi_changed(self.roi_combo.currentLayer())
        return w

    # ---- tab 3
    def _tab_classify(self):
        w = QWidget()
        lay = _vlayout(w)
        row = QHBoxLayout()
        self.r_sup = QRadioButton('Supervised')
        self.r_uns = QRadioButton('Unsupervised')
        self.r_sup.setChecked(True)
        self.r_sup.toggled.connect(self._mode_changed)
        row.addWidget(self.r_sup)
        row.addWidget(self.r_uns)
        row.addStretch()
        lay.addLayout(row)

        box = QGroupBox('Algorithm')
        f = QFormLayout(box)
        self.algo = QComboBox()
        self.algo.currentIndexChanged.connect(self._algo_changed)
        f.addRow(self.algo)
        self.params = QStackedWidget()
        self.params.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        f.addRow(self.params)
        self.p = {}
        self._pages = {}
        self._add_page('stat', [('threshold', 'Reject threshold (0 = off)',
                                 QDoubleSpinBox, (0, 1e6, 0, 3))],
                       'Distance / angle (°) / probability (0–1) above which '
                       'a pixel stays unclassified.')
        self._add_page('para', [('sigma', 'Box width (× std. dev.)',
                                 QDoubleSpinBox, (0.5, 10, 2, 1))])
        self._add_page('rf', [('trees', 'Number of trees', QSpinBox, (10, 2000, 100))])
        self._add_page('svm', [('C', 'C (regularisation)', QDoubleSpinBox,
                                (0.01, 10000, 10, 2))])
        self._add_page('mlp', [('hidden', 'Hidden layers', QLineEdit, '100,50'),
                               ('max_iter', 'Max iterations', QSpinBox, (50, 5000, 300))])
        self._add_page('kmeans', [('k', 'Number of classes', QSpinBox, (2, 255, 8)),
                                  ('iters', 'Max iterations', QSpinBox, (5, 1000, 100))])
        self._add_page('isodata', [('k', 'Initial clusters', QSpinBox, (2, 255, 8)),
                                   ('kmax', 'Max clusters', QSpinBox, (2, 255, 16)),
                                   ('iters', 'Iterations', QSpinBox, (2, 500, 20))])
        lay.addWidget(box)

        self.holdout = QCheckBox('Validate with 30% hold-out pixels')
        self.holdout.setChecked(True)
        lay.addWidget(self.holdout)
        row = QHBoxLayout()
        self.train_btn = _primary('①  Train')
        self.train_btn.clicked.connect(self.train)
        self.prev_btn = QPushButton('②  Preview view')
        self.prev_btn.setToolTip('Classify only the current map view, fast')
        self.prev_btn.clicked.connect(self.preview)
        row.addWidget(self.train_btn)
        row.addWidget(self.prev_btn)
        lay.addLayout(row)
        self.model_label = QLabel('No model trained yet')
        self.model_label.setObjectName('metric')
        self.model_label.setWordWrap(True)
        lay.addWidget(self.model_label)

        box = QGroupBox('Full classification')
        g = _vlayout(box, 4)
        self.out_file = QgsFileWidget()
        self.out_file.setStorageMode(QgsFileWidget.SaveFile)
        self.out_file.setFilter('GeoTIFF (*.tif)')
        self.out_file.lineEdit().setPlaceholderText('[temporary file]')
        g.addWidget(self.out_file)
        self.conf_chk = QCheckBox('Also write confidence raster')
        g.addWidget(self.conf_chk)
        self.run_btn = _primary('③  Classify full image')
        self.run_btn.clicked.connect(self.classify_full)
        g.addWidget(self.run_btn)
        lay.addWidget(box)
        row = QHBoxLayout()
        b_s = QPushButton('Save model…')
        b_s.clicked.connect(self.save_model)
        b_l = QPushButton('Load model…')
        b_l.clicked.connect(self.load_model)
        row.addWidget(b_s)
        row.addWidget(b_l)
        lay.addLayout(row)
        lay.addStretch()
        self._mode_changed()
        return w

    def _add_page(self, key, fields, hint=None):
        page = QWidget()
        f = QFormLayout(page)
        f.setContentsMargins(0, 0, 0, 0)
        for name, label, cls, spec in fields:
            wdg = cls()
            if cls is QLineEdit:
                wdg.setText(spec)
            else:
                if cls is QDoubleSpinBox:
                    wdg.setDecimals(spec[3])
                wdg.setRange(spec[0], spec[1])
                wdg.setValue(spec[2])
            self.p[(key, name)] = wdg
            f.addRow(label, wdg)
        if hint:
            f.addRow(_hint(hint))
        self._pages[key] = self.params.count()
        self.params.addWidget(page)

    # ---- tab 4
    def _tab_results(self):
        w = QWidget()
        lay = _vlayout(w)
        f = QFormLayout()
        self.res_combo = QgsMapLayerComboBox()
        _set_filters(self.res_combo, raster=True)
        f.addRow('Classified raster', self.res_combo)
        lay.addLayout(f)

        box = QGroupBox('Clean-up')
        g = QFormLayout(box)
        row = QHBoxLayout()
        self.maj_size = QSpinBox()
        self.maj_size.setRange(3, 25)
        self.maj_size.setSingleStep(2)
        b_maj = QPushButton('Majority filter')
        b_maj.clicked.connect(self.run_majority)
        row.addWidget(self.maj_size)
        row.addWidget(b_maj, 1)
        g.addRow('Window', row)
        row = QHBoxLayout()
        self.sieve_px = QSpinBox()
        self.sieve_px.setRange(2, 100000)
        self.sieve_px.setValue(10)
        b_sv = QPushButton('Sieve')
        b_sv.clicked.connect(self.run_sieve)
        row.addWidget(self.sieve_px)
        row.addWidget(b_sv, 1)
        g.addRow('Min. pixels', row)
        lay.addWidget(box)

        box = QGroupBox('Accuracy assessment')
        g = QFormLayout(box)
        self.val_combo = QgsMapLayerComboBox()
        _set_filters(self.val_combo, polygon_point=True)
        self.val_field = QgsFieldComboBox()
        self.val_combo.layerChanged.connect(self.val_field.setLayer)
        self.val_field.setLayer(self.val_combo.currentLayer())
        g.addRow('Reference layer', self.val_combo)
        g.addRow('Class ID field', self.val_field)
        row = QHBoxLayout()
        b_acc = _primary('Assess accuracy')
        b_acc.clicked.connect(self.run_accuracy)
        b_area = QPushButton('Area report')
        b_area.clicked.connect(self.run_area)
        row.addWidget(b_acc)
        row.addWidget(b_area)
        g.addRow(row)
        lay.addWidget(box)

        self.metric = QLabel('')
        self.metric.setObjectName('metric')
        self.metric.setWordWrap(True)
        lay.addWidget(self.metric)
        self.report = QTableWidget(0, 0)
        self.report.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.report, 1)
        b_csv = QPushButton('Export table to CSV…')
        b_csv.clicked.connect(self.export_csv)
        lay.addWidget(b_csv)
        return w

    # ========================================================= helpers
    def say(self, msg, level=None):
        self.log.appendPlainText(time.strftime('%H:%M:%S  ') + msg)
        if level is not None:
            self.iface.messageBar().pushMessage('Tarski LULC', msg, level, 6)

    def _busy(self, on):
        for b in (self.train_btn, self.prev_btn, self.run_btn, self.sig_btn):
            b.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        if not on:
            self.progress.setValue(0)

    def run_task(self, title, fn, done, *args):
        if self.task is not None:
            self.say('Another job is still running.', Qgis.Warning)
            return

        def finished(exc, result=None):
            self.task = None
            self._busy(False)
            if isinstance(exc, engine.Cancelled) or \
                    (exc is None and result is None and task.isCanceled()):
                self.say('%s cancelled.' % title)
                return
            if exc is not None:
                self.say('%s failed: %s' % (title, exc), Qgis.Critical)
                return
            try:
                done(result)
            except Exception as e:  # keep the dock alive on UI errors
                self.say('%s: %s' % (title, e), Qgis.Critical)

        task = QgsTask.fromFunction(title, fn, *args, on_finished=finished)
        task.progressChanged.connect(lambda v: self.progress.setValue(int(v)))
        self.task = task
        self._busy(True)
        self.say(title + '…')
        QgsApplication.taskManager().addTask(task)

    def cancel_task(self):
        if self.task:
            self.task.cancel()

    # ---------------------------------------------------------- bands
    def refresh_bands(self, *_):
        checked = set(self.selected_layer_ids())
        order = [self.band_list.item(i).data(Qt.UserRole)
                 for i in range(self.band_list.count())]
        layers = [l for l in QgsProject.instance().mapLayers().values()
                  if isinstance(l, QgsRasterLayer) and
                  l.providerType() == 'gdal' and
                  l.id() != self.preview_id and
                  not l.name().startswith('Tarski ')]
        layers.sort(key=lambda l: (order.index(l.id()) if l.id() in order
                                   else len(order), l.name().lower()))
        self.band_list.blockSignals(True)
        self.band_list.clear()
        for l in layers:
            it = QListWidgetItem('%s   (%d band%s)' % (
                l.name(), l.bandCount(), '' if l.bandCount() == 1 else 's'))
            it.setData(Qt.UserRole, l.id())
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if l.id() in checked else Qt.Unchecked)
            self.band_list.addItem(it)
        self.band_list.blockSignals(False)
        self._bands_changed()

    def selected_layer_ids(self):
        return [self.band_list.item(i).data(Qt.UserRole)
                for i in range(self.band_list.count())
                if self.band_list.item(i).checkState() == Qt.Checked]

    def _check_all(self, on):
        self.band_list.blockSignals(True)
        for i in range(self.band_list.count()):
            self.band_list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.band_list.blockSignals(False)
        self._bands_changed()

    def _bands_changed(self, *_):
        layers = self.selected_layers()
        if not layers:
            self.band_summary.setText('No bands selected')
            return
        n = sum(l.bandCount() for l in layers)
        l0 = layers[0]
        self.band_summary.setText('%d band%s · %.4g × %.4g px · %s' % (
            n, '' if n == 1 else 's', l0.rasterUnitsPerPixelX(),
            l0.rasterUnitsPerPixelY(), l0.crs().authid() or 'custom CRS'))

    def selected_layers(self):
        prj = QgsProject.instance()
        return [prj.mapLayer(i) for i in self.selected_layer_ids()
                if prj.mapLayer(i)]

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, 'Add raster bands', '',
            'Rasters (*.tif *.tiff *.TIF *.jp2 *.img *.vrt *.nc *.hdf);;All files (*)')
        for fn in files:
            lyr = QgsRasterLayer(fn, os.path.splitext(os.path.basename(fn))[0])
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                for i in range(self.band_list.count()):
                    it = self.band_list.item(i)
                    if it.data(Qt.UserRole) == lyr.id():
                        it.setCheckState(Qt.Checked)

    def stack(self):
        layers = self.selected_layers()
        if not layers:
            raise ValueError('Select at least one raster in “① Bands”.')
        paths = [l.source() for l in layers]
        vrt = os.path.join(self.tmp, 'stack_%d.vrt' % int(time.time() * 1000))
        path = engine.build_stack(paths, vrt)
        return path, engine.stack_info(path)

    # -------------------------------------------------------- training
    def _roi_changed(self, layer):
        self.id_field.setLayer(layer)
        self.name_field.setLayer(layer)
        if layer:
            names = [f.name() for f in layer.fields()]
            for cand in ('class_id', 'classid', 'id_class', 'macroclass_id', 'class', 'id'):
                hit = [n for n in names if n.lower() == cand]
                if hit:
                    self.id_field.setField(hit[0])
                    break
            for cand in ('class_name', 'name', 'classname', 'label'):
                hit = [n for n in names if n.lower() == cand]
                if hit:
                    self.name_field.setField(hit[0])
                    break
        self.refresh_classes()

    def new_roi_layer(self):
        layers = self.selected_layers()
        crs = layers[0].crs().authid() if layers and layers[0].crs().authid() \
            else QgsProject.instance().crs().authid()
        lyr = QgsVectorLayer('Polygon?crs=%s' % crs, 'Tarski ROI', 'memory')
        lyr.dataProvider().addAttributes([
            QgsField('class_id', QVariant.Int),
            QgsField('class_name', QVariant.String),
            QgsField('color', QVariant.String)])
        lyr.updateFields()
        lyr.renderer().symbol().setOpacity(0.55)
        QgsProject.instance().addMapLayer(lyr)
        self.roi_combo.setLayer(lyr)
        self.say('ROI layer created (temporary — right-click › Make Permanent '
                 'to save it).')

    def _class_id_changed(self, cid):
        known = self.class_info().get(cid)
        if known:
            self.cur_name.setText(known['name'])
            if known['color']:
                self.cur_color.setColor(QColor(known['color']))
        else:
            self.cur_color.setColor(QColor(engine.PALETTE[(cid - 1) % len(engine.PALETTE)]))
        self._apply_defaults()

    def next_class(self):
        info = self.class_info()
        nxt = max(info) + 1 if info else self.cur_id.value() + 1
        self.cur_id.setValue(nxt)
        self.cur_name.setText('Class %d' % nxt)
        self.cur_name.setFocus()
        self.cur_name.selectAll()

    def _apply_defaults(self):
        """New features automatically get the current class attributes."""
        lyr = self.roi_combo.currentLayer()
        if not lyr:
            return
        id_f, name_f = self.id_field.currentField(), self.name_field.currentField()
        vals = {id_f: str(self.cur_id.value()),
                name_f: QgsExpression.quotedValue(self.cur_name.text()),
                'color': QgsExpression.quotedValue(self.cur_color.color().name())}
        for field, expr in vals.items():
            idx = lyr.fields().indexOf(field) if field else -1
            if idx >= 0:
                lyr.setDefaultValueDefinition(idx, QgsDefaultValue(expr))
        # changing default definitions reloads the field combos' models
        self.id_field.setField(id_f)
        self.name_field.setField(name_f)

    def toggle_draw(self, on):
        lyr = self.roi_combo.currentLayer()
        if on and not lyr:
            self.new_roi_layer()
            lyr = self.roi_combo.currentLayer()
        if not lyr:
            self.draw_btn.setChecked(False)
            return
        if on:
            cfg = lyr.editFormConfig()
            try:
                cfg.setSuppress(Qgis.AttributeFormSuppression.On)
            except AttributeError:
                cfg.setSuppress(QgsEditFormConfig.SuppressOn)
            lyr.setEditFormConfig(cfg)
            self._apply_defaults()
            self.iface.setActiveLayer(lyr)
            if not lyr.isEditable():
                lyr.startEditing()
            self.iface.actionAddFeature().trigger()
            self.draw_btn.setText('✓  Finish drawing')
            self.say('Draw polygons on the map for class %d · %s. '
                     'Right-click to close a polygon.'
                     % (self.cur_id.value(), self.cur_name.text()))
        else:
            if lyr.isEditable():
                lyr.commitChanges()
            self.iface.actionPan().trigger()
            self.draw_btn.setText('✎  Draw ROI')
            self.refresh_classes()

    def class_info(self):
        lyr = self.roi_combo.currentLayer()
        fid, fname = self.id_field.currentField(), self.name_field.currentField()
        out = {}
        if not lyr or not fid:
            return out
        has_color = lyr.fields().indexOf('color') >= 0
        for f in lyr.getFeatures():
            try:
                cid = int(f[fid])
            except (TypeError, ValueError):
                continue
            d = out.setdefault(cid, {'name': '', 'color': '', 'n': 0})
            d['n'] += 1
            if fname and not d['name'] and f[fname] not in (None, ''):
                d['name'] = str(f[fname])
            if has_color and not d['color'] and f['color']:
                d['color'] = str(f['color'])
        for cid, d in out.items():
            d['name'] = d['name'] or 'Class %d' % cid
        return out

    def refresh_classes(self, pixels=None):
        info = self.class_info()
        self.class_table.setRowCount(len(info))
        for r, cid in enumerate(sorted(info)):
            d = info[cid]
            it = QTableWidgetItem(str(cid))
            col = QColor(d['color'] or engine.PALETTE[(cid - 1) % len(engine.PALETTE)])
            it.setBackground(col)
            it.setForeground(QColor('white' if col.lightness() < 140 else 'black'))
            self.class_table.setItem(r, 0, it)
            self.class_table.setItem(r, 1, QTableWidgetItem(d['name']))
            self.class_table.setItem(r, 2, QTableWidgetItem(str(d['n'])))
            px = (pixels or {}).get(cid)
            self.class_table.setItem(r, 3, QTableWidgetItem('' if px is None else str(px)))
        self.class_table.resizeColumnToContents(0)

    def _pick_class(self, row, _col):
        self.cur_id.setValue(int(self.class_table.item(row, 0).text()))

    def _training_inputs(self):
        lyr = self.roi_combo.currentLayer()
        if not lyr or not self.id_field.currentField():
            raise ValueError('Choose an ROI layer and its class ID field in '
                             '“② ROI”.')
        if lyr.isEditable():
            lyr.commitChanges()
            lyr.startEditing()
        path, info = self.stack()
        rois, names, colors = collect_rois(
            lyr, self.id_field.currentField(), raster_crs(info['wkt']),
            self.name_field.currentField() or None,
            'color' if lyr.fields().indexOf('color') >= 0 else None)
        if not rois:
            raise ValueError('The ROI layer has no features with a valid class ID.')
        for cid, d in self.class_info().items():
            names.setdefault(cid, d['name'])
        return path, info, rois, names, colors

    def compute_signatures(self):
        try:
            path, info, rois, names, colors = self._training_inputs()
        except ValueError as e:
            self.say(str(e), Qgis.Warning)
            return

        def work(task):
            X, y = engine.extract_samples(path, rois, cancel=task.isCanceled)
            return X, y

        def done(res):
            X, y = res
            sigs = engine.class_signatures(X, y)
            self.sig_plot.set_data(sigs, names, colors)
            self.refresh_classes({c: s['n'] for c, s in sigs.items()})
            self.say('Signatures from %d pixels, %d classes.' % (len(y), len(sigs)))

        self.run_task('Computing signatures', work, done)

    # -------------------------------------------------------- classify
    def _mode_changed(self, *_):
        self.algo.blockSignals(True)
        self.algo.clear()
        items = engine.SUPERVISED if self.r_sup.isChecked() else engine.UNSUPERVISED
        for k, v in items.items():
            self.algo.addItem(v, k)
        self.algo.blockSignals(False)
        if self.r_sup.isChecked():
            self.algo.setCurrentIndex(list(items).index('rf'))
        self.holdout.setVisible(self.r_sup.isChecked())
        self._algo_changed()

    def _algo_changed(self, *_):
        m = self.algo.currentData()
        key = {'mindist': 'stat', 'mahalanobis': 'stat', 'maxlike': 'stat',
               'sam': 'stat', 'parallelepiped': 'para'}.get(m, m)
        if key in self._pages:
            self.params.setCurrentIndex(self._pages[key])

    def _param(self, key, name):
        w = self.p[(key, name)]
        return w.text() if isinstance(w, QLineEdit) else w.value()

    def train(self):
        method = self.algo.currentData()
        try:
            if self.r_sup.isChecked():
                path, info, rois, names, colors = self._training_inputs()
            else:
                path, info = self.stack()
        except ValueError as e:
            self.say(str(e), Qgis.Warning)
            return
        if self.r_sup.isChecked():
            params = {}
            if method in engine.STATISTICAL and method != 'parallelepiped':
                params['threshold'] = self._param('stat', 'threshold') or None
            elif method == 'parallelepiped':
                params['sigma'] = self._param('para', 'sigma')
            elif method == 'rf':
                params['trees'] = self._param('rf', 'trees')
            elif method == 'svm':
                params['C'] = self._param('svm', 'C')
            elif method == 'mlp':
                params['hidden'] = self._param('mlp', 'hidden')
                params['max_iter'] = self._param('mlp', 'max_iter')
            holdout = self.holdout.isChecked()

            def work(task):
                X, y = engine.extract_samples(path, rois, cancel=task.isCanceled)
                task.setProgress(30)
                score = engine.holdout_score(method, X, y, params) if holdout else None
                task.setProgress(60)
                model = engine.Model(method, **params).fit(X, y)
                return model, X, y, score

            def done(res):
                model, X, y, score = res
                model.names = {int(c): names.get(int(c), 'Class %d' % c)
                               for c in model.classes}
                model.colors = {c: colors.get(c) for c in model.names
                                if colors.get(c)}
                self.model = model
                sigs = engine.class_signatures(X, y)
                self.sig_plot.set_data(sigs, model.names, model.colors)
                self.refresh_classes({c: s['n'] for c, s in sigs.items()})
                txt = '✓ %s · %d classes · %d px' % (
                    engine.SUPERVISED[method], len(model.classes), len(y))
                if score:
                    txt += '\nHold-out OA %.1f%% · κ %.3f' % (
                        100 * score['overall'], score['kappa'])
                self.model_label.setText(txt)
                self.say(txt.replace('\n', ' · '), Qgis.Success)
        else:
            key = method

            k = self._param(key, 'k')
            iters = self._param(key, 'iters')
            kmax = self._param('isodata', 'kmax')

            def work(task):
                X = engine.sample_pixels(path)
                task.setProgress(40)
                if method == 'kmeans':
                    C = engine.kmeans(X, k, iters)
                else:
                    C = engine.isodata(X, k, max(k, kmax), iters)
                return engine.Model.from_centroids(C, method), len(X)

            def done(res):
                model, n = res
                model.names = {int(c): 'Cluster %d' % c for c in model.classes}
                model.colors = {}
                self.model = model
                txt = '✓ %s · %d clusters from %d sampled px' % (
                    engine.UNSUPERVISED[method], len(model.classes), n)
                self.model_label.setText(txt)
                self.sig_plot.set_data(
                    {int(c): {'mean': m} for c, m in zip(model.classes, model.mu)},
                    model.names, {})
                self.say(txt, Qgis.Success)

        self.run_task('Training ' + self.algo.currentText(), work, done)

    def _need_model(self):
        if self.model is None:
            self.say('Train or load a model first.', Qgis.Warning)
            return False
        return True

    def preview(self):
        if not self._need_model():
            return
        try:
            path, info = self.stack()
        except ValueError as e:
            self.say(str(e), Qgis.Warning)
            return
        canvas = self.iface.mapCanvas()
        ext = canvas.extent()
        dst = raster_crs(info['wkt'])
        src = canvas.mapSettings().destinationCrs()
        if dst.isValid() and src != dst:
            ext = QgsCoordinateTransform(src, dst, QgsProject.instance()).transformBoundingBox(ext)
        gt = info['geotransform']
        c0 = max(0, int((ext.xMinimum() - gt[0]) / gt[1]))
        c1 = min(info['width'], int((ext.xMaximum() - gt[0]) / gt[1]) + 1)
        r0 = max(0, int((ext.yMaximum() - gt[3]) / gt[5]))
        r1 = min(info['height'], int((ext.yMinimum() - gt[3]) / gt[5]) + 1)
        if c1 <= c0 or r1 <= r0:
            self.say('The map view does not overlap the bands.', Qgis.Warning)
            return
        w, h = c1 - c0, r1 - r0
        scale = min(1.0, max(canvas.width(), 400) / float(w),
                    max(canvas.height(), 300) / float(h))
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        out = os.path.join(self.tmp, 'preview_%d.tif' % int(time.time() * 1000))
        model = self.model

        def work(task):
            return engine.classify(path, model, out, src_win=(c0, r0, w, h),
                                   out_size=size, progress=task.setProgress,
                                   cancel=task.isCanceled)

        def done(p):
            lyr = add_classified(p, 'Tarski preview', model.names, model.colors,
                                 replace_id=self.preview_id)
            self.preview_id = lyr.id() if lyr else None
            self.say('Preview ready (%d × %d px).' % size)

        self.run_task('Preview', work, done)

    def classify_full(self):
        if not self._need_model():
            return
        try:
            path, info = self.stack()
        except ValueError as e:
            self.say(str(e), Qgis.Warning)
            return
        out = self.out_file.filePath().strip() or os.path.join(
            self.tmp, 'classification_%d.tif' % int(time.time()))
        if not out.lower().endswith(('.tif', '.tiff')):
            out += '.tif'
        conf = os.path.splitext(out)[0] + '_confidence.tif' if self.conf_chk.isChecked() else None
        model = self.model

        def work(task):
            return engine.classify(path, model, out, conf_path=conf,
                                   progress=task.setProgress, cancel=task.isCanceled)

        def done(p):
            title = 'Tarski LULC · %s' % (engine.SUPERVISED.get(model.method) or
                                          engine.UNSUPERVISED.get(model.params.get('source'), 'Clusters'))
            lyr = add_classified(p, title, model.names, model.colors)
            if conf:
                QgsProject.instance().addMapLayer(QgsRasterLayer(conf, 'Tarski confidence'))
            if lyr:
                self.res_combo.setLayer(lyr)
            self.say('Saved %s' % p, Qgis.Success)
            self.tabs.setCurrentIndex(3)

        self.run_task('Classifying', work, done)

    def save_model(self):
        if not self._need_model():
            return
        fn, _ = QFileDialog.getSaveFileName(self, 'Save model', '', 'Tarski model (*.tlc)')
        if fn:
            if not fn.endswith('.tlc'):
                fn += '.tlc'
            self.model.save(fn)
            self.say('Model saved: %s' % fn)

    def load_model(self):
        fn, _ = QFileDialog.getOpenFileName(self, 'Load model', '', 'Tarski model (*.tlc)')
        if fn:
            try:
                self.model = engine.Model.load(fn)
            except Exception as e:
                self.say('Cannot load model: %s' % e, Qgis.Critical)
                return
            self.model_label.setText('✓ Loaded %s · %d classes · %d bands' % (
                os.path.basename(fn), len(self.model.classes), self.model.bands))

    # --------------------------------------------------------- results
    def _result_path(self):
        lyr = self.res_combo.currentLayer()
        if not lyr:
            self.say('Choose a classified raster.', Qgis.Warning)
            return None, None
        return lyr.source(), lyr

    def run_majority(self):
        src, lyr = self._result_path()
        if not src:
            return
        size = self.maj_size.value() | 1
        out = os.path.join(self.tmp, 'majority%d_%d.tif' % (size, int(time.time())))

        def work(task):
            return engine.majority_filter(src, out, size, progress=task.setProgress,
                                          cancel=task.isCanceled)

        self.run_task('Majority filter', work,
                      lambda p: self._post_done(p, '%s · majority %d' % (lyr.name(), size)))

    def run_sieve(self):
        src, lyr = self._result_path()
        if not src:
            return
        n = self.sieve_px.value()
        out = os.path.join(self.tmp, 'sieve%d_%d.tif' % (n, int(time.time())))
        self.run_task('Sieve', lambda task: engine.sieve(src, out, n),
                      lambda p: self._post_done(p, '%s · sieve %d' % (lyr.name(), n)))

    def _post_done(self, path, title):
        lyr = add_classified(path, title)
        if lyr:
            self.res_combo.setLayer(lyr)
        self.say('Created %s' % title)

    def run_accuracy(self):
        src, lyr = self._result_path()
        ref = self.val_combo.currentLayer()
        if not src:
            return
        if not ref or not self.val_field.currentField():
            self.say('Choose a reference layer and class field.', Qgis.Warning)
            return
        rois, _, _ = collect_rois(ref, self.val_field.currentField(), lyr.crs())

        def work(task):
            return engine.accuracy(src, rois, cancel=task.isCanceled)

        def done(acc):
            labels = acc['labels']
            n = len(labels)
            self.metric.setText('Overall accuracy %.2f%%  ·  Kappa %.3f  ·  %d px'
                                % (100 * acc['overall'], acc['kappa'], acc['samples']))
            hdr = [str(l) for l in labels] + ['Total', "User's %"]
            t = self.report
            t.clear()
            t.setRowCount(n + 2)
            t.setColumnCount(n + 2)
            t.setHorizontalHeaderLabels(hdr)
            t.setVerticalHeaderLabels([str(l) for l in labels] + ['Total', "Producer's %"])
            cm = acc['matrix']
            rows = [[str(int(v)) for v in cm[i]] + [str(int(cm[i].sum())),
                    '%.1f' % (100 * acc['users'][i])] for i in range(n)]
            rows.append([str(int(v)) for v in cm.sum(0)] + [str(int(cm.sum())), ''])
            rows.append(['%.1f' % (100 * v) for v in acc['producers']] + ['', ''])
            for r, vals in enumerate(rows):
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if r == c and r < n:
                        it.setBackground(QColor('#ccfbf1'))
                    t.setItem(r, c, it)
            t.resizeColumnsToContents()
            self.last_report = [['classified \\ reference'] + hdr] + \
                [[t.verticalHeaderItem(r).text()] + vals for r, vals in enumerate(rows)] + \
                [[], ['overall_accuracy', acc['overall']], ['kappa', acc['kappa']]]
            self.say('Accuracy: OA %.2f%%, kappa %.3f' % (100 * acc['overall'], acc['kappa']),
                     Qgis.Success)

        self.run_task('Accuracy assessment', work, done)

    def run_area(self):
        src, lyr = self._result_path()
        if not src:
            return

        def done(rep):
            rows = rep['rows']
            t = self.report
            t.clear()
            hdr = ['Class', 'Name', 'Pixels', 'Area (ha)', '%']
            t.setColumnCount(len(hdr))
            t.setRowCount(len(rows))
            t.setHorizontalHeaderLabels(hdr)
            t.verticalHeader().setVisible(False)
            for r, d in enumerate(rows):
                vals = [str(d['class']), d['name'], str(d['pixels']),
                        '%.2f' % d['area_ha'], '%.2f' % d['percent']]
                for c, v in enumerate(vals):
                    t.setItem(r, c, QTableWidgetItem(v))
            t.resizeColumnsToContents()
            tot = sum(d['area_ha'] for d in rows)
            self.metric.setText('Total classified area %.2f ha%s' % (
                tot, '  (⚠ geographic CRS: areas in degrees²)' if rep['geographic'] else ''))
            self.last_report = [hdr] + [[d['class'], d['name'], d['pixels'],
                                         d['area_ha'], d['percent']] for d in rows]

        self.run_task('Area report', lambda task: engine.area_report(src, task.isCanceled), done)

    def export_csv(self):
        if not self.last_report:
            self.say('Run an accuracy or area report first.', Qgis.Warning)
            return
        fn, _ = QFileDialog.getSaveFileName(self, 'Export CSV', '', 'CSV (*.csv)')
        if fn:
            if not fn.lower().endswith('.csv'):
                fn += '.csv'
            with open(fn, 'w', newline='') as f:
                csv.writer(f).writerows(self.last_report)
            self.say('Exported %s' % fn)
