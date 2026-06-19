from qgis.core import QgsMessageLog, Qgis
# -*- coding: utf-8 -*-

from qgis.PyQt import QtWidgets, QtCore

from .profile_widget import MagProfileWidget


class MagProfileDock(QtWidgets.QDockWidget):
    """Dock wrapper for the profile viewer."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.setObjectName('MagProfileDock')
        self.setWindowTitle('Profile View')
        self._iface = iface
        self._widget = MagProfileWidget(iface, parent=self)
        self.setWidget(self._widget)

        self.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea |
            QtCore.Qt.RightDockWidgetArea |
            QtCore.Qt.BottomDockWidgetArea |
            QtCore.Qt.TopDockWidgetArea
        )

    def defaultDockArea(self):
        return QtCore.Qt.BottomDockWidgetArea

    def closeEvent(self, event):
        event.ignore()
        self.hide()
