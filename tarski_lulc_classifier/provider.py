"""Processing provider so every tool also works in the Toolbox, batch mode
and the Graphical Modeler (the role SAGA NextGen plays for SAGA tools)."""
import csv
import os

from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingOutputNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField, QgsProcessingParameterFileDestination,
    QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer,
    QgsProcessingProvider, QgsProcessingUtils,
)
from qgis.PyQt.QtGui import QIcon

from . import engine
from .qgis_utils import collect_rois, raster_crs

ICON = os.path.join(os.path.dirname(__file__), 'icon.png')


class _QmlStyle(QgsProcessingLayerPostProcessorInterface):
    """Apply the engine's <raster>.qml after Processing loads the output.

    Processing opens result layers without their default style, so QGIS
    would otherwise list all 256 GeoTIFF palette entries in the legend.
    """
    instance = None

    def postProcessLayer(self, layer, context, feedback):
        qml = os.path.splitext(layer.source().split('|')[0])[0] + '.qml'
        if os.path.exists(qml):
            layer.loadNamedStyle(qml)
            layer.triggerRepaint()
            layer.emitStyleChanged()

    @staticmethod
    def create():
        # keep a Python reference alive, as required for post-processors
        _QmlStyle.instance = _QmlStyle()
        return _QmlStyle.instance


class _Base(QgsProcessingAlgorithm):
    GROUP = 'Classification'

    def createInstance(self):
        return type(self)()

    def group(self):
        return self.GROUP

    def groupId(self):
        return self.GROUP.lower().replace(' ', '_')

    def icon(self):
        return QIcon(ICON)

    @staticmethod
    def _stack(alg, parameters, context):
        layers = alg.parameterAsLayerList(parameters, 'INPUT', context)
        if not layers:
            raise QgsProcessingException('No input rasters.')
        vrt = QgsProcessingUtils.generateTempFilename('tarski_stack.vrt', context)
        path = engine.build_stack([l.source() for l in layers], vrt)
        return path, engine.stack_info(path)

    @staticmethod
    def _style(context, *outputs):
        for out in outputs:
            if out and context.willLoadLayerOnCompletion(out):
                context.layerToLoadOnCompletionDetails(out).setPostProcessor(
                    _QmlStyle.create())

    @staticmethod
    def _cb(feedback):
        return (lambda v: feedback.setProgress(v)), feedback.isCanceled


class SupervisedAlg(_Base):
    METHODS = list(engine.SUPERVISED)

    def name(self):
        return 'supervised_classification'

    def displayName(self):
        return 'Supervised classification'

    def shortHelpString(self):
        return ('Classify a band stack from training ROIs (polygons/points '
                'with an integer class field). Methods: minimum distance, '
                'Mahalanobis, maximum likelihood, spectral angle, '
                'parallelepiped, random forest, SVM, multi-layer perceptron.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            'INPUT', 'Input bands', QgsProcessing.TypeRaster))
        self.addParameter(QgsProcessingParameterFeatureSource(
            'ROI', 'Training ROIs', [QgsProcessing.TypeVectorPolygon,
                                     QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            'FIELD', 'Class ID field', parentLayerParameterName='ROI',
            type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterField(
            'NAME_FIELD', 'Class name field (optional)',
            parentLayerParameterName='ROI', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            'METHOD', 'Method', list(engine.SUPERVISED.values()),
            defaultValue=self.METHODS.index('rf')))
        self.addParameter(QgsProcessingParameterNumber(
            'THRESHOLD', 'Reject threshold (statistical methods, 0 = off)',
            QgsProcessingParameterNumber.Double, 0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'TREES', 'Random forest trees', QgsProcessingParameterNumber.Integer,
            100, minValue=10))
        self.addParameter(QgsProcessingParameterRasterDestination(
            'OUTPUT', 'Classification'))
        self.addParameter(QgsProcessingParameterRasterDestination(
            'CONFIDENCE', 'Confidence', optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        path, info = self._stack(self, parameters, context)
        source = self.parameterAsSource(parameters, 'ROI', context)
        field = self.parameterAsString(parameters, 'FIELD', context)
        name_field = self.parameterAsString(parameters, 'NAME_FIELD', context) or None
        method = self.METHODS[self.parameterAsEnum(parameters, 'METHOD', context)]
        thr = self.parameterAsDouble(parameters, 'THRESHOLD', context) or None
        rois, names, colors = collect_rois(source, field, raster_crs(info['wkt']),
                                           name_field, None, context)
        if not rois:
            raise QgsProcessingException('No ROI features with a valid class ID.')
        progress, cancel = self._cb(feedback)
        feedback.pushInfo('Extracting training pixels…')
        X, y = engine.extract_samples(path, rois, cancel=cancel)
        feedback.pushInfo('%d pixels, %d classes' % (len(y), len(set(y.tolist()))))
        params = {'threshold': thr,
                  'trees': self.parameterAsInt(parameters, 'TREES', context)}
        model = engine.Model(method, **params).fit(X, y)
        model.names = {int(c): names.get(int(c), 'Class %d' % c) for c in model.classes}
        out = self.parameterAsOutputLayer(parameters, 'OUTPUT', context)
        conf = self.parameterAsOutputLayer(parameters, 'CONFIDENCE', context) or None
        try:
            engine.classify(path, model, out, conf_path=conf,
                            progress=progress, cancel=cancel)
        except engine.Cancelled:
            return {}
        self._style(context, out)
        res = {'OUTPUT': out}
        if conf:
            res['CONFIDENCE'] = conf
        return res


class UnsupervisedAlg(_Base):
    METHODS = list(engine.UNSUPERVISED)

    def name(self):
        return 'unsupervised_classification'

    def displayName(self):
        return 'Unsupervised classification (K-Means / ISODATA)'

    def shortHelpString(self):
        return 'Cluster a band stack into spectral classes.'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            'INPUT', 'Input bands', QgsProcessing.TypeRaster))
        self.addParameter(QgsProcessingParameterEnum(
            'METHOD', 'Method', list(engine.UNSUPERVISED.values()), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'CLASSES', 'Number of classes (ISODATA: initial)',
            QgsProcessingParameterNumber.Integer, 8, minValue=2, maxValue=255))
        self.addParameter(QgsProcessingParameterNumber(
            'MAX_CLASSES', 'ISODATA maximum classes',
            QgsProcessingParameterNumber.Integer, 16, minValue=2, maxValue=255))
        self.addParameter(QgsProcessingParameterRasterDestination('OUTPUT', 'Clusters'))

    def processAlgorithm(self, parameters, context, feedback):
        path, _ = self._stack(self, parameters, context)
        method = self.METHODS[self.parameterAsEnum(parameters, 'METHOD', context)]
        k = self.parameterAsInt(parameters, 'CLASSES', context)
        X = engine.sample_pixels(path)
        C = engine.kmeans(X, k) if method == 'kmeans' else engine.isodata(
            X, k, max(k, self.parameterAsInt(parameters, 'MAX_CLASSES', context)))
        model = engine.Model.from_centroids(C, method)
        model.names = {int(c): 'Cluster %d' % c for c in model.classes}
        out = self.parameterAsOutputLayer(parameters, 'OUTPUT', context)
        progress, cancel = self._cb(feedback)
        try:
            engine.classify(path, model, out, progress=progress, cancel=cancel)
        except engine.Cancelled:
            return {}
        self._style(context, out)
        return {'OUTPUT': out}


class MajorityAlg(_Base):
    GROUP = 'Post-processing'

    def name(self):
        return 'majority_filter'

    def displayName(self):
        return 'Majority filter'

    def shortHelpString(self):
        return 'Smooth a classification with a moving-window majority vote.'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer('INPUT', 'Classified raster'))
        self.addParameter(QgsProcessingParameterNumber(
            'SIZE', 'Window size (odd)', QgsProcessingParameterNumber.Integer,
            3, minValue=3, maxValue=51))
        self.addParameter(QgsProcessingParameterRasterDestination('OUTPUT', 'Filtered'))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsRasterLayer(parameters, 'INPUT', context).source()
        out = self.parameterAsOutputLayer(parameters, 'OUTPUT', context)
        progress, cancel = self._cb(feedback)
        try:
            engine.majority_filter(src, out, self.parameterAsInt(parameters, 'SIZE', context) | 1,
                                   progress=progress, cancel=cancel)
        except engine.Cancelled:
            return {}
        self._style(context, out)
        return {'OUTPUT': out}


class AccuracyAlg(_Base):
    GROUP = 'Post-processing'

    def name(self):
        return 'accuracy_assessment'

    def displayName(self):
        return 'Accuracy assessment'

    def shortHelpString(self):
        return ('Confusion matrix, overall accuracy, kappa, user\'s and '
                'producer\'s accuracy against reference polygons/points.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer('INPUT', 'Classified raster'))
        self.addParameter(QgsProcessingParameterFeatureSource(
            'REFERENCE', 'Reference ROIs', [QgsProcessing.TypeVectorPolygon,
                                            QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            'FIELD', 'Class ID field', parentLayerParameterName='REFERENCE',
            type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT', 'Report (CSV)', 'CSV (*.csv)'))
        self.addOutput(QgsProcessingOutputNumber('OVERALL', 'Overall accuracy'))
        self.addOutput(QgsProcessingOutputNumber('KAPPA', 'Kappa'))

    def processAlgorithm(self, parameters, context, feedback):
        lyr = self.parameterAsRasterLayer(parameters, 'INPUT', context)
        src = self.parameterAsSource(parameters, 'REFERENCE', context)
        field = self.parameterAsString(parameters, 'FIELD', context)
        rois, _, _ = collect_rois(src, field, lyr.crs(), context=context)
        acc = engine.accuracy(lyr.source(), rois, cancel=feedback.isCanceled)
        out = self.parameterAsFileOutput(parameters, 'OUTPUT', context)
        labels = acc['labels']
        with open(out, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['classified \\ reference'] + labels + ['users_accuracy'])
            for i, l in enumerate(labels):
                w.writerow([l] + acc['matrix'][i].tolist() + [acc['users'][i]])
            w.writerow(['producers_accuracy'] + acc['producers'])
            w.writerow([])
            w.writerow(['overall_accuracy', acc['overall']])
            w.writerow(['kappa', acc['kappa']])
        feedback.pushInfo('Overall accuracy %.4f, kappa %.4f' % (acc['overall'], acc['kappa']))
        return {'OUTPUT': out, 'OVERALL': acc['overall'], 'KAPPA': acc['kappa']}


class TarskiProvider(QgsProcessingProvider):

    def id(self):
        return 'tarskilulc'

    def name(self):
        return 'Tarski Labs - LULC'

    def icon(self):
        return QIcon(ICON)

    def loadAlgorithms(self):
        for alg in (SupervisedAlg, UnsupervisedAlg, MajorityAlg, AccuracyAlg):
            self.addAlgorithm(alg())
