# -*- coding: utf-8 -*-
# - Trace vs TWT(ms) + Gain + Reflector Overlay (multi-reflector)
# - Polyline picking: LEFT joints, live preview, DOUBLE click pauses
# - Eraser (Rect): two-click rectangle to null picks inside (by TWT range)
# - Box Tracker (Rect): two-click rectangle -> run autotrack in the box
# - ESC cancels active pick (polyline/erase/box)
# - Master "Show Reflectors" checkbox hides/shows all overlays
#
# Paste into QGIS Python Console and run.

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtGui import QColor
from PyQt5.QtCore import QVariant
import pyqtgraph as pg
import numpy as np
import os, json

from qgis.core import (QgsMessageLog, Qgis, 
    QgsProject,
    QgsVectorLayer,
    QgsPointXY,
    QgsField,
    QgsFeatureRequest, 
    QgsSpatialIndex, 
    QgsRectangle,
    QgsGeometry,
)
from qgis.gui import QgsVertexMarker
from qgis.utils import iface


# Picking Dialog (NON-MODAL) - LIVE SETTINGS + AUTO TRACKER
class SBPPickingDialog(QtWidgets.QDialog):
    settingsChanged      = QtCore.pyqtSignal(dict)
    reflectorChanged     = QtCore.pyqtSignal(str)
    autoTrackRequested   = QtCore.pyqtSignal(dict)
    closed               = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SBP Picking")
        self.setModal(False)
        self.resize(440, 640)

        # Reflector registry: name -> {color, show}
        self.reflectors = {"Seabed": {"color": "Red", "show": True}}

        self.settings = {
            "enabled": False,
            "mode": "Manual (Polyline)",
            "snap_to_sample": True,
            "overwrite_trace": True,
            "write_immediately": True,
            "active_reflector": "Seabed",
            "reflectors": dict(self.reflectors),
        }

        self._build_ui()
        self._wire_signals()
        self._sync_ui_from_active()
        self._emit_settings()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Reflector
        grpH = QtWidgets.QGroupBox("Reflector")
        gl = QtWidgets.QGridLayout(grpH)

        gl.addWidget(QtWidgets.QLabel("Reflector:"), 0, 0)

        row0 = QtWidgets.QHBoxLayout()
        self.cmbReflector = QtWidgets.QComboBox()
        self.cmbReflector.addItems(["Seabed"])
        row0.addWidget(self.cmbReflector, 1)

        self.btnAddReflector = QtWidgets.QToolButton()
        self.btnAddReflector.setText("+")
        self.btnAddReflector.setToolTip("Add reflector")
        row0.addWidget(self.btnAddReflector)

        self.btnDelReflector = QtWidgets.QToolButton()
        self.btnDelReflector.setText("-")
        self.btnDelReflector.setToolTip("Remove reflector (Seabed cannot be removed)")
        row0.addWidget(self.btnDelReflector)

        gl.addLayout(row0, 0, 1, 1, 3)

        gl.addWidget(QtWidgets.QLabel("Color:"), 1, 0)
        self.cmbColor = QtWidgets.QComboBox()
        self.cmbColor.addItems(["Red","Green","Blue","Yellow","Cyan","Magenta","White","Black","Orange"])
        gl.addWidget(self.cmbColor, 1, 1, 1, 3)

        self.chkShow = QtWidgets.QCheckBox("Show this Reflector overlay")
        self.chkShow.setChecked(True)
        gl.addWidget(self.chkShow, 2, 1, 1, 3)

        self.lblTarget = QtWidgets.QLabel("Target: (not connected yet)")
        self.lblTarget.setStyleSheet("color: gray; font-size: 10px;")
        gl.addWidget(self.lblTarget, 3, 0, 1, 4)

        layout.addWidget(grpH)

        # Edit tools
        grpE = QtWidgets.QGroupBox("Edit tools")
        ge = QtWidgets.QGridLayout(grpE)

        self.chkEnable = QtWidgets.QCheckBox("Enable picking")
        ge.addWidget(self.chkEnable, 0, 0, 1, 2)

        ge.addWidget(QtWidgets.QLabel("Mode:"), 1, 0)
        self.cmbMode = QtWidgets.QComboBox()
        self.cmbMode.addItems(["Manual (Polyline)", "Eraser (Rect)", "Box Tracker (Rect)"])
        ge.addWidget(self.cmbMode, 1, 1)

        self.chkSnap = QtWidgets.QCheckBox("Snap to sample grid")
        self.chkSnap.setChecked(True)
        ge.addWidget(self.chkSnap, 2, 0, 1, 2)

        self.chkOverwrite = QtWidgets.QCheckBox("Overwrite existing pick at trace")
        self.chkOverwrite.setChecked(True)
        ge.addWidget(self.chkOverwrite, 3, 0, 1, 2)

        self.chkWrite = QtWidgets.QCheckBox("Write to layer immediately")
        self.chkWrite.setChecked(True)
        ge.addWidget(self.chkWrite, 4, 0, 1, 2)

        layout.addWidget(grpE)

        # Auto tracker
        grpA = QtWidgets.QGroupBox("Auto tracker (threshold detection)")
        ga = QtWidgets.QGridLayout(grpA)

        ga.addWidget(QtWidgets.QLabel("Picking strategy:"), 0, 0)
        self.cmbStrategy = QtWidgets.QComboBox()
        self.cmbStrategy.addItems(["Absolute peak", "Positive peak", "Negative peak"])
        self.cmbStrategy.setCurrentText("Absolute peak")
        ga.addWidget(self.cmbStrategy, 0, 1, 1, 2)

        self.chkZeroCross = QtWidgets.QCheckBox("Pick at zero crossing")
        self.chkZeroCross.setChecked(False)
        ga.addWidget(self.chkZeroCross, 1, 0, 1, 3)

        ga.addWidget(QtWidgets.QLabel("Amplitude threshold (% max):"), 2, 0)
        self.spinThr = QtWidgets.QDoubleSpinBox()
        self.spinThr.setRange(0.0, 100.0)
        self.spinThr.setDecimals(1)
        self.spinThr.setValue(10.0)
        ga.addWidget(self.spinThr, 2, 1, 1, 2)

        ga.addWidget(QtWidgets.QLabel("Smoothing window (traces):"), 3, 0)
        self.spinSmooth = QtWidgets.QSpinBox()
        self.spinSmooth.setRange(0, 9999)
        self.spinSmooth.setValue(0)
        ga.addWidget(self.spinSmooth, 3, 1, 1, 2)

        ga.addWidget(QtWidgets.QLabel("Start from (ms):"), 5, 0)
        self.spinStart = QtWidgets.QDoubleSpinBox()
        self.spinStart.setRange(0.0, 999999.0)
        self.spinStart.setDecimals(2)
        self.spinStart.setValue(0.0)
        ga.addWidget(self.spinStart, 5, 1, 1, 2)

        ga.addWidget(QtWidgets.QLabel("Search window length (ms):"), 6, 0)
        self.spinLen = QtWidgets.QDoubleSpinBox()
        self.spinLen.setRange(0.0, 999999.0)
        self.spinLen.setDecimals(2)
        self.spinLen.setValue(10.0)
        ga.addWidget(self.spinLen, 6, 1, 1, 2)

        ga.addWidget(QtWidgets.QLabel("Trace interval:"), 7, 0)
        self.cmbInterval = QtWidgets.QComboBox()
        self.cmbInterval.addItems(["Full", "Visible", "Custom"])
        self.cmbInterval.setCurrentText("Full")
        ga.addWidget(self.cmbInterval, 7, 1, 1, 2)

        hb = QtWidgets.QHBoxLayout()
        self.spinFrom = QtWidgets.QSpinBox(); self.spinFrom.setRange(0, 10_000_000); self.spinFrom.setValue(0)
        self.spinTo   = QtWidgets.QSpinBox(); self.spinTo.setRange(0, 10_000_000); self.spinTo.setValue(0)
        hb.addWidget(QtWidgets.QLabel("From")); hb.addWidget(self.spinFrom)
        hb.addWidget(QtWidgets.QLabel("To"));   hb.addWidget(self.spinTo)
        ga.addLayout(hb, 8, 0, 1, 3)

        self.btnRunAuto = QtWidgets.QPushButton("Run Auto Track")
        ga.addWidget(self.btnRunAuto, 9, 0, 1, 3)

        layout.addWidget(grpA)

        # Bottom buttons
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        self.btnClose = QtWidgets.QPushButton("Close")
        btns.addWidget(self.btnClose)
        layout.addLayout(btns)

    def _wire_signals(self):
        self.btnClose.clicked.connect(self.close)

        self.cmbReflector.currentTextChanged.connect(self._on_reflector_changed)
        self.btnAddReflector.clicked.connect(self._add_reflector)
        self.btnDelReflector.clicked.connect(self._delete_reflector)

        self.cmbColor.currentTextChanged.connect(self._on_color_changed)
        self.chkShow.stateChanged.connect(self._on_show_changed)

        self.chkEnable.stateChanged.connect(self._emit_settings)
        self.cmbMode.currentTextChanged.connect(self._emit_settings)
        self.chkSnap.stateChanged.connect(self._emit_settings)
        self.chkOverwrite.stateChanged.connect(self._emit_settings)
        self.chkWrite.stateChanged.connect(self._emit_settings)

        self.btnRunAuto.clicked.connect(self._emit_autotrack)

    def _ensure_reflector_defaults(self, name: str):
        name = (name or "Seabed").strip() or "Seabed"
        if name not in self.reflectors or not isinstance(self.reflectors.get(name), dict):
            self.reflectors[name] = {}
        self.reflectors[name].setdefault("color", "Red")
        self.reflectors[name].setdefault("show", True)
        return name

    def _sync_ui_from_active(self):
        name = self._ensure_reflector_defaults(self.cmbReflector.currentText())

        c = self.reflectors[name].get("color", "Red")
        idx = self.cmbColor.findText(str(c))
        self.cmbColor.blockSignals(True)
        if idx >= 0:
            self.cmbColor.setCurrentIndex(idx)
        else:
            self.cmbColor.setCurrentText("Red")
        self.cmbColor.blockSignals(False)

        self.chkShow.blockSignals(True)
        self.chkShow.setChecked(bool(self.reflectors[name].get("show", True)))
        self.chkShow.blockSignals(False)

    def _on_reflector_changed(self, name: str):
        name = self._ensure_reflector_defaults(name)
        self._sync_ui_from_active()
        self.reflectorChanged.emit(name)
        self._emit_settings()

    def _on_color_changed(self, *_):
        name = self._ensure_reflector_defaults(self.cmbReflector.currentText())
        self.reflectors[name]["color"] = self.cmbColor.currentText()
        self._emit_settings()

    def _on_show_changed(self, *_):
        name = self._ensure_reflector_defaults(self.cmbReflector.currentText())
        self.reflectors[name]["show"] = bool(self.chkShow.isChecked())
        self._emit_settings()

    def _emit_settings(self):
        active = self._ensure_reflector_defaults(self.cmbReflector.currentText())

        self.settings = {
            "enabled": bool(self.chkEnable.isChecked()),
            "mode": str(self.cmbMode.currentText()),
            "snap_to_sample": bool(self.chkSnap.isChecked()),
            "overwrite_trace": bool(self.chkOverwrite.isChecked()),
            "write_immediately": bool(self.chkWrite.isChecked()),
            "active_reflector": str(active),
            "reflectors": dict(self.reflectors),
        }
        self.settingsChanged.emit(dict(self.settings))

    def _emit_autotrack(self):
        params = {
            "strategy": self.cmbStrategy.currentText(),
            "zero_cross": self.chkZeroCross.isChecked(),
            "threshold_pct": float(self.spinThr.value()),
            "smooth_traces": int(self.spinSmooth.value()),
            "start_ms": float(self.spinStart.value()),
            "window_ms": float(self.spinLen.value()),
            "interval_mode": self.cmbInterval.currentText(),
            "custom_from": int(self.spinFrom.value()),
            "custom_to": int(self.spinTo.value()),
        }
        self.autoTrackRequested.emit(params)

    def setReflectors(self, reflectors_dict: dict, active: str = "Seabed"):
        if not isinstance(reflectors_dict, dict) or "Seabed" not in reflectors_dict:
            reflectors_dict = {"Seabed": {"color": "Red", "show": True}}

        self.reflectors = dict(reflectors_dict)
        for k in list(self.reflectors.keys()):
            self._ensure_reflector_defaults(k)

        if active not in self.reflectors:
            active = "Seabed"

        self.cmbReflector.blockSignals(True)
        self.cmbReflector.clear()
        self.cmbReflector.addItems(list(self.reflectors.keys()))
        self.cmbReflector.setCurrentText(active)
        self.cmbReflector.blockSignals(False)

        self._sync_ui_from_active()
        self._emit_settings()

    def _add_reflector(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Add Reflector", "Reflector name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        if name in self.reflectors:
            QtWidgets.QMessageBox.information(self, "Exists", f"'{name}' already exists.")
            return

        self.reflectors[name] = {"color": self.cmbColor.currentText(), "show": True}
        self.cmbReflector.addItem(name)
        self.cmbReflector.setCurrentText(name)
        self._emit_settings()

    def _delete_reflector(self):
        name = (self.cmbReflector.currentText() or "Seabed").strip() or "Seabed"
        if name == "Seabed":
            QtWidgets.QMessageBox.information(self, "Not allowed", "Seabed cannot be removed.")
            return

        ret = QtWidgets.QMessageBox.question(
            self, "Remove Reflector",
            f"Remove '{name}'?\n(Field will be deleted in GPKG by viewer.)"
        )
        if ret != QtWidgets.QMessageBox.Yes:
            return

        self.reflectors.pop(name, None)

        self.cmbReflector.blockSignals(True)
        self.cmbReflector.clear()
        self.cmbReflector.addItems(list(self.reflectors.keys()))
        self.cmbReflector.setCurrentText("Seabed")
        self.cmbReflector.blockSignals(False)

        self._sync_ui_from_active()
        self._emit_settings()

    def setTargetText(self, text: str):
        self.lblTarget.setText(f"Target: {text}")

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


# Gain Settings Dialog
class SBPGainSettingsDialog(QtWidgets.QDialog):
    applied = QtCore.pyqtSignal()

    def __init__(self, json_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SBP Gain Settings")
        self.json_path = json_path
        self._original_data = None
        self._json_existed = os.path.exists(json_path)
        self._backup_path = json_path + ".bak_tmp"

        self._load_or_init_json()

        try:
            with open(self._backup_path, "w", encoding="utf-8") as f:
                json.dump(self._original_data, f, indent=2)
        except Exception as e:
            QgsMessageLog.logMessage(str(f"[SBPGainDialog] Failed to create backup: {e}"), "PluginLogger", Qgis.Info)

        self._build_ui()
        self._populate_from_processing()

    def _load_or_init_json(self):
        if self._json_existed:
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    self._original_data = json.load(f)
            except Exception as err:
                self._original_data = {}
        else:
            self._original_data = {}

        if "processing" not in self._original_data:
            self._original_data["processing"] = {}
        proc = self._original_data["processing"]

        proc.setdefault("bandpass", {"enabled": False, "low_hz": 200.0, "high_hz": 3000.0, "taper_pct": 5.0})
        proc.setdefault("agc", {"enabled": False, "window_pct": 80.0, "intensity_pct": 10.0})
        proc.setdefault("tvg", {"enabled": False, "scalar": 0.0})
        proc.setdefault("stacking", {"enabled": False, "shots": 1, "mode": "Avg"})
        proc.setdefault("blank_water", {"enabled": False})

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        bp_group = QtWidgets.QGroupBox("Band-pass (per trace)")
        bp_layout = QtWidgets.QGridLayout(bp_group)
        self.chkBP = QtWidgets.QCheckBox("Enable band-pass")
        bp_layout.addWidget(self.chkBP, 0, 0, 1, 2)

        bp_layout.addWidget(QtWidgets.QLabel("Low (Hz):"), 1, 0)
        self.spinBPLow = QtWidgets.QDoubleSpinBox(); self.spinBPLow.setRange(0.0, 1e6); self.spinBPLow.setDecimals(1)
        bp_layout.addWidget(self.spinBPLow, 1, 1)

        bp_layout.addWidget(QtWidgets.QLabel("High (Hz):"), 2, 0)
        self.spinBPHigh = QtWidgets.QDoubleSpinBox(); self.spinBPHigh.setRange(0.0, 1e6); self.spinBPHigh.setDecimals(1)
        bp_layout.addWidget(self.spinBPHigh, 2, 1)

        bp_layout.addWidget(QtWidgets.QLabel("Taper (% band):"), 3, 0)
        self.spinBPTaper = QtWidgets.QDoubleSpinBox(); self.spinBPTaper.setRange(0.0, 50.0); self.spinBPTaper.setDecimals(1)
        bp_layout.addWidget(self.spinBPTaper, 3, 1)
        layout.addWidget(bp_group)

        agc_group = QtWidgets.QGroupBox("AGC")
        agc_layout = QtWidgets.QGridLayout(agc_group)
        self.chkAGC = QtWidgets.QCheckBox("Enable AGC")
        agc_layout.addWidget(self.chkAGC, 0, 0, 1, 2)

        agc_layout.addWidget(QtWidgets.QLabel("Window (% samples):"), 1, 0)
        self.spinAGCWin = QtWidgets.QDoubleSpinBox(); self.spinAGCWin.setRange(1.0, 100.0); self.spinAGCWin.setDecimals(1)
        agc_layout.addWidget(self.spinAGCWin, 1, 1)

        agc_layout.addWidget(QtWidgets.QLabel("Intensity (%):"), 2, 0)
        self.spinAGCInt = QtWidgets.QDoubleSpinBox(); self.spinAGCInt.setRange(0.0, 100.0); self.spinAGCInt.setDecimals(1)
        agc_layout.addWidget(self.spinAGCInt, 2, 1)
        layout.addWidget(agc_group)

        tvg_group = QtWidgets.QGroupBox("TVG")
        tvg_layout = QtWidgets.QGridLayout(tvg_group)
        self.chkTVG = QtWidgets.QCheckBox("Enable TVG")
        tvg_layout.addWidget(self.chkTVG, 0, 0, 1, 2)

        tvg_layout.addWidget(QtWidgets.QLabel("Scalar (%):"), 1, 0)
        self.spinTVGScalar = QtWidgets.QDoubleSpinBox(); self.spinTVGScalar.setRange(-100.0, 100.0); self.spinTVGScalar.setDecimals(1)
        tvg_layout.addWidget(self.spinTVGScalar, 1, 1)
        layout.addWidget(tvg_group)

        stack_group = QtWidgets.QGroupBox("Stacking (horizontal)")
        stack_layout = QtWidgets.QGridLayout(stack_group)
        self.chkStack = QtWidgets.QCheckBox("Enable stacking")
        stack_layout.addWidget(self.chkStack, 0, 0, 1, 2)

        stack_layout.addWidget(QtWidgets.QLabel("Shots (window width):"), 1, 0)
        self.spinStackShots = QtWidgets.QSpinBox(); self.spinStackShots.setRange(1, 21)
        stack_layout.addWidget(self.spinStackShots, 1, 1)

        stack_layout.addWidget(QtWidgets.QLabel("Mode:"), 2, 0)
        self.comboStackMode = QtWidgets.QComboBox(); self.comboStackMode.addItems(["Avg", "Median"])
        stack_layout.addWidget(self.comboStackMode, 2, 1)
        layout.addWidget(stack_group)

        blank_group = QtWidgets.QGroupBox("Blank Water Column")
        blank_layout = QtWidgets.QHBoxLayout(blank_group)
        self.chkBlank = QtWidgets.QCheckBox("Enable blanking")
        blank_layout.addWidget(self.chkBlank)
        layout.addWidget(blank_group)

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()
        self.btnOK = QtWidgets.QPushButton("OK")
        self.btnApply = QtWidgets.QPushButton("Apply")
        self.btnCancel = QtWidgets.QPushButton("Cancel")
        btn_layout.addWidget(self.btnOK)
        btn_layout.addWidget(self.btnApply)
        btn_layout.addWidget(self.btnCancel)
        layout.addLayout(btn_layout)

        self.btnApply.clicked.connect(self.on_apply_clicked)
        self.btnOK.clicked.connect(self.on_ok_clicked)
        self.btnCancel.clicked.connect(self.on_cancel_clicked)

    def _populate_from_processing(self):
        proc = self._original_data["processing"]

        bp = proc.get("bandpass", {})
        self.chkBP.setChecked(bool(bp.get("enabled", False)))
        self.spinBPLow.setValue(float(bp.get("low_hz", 500.0)))
        self.spinBPHigh.setValue(float(bp.get("high_hz", 2500.0)))
        self.spinBPTaper.setValue(float(bp.get("taper_pct", 5.0)))

        agc = proc.get("agc", {})
        self.chkAGC.setChecked(bool(agc.get("enabled", False)))
        self.spinAGCWin.setValue(float(agc.get("window_pct", 0.0)))
        self.spinAGCInt.setValue(float(agc.get("intensity_pct", 0.0)))

        tvg = proc.get("tvg", {})
        self.chkTVG.setChecked(bool(tvg.get("enabled", False)))
        self.spinTVGScalar.setValue(float(tvg.get("scalar", 0.0)))

        stack = proc.get("stacking", {})
        self.chkStack.setChecked(bool(stack.get("enabled", False)))
        self.spinStackShots.setValue(int(stack.get("shots", 1)))
        mode = str(stack.get("mode", "Avg") or "Avg")
        idx = self.comboStackMode.findText(mode)
        if idx < 0:
            idx = 0
        self.comboStackMode.setCurrentIndex(idx)

        blank = proc.get("blank_water", {})
        self.chkBlank.setChecked(bool(blank.get("enabled", False)))

    def _update_processing_in_memory(self):
        proc = self._original_data.setdefault("processing", {})
        proc["bandpass"] = {
            "enabled": self.chkBP.isChecked(),
            "low_hz": float(self.spinBPLow.value()),
            "high_hz": float(self.spinBPHigh.value()),
            "taper_pct": float(self.spinBPTaper.value())
        }
        proc["agc"] = {
            "enabled": self.chkAGC.isChecked(),
            "window_pct": float(self.spinAGCWin.value()),
            "intensity_pct": float(self.spinAGCInt.value())
        }
        proc["tvg"] = {
            "enabled": self.chkTVG.isChecked(),
            "scalar": float(self.spinTVGScalar.value())
        }
        proc["stacking"] = {
            "enabled": self.chkStack.isChecked(),
            "shots": int(self.spinStackShots.value()),
            "mode": self.comboStackMode.currentText()
        }
        proc["blank_water"] = {"enabled": self.chkBlank.isChecked()}

    def _save_to_json(self):
        self._update_processing_in_memory()
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception as err:
                current = {}
        else:
            current = {}

        current.update({k: v for k, v in self._original_data.items() if k != "processing"})
        current["processing"] = self._original_data["processing"]

        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Save Error", f"Failed to write JSON:\n{e}")

    def on_apply_clicked(self):
        self._save_to_json()
        self.applied.emit()

    def on_ok_clicked(self):
        self._save_to_json()
        if os.path.exists(self._backup_path):
            try:
                os.remove(self._backup_path)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        self.applied.emit()
        self.accept()

    def on_cancel_clicked(self):
        if os.path.exists(self._backup_path):
            try:
                with open(self._backup_path, "r", encoding="utf-8") as f:
                    original = json.load(f)

                if self._json_existed:
                    with open(self.json_path, "w", encoding="utf-8") as f:
                        json.dump(original, f, indent=2)
                else:
                    if os.path.exists(self.json_path):
                        os.remove(self.json_path)

                os.remove(self._backup_path)
            except Exception as e:
                QgsMessageLog.logMessage(str(f"[SBPGainDialog] Error restoring backup: {e}"), "PluginLogger", Qgis.Info)

        self.applied.emit()
        self.reject()

    def closeEvent(self, event):
        if os.path.exists(self._backup_path):
            self.on_cancel_clicked()
            event.ignore()
        else:
            super().closeEvent(event)


# Markers Dialog (Point layers -> vertical markers)
class SBPMarkersDialog(QtWidgets.QDialog):
    applied = QtCore.pyqtSignal(dict)
    closed  = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SBP Markers")
        self.setModal(False)
        self.resize(420, 520)

        self._build_ui()
        self._populate_layers()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        grpL = QtWidgets.QGroupBox("Marker point layers (multiple)")
        gl = QtWidgets.QVBoxLayout(grpL)

        self.lstLayers = QtWidgets.QListWidget()
        self.lstLayers.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        gl.addWidget(self.lstLayers)

        row = QtWidgets.QHBoxLayout()
        self.btnRefresh = QtWidgets.QPushButton("Refresh")
        self.btnSelectAll = QtWidgets.QPushButton("Select All")
        self.btnClearAll  = QtWidgets.QPushButton("Clear")
        row.addWidget(self.btnRefresh)
        row.addStretch()
        row.addWidget(self.btnSelectAll)
        row.addWidget(self.btnClearAll)
        gl.addLayout(row)

        layout.addWidget(grpL)

        # Settings group
        grpS = QtWidgets.QGroupBox("Settings")
        gs = QtWidgets.QGridLayout(grpS)

        gs.addWidget(QtWidgets.QLabel("Tolerance (m):"), 0, 0)
        self.spinTol = QtWidgets.QDoubleSpinBox()
        self.spinTol.setRange(0.0, 1e9)
        self.spinTol.setDecimals(2)
        self.spinTol.setValue(5.0)
        gs.addWidget(self.spinTol, 0, 1)

        self.lblCluster = QtWidgets.QLabel("")
        self.lblCluster.setStyleSheet("color: gray; font-size: 10px;")
        gs.addWidget(self.lblCluster, 1, 0, 1, 2)

        gs.addWidget(QtWidgets.QLabel("Color:"), 2, 0)
        self.cmbColor = QtWidgets.QComboBox()
        self.cmbColor.addItems(["Blue","Red","Green","Yellow","Cyan","Magenta","White","Black","Orange"])
        self.cmbColor.setCurrentText("Blue")
        gs.addWidget(self.cmbColor, 2, 1)



        layout.addWidget(grpS)

        # Buttons
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        self.btnApply = QtWidgets.QPushButton("Apply")
        self.btnClose = QtWidgets.QPushButton("Close")
        btns.addWidget(self.btnApply)
        btns.addWidget(self.btnClose)
        layout.addLayout(btns)

        # Signals
        self.btnRefresh.clicked.connect(self._populate_layers)
        self.btnSelectAll.clicked.connect(self._select_all)
        self.btnClearAll.clicked.connect(self._clear_all)
        self.btnApply.clicked.connect(self._emit_applied)
        self.btnClose.clicked.connect(self.close)
        self.spinTol.valueChanged.connect(self._update_cluster_label)

        self._update_cluster_label()

    def _update_cluster_label(self):
        tol = float(self.spinTol.value())
        self.lblCluster.setText(f"Cluster distance = 3 × tolerance = {3.0 * tol:.2f} m")

    def _populate_layers(self, keep_checked_ids=None):
        """Rebuild the layer list. If keep_checked_ids is given, restore those
        items to Checked state (used when switching SBP lines to preserve
        the user's marker-layer selection)."""
        self.lstLayers.clear()

        layers = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            if lyr.geometryType() != 0:  # Point only
                continue
            layers.append(lyr)

        layers.sort(key=lambda L: L.name().lower())

        checked_ids = set(keep_checked_ids) if keep_checked_ids else set()

        for lyr in layers:
            it = QtWidgets.QListWidgetItem(lyr.name())
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            state = QtCore.Qt.Checked if lyr.id() in checked_ids else QtCore.Qt.Unchecked
            it.setCheckState(state)
            it.setData(QtCore.Qt.UserRole, lyr.id())
            self.lstLayers.addItem(it)

    def _select_all(self):
        for i in range(self.lstLayers.count()):
            self.lstLayers.item(i).setCheckState(QtCore.Qt.Checked)

    def _clear_all(self):
        for i in range(self.lstLayers.count()):
            self.lstLayers.item(i).setCheckState(QtCore.Qt.Unchecked)

    def _selected_layers(self):
        out = []
        for i in range(self.lstLayers.count()):
            it = self.lstLayers.item(i)
            if it.checkState() == QtCore.Qt.Checked:
                lid = it.data(QtCore.Qt.UserRole)
                lyr = QgsProject.instance().mapLayer(lid)
                if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == 0:
                    out.append(lyr)
        return out

    def _emit_applied(self):
        params = {
            "layer_ids": [lyr.id() for lyr in self._selected_layers()],
            "tolerance_m": float(self.spinTol.value()),
            "cluster_m": float(self.spinTol.value()) * 3.0,
            "color": str(self.cmbColor.currentText()),
        }
        self.applied.emit(params)


    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)



# Color Scale Dialog
class SBPColorDialog(QtWidgets.QDialog):
    """Non-modal dialog for colour-scale controls (Min, Max, Scale to Best/Data, colormap, Invert)."""
    closed = QtCore.pyqtSignal()

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.setWindowTitle("Color Scale")
        self.setModal(False)
        self.setMinimumWidth(310)
        self._build_ui()
        self._sync_from_viewer()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        grp = QtWidgets.QGroupBox("Display Range")
        gl = QtWidgets.QGridLayout(grp)

        gl.addWidget(QtWidgets.QLabel("Min:"), 0, 0)
        self.txtMin = QtWidgets.QDoubleSpinBox()
        self.txtMin.setDecimals(3)
        self.txtMin.setRange(-1e12, 1e12)
        self.txtMin.valueChanged.connect(self._push_min)
        gl.addWidget(self.txtMin, 0, 1)

        gl.addWidget(QtWidgets.QLabel("Max:"), 1, 0)
        self.txtMax = QtWidgets.QDoubleSpinBox()
        self.txtMax.setDecimals(3)
        self.txtMax.setRange(-1e12, 1e12)
        self.txtMax.valueChanged.connect(self._push_max)
        gl.addWidget(self.txtMax, 1, 1)

        # Scale buttons
        btnRow = QtWidgets.QHBoxLayout()
        self.btnBest = QtWidgets.QPushButton("Scale to Best")
        self.btnBest.clicked.connect(self._on_scale_best)
        self.btnData = QtWidgets.QPushButton("Scale to Data")
        self.btnData.clicked.connect(self._on_scale_data)
        btnRow.addWidget(self.btnBest)
        btnRow.addWidget(self.btnData)
        gl.addLayout(btnRow, 2, 0, 1, 2)

        layout.addWidget(grp)

        # Colormap + Invert row
        grp2 = QtWidgets.QGroupBox("Colormap")
        hl = QtWidgets.QHBoxLayout(grp2)

        self.comboColor = QtWidgets.QComboBox()
        self.comboColor.addItems(["Grey", "Grey bipolar"])
        self.comboColor.currentTextChanged.connect(self._push_color)
        hl.addWidget(self.comboColor, 1)

        self.chkInvert = QtWidgets.QCheckBox("Inv")
        self.chkInvert.stateChanged.connect(self._push_invert)
        hl.addWidget(self.chkInvert)

        layout.addWidget(grp2)

        # Close button
        btnClose = QtWidgets.QPushButton("Close")
        btnClose.clicked.connect(self.close)
        layout.addWidget(btnClose)

    def _sync_from_viewer(self):
        """Pull current values from the viewer's hidden widgets into our controls."""
        v = self.viewer
        # Block signals to avoid feedback loops while syncing
        self.txtMin.blockSignals(True)
        self.txtMax.blockSignals(True)
        self.comboColor.blockSignals(True)
        self.chkInvert.blockSignals(True)

        self.txtMin.setValue(v.txtMin.value())
        self.txtMax.setValue(v.txtMax.value())
        idx = self.comboColor.findText(v.comboColor.currentText())
        if idx >= 0:
            self.comboColor.setCurrentIndex(idx)
        self.chkInvert.setChecked(v.chkInvert.isChecked())

        self.txtMin.blockSignals(False)
        self.txtMax.blockSignals(False)
        self.comboColor.blockSignals(False)
        self.chkInvert.blockSignals(False)

    # -- push helpers: write dialog value -> viewer hidden widget (which triggers updatePlot + save) --
    def _push_min(self, val):
        self.viewer.txtMin.setValue(val)

    def _push_max(self, val):
        self.viewer.txtMax.setValue(val)

    def _push_color(self, text):
        idx = self.viewer.comboColor.findText(text)
        if idx >= 0:
            self.viewer.comboColor.setCurrentIndex(idx)

    def _push_invert(self, state):
        self.viewer.chkInvert.setChecked(bool(state))

    def _on_scale_best(self):
        self.viewer.scaleToBest(reset_zoom=False)
        self._sync_from_viewer()

    def _on_scale_data(self):
        self.viewer.scaleToData(reset_zoom=False)
        self._sync_from_viewer()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


# SBP Viewer
class SBPViewerRaw(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("SBP Viewer – Trace vs TWT (ms) + Gain + Picking + Overlay (v3.3.2)")
        self.resize(1400, 800)

        # state
        self._loading_viewer_state = False

        # meta
        self.distance = None
        self.dt_ms = None
        self.t0_ms = 0.0
        self.vw = 1500.0
        self.vs = 1500.0
        self.view_domain = "TWT"                # "TWT" or "DEPTH"
        self.dt_shift_ms_depth_arr = None       # per-trace for depth domain (B)
        self.twt_axis_ms = None

        # processing settings (loaded from json)
        self.proc_bp_enabled = False
        self.proc_bp_low_hz = 200.0
        self.proc_bp_high_hz = 3000.0
        self.proc_bp_taper_pct = 5.0
        self.proc_agc_enabled = False
        self.proc_agc_window_pct = 80.0
        self.proc_agc_intensity_pct = 10.0
        self.proc_tvg_enabled = False
        self.proc_tvg_scalar = 0.0
        self.proc_stack_enabled = False
        self.proc_stack_shots = 1
        self.proc_stack_mode = "Avg"
        self.proc_blank_water_enabled = False

        #data
        self.amp = None
        self.amp_proc = None

        self.amp_view = None          # shifted-for-display image (same shape)
        self.dt_shift_ms_arr = None   # per-trace dt_shift in ms (len = n_traces)
        self.dt_shift_enabled = True  # default ON

        self.current_npy_path = None

        # markers
        self.markersDlg = None
        self.marker_settings = {"layers": [], "tolerance_m": 5.0, "cluster_m": 15.0, "color": "Blue"}
        self._marker_items = []      # pg items for vertical ticks
        self._marker_labels = []     # pg TextItem labels
        self._markers_cache = []     # list of dicts: computed markersDlg
        self._cross_reflector_items = []   # scatter markers at reflector intersections
        self._borehole_items = []    # pg items for borehole lithology bars



        self.layer = iface.activeLayer()
        self.freeze_layer = False

        # nav + marker
        self.nav_layer = None
        self.nav_x = None
        self.nav_y = None
        self.map_canvas = iface.mapCanvas()
        self.map_marker = None
        self.last_trace_index = None

        self.reflectors = {"Seabed": {"color": "Red", "show": True}}
        self.active_reflector = "Seabed"
        self.reflector_overlays = {}
        self.show_reflectors_master = True

        # picking dialog
        self.pickDlg = None
        self.colorDlg = None
        self.pick_settings = {"enabled": False, "mode": "Manual (Polyline)"}
        self.pick_reflector = "Seabed"

        self.poly_preview = None
        self.poly_active = False
        self.poly_anchor = None

        # erase rect state
        self.erase_active = False
        self.erase_anchor = None
        self.erase_rect_item = None

        self.box_active = False
        self.box_anchor = None
        self.box_rect_item = None  # IMPORTANT: must exist before any clear

        # plot image item
        self.img = None

        # caches
        self._traceid_to_fid = {}
        self._overlay_cache_dirty = True
        self._overlay_cache = {}
        self._loading_layer = False


        # IMPORTANT: actually destroy widget when closed
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self._is_closing = False

        # UI ------------------------------------------------
        mainLayout = QtWidgets.QHBoxLayout(self)

        # LEFT
        leftWidget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(leftWidget)

        fileLayout = QtWidgets.QHBoxLayout()

        self.btnGain = QtWidgets.QPushButton("Gain")
        self.btnGain.setMaximumWidth(60)
        self.btnGain.setStyleSheet("font-size:10px; padding:2px;")
        self.btnGain.clicked.connect(self.openGainDialog)
        fileLayout.addWidget(self.btnGain)

        self.btnPick = QtWidgets.QPushButton("Pick")
        self.btnPick.setMaximumWidth(60)
        self.btnPick.setStyleSheet("font-size:10px; padding:2px;")
        self.btnPick.clicked.connect(self.openPickingDialog)
        fileLayout.addWidget(self.btnPick)

        self.btnMarkers = QtWidgets.QPushButton("Markers")
        self.btnMarkers.setMaximumWidth(70)
        self.btnMarkers.setStyleSheet("font-size:10px; padding:2px;")
        self.btnMarkers.clicked.connect(self.openMarkersDialog)
        fileLayout.addWidget(self.btnMarkers)

        self.btnColorToolbar = QtWidgets.QPushButton("Color")
        self.btnColorToolbar.setMaximumWidth(60)
        self.btnColorToolbar.setStyleSheet("font-size:10px; padding:2px;")
        self.btnColorToolbar.clicked.connect(self.openColorDialog)
        fileLayout.addWidget(self.btnColorToolbar)


        self.fileEdit = QtWidgets.QLineEdit()
        self.fileEdit.setReadOnly(True)
        fileLayout.addWidget(self.fileEdit, 1)

        self.show_reflectors_checkbox = QtWidgets.QCheckBox("Show Reflectors")
        self.show_reflectors_checkbox.setChecked(True)
        self.show_reflectors_checkbox.setToolTip("Master switch: show/hide all reflector overlays")
        self.show_reflectors_checkbox.stateChanged.connect(self.onShowReflectorsMasterChanged)
        fileLayout.addWidget(self.show_reflectors_checkbox)

        layout.addLayout(fileLayout)

        self.pgview = pg.PlotWidget()
        self.pgview.setLabel("bottom", "Trace")
        self.pgview.setLabel("left", "TWT (ms)")
        self.pgview.invertY(True)
        self.pgview.showGrid(x=True, y=True)
        layout.addWidget(self.pgview)

        controlLayout = QtWidgets.QHBoxLayout()
        self.statusLabel = QtWidgets.QLabel("Trace - (Distance — m)  TWT — ms")
        self.statusLabel.setStyleSheet("font-size:10px; color:gray; padding-right:8px;")
        controlLayout.addWidget(self.statusLabel)
        controlLayout.addStretch()


        self.cmbDomain = QtWidgets.QComboBox()
        self.cmbDomain.addItems(["TWT (ms)", "Depth (m)"])
        self.cmbDomain.setMaximumWidth(110)
        self.cmbDomain.setStyleSheet("font-size:10px;")
        self.cmbDomain.currentTextChanged.connect(self.onDomainChanged)
        controlLayout.addWidget(self.cmbDomain)
        # --- backing-store colour widgets (hidden; used by updatePlot, save/load JSON) ---
        lblMin = QtWidgets.QLabel("Min:", self)
        lblMin.hide()
        self.txtMin = QtWidgets.QDoubleSpinBox(self)
        self.txtMin.setDecimals(3)
        self.txtMin.setRange(-1e12, 1e12)
        self.txtMin.setMaximumWidth(90)
        self.txtMin.hide()
        self.txtMin.valueChanged.connect(self.updatePlot)
        self.txtMin.valueChanged.connect(lambda *_: self._save_viewer_settings_to_json())

        lblMax = QtWidgets.QLabel("Max:", self)
        lblMax.hide()
        self.txtMax = QtWidgets.QDoubleSpinBox(self)
        self.txtMax.setDecimals(3)
        self.txtMax.setRange(-1e12, 1e12)
        self.txtMax.setMaximumWidth(90)
        self.txtMax.hide()
        self.txtMax.valueChanged.connect(self.updatePlot)
        self.txtMax.valueChanged.connect(lambda *_: self._save_viewer_settings_to_json())

        self.btnScaleBest = QtWidgets.QPushButton("Scale to Best", self)
        self.btnScaleBest.hide()
        self.btnScaleBest.clicked.connect(self.scaleToBest)

        self.btnScaleData = QtWidgets.QPushButton("Scale to Data", self)
        self.btnScaleData.hide()
        self.btnScaleData.clicked.connect(self.scaleToData)

        self.comboColor = QtWidgets.QComboBox(self)
        self.comboColor.addItems(["Grey", "Grey bipolar"])
        self.comboColor.hide()
        self.comboColor.currentTextChanged.connect(self.updatePlot)
        self.comboColor.currentTextChanged.connect(lambda *_: self._save_viewer_settings_to_json())

        self.chkInvert = QtWidgets.QCheckBox("Inv", self)
        self.chkInvert.setChecked(False)
        self.chkInvert.hide()
        self.chkInvert.stateChanged.connect(self.updatePlot)
        self.chkInvert.stateChanged.connect(lambda *_: self._save_viewer_settings_to_json())

        # dt_shift toggle
        self.chkApplyShift = QtWidgets.QCheckBox("dt_shift")
        self.chkApplyShift.setChecked(True)
        self.chkApplyShift.setToolTip("Apply dt_shift_ms (per trace) to shift the SBP image vertically")
        self.chkApplyShift.setStyleSheet("font-size:10px; padding-left:4px;")
        self.chkApplyShift.stateChanged.connect(self.onShiftToggle)
        controlLayout.addWidget(self.chkApplyShift)

        layout.addLayout(controlLayout)
        mainLayout.addWidget(leftWidget, 8)

        # RIGHT wiggle
        self.wiggleView = pg.PlotWidget()
        self.wiggleView.setLabel("left", "TWT (ms)")
        self.wiggleView.setLabel("bottom", "Amplitude")
        self.wiggleView.invertY(True)
        self.wiggleView.showGrid(x=True, y=True)
        self.wiggleCurve = self.wiggleView.plot([], [], pen='w')

        self.hoverLine = pg.InfiniteLine(angle=0, pen=pg.mkPen('r', width=1))
        self.wiggleView.addItem(self.hoverLine)
        self.hoverLine.hide()

        mainLayout.addWidget(self.wiggleView, 1)

        # signals
        self.pgview.scene().sigMouseMoved.connect(self.onMouseMoved)
        self.pgview.scene().sigMouseClicked.connect(self.onMouseClicked)
        self.pgview.viewport().installEventFilter(self)
        iface.currentLayerChanged.connect(self.onCurrentLayerChanged)

        # focus for ESC
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.pgview.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.pgview.setFocus()

        if self.layer and isinstance(self.layer, QgsVectorLayer):
            self._load_from_layer(self.layer)


    def openMarkersDialog(self):
        if self.markersDlg is None:
            self.markersDlg = SBPMarkersDialog(parent=self)
            self.markersDlg.applied.connect(self.onMarkersApplied)
            self.markersDlg.closed.connect(self.onMarkersClosed)

        self.markersDlg.show()
        self.markersDlg.raise_()
        self.markersDlg.activateWindow()

    def _twt_view_ms_to_display_y(self, twt_view_ms: float) -> float:
        """
        Convert TWT_VIEW(ms) -> display Y using the SAME mapping as the image axis.
        - TWT view: y = ms
        - Depth view: y = (ms/1000) * Vs/2   (same as updatePlot depth axis)
        """
        if self.view_domain == "DEPTH":
            return (float(twt_view_ms) / 1000.0) * float(self.vs) / 2.0
        return float(twt_view_ms)

    def _display_y_to_twt_view_ms(self, y_disp: float) -> float:
        """
        Inverse of _twt_view_ms_to_display_y()
        - Depth view: ms = (depth * 2 / Vs) * 1000
        - TWT view: ms = y
        """
        if self.view_domain == "DEPTH":
            return (float(y_disp) * 2.0 / float(self.vs)) * 1000.0
        return float(y_disp)


    def _clear_cross_reflectors(self):
        for it in getattr(self, "_cross_reflector_items", []):
            try:
                self.pgview.removeItem(it)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        self._cross_reflector_items = []

    def onMarkersClosed(self):
        pass

    def onMarkersApplied(self, params: dict):
        self.marker_settings = dict(params or {})
        self._recompute_markers()
        self._draw_markers()


    def _apply_markers_safe(self):
        if getattr(self, "_loading_layer", False):
            return
        self._recompute_markers()
        self._draw_markers()


    def _recompute_markers(self):
        self._markers_cache = []

        if getattr(self, "_loading_layer", False):
            return


        if self.nav_layer is None or self.amp is None:
            return
        if self.nav_x is None or self.nav_y is None:
            return

        layer_ids = self.marker_settings.get("layer_ids", []) or []
        layers = []
        # current nav layer ID — a ticked layer matching this is skipped at compute
        # time so a line never shows markers against its own nav traces
        current_nav_id = self.nav_layer.id() if self.nav_layer is not None else None
        for lid in layer_ids:
            if lid == current_nav_id:
                continue  # skip self — nav layer is the current SBP line
            lyr = QgsProject.instance().mapLayer(lid)
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == 0:
                layers.append(lyr)

        tol_m = float(self.marker_settings.get("tolerance_m", 5.0) or 0.0)
        cluster_m = float(self.marker_settings.get("cluster_m", 3.0 * tol_m) or 0.0)
        if tol_m <= 0 or not layers:
            return

        n_traces = int(self.amp.shape[0])

        # distance-along array (fallback to trace index if missing)
        dist_along_arr = None
        if self.distance is not None and len(self.distance) == n_traces:
            dist_along_arr = self.distance

        # Build spatial index for EACH marker layer (fast queries)
        indexed_layers = []  # (layer_name, layer, spatial_index)
        for lyr in layers:
            if not isinstance(lyr, QgsVectorLayer) or lyr.geometryType() != 0:
                continue
            try:
                sidx = QgsSpatialIndex(lyr.getFeatures())
            except Exception as err:
                continue
            indexed_layers.append((lyr.name(), lyr, sidx))

        if not indexed_layers:
            return

        candidates = []  # (layer_name, trace_index, dist_along, best_xy_dist, ref_vals, src_shift_ms)

        for tr_idx in range(n_traces):
            x = float(self.nav_x[tr_idx])
            y = float(self.nav_y[tr_idx])
            if not (np.isfinite(x) and np.isfinite(y)):
                continue

            rect = QgsRectangle(x - tol_m, y - tol_m, x + tol_m, y + tol_m)

            if dist_along_arr is not None:
                dv = dist_along_arr[tr_idx]
                dist_along = float(dv) if dv == dv else float(tr_idx)
            else:
                dist_along = float(tr_idx)

            nav_pt = QgsPointXY(x, y)
            nav_geom = QgsGeometry.fromPointXY(nav_pt)

            for lname, lyr, sidx in indexed_layers:
                try:
                    hit_ids = sidx.intersects(rect)
                except Exception as err:
                    continue
                if not hit_ids:
                    continue

                best_dxy = None
                best_feat = None

                for fid in hit_ids:
                    ff = lyr.getFeature(fid)
                    g = ff.geometry()
                    if g is None or g.isEmpty():
                        continue
                    try:
                        dxy = float(g.distance(nav_geom))
                    except Exception as err:
                        try:
                            pt = g.asPoint()
                            dx = float(pt.x()) - x
                            dy = float(pt.y()) - y
                            dxy = float((dx * dx + dy * dy) ** 0.5)
                        except Exception as err:
                            continue

                    if dxy <= tol_m and (best_dxy is None or dxy < best_dxy):
                        best_dxy = dxy
                        best_feat = ff

                if best_feat is None or best_dxy is None:
                    continue

                # --- read attributes from THIS layer's best feature ---
                ref_vals = {}

                # dt_shift_ms on the OTHER layer feature
                src_shift_ms = 0.0
                idx_src_shift = lyr.fields().indexOf("dt_shift_ms")
                if idx_src_shift != -1:
                    vv = self._qvariant_to_float_or_none(best_feat.attribute(idx_src_shift))
                    if vv is not None:
                        src_shift_ms = float(vv)

                # reflector twt fields on the OTHER layer feature
                for rname in (self.reflectors or {}).keys():
                    fn = self._reflector_field_name(rname)  # e.g. seabed_twt
                    fidx = lyr.fields().indexOf(fn)
                    if fidx != -1:
                        vv = self._qvariant_to_float_or_none(best_feat.attribute(fidx))
                        if vv is not None:
                            ref_vals[rname] = float(vv)

                # --- borehole attributes (only if this is a borehole layer) ---
                borehole_data = None
                if lyr.fields().indexOf("StickToSeabed") != -1 or lyr.fields().indexOf("l1_end") != -1:
                    bh_width = 0.0
                    idx_w = lyr.fields().indexOf("Width")
                    if idx_w != -1:
                        bh_width = self._qvariant_to_float_or_none(best_feat.attribute(idx_w)) or 0.0

                    bh_stick = False
                    idx_st = lyr.fields().indexOf("StickToSeabed")
                    if idx_st != -1:
                        raw_st = best_feat.attribute(idx_st)
                        bh_stick = bool(int(raw_st)) if raw_st not in (None, "") else False

                    bh_depth = 0.0
                    idx_dp = lyr.fields().indexOf("Depth")
                    if idx_dp != -1:
                        bh_depth = self._qvariant_to_float_or_none(best_feat.attribute(idx_dp)) or 0.0

                    bh_name = ""
                    idx_nm = lyr.fields().indexOf("Name")
                    if idx_nm != -1:
                        raw_nm = best_feat.attribute(idx_nm)
                        bh_name = str(raw_nm).strip() if raw_nm is not None else ""

                    # Collect lithology quadruplets l1_, l2_, ...
                    lithologies = []
                    i = 1
                    while True:
                        idx_lname  = lyr.fields().indexOf(f"l{i}_name")
                        idx_lcolor = lyr.fields().indexOf(f"l{i}_color")
                        idx_ldesc  = lyr.fields().indexOf(f"l{i}_desc")
                        idx_lend   = lyr.fields().indexOf(f"l{i}_end")
                        if idx_lend == -1:
                            break  # no more quadruplets
                        lend_val = self._qvariant_to_float_or_none(best_feat.attribute(idx_lend)) if idx_lend != -1 else None
                        if lend_val is None:
                            i += 1
                            continue  # skip empty quadruplets
                        lname_val  = str(best_feat.attribute(idx_lname)).strip()  if idx_lname  != -1 else ""
                        lcolor_val = str(best_feat.attribute(idx_lcolor)).strip() if idx_lcolor != -1 else "Gray"
                        ldesc_raw  = best_feat.attribute(idx_ldesc) if idx_ldesc != -1 else None
                        ldesc_val  = str(ldesc_raw).strip() if ldesc_raw not in (None, "") else ""
                        lithologies.append({"name": lname_val, "color": lcolor_val, "desc": ldesc_val, "end": float(lend_val)})
                        i += 1

                    if lithologies:
                        borehole_data = {
                            "bh_name":    bh_name,
                            "width_m":    bh_width,
                            "stick":      bh_stick,
                            "depth_m":    bh_depth,
                            "lithologies": lithologies,
                        }

                candidates.append((lname, int(tr_idx), float(dist_along), float(best_dxy), ref_vals, float(src_shift_ms), borehole_data))

        if not candidates:
            return

        # Cluster per layer along distance, keep nearest XY in cluster
        out = []
        by_layer = {}
        for rec in candidates:
            by_layer.setdefault(rec[0], []).append(rec)

        for lname, recs in by_layer.items():
            recs.sort(key=lambda r: r[2])  # dist_along

            cur = []
            last_d = None

            def flush_cluster(cluster):
                if not cluster:
                    return
                best = min(cluster, key=lambda r: r[3])  # min xy distance
                out.append({
                    "layer": best[0],
                    "trace": best[1],
                    "dist": best[2],
                    "xy_dist": best[3],
                    "refs": best[4],
                    "src_shift_ms": best[5],
                    "borehole": best[6] if len(best) > 6 else None,
                })

            for r in recs:
                d = r[2]
                if last_d is None:
                    cur = [r]
                    last_d = d
                    continue

                if abs(d - last_d) <= max(cluster_m, 0.0):
                    cur.append(r)
                else:
                    flush_cluster(cur)
                    cur = [r]
                last_d = d

            flush_cluster(cur)

        self._markers_cache = out

    def _clear_markers(self):
        for it in getattr(self, "_marker_items", []):
            try: self.pgview.removeItem(it)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        for tx in getattr(self, "_marker_labels", []):
            try: self.pgview.removeItem(tx)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        self._marker_items = []
        self._marker_labels = []
        self._clear_cross_reflectors()
        self._clear_borehole_bars()



    def _draw_markers(self):
        self._clear_markers()
        if getattr(self, "_loading_layer", False):
            return
        
        if not self._markers_cache or self.amp is None or self.img is None:
            return

        n_traces = int(self.amp.shape[0])

        # top of SBP image for label placement
        try:
            r = self.img.boundingRect()
            y_top = float(min(r.top(), r.bottom()))
        except Exception as err:
            y_top = float(getattr(self, "t0_view_ms", self.t0_ms or 0.0))

        # marker color from dialog
        c_name = str(self.marker_settings.get("color", "Blue") or "Blue")
        rgb_marker = self._rgb_from_name(c_name)
        pen = pg.mkPen(rgb_marker, width=2)

        # white label background (slightly transparent)
        fill_brush = pg.mkBrush(255, 255, 255, 220)
        border_pen = pg.mkPen(0, 0, 0, 180)

        # --- 1) draw vertical marker lines + labels ---
        for m in self._markers_cache:
            x = float(m.get("trace", 0))
            if x < 0 or x > (n_traces - 1):
                continue

            vline = pg.InfiniteLine(pos=x, angle=90, pen=pen, movable=False)
            vline.setZValue(5000)
            self.pgview.addItem(vline)
            self._marker_items.append(vline)

            txt = pg.TextItem(text=str(m.get("layer", "Markers")), color=rgb_marker, anchor=(0.5, 1.0))
            txt.setZValue(5001)
            txt.setPos(x, y_top)

            try:
                txt.setText(txt.text, color=rgb_marker, fill=fill_brush)
                txt.setBorder(border_pen)
            except Exception as err:
                try:
                    txt.fill = fill_brush
                    txt.border = border_pen
                    txt.update()
                except Exception as e:
                    QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

            self.pgview.addItem(txt)
            self._marker_labels.append(txt)

        # --- 2) draw cross-line reflector dashes ONCE ---
        self._clear_cross_reflectors()

        dash_dx = 50  # half-length of dash in TRACE units
        by_lr = {}    # (layer_name, reflector_name) -> list of (trace, y_disp)

        for m in self._markers_cache:
            tr = int(m.get("trace", -1))
            if tr < 0:
                continue

            ref_vals = m.get("refs", {}) or {}
        # --- 2) draw cross-line reflector dashes ONCE ---
        self._clear_cross_reflectors()

        dash_dx = 50  # half-length of dash in TRACE units
        by_lr = {}    # (layer_name, reflector_name) -> list of (trace, y_disp)

        for m in self._markers_cache:
            tr = int(m.get("trace", -1))
            if tr < 0:
                continue

            ref_vals = m.get("refs", {}) or {}

            # other-line dt_shift (stored in m) is the "water-domain" shift (same as TWT domain)
            src_shift_ms = float(m.get("src_shift_ms", 0.0))

            # ✅ If we are in DEPTH view, convert other-line shift using the SAME B formula
            # s_new = (t_sb + s_old)*(Vw/Vs) - t_sb
            if self.view_domain == "DEPTH":
                t_sb = None
                v_sb = ref_vals.get("Seabed", None)
                if v_sb is not None and np.isfinite(v_sb):
                    t_sb = float(v_sb)

                if t_sb is not None:
                    src_shift_ms = (t_sb + src_shift_ms) * (float(self.vw) / float(self.vs)) - t_sb
                # else: no seabed pick available -> fallback: keep src_shift_ms unchanged

            for rname, twt_raw_other in ref_vals.items():
                if twt_raw_other is None or not np.isfinite(twt_raw_other):
                    continue

                # ✅ other-line RAW + (converted) other-line shift -> other-line VIEW(ms)
                twt_view_other = float(twt_raw_other) + float(src_shift_ms)

                # ✅ convert to DISPLAY Y using SAME mapping as image axis
                y_disp = self._twt_view_ms_to_display_y(twt_view_other)

                key = (str(m.get("layer", "Layer")), str(rname))
                by_lr.setdefault(key, []).append((float(tr), float(y_disp)))

        for (lname, rname), pts in by_lr.items():
            if not pts:
                continue

            xs = []
            ys = []
            for x, y in pts:
                xs.extend([x - dash_dx, x + dash_dx, np.nan])
                ys.extend([y, y, np.nan])

            rgb = self._rgb_from_name((self.reflectors.get(rname, {}) or {}).get("color", "Red"))
            it = pg.PlotDataItem(xs, ys, pen=pg.mkPen(rgb, width=1))
            it.setZValue(6000)
            self.pgview.addItem(it)
            self._cross_reflector_items.append(it)

        # --- 3) draw borehole lithology stacked bars ---
        self._draw_borehole_bars()



    def _clear_borehole_bars(self):
        """Remove all borehole bar graphics items from the plot."""
        for it in getattr(self, "_borehole_items", []):
            try:
                self.pgview.removeItem(it)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        self._borehole_items = []

    def _depth_m_to_display_y(self, depth_m: float, top_display_y: float) -> float:
        """
        Convert a meters offset below the borehole top to a display Y value.
        In TWT domain: depth_m -> ms using Vs, then add to top_display_y.
        In DEPTH domain: depth_m is already in meters, just add to top_display_y.
        """
        if self.view_domain == "DEPTH":
            return top_display_y + float(depth_m)
        else:
            # depth_m -> TWT ms: t = 2 * d / Vs * 1000
            delta_ms = (float(depth_m) * 2.0 / float(self.vs)) * 1000.0
            return top_display_y + delta_ms

    def _m_to_trace_width(self, width_m: float, trace_idx: int) -> float:
        """
        Convert a physical width in meters to a width in trace units.
        Uses the distance array spacing near the given trace.
        Falls back to 5 traces if distance data not available.
        """
        try:
            dist = self.distance
            n = int(self.amp.shape[0]) if self.amp is not None else 0
            if dist is None or len(dist) < 2 or n < 2:
                return 5.0
            # Use median spacing across the line for robustness
            diffs = np.diff(dist.astype(float))
            diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
            if len(diffs) == 0:
                return 5.0
            m_per_trace = float(np.median(diffs))
            if m_per_trace <= 0:
                return 5.0
            return float(width_m) / m_per_trace
        except Exception as err:
            return 5.0

    def _draw_borehole_bars(self):
        """Draw colored stacked lithology bars for every borehole in _markers_cache."""
        self._clear_borehole_bars()

        if not self._markers_cache or self.amp is None or self.img is None:
            return

        n_traces = int(self.amp.shape[0])

        for m in self._markers_cache:
            bh = m.get("borehole")
            if not bh:
                continue

            tr = int(m.get("trace", -1))
            if tr < 0 or tr >= n_traces:
                continue

            lithologies = bh.get("lithologies", [])
            if not lithologies:
                continue

            width_m   = float(bh.get("width_m",  2.0))
            stick     = bool(bh.get("stick",     False))
            depth_m   = float(bh.get("depth_m",  0.0))
            bh_name   = str(bh.get("bh_name",   ""))

            # --- Determine the Y position of the borehole top ---
            if stick:
                # Anchor to the Seabed reflector of THIS (current) line
                # First check if the marker carries a seabed ref from the OTHER line,
                # otherwise fall back to reading it from the current nav_layer at this trace.
                seabed_twt_raw = m.get("refs", {}).get("Seabed", None)

                if seabed_twt_raw is None and self.nav_layer is not None:
                    # Read seabed from current nav layer at this trace
                    seabed_field = self._reflector_field_name("Seabed")
                    fidx = self.nav_layer.fields().indexOf(seabed_field)
                    tr_field = self._find_traceid_field(self.nav_layer)
                    if fidx != -1 and tr_field is not None:
                        for feat in self.nav_layer.getFeatures(
                            QgsFeatureRequest().setFilterExpression(f'"{tr_field}" = {tr}')
                        ):
                            seabed_twt_raw = self._qvariant_to_float_or_none(feat.attribute(fidx))
                            break

                if seabed_twt_raw is not None and np.isfinite(seabed_twt_raw):
                    # Convert raw seabed TWT to view coordinates
                    twt_view = seabed_twt_raw + self._shift_ms_at_trace(tr)
                    top_display_y = self._twt_view_ms_to_display_y(twt_view)
                else:
                    # Fallback: no seabed available, place at top of image
                    try:
                        r = self.img.boundingRect()
                        top_display_y = float(min(r.top(), r.bottom()))
                    except Exception as err:
                        top_display_y = 0.0
            else:
                # Absolute depth below sea surface, convert to display Y
                if self.view_domain == "DEPTH":
                    top_display_y = float(depth_m)
                else:
                    top_display_y = self._twt_view_ms_to_display_y(
                        (float(depth_m) * 2.0 / float(self.vs)) * 1000.0
                    )

            # --- Convert width from meters to trace units ---
            half_w = self._m_to_trace_width(width_m, tr) / 2.0

            # --- Draw each lithology as a colored rectangle ---
            prev_end_display_y = top_display_y
            for litho in lithologies:
                lend_m  = float(litho.get("end",   1.0))
                lcolor  = str(litho.get("color", "Gray")).strip()
                lname   = str(litho.get("name",  "")).strip()
                ldesc   = str(litho.get("desc",  "")).strip()

                end_display_y = self._depth_m_to_display_y(lend_m, top_display_y)

                # Build rectangle: x = trace ± half_w, y = prev_end to end
                rect_x = float(tr) - half_w
                rect_y = float(min(prev_end_display_y, end_display_y))
                rect_w = half_w * 2.0
                rect_h = abs(end_display_y - prev_end_display_y)

                rgb = self._rgb_from_name(lcolor)
                brush = pg.mkBrush(rgb[0], rgb[1], rgb[2], 180)
                border_pen = pg.mkPen(rgb[0] // 2, rgb[1] // 2, rgb[2] // 2, 220, width=1)

                rect_item = QtWidgets.QGraphicsRectItem(rect_x, rect_y, rect_w, rect_h)
                rect_item.setBrush(brush)
                rect_item.setPen(border_pen)
                rect_item.setZValue(4900)
                rect_item.setAcceptHoverEvents(True)

                # Build tooltip: show borehole name, lithology name, description and depth range
                if self.view_domain == "DEPTH":
                    prev_depth_label = f"{(prev_end_display_y - top_display_y):.2f} m"
                    end_depth_label  = f"{(end_display_y - top_display_y):.2f} m"
                else:
                    prev_m = ((float(prev_end_display_y) - float(top_display_y)) * float(self.vs) / 2.0) / 1000.0
                    end_m  = ((float(end_display_y)      - float(top_display_y)) * float(self.vs) / 2.0) / 1000.0
                    prev_depth_label = f"{prev_m:.2f} m  ({float(prev_end_display_y) - float(top_display_y):.1f} ms)"
                    end_depth_label  = f"{end_m:.2f} m  ({float(end_display_y) - float(top_display_y):.1f} ms)"

                tooltip_lines = [
                    f"<b>{bh_name}</b>" if bh_name else "",
                    f"<b>{lname}</b>" if lname else "",
                    f"Depth: {prev_depth_label} → {end_depth_label}",
                ]
                if ldesc:
                    tooltip_lines.append(f"<i>{ldesc}</i>")
                rect_item.setToolTip("<br>".join(line for line in tooltip_lines if line))

                self.pgview.addItem(rect_item)
                self._borehole_items.append(rect_item)

                # Add lithology name label (small text centered in bar)
                if lname:
                    label_y = (float(prev_end_display_y) + float(end_display_y)) / 2.0
                    txt = pg.TextItem(text=lname, color=(0, 0, 0), anchor=(0.5, 0.5))
                    txt.setZValue(4901)
                    txt.setFont(QtGui.QFont("Arial", 7))
                    txt.setPos(float(tr), label_y)
                    self.pgview.addItem(txt)
                    self._borehole_items.append(txt)

                prev_end_display_y = end_display_y

            # Add borehole name label at the top
            if bh_name:
                try:
                    r = self.img.boundingRect()
                    img_top = float(min(r.top(), r.bottom()))
                except Exception as err:
                    img_top = top_display_y - 20
                name_txt = pg.TextItem(text=f"[{bh_name}]", color=(50, 50, 50), anchor=(0.5, 1.0))
                name_txt.setFont(QtGui.QFont("Arial", 8, QtGui.QFont.Bold))
                name_txt.setZValue(4902)
                name_txt.setPos(float(tr), img_top)
                self.pgview.addItem(name_txt)
                self._borehole_items.append(name_txt)


    def onShiftToggle(self, state):
        self.dt_shift_enabled = (state == QtCore.Qt.Checked)
        self._build_shifted_image()
        self.updatePlot()
        self._refresh_all_reflector_overlays()

        # ✅ redraw markers/cross-reflectors in the new display domain
        self._draw_markers()

    def onDomainChanged(self, text):
        self.view_domain = "DEPTH" if "Depth" in str(text) else "TWT"
        self._build_shifted_image()
        self.updatePlot(reset_zoom=True)
        self._refresh_all_reflector_overlays()

        # ✅ redraw markers/cross-reflectors in the new display domain
        self._draw_markers()


    def _shift_ms_at_trace(self, trace_index: int) -> float:
        if not self.dt_shift_enabled:
            return 0.0
        arr = self.dt_shift_ms_arr if self.view_domain == "TWT" else self.dt_shift_ms_depth_arr
        if arr is None or not (0 <= trace_index < len(arr)):
            return 0.0
        v = arr[trace_index]
        return float(v) if np.isfinite(v) else 0.0

    def _view_twt_to_raw(self, trace_index: int, twt_view_ms: float) -> float:
        return float(twt_view_ms) - self._shift_ms_at_trace(trace_index)

    def _raw_twt_to_view(self, trace_index: int, twt_raw_ms: float) -> float:
        return float(twt_raw_ms) + self._shift_ms_at_trace(trace_index)

    def _twt_view_ms_to_depth_m(self, twt_view_ms: float) -> float:
        return (float(twt_view_ms) / 1000.0) * float(self.vs) / 2.0

    def _depth_m_to_twt_view_ms(self, depth_m: float) -> float:
        return (float(depth_m) * 2.0 / float(self.vs)) * 1000.0

    def _raw_twt_to_view_y(self, trace_index: int, twt_raw_ms: float) -> float:
        twt_view_ms = self._raw_twt_to_view(trace_index, twt_raw_ms)
        return self._twt_view_ms_to_display_y(twt_view_ms)

    def _view_y_to_raw_twt(self, trace_index: int, y_view: float) -> float:
        if self.view_domain == "DEPTH":
            twt_view_ms = self._depth_m_to_twt_view_ms(float(y_view))
        else:
            twt_view_ms = float(y_view)
        return self._view_twt_to_raw(trace_index, twt_view_ms)

    def _depth_from_twt_raw(self, trace_index: int, twt_ms_raw: float) -> float:
        """
        Depth in meters from RAW TWT(ms).
        Piecewise:
          - above seabed: Vw
          - below seabed: Vw to seabed + Vs below
        Fallback: Vw only.
        """
        twt = float(twt_ms_raw)
        depth = (twt / 1000.0) * float(self.vw) / 2.0

        if self.nav_layer is None:
            return depth

        seabed_field = self._reflector_field_name("Seabed")
        fidx = self.nav_layer.fields().indexOf(seabed_field)
        if fidx == -1:
            return depth

        feat = self._get_feature_by_traceid(int(trace_index) + 1)
        if feat is None:
            return depth

        t_sb = self._qvariant_to_float_or_none(feat.attribute(fidx))
        if t_sb is None or not np.isfinite(t_sb):
            return depth

        t_sb = float(t_sb)
        if twt <= t_sb:
            return (twt / 1000.0) * float(self.vw) / 2.0

        depth_water = (t_sb / 1000.0) * float(self.vw) / 2.0
        depth_sed   = ((twt - t_sb) / 1000.0) * float(self.vs) / 2.0
        return depth_water + depth_sed

    def _ensure_reflector_overlay_items(self):
        if not isinstance(self.reflector_overlays, dict):
            self.reflector_overlays = {}

        for name in self.reflectors.keys():
            if name not in self.reflector_overlays:
                it = pg.PlotDataItem([], [], connect="finite")
                self.reflector_overlays[name] = it
                self.pgview.addItem(it)

        # remove overlays for deleted reflectors
        for name in list(self.reflector_overlays.keys()):
            if name not in self.reflectors:
                try:
                    self.pgview.removeItem(self.reflector_overlays[name])
                except Exception as e:
                    QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
                self.reflector_overlays.pop(name, None)


    def _update_minmax_spinbox_step(self):
        """
        Adapt Min/Max spinbox step to data amplitude range.
        """
        try:
            vmin = float(self.txtMin.value())
            vmax = float(self.txtMax.value())
        except Exception as err:
            return

        rng = abs(vmax - vmin)
        if not np.isfinite(rng) or rng <= 0:
            return

        # heuristic: ~200 steps across full range
        step = rng / 200.0

        # clamp to sane limits
        if rng < 1.0:
            step = max(step, 1e-5)
        elif rng < 100.0:
            step = max(step, 1e-3)
        elif rng < 10_000.0:
            step = max(step, 0.1)
        else:
            step = max(step, 1.0)

        self.txtMin.setSingleStep(step)
        self.txtMax.setSingleStep(step)


    def onShowReflectorsMasterChanged(self, state):
        self.show_reflectors_master = (state == QtCore.Qt.Checked)
        self._refresh_all_reflector_overlays()

    def cleanup(self):
        if getattr(self, "_is_closing", False):
            return
        self._is_closing = True

        try:
            iface.currentLayerChanged.disconnect(self.onCurrentLayerChanged)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        try:
            self.pgview.scene().sigMouseMoved.disconnect(self.onMouseMoved)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        try:
            self.pgview.scene().sigMouseClicked.disconnect(self.onMouseClicked)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        try:
            if self.pickDlg is not None:
                self.pickDlg.blockSignals(True)
                self.pickDlg.close()
                self.pickDlg.deleteLater()
                self.pickDlg = None
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        try:
            if self.map_marker is not None:
                self.map_marker.hide()
                self.map_marker = None
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        try:
            self.pgview.viewport().removeEventFilter(self)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        self.nav_layer = None
        self.layer = None

    def _reflector_field_name(self, reflector_name: str) -> str:
        safe = (reflector_name or "Seabed").strip().lower().replace(" ", "_")
        return f"{safe}_twt"

    def _ensure_field(self, layer: QgsVectorLayer, field_name: str) -> int:
        idx = layer.fields().indexOf(field_name)
        if idx != -1:
            return idx
        pr = layer.dataProvider()
        ok = pr.addAttributes([QgsField(field_name, QVariant.Double)])
        layer.updateFields()
        if not ok:
            raise RuntimeError(f"Failed to add field: {field_name}")
        return layer.fields().indexOf(field_name)

    def _qvariant_to_float_or_none(self, v):
        try:
            if v is None:
                return None
            if hasattr(v, "isNull") and v.isNull():
                return None
            vv = float(v)
            if not np.isfinite(vv):
                return None
            return vv
        except Exception as err:
            return None

    def _rgb_from_name(self, c: str):
        cmap = {
            "red":     (255,   0,   0),
            "green":   (  0, 255,   0),
            "blue":    (  0,   0, 255),
            "yellow":  (255, 255,   0),
            "cyan":    (  0, 255, 255),
            "magenta": (255,   0, 255),
            "white":   (255, 255, 255),
            "black":   (  0,   0,   0),
            "orange":  (255, 165,   0),
            "gray":    (128, 128, 128),
            "grey":    (128, 128, 128),
            "brown":   (139,  69,  19),
            "pink":    (255, 182, 193),
            "purple":  (128,   0, 128),
            "lime":    (  0, 255,   0),
            "navy":    (  0,   0, 128),
            "teal":    (  0, 128, 128),
        }
        return cmap.get((c or "Red").lower(), (255, 0, 0))

    def _find_traceid_field(self, layer: QgsVectorLayer):
        if layer is None:
            return None
        if layer.fields().indexOf("TraceID") != -1:
            return "TraceID"
        for name in ["traceid", "TRACEID", "trace_id", "TraceId", "TRACE_ID"]:
            if layer.fields().indexOf(name) != -1:
                return name
        return None

    def _layer_gpkg_path(self, layer: QgsVectorLayer):
        try:
            uri = layer.dataProvider().dataSourceUri()
            return uri.split("|")[0]
        except Exception as err:
            return None

    def _pick_nav_layer_from_same_gpkg(self, gpkg_path: str):
        if not gpkg_path:
            return None

        candidates = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            p = self._layer_gpkg_path(lyr)
            if not p or os.path.normpath(p) != os.path.normpath(gpkg_path):
                continue
            if lyr.fields().indexOf("distance") == -1:
                continue
            has_tid = (self._find_traceid_field(lyr) is not None)
            candidates.append((has_tid, lyr.featureCount(), lyr))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2]

    def _rebuild_overlay_cache(self):
        self._overlay_cache = {}
        self._overlay_cache_dirty = False

        if self.nav_layer is None or self.amp is None:
            return

        n_traces = int(self.amp.shape[0])
        xs = np.arange(n_traces, dtype=float)

        tid_field = self._find_traceid_field(self.nav_layer)
        if tid_field is None:
            for name in self.reflectors.keys():
                self._overlay_cache[name] = (xs, np.full(n_traces, np.nan, dtype=float))
            return

        # Build per reflector (read once per field)
        for name in self.reflectors.keys():
            field_name = self._reflector_field_name(name)
            fidx = self.nav_layer.fields().indexOf(field_name)
            ys = np.full((n_traces,), np.nan, dtype=float)

            if fidx == -1:
                self._overlay_cache[name] = (xs, ys)
                continue

            for f in self.nav_layer.getFeatures():
                try:
                    tid = int(f[tid_field]); i = tid - 1
                except Exception as err:
                    continue
                if i < 0 or i >= n_traces:
                    continue
                vv = self._qvariant_to_float_or_none(f.attribute(fidx))
                if vv is not None:
                    ys[i] = float(vv)

            self._overlay_cache[name] = (xs, ys)

    def _blank_water_using_reflector(self, data, reflector_name):
        if self.nav_layer is None or self.dt_ms is None or self.dt_ms <= 0:
            return data

        field_name = self._reflector_field_name(reflector_name)
        fidx = self.nav_layer.fields().indexOf(field_name)
        if fidx == -1:
            return data

        tid_field = self._find_traceid_field(self.nav_layer)
        if tid_field is None:
            return data

        out = data.copy()
        n_traces, n_samples = out.shape

        picks = np.full((n_traces,), np.nan, dtype=np.float32)
        for f in self.nav_layer.getFeatures():
            try:
                tid = int(f.attribute(tid_field)) - 1
            except Exception as err:
                continue
            if tid < 0 or tid >= n_traces:
                continue
            v = self._qvariant_to_float_or_none(f.attribute(fidx))
            if v is not None:
                picks[tid] = float(v)

        for i in range(n_traces):
            twt = picks[i]
            if not np.isfinite(twt):
                continue
            k = int(round((float(twt) - float(self.t0_ms or 0.0)) / float(self.dt_ms)))
            k = max(0, min(k, n_samples - 1))
            out[i, :k] = 0.0
        return out

    def _ensure_map_marker(self):
        if self.map_marker is None:
            self.map_marker = QgsVertexMarker(self.map_canvas)
            self.map_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.map_marker.setColor(QColor(0, 0, 0))
            self.map_marker.setIconSize(100000)
            self.map_marker.setPenWidth(1)
            self.map_marker.setZValue(1000)

    def _cancel_active_pick(self):
        self.poly_active = False
        self.poly_anchor = None
        if self.poly_preview is not None:
            self.poly_preview.setData([], [])

        self.erase_active = False
        self.erase_anchor = None
        self._clear_rect_preview("erase")

        self.box_active = False
        self.box_anchor = None
        self._clear_rect_preview("box")

        self.statusLabel.setText("Pick cancelled (ESC).")

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self._cancel_active_pick()
            event.accept()
            return
        super().keyPressEvent(event)

    def _meta_json_path(self):
        if not self.current_npy_path:
            return None
        base = os.path.splitext(os.path.basename(self.current_npy_path))[0]
        return os.path.join(os.path.dirname(self.current_npy_path), f"{base}.json")

    def _load_meta_json(self, npy_path):
        base = os.path.splitext(os.path.basename(npy_path))[0]
        meta_path = os.path.join(os.path.dirname(npy_path), f"{base}.json")
        if not os.path.exists(meta_path):
            QgsMessageLog.logMessage(str(f"[SBPViewer] No JSON meta found for {base}"), "PluginLogger", Qgis.Info)
            return

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}

            self.dt_ms = float(meta.get("sample_interval_ms") or 0) or None
            self.t0_ms = float(meta.get("t0_ms") or 0.0)
            self.vw = float(meta.get("sound_velocity_water_m_s") or 1500.0)
            self.vs = float(meta.get("sound_velocity_sediment_m_s") or 1500.0)

            r = meta.get("reflectors", None)
            if isinstance(r, dict) and "Seabed" in r:
                self.reflectors = dict(r)
                for k, v in list(self.reflectors.items()):
                    if not isinstance(v, dict):
                        self.reflectors[k] = {}
                    self.reflectors[k].setdefault("color", "Red")
                    self.reflectors[k].setdefault("show", True)

            self.active_reflector = (meta.get("viewer", {}) or {}).get("active_reflector", "Seabed")
            if self.active_reflector not in self.reflectors:
                self.active_reflector = "Seabed"

            proc = meta.get("processing", {}) or {}

            bp = proc.get("bandpass", {}) or {}
            self.proc_bp_enabled = bool(bp.get("enabled", False))
            self.proc_bp_low_hz = float(bp.get("low_hz", 200.0) or 0.0)
            self.proc_bp_high_hz = float(bp.get("high_hz", 3000.0) or 0.0)
            self.proc_bp_taper_pct = float(bp.get("taper_pct", 5.0) or 0.0)

            agc = proc.get("agc", {}) or {}
            self.proc_agc_enabled = bool(agc.get("enabled", False))
            self.proc_agc_window_pct = float(agc.get("window_pct", 80.0) or 80.0)
            self.proc_agc_intensity_pct = float(agc.get("intensity_pct", 10.0) or 10.0)

            tvg = proc.get("tvg", {}) or {}
            self.proc_tvg_enabled = bool(tvg.get("enabled", False))
            self.proc_tvg_scalar = float(tvg.get("scalar", 0.0) or 0.0)

            stack = proc.get("stacking", {}) or {}
            self.proc_stack_enabled = bool(stack.get("enabled", False))
            self.proc_stack_shots = int(stack.get("shots", 1) or 1)
            self.proc_stack_mode = str(stack.get("mode", "Avg") or "Avg")

            blank = proc.get("blank_water", {}) or {}
            self.proc_blank_water_enabled = bool(blank.get("enabled", False))

        except Exception as e:
            QgsMessageLog.logMessage(f"[SBPViewer] Failed to read meta JSON ({meta_path}): {e}", "PluginLogger", Qgis.Info)

    def _save_meta_reflectors(self):
        p = self._meta_json_path()
        if not p:
            return
        try:
            meta = {}
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    meta = json.load(f) or {}

            meta["reflectors"] = dict(self.reflectors)
            meta.setdefault("viewer", {})
            meta["viewer"]["active_reflector"] = str(self.active_reflector or "Seabed")

            with open(p, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            QgsMessageLog.logMessage(str(f"[SBPViewer] Failed to save reflectors: {e}"), "PluginLogger", Qgis.Info)

    def _load_viewer_settings_from_json(self):
        p = self._meta_json_path()
        if not p or not os.path.exists(p):
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
            v = meta.get("viewer", {}) or {}

            self._loading_viewer_state = True
            try:
                if "colormap" in v:
                    idx = self.comboColor.findText(str(v.get("colormap")))
                    if idx >= 0:
                        self.comboColor.setCurrentIndex(idx)
                if "invert" in v:
                    self.chkInvert.setChecked(bool(v.get("invert")))
                if ("min" in v) and ("max" in v):
                    self.txtMin.setValue(float(v.get("min")))
                    self.txtMax.setValue(float(v.get("max")))
                    self._update_minmax_spinbox_step()
            finally:
                self._loading_viewer_state = False
            
            return True
        except Exception as e:
            QgsMessageLog.logMessage(str(f"[SBPViewer] Failed to load viewer settings: {e}"), "PluginLogger", Qgis.Info)
            return False

    def _save_viewer_settings_to_json(self):
        if self._loading_viewer_state:
            return
        p = self._meta_json_path()
        if not p:
            return
        try:
            meta = {}
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    meta = json.load(f) or {}

            meta.setdefault("viewer", {})
            meta["viewer"].update({
                "min": float(self.txtMin.value()),
                "max": float(self.txtMax.value()),
                "colormap": str(self.comboColor.currentText()),
                "invert": bool(self.chkInvert.isChecked()),
                "active_reflector": str(self.active_reflector or "Seabed"),
            })

            with open(p, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            QgsMessageLog.logMessage(str(f"[SBPViewer] Failed to save viewer settings: {e}"), "PluginLogger", Qgis.Info)

    def _build_twt_axis_cache(self):
        self.twt_axis_ms = None
        if self.amp is None:
            return
        if self.dt_ms is None or self.dt_ms <= 0:
            return
        n_samples = int(self.amp.shape[1])
        self.twt_axis_ms = float(self.t0_ms or 0.0) + np.arange(n_samples, dtype=np.float32) * float(self.dt_ms)

    def _geom_to_point_xy(self, geom):
        try:
            if geom is None or geom.isEmpty():
                return (np.nan, np.nan)
            if geom.type() == 0:
                if geom.isMultipart():
                    pts = geom.asMultiPoint()
                    if pts:
                        return (pts[0].x(), pts[0].y())
                else:
                    pt = geom.asPoint()
                    return (pt.x(), pt.y())
            c = geom.centroid()
            if not c.isEmpty():
                pt = c.asPoint()
                return (pt.x(), pt.y())
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        return (np.nan, np.nan)

    def _load_nav_layer_arrays(self):
        if self.amp is None or self.nav_layer is None:
            return False

        n_traces = int(self.amp.shape[0])
        idx_dist = self.nav_layer.fields().indexOf("distance")
        if idx_dist == -1:
            return False

        tid_field = self._find_traceid_field(self.nav_layer)
        if tid_field is None:
            return False

        idx_shift = self.nav_layer.fields().indexOf("dt_shift_ms")  # optional field

        dist  = np.full((n_traces,), np.nan, dtype=float)
        xs    = np.full((n_traces,), np.nan, dtype=float)
        ys    = np.full((n_traces,), np.nan, dtype=float)
        shift = np.zeros((n_traces,), dtype=float)  # default 0

        self._traceid_to_fid = {}
        self._overlay_cache_dirty = True

        for f in self.nav_layer.getFeatures():
            try:
                tid = int(f[tid_field])
                i = tid - 1
            except Exception as err:
                continue
            if i < 0 or i >= n_traces:
                continue

            try:
                dv = f.attribute(idx_dist)
                dist[i] = float(dv) if dv is not None else np.nan
            except Exception as err:
                dist[i] = np.nan

            # dt_shift_ms (optional)
            if idx_shift != -1:
                sv = self._qvariant_to_float_or_none(f.attribute(idx_shift))
                if sv is not None:
                    shift[i] = float(sv)

            gx, gy = self._geom_to_point_xy(f.geometry())
            xs[i] = gx
            ys[i] = gy

            self._traceid_to_fid[tid] = f.id()

        self.distance = dist
        self.nav_x = xs
        self.nav_y = ys
        self.dt_shift_ms_arr = shift
        return True

    def _build_shifted_image(self):
        self.amp_view = None

        base = self.amp_proc if self.amp_proc is not None else self.amp
        if base is None:
            return

        self.t0_view_ms = float(self.t0_ms or 0.0)

        if (not self.dt_shift_enabled) or (self.dt_ms is None) or (self.dt_ms <= 0):
            self.amp_view = base
            return

        n_traces, n_samples = base.shape

        arr = self.dt_shift_ms_arr if self.view_domain == "TWT" else self.dt_shift_ms_depth_arr
        if arr is None or len(arr) != n_traces:
            self.amp_view = base
            return

        shifts = np.rint(arr / float(self.dt_ms)).astype(np.int32)

        s_min = int(np.min(shifts))
        s_max = int(np.max(shifts))

        pad_top = max(0, -s_min)
        pad_bot = max(0,  s_max)

        new_ns = n_samples + pad_top + pad_bot
        out = np.zeros((n_traces, new_ns), dtype=np.float32)

        for i in range(n_traces):
            s = int(shifts[i])
            j0 = pad_top + s
            j1 = j0 + n_samples
            if j0 < 0 or j1 > new_ns:
                j0c = max(0, j0)
                j1c = min(new_ns, j1)
                src0 = max(0, -(j0))
                src1 = src0 + (j1c - j0c)
                out[i, j0c:j1c] = base[i, src0:src1]
            else:
                out[i, j0:j1] = base[i, :]

        self.amp_view = out
        self.t0_view_ms = float(self.t0_ms or 0.0) - pad_top * float(self.dt_ms)

    def _bandpass_fft_trace(self, tr, dt_s, f_low, f_high, taper_pct=5.0):
        n = tr.size
        if n < 8:
            return tr

        x = tr.astype(np.float32, copy=False)
        X = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(n, d=dt_s)

        low = max(0.0, float(f_low))
        high = max(low, float(f_high))
        if high <= 0.0:
            return tr

        H = np.zeros_like(freqs, dtype=np.float32)
        passband = (freqs >= low) & (freqs <= high)
        H[passband] = 1.0

        tp = max(0.0, min(50.0, float(taper_pct))) / 100.0
        if tp > 0 and high > low:
            bw = high - low
            tw = bw * tp
            if tw > 0:
                lo1 = low
                lo2 = low + tw
                m = (freqs >= lo1) & (freqs < lo2)
                if np.any(m):
                    t = (freqs[m] - lo1) / (lo2 - lo1)
                    H[m] = 0.5 - 0.5 * np.cos(np.pi * t)

                hi1 = high - tw
                hi2 = high
                m = (freqs > hi1) & (freqs <= hi2)
                if np.any(m):
                    t = (freqs[m] - hi1) / (hi2 - hi1)
                    H[m] = 0.5 + 0.5 * np.cos(np.pi * t)

        Y = X * H
        y = np.fft.irfft(Y, n=n).astype(np.float32)
        return y

    def _agc_trace(self, tr, win_samp, intensity_pct):
        n = tr.size
        win = int(max(3, win_samp))
        if win % 2 == 0:
            win += 1
        half = win // 2

        x = tr.astype(np.float32, copy=False)
        x2 = x * x

        c = np.cumsum(np.pad(x2, (1, 0), mode="constant"))
        idx = np.arange(n)
        a = np.maximum(0, idx - half)
        b = np.minimum(n, idx + half + 1)
        sums = c[b] - c[a]
        lens = (b - a).astype(np.float32)
        rms = np.sqrt(sums / np.maximum(lens, 1.0))

        inten = max(0.0, min(100.0, float(intensity_pct))) / 100.0
        eps = 1e-8
        y_full = x / np.maximum(rms, eps)
        y = (1.0 - inten) * x + inten * y_full
        return y.astype(np.float32)

    def _tvg_gain_curve(self, n_samples, dt_ms, scalar_pct):
        p = (float(scalar_pct) / 50.0)  # -2 .. +2
        if abs(p) < 1e-9:
            return np.ones((n_samples,), dtype=np.float32)

        t = (np.arange(n_samples, dtype=np.float32) * float(dt_ms)) / 1000.0
        t_end = max(t[-1], 1e-6)
        g = (np.maximum(t, 1e-6) / t_end) ** p
        return g.astype(np.float32)

    def _apply_stacking(self, data):
        if not self.proc_stack_enabled or self.proc_stack_shots <= 1:
            return data
        n_traces, _ = data.shape
        out = np.empty_like(data)
        half = self.proc_stack_shots // 2
        use_median = (self.proc_stack_mode.lower().startswith("med"))
        for i in range(n_traces):
            i0 = max(0, i - half)
            i1 = min(n_traces, i + half + 1)
            window = data[i0:i1, :]
            out[i, :] = np.median(window, axis=0) if use_median else np.mean(window, axis=0)
        return out

    def _apply_processing_pipeline(self):
        if self.amp is None:
            self.amp_proc = None
            return

        data = self.amp.astype(np.float32, copy=True)
        n_traces, n_samples = data.shape

        if self.proc_bp_enabled and self.dt_ms is not None and self.dt_ms > 0:
            dt_s = float(self.dt_ms) / 1000.0
            low = float(self.proc_bp_low_hz)
            high = float(self.proc_bp_high_hz)
            taper = float(self.proc_bp_taper_pct)
            for i in range(n_traces):
                data[i, :] = self._bandpass_fft_trace(data[i, :], dt_s, low, high, taper)

        if self.proc_agc_enabled:
            win = max(3, int(round((float(self.proc_agc_window_pct) / 100.0) * n_samples)))
            inten = float(self.proc_agc_intensity_pct)
            for i in range(n_traces):
                data[i, :] = self._agc_trace(data[i, :], win, inten)

        if self.proc_tvg_enabled and self.dt_ms is not None and self.dt_ms > 0:
            g = self._tvg_gain_curve(n_samples, self.dt_ms, self.proc_tvg_scalar)
            data *= g[None, :]

        if self.proc_stack_enabled and self.proc_stack_shots > 1:
            data = self._apply_stacking(data)

        if self.proc_blank_water_enabled:
            data = self._blank_water_using_reflector(data, "Seabed")

        self.amp_proc = data
        self._build_shifted_image()

    def _current_data(self):
        # show shifted image if built, else fall back
        if self.amp_view is not None:
            return self.amp_view
        return self.amp_proc if self.amp_proc is not None else self.amp

    def scaleToBest(self, *args, **kwargs):
        reset_zoom = kwargs.get('reset_zoom', False)
        data = self._current_data()
        if data is None:
            return
        self.txtMin.setValue(float(np.percentile(data, 0.1)))
        self.txtMax.setValue(float(np.percentile(data, 99.9)))
        self._update_minmax_spinbox_step()
        self.updatePlot(reset_zoom=reset_zoom)

    def scaleToData(self, *args, **kwargs):
        reset_zoom = kwargs.get('reset_zoom', False)
        data = self._current_data()
        if data is None:
            return
        self.txtMin.setValue(float(np.min(data)))
        self.txtMax.setValue(float(np.max(data)))
        self._update_minmax_spinbox_step()
        self.updatePlot(reset_zoom=reset_zoom)

    def sonarLUT(self, mode="Grey", invert=True):
        lut = []
        if mode == "Grey bipolar":
            for i in range(256):
                if i <= 128:
                    g = i / 128.0
                else:
                    g = 1.0 - (i - 128) / 127.0
                if invert:
                    g = 1.0 - g
                val = int(max(0, min(255, round(g * 255))))
                lut.append([val, val, val])
        else:
            for i in range(256):
                g = i if invert else (255 - i)
                g = max(min(g, 255), 0)
                lut.append([g, g, g])
        return np.array(lut, dtype=np.ubyte)

    def updatePlot(self, *args, **kwargs):
        reset_zoom = kwargs.get('reset_zoom', False)
        data = self._current_data()
        if data is None:
            return

        minv = float(self.txtMin.value())
        maxv = float(self.txtMax.value())
        n_traces, n_samples = data.shape

        is_first_load = False
        if self.img is None:
            self.img = pg.ImageItem(data)
            self.pgview.addItem(self.img)
            is_first_load = True
        else:
            self.img.setImage(data, autoLevels=False)

        lut = self.sonarLUT(mode=str(self.comboColor.currentText()), invert=bool(self.chkInvert.isChecked()))
        self.img.setLookupTable(lut)
        self.img.setLevels([minv, maxv])

        if self.dt_ms is not None and self.dt_ms > 0:
            if self.view_domain == "TWT":
                y0 = float(self.t0_view_ms)
                dy = float(self.dt_ms)
                y_label = "TWT (ms)"
            else:
                y0 = (float(self.t0_view_ms) / 1000.0) * float(self.vs) / 2.0
                dy = (float(self.dt_ms) / 1000.0) * float(self.vs) / 2.0
                y_label = "Depth (m)"

            height = float((n_samples - 1) * dy)
            self.img.setRect(QtCore.QRectF(0.0, y0, float(n_traces), height))
            self.pgview.setLabel("left", y_label)
            self.wiggleView.setLabel("left", y_label)
            if reset_zoom or is_first_load:
                self.pgview.setYRange(y0, y0 + height, padding=0)
                self.wiggleView.setYRange(y0, y0 + height, padding=0)
        else:
            self.img.setRect(QtCore.QRectF(0.0, 0.0, float(n_traces), float(n_samples)))
            if reset_zoom or is_first_load:
                self.pgview.setYRange(0, n_samples - 1, padding=0)
                self.wiggleView.setYRange(0, n_samples - 1, padding=0)

        if reset_zoom or is_first_load:
            self.pgview.setXRange(0, n_traces - 1, padding=0)
        self.wiggleView.setXRange(minv, maxv, padding=0)

        if self.poly_preview is None:
            self.poly_preview = pg.PlotDataItem([], [], pen=pg.mkPen('r', width=2, style=QtCore.Qt.DashLine))
            self.pgview.addItem(self.poly_preview)

        self._refresh_all_reflector_overlays()


    def _refresh_all_reflector_overlays(self):
        self._ensure_reflector_overlay_items()

        if not self.show_reflectors_master:
            for it in self.reflector_overlays.values():
                it.setData([], [])
            return

        if self.nav_layer is None or self.amp is None:
            for it in self.reflector_overlays.values():
                it.setData([], [])
            return

        if self._overlay_cache_dirty:
            self._rebuild_overlay_cache()

        for name, it in self.reflector_overlays.items():
            info = self.reflectors.get(name, {}) or {}
            if not bool(info.get("show", True)):
                it.setData([], [])
                continue

            xs, ys_raw = self._overlay_cache.get(name, (None, None))
            if xs is None or ys_raw is None:
                it.setData([], [])
                continue

            ys_view = np.full_like(ys_raw, np.nan, dtype=float)
            for i in range(len(ys_raw)):
                v = ys_raw[i]
                if np.isfinite(v):
                    ys_view[i] = self._raw_twt_to_view_y(i, float(v))

            rgb = self._rgb_from_name(info.get("color", "Red"))
            it.setPen(pg.mkPen(rgb, width=2))
            it.setData(xs, ys_view, connect="finite")

    def _build_depth_shift_array_B(self):
        """
        Build dt_shift_ms_depth_arr so that seabed depth is preserved from TWT-view (B):
        s_new = (t_sb + s_old)*(Vw/Vs) - t_sb
        """
        self.dt_shift_ms_depth_arr = None
        if self.nav_layer is None or self.amp is None:
            return
        if self.dt_ms is None or self.dt_ms <= 0:
            return

        n_traces = int(self.amp.shape[0])

        seabed_field = self._reflector_field_name("Seabed")
        fidx_sb = self.nav_layer.fields().indexOf(seabed_field)
        if fidx_sb == -1:
            return

        tid_field = self._find_traceid_field(self.nav_layer)
        if tid_field is None:
            return

        old = self.dt_shift_ms_arr
        if old is None or len(old) != n_traces:
            old = np.zeros((n_traces,), dtype=float)

        out = np.array(old, dtype=float, copy=True)

        for f in self.nav_layer.getFeatures():
            try:
                tid = int(f[tid_field]); i = tid - 1
            except Exception as err:
                continue
            if i < 0 or i >= n_traces:
                continue

            t_sb = self._qvariant_to_float_or_none(f.attribute(fidx_sb))
            if t_sb is None:
                continue

            t_sb = float(t_sb)
            s_old = float(old[i]) if np.isfinite(old[i]) else 0.0
            out[i] = (t_sb + s_old) * (float(self.vw) / float(self.vs)) - t_sb

        self.dt_shift_ms_depth_arr = out

    def _delete_reflector_field_from_layer(self, reflector_name: str):
        if self.nav_layer is None or not isinstance(self.nav_layer, QgsVectorLayer):
            return
        field_name = self._reflector_field_name(reflector_name)
        idx = self.nav_layer.fields().indexOf(field_name)
        if idx == -1:
            return

        if not self.nav_layer.isEditable():
            self.nav_layer.startEditing()

        ok = self.nav_layer.dataProvider().deleteAttributes([idx])
        self.nav_layer.updateFields()
        if not ok:
            self.nav_layer.rollBack()
            QtWidgets.QMessageBox.warning(self, "Delete Field", f"Failed to delete field '{field_name}'.")
            return

        if not self.nav_layer.commitChanges():
            self.nav_layer.rollBack()
            QtWidgets.QMessageBox.warning(self, "Delete Field", f"Commit failed while deleting '{field_name}'.")
            return

        self.nav_layer.startEditing()

    def openPickingDialog(self):
        if self.pickDlg is None:
            self.pickDlg = SBPPickingDialog(parent=self)
            self.pickDlg.settingsChanged.connect(self.onPickingSettingsChanged)
            self.pickDlg.reflectorChanged.connect(self.onPickingReflectorChanged)
            self.pickDlg.autoTrackRequested.connect(self.runAutoTrack)
            self.pickDlg.closed.connect(self.onPickingDialogClosed)

        self._update_picking_target_label()
        self.pickDlg.setReflectors(self.reflectors, self.active_reflector)
        self.pickDlg.show()
        self.pickDlg.raise_()
        self.pickDlg.activateWindow()

    def _update_picking_target_label(self):
        if self.pickDlg is None:
            return
        if self.layer and isinstance(self.layer, QgsVectorLayer):
            uri = self.layer.dataProvider().dataSourceUri()
            gpkg_path = uri.split("|")[0]
            base = os.path.splitext(os.path.basename(gpkg_path))[0]
            self.pickDlg.setTargetText(base)
        else:
            self.pickDlg.setTargetText("(no active vector layer)")

    def onPickingSettingsChanged(self, settings: dict):
        old_names = set(self.reflectors.keys())

        self.pick_settings = dict(settings)
        self._cancel_active_pick()

        r = settings.get("reflectors", None)
        if isinstance(r, dict) and "Seabed" in r:
            self.reflectors = dict(r)

        new_names = set(self.reflectors.keys())
        removed = sorted(old_names - new_names)

        for name in removed:
            if name != "Seabed":
                self._delete_reflector_field_from_layer(name)

        ar = settings.get("active_reflector", None)
        if ar and ar in self.reflectors:
            self.active_reflector = ar
        else:
            self.active_reflector = "Seabed"

        self.pick_reflector = self.active_reflector
        self._save_meta_reflectors()
        
        self._ensure_reflector_overlay_items()
        self._overlay_cache_dirty = True
        self._refresh_all_reflector_overlays()

    def onPickingReflectorChanged(self, name: str):
        name = (name or "Seabed").strip() or "Seabed"
        if name in self.reflectors:
            self.active_reflector = name
            self.pick_reflector = name
        self._save_meta_reflectors()
        self._overlay_cache_dirty = True
        self._refresh_all_reflector_overlays()

    def onPickingDialogClosed(self):
        pass

    def onCurrentLayerChanged(self, new_layer):
        if getattr(self, "_loading_layer", False):
            return

        self._loading_layer = True
        try:
            # ✅ Preserve the user's ticked marker-layer IDs across SBP line changes.
            # We keep marker_settings["layer_ids"] intact so that after the new
            # line loads, _recompute_markers() will immediately try to draw them.
            preserved_ids = list(self.marker_settings.get("layer_ids", []) or [])

            # Clear the visual markers for the old line (new ones drawn after load)
            self._clear_markers()
            self._markers_cache = []

            # Refresh dialog list but restore the previous tick state
            if self.markersDlg is not None:
                self.markersDlg._populate_layers(keep_checked_ids=preserved_ids)

            if self.freeze_layer:
                return
            self.layer = new_layer
            self._load_from_layer(new_layer)
            self._update_picking_target_label()
        finally:
            self._loading_layer = False

        # ✅ Recompute and draw markers NOW that _loading_layer is False.
        # (Inside loadNPY this call was silently skipped by the guard flag.)
        self._recompute_markers()
        self._draw_markers()

    def _populate_layers(self):
        self.lstLayers.clear()

        # viewer parent
        viewer = self.parent()

        # exclude: current nav layer (viewer.nav_layer)
        nav_layer = getattr(viewer, "nav_layer", None) if viewer else None

        # exclude: current active layer in QGIS (iface.activeLayer)
        active_layer = iface.activeLayer()

        layers = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            if lyr.geometryType() != 0:  # Point only
                continue

            if nav_layer is not None and lyr.id() == nav_layer.id():
                continue

            # ❌ exclude current active layer (often your SBP layer)
            if active_layer is not None and lyr.id() == active_layer.id():
                continue

            layers.append(lyr)

        layers.sort(key=lambda L: L.name().lower())

        for lyr in layers:
            it = QtWidgets.QListWidgetItem(lyr.name())
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, lyr.id())
            self.lstLayers.addItem(it)


    def _load_from_layer(self, layer):
        if layer is None or not isinstance(layer, QgsVectorLayer):
            QgsMessageLog.logMessage(str("[SBPViewer] Active layer is not a vector layer – keeping current data."), "PluginLogger", Qgis.Info)
            return

        uri = layer.dataProvider().dataSourceUri()
        gpkg_path = uri.split("|")[0]
        base = os.path.splitext(os.path.basename(gpkg_path))[0]
        folder = os.path.dirname(gpkg_path)
        npy_path = os.path.join(folder, f"{base}.npy")

        if not os.path.exists(npy_path):
            QgsMessageLog.logMessage(str(f"[SBPViewer] No matching NPY found for {gpkg_path} – keeping current data."), "PluginLogger", Qgis.Info)
            return

        self.fileEdit.setText(npy_path)

        self.nav_layer = self._pick_nav_layer_from_same_gpkg(gpkg_path)
        self.loadNPY(npy_path)
        # ✅ refresh markers dialog list so it excludes the NEW nav layer,
        #    but restore the user's previously-ticked marker-layer IDs.
        if self.markersDlg is not None:
            preserved_ids = list(self.marker_settings.get("layer_ids", []) or [])
            self.markersDlg._populate_layers(keep_checked_ids=preserved_ids)

    def loadNPY(self, path, reset_zoom=True):
        self.current_npy_path = path
        self.amp = np.load(path)

        self._load_meta_json(path)
        self._build_twt_axis_cache()

        if self.nav_layer is not None:
            tid_field = self._find_traceid_field(self.nav_layer)
            if tid_field is None:
                QtWidgets.QMessageBox.warning(self, "SBP Viewer", "Nav layer is missing TraceID (or variants).")
                self.nav_layer = None

        if not self._load_nav_layer_arrays():
            QgsMessageLog.logMessage("[SBPViewer] Navigation layer arrays could not be built (TraceID/distance/geometry issue).", "PluginLogger", Qgis.Info)

        # build depth-domain shift array (depends on Seabed + dt_shift_ms)
        self._build_depth_shift_array_B()

        self._apply_processing_pipeline()
        # ensure shift image built (in case processing disabled / early return)
        self._build_shifted_image()
        self._load_viewer_settings_from_json()

        if self.txtMax.value() == self.txtMin.value():
            self.scaleToBest(reset_zoom=reset_zoom)
        else:
            self.updatePlot(reset_zoom=reset_zoom)

        # Redraw markers only when loadNPY is called directly (e.g. gain apply,
        # refresh). When called from onCurrentLayerChanged, _loading_layer is True
        # here and the explicit recompute after that function handles it instead.
        if not getattr(self, "_loading_layer", False):
            self._recompute_markers()
            self._draw_markers()


    def openGainDialog(self):
        if not self.current_npy_path:
            QtWidgets.QMessageBox.warning(self, "No Line Loaded", "No SBP NPY file is currently loaded from a layer.")
            return
        base = os.path.splitext(os.path.basename(self.current_npy_path))[0]
        json_path = os.path.join(os.path.dirname(self.current_npy_path), f"{base}.json")
        dlg = SBPGainSettingsDialog(json_path, parent=self)
        def _reload_after_gain():
            self.loadNPY(self.current_npy_path, reset_zoom=False)
            self.scaleToBest(reset_zoom=False)     # <-- force best scale after gain
            self.updatePlot(reset_zoom=False)

        dlg.applied.connect(_reload_after_gain)

        dlg.exec_()

    def openColorDialog(self):
        """Open (or bring to front) the non-modal Color Scale dialog."""
        if self.colorDlg is None:
            self.colorDlg = SBPColorDialog(viewer=self, parent=self)
            self.colorDlg.closed.connect(self._on_color_dialog_closed)
        else:
            # Sync current viewer values before showing again
            self.colorDlg._sync_from_viewer()
        self.colorDlg.show()
        self.colorDlg.raise_()
        self.colorDlg.activateWindow()

    def _on_color_dialog_closed(self):
        self.colorDlg = None

    def _snap_twt_to_grid(self, twt_ms: float, n_samples: int) -> float:
        if self.dt_ms is None or self.dt_ms <= 0:
            return float(twt_ms)
        k = int(round((float(twt_ms) - float(self.t0_ms or 0.0)) / float(self.dt_ms)))
        k = max(0, min(k, n_samples - 1))
        return float(self.t0_ms or 0.0) + k * float(self.dt_ms)

    def _get_feature_by_traceid(self, trace_id_value: int):
        if self.nav_layer is None:
            return None
        fid = self._traceid_to_fid.get(int(trace_id_value))
        if fid is None:
            return None
        return self.nav_layer.getFeature(fid)

    def _find_first_peak(self, tr, klo, khi, strategy, thr):
        for k in range(klo + 1, khi - 1):
            a0 = tr[k - 1]
            a1 = tr[k]
            a2 = tr[k + 1]

            if strategy == "Positive peak":
                if a0 < a1 > a2 and a1 >= thr:
                    return k
            elif strategy == "Negative peak":
                if a0 > a1 < a2 and abs(a1) >= thr:
                    return k
            else:
                if ((a0 < a1 > a2) or (a0 > a1 < a2)) and (abs(a1) >= thr):
                    return k
        return None

    def _twt_ms_to_sample(self, twt_ms: float, n_samples: int) -> int:
        if self.dt_ms is None or self.dt_ms <= 0:
            return int(round(twt_ms))
        k = int(round((float(twt_ms) - float(self.t0_ms or 0.0)) / float(self.dt_ms)))
        return max(0, min(k, n_samples - 1))

    def _sample_to_twt_ms(self, k: int) -> float:
        if self.dt_ms is None or self.dt_ms <= 0:
            return float(k)
        return float(self.t0_ms or 0.0) + float(k) * float(self.dt_ms)

    def _nearest_zero_crossing(self, trace: np.ndarray, k0: int, k_lo: int, k_hi: int):
        if k_hi <= k_lo:
            return None
        k0 = int(max(k_lo, min(k0, k_hi)))
        x = trace

        def check_pair(i, j):
            a = float(x[i]); b = float(x[j])
            if a == 0.0:
                return float(i)
            if (a > 0 and b < 0) or (a < 0 and b > 0):
                denom = (b - a)
                if denom == 0:
                    return None
                t = -a / denom
                t = max(0.0, min(1.0, t))
                return float(i) + t
            if b == 0.0:
                return float(j)
            return None

        for d in range(0, max(k0 - k_lo, k_hi - k0) + 1):
            i = k0 - d
            j = i + 1
            if i >= k_lo and j <= k_hi:
                z = check_pair(i, j)
                if z is not None:
                    return z
            i2 = k0 + d
            j2 = i2 + 1
            if i2 >= k_lo and j2 <= k_hi:
                z = check_pair(i2, j2)
                if z is not None:
                    return z
        return None

    def _rolling_median_nan(self, arr, win):
        if win <= 1:
            return arr
        if win % 2 == 0:
            win += 1
        half = win // 2
        out = np.array(arr, dtype=float, copy=True)
        n = len(out)
        for i in range(n):
            a = max(0, i - half)
            b = min(n, i + half + 1)
            w = out[a:b]
            w = w[np.isfinite(w)]
            if w.size > 0:
                out[i] = float(np.median(w))
        return out

    def runAutoTrack(self, params: dict):
        # IMPORTANT:
        # AutoTrack should SEARCH on RAW (unshifted) data, but the WINDOW comes from VIEW.
        base = self.amp_proc if self.amp_proc is not None else self.amp
        if base is None:
            QtWidgets.QMessageBox.warning(self, "Auto Track", "No SBP data loaded.")
            return

        if self.nav_layer is None or not isinstance(self.nav_layer, QgsVectorLayer):
            QtWidgets.QMessageBox.warning(self, "Auto Track", "No nav layer found for this line.")
            return
        if self.dt_ms is None or self.dt_ms <= 0:
            QtWidgets.QMessageBox.warning(self, "Auto Track", "dt_ms missing. Cannot auto-track in TWT space.")
            return

        self._cancel_active_pick()

        n_traces, n_samples = base.shape

        mode = (params.get("interval_mode") or "Full")
        if mode == "Visible":
            try:
                xr = self.pgview.getViewBox().viewRange()[0]
                i0 = int(max(0, np.floor(xr[0])))
                i1 = int(min(n_traces - 1, np.ceil(xr[1])))
            except Exception as err:
                i0, i1 = 0, n_traces - 1
        elif mode == "Custom":
            i0 = int(max(0, min(n_traces - 1, params.get("custom_from", 0))))
            i1 = int(max(0, min(n_traces - 1, params.get("custom_to", i0))))
            if i1 < i0:
                i0, i1 = i1, i0
        else:
            i0, i1 = 0, n_traces - 1

        if i1 <= i0:
            QtWidgets.QMessageBox.warning(self, "Auto Track", "Trace interval is empty.")
            return

        # These are in VIEW coordinates
        start_ms_view  = float(params.get("start_ms", 0.0))
        window_ms_view = max(0.0, float(params.get("window_ms", 0.0)))
        t_start_view = start_ms_view
        t_end_view   = start_ms_view + window_ms_view

        # Convert VIEW window to sample indices (still VIEW reference)
        k_start_view = self._twt_ms_to_sample(t_start_view, n_samples)
        k_end_view   = self._twt_ms_to_sample(t_end_view, n_samples)
        if k_end_view < k_start_view:
            k_start_view, k_end_view = k_end_view, k_start_view

        k_start_view = max(0, min(k_start_view, n_samples - 2))
        k_end_view   = max(0, min(k_end_view, n_samples - 2))
        if k_end_view <= k_start_view:
            QtWidgets.QMessageBox.warning(self, "Auto Track", "Search window too small/out of range.")
            return

        strategy = (params.get("strategy") or "Absolute peak")
        use_zero = bool(params.get("zero_cross", False))
        thr_pct  = max(0.0, min(100.0, float(params.get("threshold_pct", 10.0))))
        smooth_n = int(params.get("smooth_traces", 0))

        field_name = self._reflector_field_name(self.pick_reflector)
        try:
            fld_idx = self._ensure_field(self.nav_layer, field_name)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Auto Track", str(e))
            return

        if not self.nav_layer.isEditable():
            self.nav_layer.startEditing()

        picks_raw = np.full((i1 - i0 + 1,), np.nan, dtype=float)
        prev_raw = None

        for ii, tr_idx in enumerate(range(i0, i1 + 1)):
            tr = base[tr_idx, :].astype(float, copy=False)

            # Convert VIEW window -> RAW window per trace using dt_shift
            s_ms = self._shift_ms_at_trace(tr_idx)
            s_samp = int(round(float(s_ms) / float(self.dt_ms)))

            ks = k_start_view - s_samp
            ke = k_end_view   - s_samp
            ks = max(0, min(int(ks), n_samples - 2))
            ke = max(0, min(int(ke), n_samples - 2))
            if ke <= ks:
                continue

            seg = tr[ks:ke+1]
            if seg.size < 2:
                continue

            max_abs = float(np.max(np.abs(seg))) if seg.size else 0.0
            thr = (thr_pct / 100.0) * max_abs if max_abs > 0 else 0.0

            k_peak = self._find_first_peak(tr, ks, ke, strategy, thr)
            if k_peak is None:
                if prev_raw is not None:
                    picks_raw[ii] = prev_raw
                continue

            if use_zero:
                z = self._nearest_zero_crossing(tr, k_peak, ks, min(ke, n_samples - 2))
                twt_raw = float(self.t0_ms or 0.0) + float(z) * float(self.dt_ms) if (z is not None) else self._sample_to_twt_ms(k_peak)
            else:
                twt_raw = self._sample_to_twt_ms(k_peak)

            # Snap in RAW
            if self.pick_settings.get("snap_to_sample", True):
                twt_raw = self._snap_twt_to_grid(twt_raw, n_samples)

            picks_raw[ii] = float(twt_raw)
            prev_raw = float(twt_raw)

        if smooth_n and smooth_n > 1:
            picks_raw = self._rolling_median_nan(picks_raw, smooth_n)

        overwrite = bool(self.pick_settings.get("overwrite_trace", True))
        written = 0

        for ii, tr_idx in enumerate(range(i0, i1 + 1)):
            v_raw = picks_raw[ii]
            trace_id_value = int(tr_idx) + 1
            feat = self._get_feature_by_traceid(trace_id_value)
            if feat is None:
                continue

            cur = feat.attribute(fld_idx)
            curv = self._qvariant_to_float_or_none(cur)
            if (curv is not None) and (not overwrite):
                continue
            if not np.isfinite(v_raw):
                continue

            if self.nav_layer.changeAttributeValue(feat.id(), fld_idx, float(v_raw)):
                written += 1

        if self.pick_settings.get("write_immediately", True):
            if not self.nav_layer.commitChanges():
                self.nav_layer.rollBack()
                QtWidgets.QMessageBox.warning(self, "Auto Track", "Commit failed (layer rolled back).")
                return
            self.nav_layer.startEditing()

        self._overlay_cache_dirty = True
        self._refresh_all_reflector_overlays()
        self.statusLabel.setText(f"AutoTrack: wrote {written} RAW picks to '{field_name}' (traces {i0}..{i1}).")


    def _ensure_rect_preview(self, which="erase"):
        """
        Create (if needed) and return the preview rectangle PlotDataItem.
        Safe to call anytime after pgview exists (mouse move/click).
        """
        attr = "erase_rect_item" if which == "erase" else "box_rect_item"
        item = getattr(self, attr, None)

        if item is None:
            item = pg.PlotDataItem([], [], pen=pg.mkPen('r', width=1, style=QtCore.Qt.DashLine))
            item.setBrush(pg.mkBrush(255, 0, 0, 60))  # RGBA (alpha=60)
            item.setFillLevel(0)  # baseline; we will override dynamically
            item.setZValue(999)
            try:
                self.pgview.addItem(item)
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
            setattr(self, attr, item)

        return item

    def _set_rect_preview(self, item, x0, x1, y0, y1):
        xs = [x0, x1, x1, x0, x0]
        ys = [y0, y0, y1, y1, y0]
        item.setData(xs, ys)
        item.setFillLevel(y0)

    def _clear_rect_preview(self, which="erase"):
        attr = "erase_rect_item" if which == "erase" else "box_rect_item"
        item = getattr(self, attr, None)
        if item is not None:
            item.setData([], [])

    def _depth_from_twt_view_ms(self, trace_index: int, twt_view_ms: float) -> float:
        """
        Depth in meters from VIEW TWT(ms) (i.e. AFTER dt_shift).
        Uses piecewise water/sediment with seabed also in VIEW time.
        """
        twt_view = float(twt_view_ms)

        # fallback if no nav layer / seabed: just Vw on full twt_view
        if self.nav_layer is None:
            return (twt_view / 1000.0) * float(self.vw) / 2.0

        seabed_field = self._reflector_field_name("Seabed")
        fidx = self.nav_layer.fields().indexOf(seabed_field)
        if fidx == -1:
            return (twt_view / 1000.0) * float(self.vw) / 2.0

        feat = self._get_feature_by_traceid(int(trace_index) + 1)
        if feat is None:
            return (twt_view / 1000.0) * float(self.vw) / 2.0

        t_sb_raw = self._qvariant_to_float_or_none(feat.attribute(fidx))
        if t_sb_raw is None or not np.isfinite(t_sb_raw):
            return (twt_view / 1000.0) * float(self.vw) / 2.0

        # convert seabed RAW -> seabed VIEW using SAME shift as display
        t_sb_view = float(self._raw_twt_to_view(trace_index, float(t_sb_raw)))

        if twt_view <= t_sb_view:
            return (twt_view / 1000.0) * float(self.vw) / 2.0

        depth_water = (t_sb_view / 1000.0) * float(self.vw) / 2.0
        depth_sed   = ((twt_view - t_sb_view) / 1000.0) * float(self.vs) / 2.0
        return depth_water + depth_sed


    def onMouseMoved(self, pos):
        if getattr(self, "_is_closing", False):
            return

        data_view = self._current_data()
        if data_view is None or self.img is None:
            return

        mp = self.pgview.plotItem.vb.mapSceneToView(pos)
        x = mp.x()
        y = mp.y()

        n_traces, n_samples = data_view.shape

        # --- if cursor outside image: clear ALL previews ---
        if x < 0 or x > n_traces - 1:
            self.hoverLine.hide()
            if self.map_marker is not None:
                self.map_marker.hide()
            if self.poly_preview is not None:
                self.poly_preview.setData([], [])
            self._clear_rect_preview("erase")
            self._clear_rect_preview("box")
            return

        trace_index = max(0, min(int(round(x)), n_traces - 1))
        if self.dt_ms is None or self.dt_ms <= 0:
            return

        # y in DISPLAY units (either TWT ms or Depth m)
        y_cursor_view_display = float(y)

        # Convert cursor to TWT_VIEW(ms) (for snapping + RAW conversion)
        if self.view_domain == "DEPTH":
            twt_ms_cursor_view = self._depth_m_to_twt_view_ms(y_cursor_view_display)
        else:
            twt_ms_cursor_view = y_cursor_view_display

        # Snap to VIEW sample grid using t0_view_ms (IMPORTANT)
        sample_float = (twt_ms_cursor_view - float(self.t0_view_ms)) / float(self.dt_ms)
        sample_index = int(round(sample_float))
        sample_index = max(0, min(sample_index, n_samples - 1))
        twt_ms_view = float(self.t0_view_ms) + sample_index * float(self.dt_ms)
        # Wiggle (RIGHT panel)
        if self.wiggleCurve is not None:
            trace_view = data_view[trace_index, :]
            if self.view_domain == "DEPTH":
                # y-axis in depth (m), using the same VIEW axis convention as the image
                y_axis = (((float(self.t0_view_ms) + np.arange(n_samples, dtype=float) * float(self.dt_ms)) / 1000.0)
                          * float(self.vs) / 2.0)
            else:
                y_axis = float(self.t0_view_ms) + np.arange(n_samples, dtype=float) * float(self.dt_ms)

            self.wiggleCurve.setData(trace_view, y_axis)

       
        # Display y for hover line (matches current display axis)
        y_hover_view = self._twt_view_ms_to_display_y(twt_ms_view)

        self.hoverLine.setPos(y_hover_view)

        self.hoverLine.show()

        try:
            depth_view_m = self._depth_from_twt_view_ms(trace_index, twt_ms_view)
        except Exception as err:
            depth_view_m = None


        dist_val = None
        if self.distance is not None and 0 <= trace_index < len(self.distance):
            dv = self.distance[trace_index]
            if dv == dv:
                dist_val = float(dv)

        text = f"Trace {trace_index}"
        if dist_val is not None:
            text += f" (Distance {dist_val:.2f} m)"
        if self.view_domain == "DEPTH":
            text += f"  Depth {y_hover_view:.2f} m"
            #if depth_m is not None:
            #    text += f" (RAW-depth {depth_m:.2f} m)"
        else:
            text += f"  TWT {twt_ms_view:.2f} ms"
            if depth_view_m is not None:
                text += f" (Depth {depth_view_m:.2f} m)"

        self.statusLabel.setText(text)

        # --- map marker ---
        if self.nav_x is not None and self.nav_y is not None:
            if trace_index != self.last_trace_index:
                self.last_trace_index = trace_index
                x_nav = self.nav_x[trace_index]
                y_nav = self.nav_y[trace_index]
                if np.isfinite(x_nav) and np.isfinite(y_nav):
                    self._ensure_map_marker()
                    self.map_marker.setCenter(QgsPointXY(x_nav, y_nav))
                    self.map_marker.show()
                else:
                    if self.map_marker is not None:
                        self.map_marker.hide()

        # Anchor stored in RAW, preview must be drawn in VIEW (TWT or Depth)
        mode = self.pick_settings.get("mode", "")
        if (
            self.pick_settings.get("enabled", False)
            and mode == "Manual (Polyline)"
            and self.poly_active
            and self.poly_anchor is not None
            and self.poly_preview is not None
        ):
            a_i, a_raw = self.poly_anchor
            b_i = int(trace_index)

            # cursor TWT_VIEW(ms) -> RAW
            b_raw = self._view_twt_to_raw(b_i, float(twt_ms_cursor_view))

            # snap in RAW (so stored values align to raw grid)
            if self.pick_settings.get("snap_to_sample", True):
                b_raw = self._snap_twt_to_grid(float(b_raw), n_samples)

            # RAW -> VIEW y for display (this must match current view domain)
            a_view = self._raw_twt_to_view_y(int(a_i), float(a_raw))
            b_view = self._raw_twt_to_view_y(int(b_i), float(b_raw))

            rgb = self._rgb_from_name((self.reflectors.get(self.pick_reflector, {}) or {}).get("color", "Red"))
            self.poly_preview.setPen(pg.mkPen(rgb, width=2, style=QtCore.Qt.DashLine))
            self.poly_preview.setData([a_i, b_i], [a_view, b_view])
        else:
            if self.poly_preview is not None:
                self.poly_preview.setData([], [])

        # Live rectangle preview (Eraser / Box) in DISPLAY coords (what user sees)
        # (anchors are stored in VIEW coords by onMouseClicked)
        if mode == "Eraser (Rect)" and self.erase_active and self.erase_anchor is not None:
            item = self._ensure_rect_preview("erase")
            a_i, a_y = self.erase_anchor
            x0 = float(min(a_i, trace_index))
            x1 = float(max(a_i, trace_index))
            y0 = float(min(a_y, y_cursor_view_display))
            y1 = float(max(a_y, y_cursor_view_display))
            self._set_rect_preview(item, x0, x1, y0, y1)
        else:
            self._clear_rect_preview("erase")

        if mode == "Box Tracker (Rect)" and self.box_active and self.box_anchor is not None:
            item = self._ensure_rect_preview("box")
            a_i, a_y = self.box_anchor
            x0 = float(min(a_i, trace_index))
            x1 = float(max(a_i, trace_index))
            y0 = float(min(a_y, y_cursor_view_display))
            y1 = float(max(a_y, y_cursor_view_display))
            self._set_rect_preview(item, x0, x1, y0, y1)
        else:
            self._clear_rect_preview("box")

    def onMouseClicked(self, event):
        if getattr(self, "_is_closing", False):
            return
        if event.button() == QtCore.Qt.RightButton:
            return
        if not self.pick_settings.get("enabled", False):
            return

        data_view = self._current_data()
        if data_view is None or self.img is None:
            return

        if self.nav_layer is None or not isinstance(self.nav_layer, QgsVectorLayer):
            QtWidgets.QMessageBox.warning(self, "Pick Error", "No nav layer found for this line.")
            return

        if self.dt_ms is None or self.dt_ms <= 0:
            QtWidgets.QMessageBox.warning(self, "Pick Error", "dt_ms missing. Cannot pick in TWT space.")
            return

        n_traces, n_samples = data_view.shape

        pos = event.scenePos()
        mp = self.pgview.plotItem.vb.mapSceneToView(pos)
        x = mp.x()
        y_display = float(mp.y())  # this is in CURRENT DISPLAY units (TWT ms or Depth m)

        if x < 0 or x > n_traces - 1:
            return

        trace_index = max(0, min(int(round(x)), n_traces - 1))
        mode = self.pick_settings.get("mode", "")

        # Helper: convert DISPLAY y -> TWT_VIEW(ms)
        def display_to_twt_view_ms(y_disp: float) -> float:
            return float(self._display_y_to_twt_view_ms(float(y_disp)))


        # - store anchors in DISPLAY units (so box matches what user sees)
        # - convert to TWT_VIEW(ms) only when running AutoTrack
        if mode == "Box Tracker (Rect)":
            if not self.box_active:
                self.box_active = True
                self.box_anchor = (int(trace_index), float(y_display))  # DISPLAY units
                self.statusLabel.setText("Box Tracker: first corner set. Move mouse, click second corner.")
                return

            a_i, a_y_disp = self.box_anchor
            b_i = int(trace_index)
            b_y_disp = float(y_display)

            x0 = int(min(a_i, b_i))
            x1 = int(max(a_i, b_i))
            y0_disp = float(min(a_y_disp, b_y_disp))
            y1_disp = float(max(a_y_disp, b_y_disp))

            self.box_active = False
            self.box_anchor = None
            self._clear_rect_preview("box")

            if x1 <= x0 or y1_disp <= y0_disp:
                self.statusLabel.setText("Box Tracker: invalid box (too small).")
                return

            # Convert DISPLAY y-range -> TWT_VIEW(ms) range for AutoTrack
            y0_ms = display_to_twt_view_ms(y0_disp)
            y1_ms = display_to_twt_view_ms(y1_disp)
            y0_ms, y1_ms = (min(y0_ms, y1_ms), max(y0_ms, y1_ms))

            params = {
                "strategy": self.pickDlg.cmbStrategy.currentText() if self.pickDlg else "Absolute peak",
                "zero_cross": self.pickDlg.chkZeroCross.isChecked() if self.pickDlg else False,
                "threshold_pct": float(self.pickDlg.spinThr.value()) if self.pickDlg else 10.0,
                "smooth_traces": int(self.pickDlg.spinSmooth.value()) if self.pickDlg else 0,
                "start_ms": float(y0_ms),                 # VIEW window start (ms)
                "window_ms": float(y1_ms - y0_ms),        # VIEW window length (ms)
                "interval_mode": "Custom",
                "custom_from": x0,
                "custom_to": x1,
            }
            self.statusLabel.setText(
                f"Box Tracker: AutoTrack traces {x0}..{x1}, "
                f"TWT {y0_ms:.2f}..{y1_ms:.2f} ms"
            )
            self.runAutoTrack(params)
            return

        # ERASE (Rect)
        # - delete stored RAW values (field is RAW ms)
        if mode == "Eraser (Rect)":
            if not self.erase_active:
                self.erase_active = True
                self.erase_anchor = (int(trace_index), float(y_display))  # DISPLAY units
                self.statusLabel.setText("Erase: first corner set. Move mouse, click second corner.")
                return

            a_i, a_y_disp = self.erase_anchor
            b_i = int(trace_index)
            b_y_disp = float(y_display)

            x0 = int(min(a_i, b_i))
            x1 = int(max(a_i, b_i))
            y0_disp = float(min(a_y_disp, b_y_disp))
            y1_disp = float(max(a_y_disp, b_y_disp))

            field_name = self._reflector_field_name(self.pick_reflector)
            fld_idx = self.nav_layer.fields().indexOf(field_name)
            if fld_idx == -1:
                self.erase_active = False
                self.erase_anchor = None
                self._clear_rect_preview("erase")
                self.statusLabel.setText(f"Erase: field '{field_name}' not found.")
                return

            if not self.nav_layer.isEditable():
                self.nav_layer.startEditing()

            erased = 0
            for xi in range(x0, x1 + 1):
                feat = self._get_feature_by_traceid(int(xi) + 1)
                if feat is None:
                    continue
                cur_raw = self._qvariant_to_float_or_none(feat.attribute(fld_idx))
                if cur_raw is None:
                    continue

                # RAW -> VIEW(ms)
                cur_view_ms = float(self._raw_twt_to_view(int(xi), float(cur_raw)))

                # VIEW(ms) -> DISPLAY units (Depth m if in depth mode)
                if self.view_domain == "DEPTH":
                    cur_disp = float(self._twt_view_ms_to_depth_m(cur_view_ms))
                else:
                    cur_disp = cur_view_ms

                if y0_disp <= cur_disp <= y1_disp:
                    if self.nav_layer.changeAttributeValue(feat.id(), fld_idx, None):
                        erased += 1

            if self.pick_settings.get("write_immediately", True):
                if not self.nav_layer.commitChanges():
                    self.nav_layer.rollBack()
                    QtWidgets.QMessageBox.warning(self, "Erase Error", "Commit failed (layer rolled back).")
                    return
                self.nav_layer.startEditing()

            self.erase_active = False
            self.erase_anchor = None
            self._clear_rect_preview("erase")
            self._overlay_cache_dirty = True
            self._refresh_all_reflector_overlays()
            self.statusLabel.setText(f"Erase: removed {erased} picks in rectangle.")
            return

        # - store RAW picks (ms, unshifted)
        # - supports DEPTH display by converting click -> TWT_VIEW(ms) first
        if mode != "Manual (Polyline)":
            return

        twt_view_ms = display_to_twt_view_ms(y_display)

        # Convert click VIEW(ms) -> RAW(ms) for storage
        twt_raw = float(self._view_twt_to_raw(trace_index, float(twt_view_ms)))

        # Snap in RAW space
        if self.pick_settings.get("snap_to_sample", True):
            twt_raw = float(self._snap_twt_to_grid(twt_raw, n_samples))

        field_name = self._reflector_field_name(self.pick_reflector)
        try:
            fld_idx = self._ensure_field(self.nav_layer, field_name)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Pick Error", str(e))
            return

        if event.double():
            self.poly_active = False
            self.poly_anchor = None
            if self.poly_preview is not None:
                self.poly_preview.setData([], [])
            self.statusLabel.setText("Polyline picking paused (double-click).")
            return

        if not self.nav_layer.isEditable():
            self.nav_layer.startEditing()

        def write_one(tidx, tms_raw):
            feat = self._get_feature_by_traceid(int(tidx) + 1)
            if feat is None:
                return False
            curv = self._qvariant_to_float_or_none(feat.attribute(fld_idx))
            if (curv is not None) and (not self.pick_settings.get("overwrite_trace", True)):
                return True
            return self.nav_layer.changeAttributeValue(feat.id(), fld_idx, float(tms_raw))

        # First click: write RAW + set anchor (RAW)
        if (not self.poly_active) or (self.poly_anchor is None):
            if not write_one(trace_index, twt_raw):
                self.nav_layer.rollBack()
                QtWidgets.QMessageBox.warning(self, "Pick Error", "Failed to write pick value.")
                return

            if self.pick_settings.get("write_immediately", True):
                if not self.nav_layer.commitChanges():
                    self.nav_layer.rollBack()
                    QtWidgets.QMessageBox.warning(self, "Pick Error", "Commit failed (layer rolled back).")
                    return
                self.nav_layer.startEditing()

            self.poly_active = True
            self.poly_anchor = (int(trace_index), float(twt_raw))
            self._overlay_cache_dirty = True
            self._refresh_all_reflector_overlays()
            self.statusLabel.setText(f"Polyline: anchor set at trace {trace_index}. Click next point to interpolate.")
            return

        # Second+ click: interpolate RAW between anchor and current
        a_i, a_raw = self.poly_anchor
        b_i = int(trace_index)
        b_raw = float(twt_raw)

        if b_i == int(a_i):
            # same trace: just write one
            if not write_one(b_i, b_raw):
                self.nav_layer.rollBack()
                QtWidgets.QMessageBox.warning(self, "Pick Error", "Failed to write pick value.")
                return
            if self.pick_settings.get("write_immediately", True):
                if not self.nav_layer.commitChanges():
                    self.nav_layer.rollBack()
                    QtWidgets.QMessageBox.warning(self, "Pick Error", "Commit failed (layer rolled back).")
                    return
                self.nav_layer.startEditing()

            self.poly_anchor = (int(b_i), float(b_raw))
            self._overlay_cache_dirty = True
            self._refresh_all_reflector_overlays()
            return

        i0 = int(min(a_i, b_i))
        i1 = int(max(a_i, b_i))
        y0 = float(a_raw if a_i <= b_i else b_raw)
        y1 = float(b_raw if a_i <= b_i else a_raw)

        # Linear interpolation in RAW space
        xs = np.arange(i0, i1 + 1, dtype=float)
        ys = y0 + (y1 - y0) * (xs - float(i0)) / float(max(1, (i1 - i0)))

        wrote = 0
        for xi, yr in zip(xs.astype(int), ys):
            if write_one(int(xi), float(yr)):
                wrote += 1

        if self.pick_settings.get("write_immediately", True):
            if not self.nav_layer.commitChanges():
                self.nav_layer.rollBack()
                QtWidgets.QMessageBox.warning(self, "Pick Error", "Commit failed (layer rolled back).")
                return
            self.nav_layer.startEditing()

        # new anchor at current click
        self.poly_anchor = (int(b_i), float(b_raw))
        self._overlay_cache_dirty = True
        self._refresh_all_reflector_overlays()
        self.statusLabel.setText(f"Polyline: wrote {wrote} picks (traces {i0}..{i1}).")


    def eventFilter(self, obj, event):
        if obj == self.pgview.viewport():
            if event.type() == QtCore.QEvent.Leave:
                self.hoverLine.hide()
                if self.map_marker is not None:
                    self.map_marker.hide()
                if self.poly_preview is not None:
                    self.poly_preview.setData([], [])
                self._clear_rect_preview("erase")
                self._clear_rect_preview("box")
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        self.cleanup()
        event.accept()
        super().closeEvent(event)


# Run viewer (close previous)
try:
    if 'win' in globals() and win is not None:
        win.close()
        win.deleteLater()
        win = None
except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
