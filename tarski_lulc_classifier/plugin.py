import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

MENU = '&Tarski Labs'


class TarskiLULCPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self.provider = None

    def initProcessing(self):
        from .provider import TarskiProvider
        self.provider = TarskiProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        icon = QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))
        self.action = QAction(icon, 'LULC Classifier', self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip('Tarski Labs - LULC Classifier')
        self.action.toggled.connect(self.toggle)
        self.iface.addRasterToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)

    def toggle(self, on):
        if self.dock is None:
            from .dock import TarskiDock
            self.dock = TarskiDock(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)
        self.dock.setVisible(on)
        if on:
            self.dock.raise_()

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.dock:
            self.dock.cancel_task()
            self.dock.disconnect_project()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action:
            self.iface.removePluginRasterMenu(MENU, self.action)
            self.iface.removeRasterToolBarIcon(self.action)
            self.action = None
