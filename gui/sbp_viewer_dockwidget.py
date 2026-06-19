# -*- coding: utf-8 -*-
from qgis.core import QgsMessageLog, Qgis
from qgis.PyQt import QtWidgets
from qgis.gui import QgsDockWidget

from ..core.sbp_viewer_core import SBPViewerRaw


class SBPViewerDockWidget(QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setObjectName("SBPViewerDockWidget")
        self.setWindowTitle("SBP Viewer")

        self.container = QtWidgets.QWidget(self)
        self.layout = QtWidgets.QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.viewer = None
        self.setWidget(self.container)

        self._create_viewer()

    def _create_viewer(self):
        self.viewer = SBPViewerRaw(parent=self.container)
        self.layout.addWidget(self.viewer)

    def _destroy_viewer(self):
        if self.viewer is not None:
            self.viewer.setParent(None)
            self.viewer.deleteLater()
            self.viewer = None

    def closeEvent(self, event):
        self._destroy_viewer()
        super().closeEvent(event)

    def showEvent(self, event):
        if self.viewer is None:
            self._create_viewer()
        super().showEvent(event)

