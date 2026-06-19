# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolButton, QMessageBox
from qgis.core import QgsApplication, QgsMessageLog, Qgis
import processing
from pathlib import Path

from .dock.profile_dockwidget import MagProfileDock
from .gui.sbp_viewer_dockwidget import SBPViewerDockWidget
from .processing.provider import MarineGeoToolsProvider


class MarineGeoToolsPlugin:
    """Hybrid plugin: Processing provider + MAG & SBP Dockable Viewers."""

    def __init__(self, iface):
        self.iface = iface
        
        # MAG Profile state
        self._mag_dock = None
        self._mag_action = None
        
        # SBP Viewer state
        self._sbp_dock = None
        self._sbp_viewer_action = None
        
        self._toolbar = None
        self._sbp_tool_button = None
        self._sbp_root_menu = None
        self._sbp_main_menu = None
        self._actions = []
        
        self._provider = None

    def tr(self, message):
        return QCoreApplication.translate('MarineGeoToolsPlugin', message)

    def initGui(self):
        # 1. Register processing provider
        self._provider = MarineGeoToolsProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)

        # 2. Main Toolbar
        self._toolbar = self.iface.addToolBar('Marine GeoTools')
        self._toolbar.setObjectName('MarineGeoToolsToolbar')

        # --- MAG Profile Viewer Button ---
        mag_icon_path = str(Path(__file__).resolve().parent / 'icons' / 'mag_profile.svg')
        mag_icon = QIcon(mag_icon_path)
        self._mag_action = QAction(mag_icon, self.tr('Profile Viewer'), self.iface.mainWindow())
        self._mag_action.setCheckable(True)
        self._mag_action.triggered.connect(self._toggle_mag_dock)
        self._toolbar.addAction(self._mag_action)

        self._mag_dock = MagProfileDock(self.iface)
        self._mag_dock.visibilityChanged.connect(lambda vis: self._sync_mag_action_state(vis))
        self.iface.addDockWidget(self._mag_dock.defaultDockArea(), self._mag_dock)
        self._mag_dock.hide()

        # --- SBP Tools Dropdown Button ---
        self._sbp_root_menu = self._build_sbp_root_menu()
        
        # Add SBP Menu to QGIS MenuBar
        self._sbp_main_menu = QMenu("SBP Tools", self.iface.mainWindow())
        self._sbp_main_menu.addActions(self._sbp_root_menu.actions())
        self.iface.mainWindow().menuBar().addMenu(self._sbp_main_menu)
        
        # Add SBP Dropdown Button to Toolbar
        sbp_icon_path = str(Path(__file__).resolve().parent / 'icons' / 'sbp_profile.svg')
        sbp_icon = QIcon(sbp_icon_path)
        
        self._sbp_tool_button = QToolButton()
        self._sbp_tool_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._sbp_tool_button.setPopupMode(QToolButton.MenuButtonPopup)
        self._sbp_tool_button.setIcon(sbp_icon)
        self._sbp_tool_button.setToolTip("SBP Tools")
        self._sbp_tool_button.setMenu(self._sbp_root_menu)
        self._sbp_tool_button.clicked.connect(self._open_sbp_viewer)
        
        self._toolbar.addWidget(self._sbp_tool_button)

    def unload(self):
        # Remove MAG dock
        if self._mag_dock is not None:
            try:
                self.iface.removeDockWidget(self._mag_dock)
            except Exception as e:
                QgsMessageLog.logMessage(f"Failed to remove MAG dock: {str(e)}", "Marine GeoTools", Qgis.Warning)
            self._mag_dock.deleteLater()
            self._mag_dock = None

        # Remove SBP dock
        if self._sbp_dock is not None:
            try:
                self.iface.removeDockWidget(self._sbp_dock)
            except Exception as e:
                QgsMessageLog.logMessage(f"Failed to remove SBP dock: {str(e)}", "Marine GeoTools", Qgis.Warning)
            self._sbp_dock.deleteLater()
            self._sbp_dock = None

        # Remove SBP menu
        try:
            if self._sbp_main_menu is not None:
                self.iface.mainWindow().menuBar().removeAction(self._sbp_main_menu.menuAction())
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to remove SBP main menu: {str(e)}", "Marine GeoTools", Qgis.Warning)
        self._sbp_main_menu = None

        # Remove toolbar
        if self._toolbar is not None:
            try:
                self.iface.mainWindow().removeToolBar(self._toolbar)
            except Exception as e:
                QgsMessageLog.logMessage(f"Failed to remove toolbar: {str(e)}", "Marine GeoTools", Qgis.Warning)
            self._toolbar = None

        # Unregister provider
        if self._provider is not None:
            try:
                QgsApplication.processingRegistry().removeProvider(self._provider)
            except Exception as e:
                QgsMessageLog.logMessage(f"Failed to remove processing provider: {str(e)}", "Marine GeoTools", Qgis.Warning)
            self._provider = None
            
        self._actions = []

    def _build_sbp_root_menu(self):
        root = QMenu()

        # Viewer (DockWidget)
        self._sbp_viewer_action = QAction("SBP Viewer (Dock)", self.iface.mainWindow())
        self._sbp_viewer_action.setCheckable(True)
        self._sbp_viewer_action.triggered.connect(self._open_sbp_viewer)
        root.addAction(self._sbp_viewer_action)

        root.addSeparator()

        m_file = root.addMenu("File Import")
        m_settings = root.addMenu("Settings")
        m_vshift = root.addMenu("Vertical Shift")
        m_depth = root.addMenu("Depth and Thickness")

        # File Import
        self._add_sbp_alg_action(m_file, "XTF import", "marine_geotools:xtf_sbp_bulk_import_sgy_compat")
        self._add_sbp_alg_action(m_file, "SBP Import (SGY)", "marine_geotools:sgy_bulk_import_obspy_add")
        self._add_sbp_alg_action(m_file, "Borehole Import (CSV)", "marine_geotools:sbp_import_borehole_csv")

        # Settings
        self._add_sbp_alg_action(m_settings, "Sound velocity setting", "marine_geotools:sbp_update_velocities_from_gpkg")
        self._add_sbp_alg_action(m_settings, "Copy setting to other", "marine_geotools:sbp_copy_settings_to_many")

        # Vertical Shift
        self._add_sbp_alg_action(m_vshift, "Bathymetry", "marine_geotools:sbp_bathy_shift")
        self._add_sbp_alg_action(m_vshift, "Tide", "marine_geotools:sbp_tide_shift_from_table")
        self._add_sbp_alg_action(m_vshift, "Constant", "marine_geotools:sbp_constant_vertical_offset_meters")

        # Depth & Thickness
        self._add_sbp_alg_action(m_depth, "Compute Reflector Depth", "marine_geotools:sbp_compute_reflector_depth_multi")
        self._add_sbp_alg_action(m_depth, "Calculate Thickness", "marine_geotools:sbp_thickness_from_depth_fields_multi")

        return root

    def _add_sbp_alg_action(self, menu, title, alg_id):
        action = QAction(title, self.iface.mainWindow())
        action.triggered.connect(lambda _=False, a=alg_id: self._open_alg_dialog(a))
        menu.addAction(action)
        self._actions.append(action)

    def _open_alg_dialog(self, alg_id):
        try:
            processing.execAlgorithmDialog(alg_id, {})
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "Marine GeoTools", f"Failed to open tool: {alg_id}\n{str(e)}")
            QgsMessageLog.logMessage(f"Failed to open tool {alg_id}: {str(e)}", "Marine GeoTools", Qgis.Critical)

    def _open_sbp_viewer(self):
        if self._sbp_dock is None:
            self._sbp_dock = SBPViewerDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._sbp_dock)
        self._sbp_dock.show()
        self._sbp_dock.raise_()
        if self._sbp_viewer_action is not None:
            self._sbp_viewer_action.setChecked(True)

    def _toggle_mag_dock(self, checked):
        if checked:
            if self._mag_dock is not None:
                self._mag_dock.show()
                self._mag_dock.raise_()
        else:
            if self._mag_dock is not None:
                self._mag_dock.hide()

    def _sync_mag_action_state(self, visible):
        if self._mag_action is not None:
            self._mag_action.blockSignals(True)
            self._mag_action.setChecked(bool(visible))
            self._mag_action.blockSignals(False)

