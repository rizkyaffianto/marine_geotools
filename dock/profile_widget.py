# -*- coding: utf-8 -*-

from qgis.PyQt import QtWidgets, QtCore
from qgis.PyQt.QtCore import QVariant

import pyqtgraph as pg
import sip
from qgis.core import QgsProject, QgsMapLayer, QgsWkbTypes



def edit(layer):
    """Tiny context manager for layer editing (start/commit/rollback)."""

    class LayerEditor:
        def __enter__(self_inner):
            layer.startEditing()
            return layer

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            if exc_type is None:
                layer.commitChanges()
            else:
                layer.rollback()

    return LayerEditor()


class MagProfileWidget(QtWidgets.QWidget):
    """
    Dock-friendly version of NTProfilePlot v14 (5 profiles).
    """

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self._iface = iface

        self.freeze_layer = False
        self.plot_point_mode = False
        self.layer = iface.activeLayer()

        self.field_x = None
        self.mask_field = None
        self.y_field_selections = [[] for _ in range(5)]
        self.plot_style_map = [dict() for _ in range(5)]
        self.multi_axis_enabled = [False] * 5
        self.data_points = []
        self.selection_start_x = None
        self.selection_end_x = None
        self.last_clicked_fid = None

        # Header Controls
        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("X Axis:"))
        self.x_combo = QtWidgets.QComboBox()
        header.addWidget(self.x_combo)

        header.addWidget(QtWidgets.QLabel("Mask Field:"))
        self.mask_combo = QtWidgets.QComboBox()
        header.addWidget(self.mask_combo)

        header.addWidget(QtWidgets.QLabel("Value:"))
        self.value_edit = QtWidgets.QLineEdit()
        self.value_edit.setPlaceholderText("Enter number and press Enter (or Space to set/NULL)")
        self.value_edit.setFixedWidth(220)
        header.addWidget(self.value_edit)

        header.addStretch()
        self.freeze_checkbox = QtWidgets.QCheckBox("Freeze layer")
        self.freeze_checkbox.setToolTip("If checked, graph layer will not change when active layer changes")
        header.addWidget(self.freeze_checkbox)

        self.pointmode_checkbox = QtWidgets.QCheckBox("Plot Points")
        self.pointmode_checkbox.setToolTip("Applies to next field selection (Line if unchecked, Point if checked)")
        header.addWidget(self.pointmode_checkbox)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.addLayout(header)

        # Signals
        self.x_combo.currentIndexChanged.connect(self.x_field_changed)
        self.mask_combo.currentIndexChanged.connect(self.mask_field_changed)
        self.freeze_checkbox.stateChanged.connect(self.toggle_freeze_layer)
        self.pointmode_checkbox.stateChanged.connect(self.toggle_point_mode)
        self.value_edit.returnPressed.connect(self.apply_value_to_selected)

        # Plot Area
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.plotWidgets, self.y_lists, self.vLines, self.regions = [], [], [], []

        for i in range(5):
            container = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(container)
            row_layout.setContentsMargins(0, 0, 0, 0)

            # Field list
            y_list = QtWidgets.QListWidget()
            y_list.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
            y_list.setFixedWidth(220)
            y_list.setFixedHeight(110)
            y_list.itemSelectionChanged.connect(lambda i=i: self.field_selection_changed(i))
            self.y_lists.append(y_list)
            row_layout.addWidget(y_list)

            # Plot column
            plot_column = QtWidgets.QVBoxLayout()
            plot_column.setContentsMargins(0, 0, 0, 0)

            title = QtWidgets.QLabel(f"<b>Profile {i + 1}</b>")
            title.setAlignment(QtCore.Qt.AlignLeft)
            plot_column.addWidget(title)

            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True)
            plot.addLegend()
            plot_column.addWidget(plot)
            self.plotWidgets.append(plot)

            vb = plot.getPlotItem().getViewBox()
            menu = vb.menu
            action = QtWidgets.QAction("Toggle Multiple Axis Mode", self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, idx=i: self.toggle_multiple_axis(idx, checked))
            menu.insertAction(menu.actions()[-1], action)
            menu.insertSeparator(menu.actions()[-1])
            if not hasattr(self, "multi_axis_actions"):
                self.multi_axis_actions = []
            self.multi_axis_actions.append(action)

            # Vertical line + region
            vline = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen("r", width=1, style=QtCore.Qt.DashLine))
            region = pg.LinearRegionItem()
            region.setBrush(pg.mkBrush(255, 0, 0, 50))
            region.setZValue(10)
            vline.hide()
            region.hide()
            plot.addItem(vline)
            plot.addItem(region)
            self.vLines.append(vline)
            self.regions.append(region)

            row_layout.addLayout(plot_column)
            self.splitter.addWidget(container)

        for i in range(1, 5):
            self.plotWidgets[i].setXLink(self.plotWidgets[0])

        self.main_layout.addWidget(self.splitter)
        self.setLayout(self.main_layout)

        # Events
        for i, plot in enumerate(self.plotWidgets):
            plot.scene().sigMouseClicked.connect(lambda e, i=i: self.point_clicked(e, i))
        for region in self.regions:
            region.sigRegionChanged.connect(self.region_changed)

        # QGIS signals
        self._iface.currentLayerChanged.connect(self.layer_changed)
        if self.layer:
            self._try_connect_layer_signals(self.layer)

        self.installEventFilter(self)
        self.refresh_fields()
        self.refresh_plot_all()

    # Cleanup
    def _layer_ok(self, layer):
        if layer is None:
            return False
        try:
            if sip.isdeleted(layer):
                return False
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        try:
            return layer.isValid()
        except Exception as err:
            return False


    def _try_disconnect_layer_signals(self, layer):
        try:
            layer.attributeValueChanged.disconnect(self.refresh_plot_all)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

    def _try_connect_layer_signals(self, layer):
        try:
            layer.attributeValueChanged.connect(self.refresh_plot_all)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

    def closeEvent(self, event):
        try:
            self._iface.currentLayerChanged.disconnect(self.layer_changed)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)
        if self._layer_ok(self.layer):
            self._try_disconnect_layer_signals(self.layer)
        event.accept()

    # Field logic
    def toggle_freeze_layer(self, state):
        self.freeze_layer = (state == QtCore.Qt.Checked)
        if not self.freeze_layer:
            current = self._iface.activeLayer()
            if current != self.layer:
                self.layer_changed(current)

    def toggle_point_mode(self, state):
        self.plot_point_mode = (state == QtCore.Qt.Checked)

    def toggle_multiple_axis(self, plot_index, enabled):
        self.multi_axis_enabled[plot_index] = enabled
        self.multi_axis_actions[plot_index].setChecked(enabled)
        axis = self.plotWidgets[plot_index].getPlotItem().getAxis("left")
        axis.setLabel("***" if enabled else "")
        self.refresh_plot_all()

    def refresh_fields(self):
        prev_x = self.x_combo.currentText() if self.x_combo.count() else None
        prev_mask = self.mask_combo.currentText() if self.mask_combo.count() else None

        self.x_combo.blockSignals(True)
        self.mask_combo.blockSignals(True)
        self.x_combo.clear()
        self.mask_combo.clear()

        for y_list in self.y_lists:
            y_list.blockSignals(True)
            y_list.clear()

        if not self._layer_ok(self.layer):
            self.x_combo.blockSignals(False)
            self.mask_combo.blockSignals(False)
            for y_list in self.y_lists:
                y_list.blockSignals(False)
            return

        numeric_fields = [f.name() for f in self.layer.fields() if f.type() in (QVariant.Int, QVariant.Double)]
        self.x_combo.addItem("FID")
        self.x_combo.addItems(numeric_fields)
        self.mask_combo.addItems(numeric_fields)

        for y_list in self.y_lists:
            for f in numeric_fields:
                y_list.addItem(f)
            y_list.blockSignals(False)

        if prev_x and self.x_combo.findText(prev_x) >= 0:
            self.x_combo.setCurrentText(prev_x)
        if prev_mask and self.mask_combo.findText(prev_mask) >= 0:
            self.mask_combo.setCurrentText(prev_mask)

        self.x_combo.blockSignals(False)
        self.mask_combo.blockSignals(False)

    def x_field_changed(self):
        text = self.x_combo.currentText()
        self.field_x = None if text == "FID" else text
        self.refresh_plot_all()

    def mask_field_changed(self):
        self.mask_field = self.mask_combo.currentText()

    def field_selection_changed(self, i):
        selected_fields = [it.text() for it in self.y_lists[i].selectedItems()]
        self.y_field_selections[i] = selected_fields

        for f in selected_fields:
            self.plot_style_map[i][f] = "Point" if self.plot_point_mode else "Line"
        for f in list(self.plot_style_map[i].keys()):
            if f not in selected_fields:
                del self.plot_style_map[i][f]
        self.refresh_plot_all()

    def layer_changed(self, new_layer):
        if self.freeze_layer:
            return

        if self._layer_ok(self.layer):
            self._try_disconnect_layer_signals(self.layer)

        # If new layer is not valid or not vector -> clear and stop
        if not self._layer_ok(new_layer) or new_layer.type() != QgsMapLayer.VectorLayer:
            self.layer = None
            self.refresh_fields()
            self.refresh_plot_all()
            return

        # If not point geometry -> clear and stop (prevents your "not point layer" crash path)
        if new_layer.geometryType() != QgsWkbTypes.PointGeometry:
            self.layer = None
            self.refresh_fields()
            self.refresh_plot_all()
            return

        self.layer = new_layer
        self._try_connect_layer_signals(self.layer)

        self.refresh_fields()
        self.refresh_plot_all()

    # Plot logic
    def refresh_plot_all(self):
        if not self._layer_ok(self.layer):
            return

        self.data_points.clear()
        feats = list(self.layer.getFeatures())

        for i in range(5):
            plot = self.plotWidgets[i]
            plot.clear()
            plot.addItem(self.vLines[i])
            plot.addItem(self.regions[i])

            # Legend reset
            if plot.getPlotItem().legend is None:
                plot.addLegend()
            else:
                plot.getPlotItem().legend.clear()

            # Remove extra viewboxes left by multi-axis mode
            for item in list(plot.scene().items()):
                if isinstance(item, pg.ViewBox) and item is not plot.getPlotItem().getViewBox():
                    try:
                        plot.scene().removeItem(item)
                    except Exception as e:
                        QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

            y_fields = self.y_field_selections[i]
            if not y_fields:
                continue

            colors = ["b", "g", "r", "m", "c", "y", "k"]
            color_cycle = iter(colors * 3)
            base_item = plot.getPlotItem()
            base_vb = base_item.getViewBox()

            def iter_xy(field_name):
                x_vals, y_vals = [], []
                for feat in feats:
                    try:
                        xv = feat[self.field_x] if self.field_x else feat.id()
                        yv = feat[field_name]
                        if yv is None or (isinstance(yv, QVariant) and yv.isNull()):
                            continue
                        xv = float(xv)
                        yv = float(yv)
                        x_vals.append(xv)
                        y_vals.append(yv)
                        self.data_points.append({"x": xv, "y": yv, "fid": feat.id()})
                    except Exception as err:
                        continue
                return x_vals, y_vals

            if self.multi_axis_enabled[i]:
                base_item.getAxis("left").setTicks([[(0, "***")]])
                for y_field in y_fields:
                    mode = self.plot_style_map[i].get(y_field, "Line")
                    color = next(color_cycle)
                    pen = pg.mkPen(color, width=2)
                    x_vals, y_vals = iter_xy(y_field)
                    if not x_vals:
                        continue

                    vb = pg.ViewBox()
                    plot.scene().addItem(vb)
                    vb.setXLink(plot)
                    vb.setGeometry(base_vb.sceneBoundingRect())

                    curve = pg.PlotCurveItem(
                        x_vals, y_vals, pen=pen,
                        symbol="o" if mode == "Point" else None,
                        symbolSize=5 if mode == "Point" else 0,
                        symbolBrush=pen.color()
                    )
                    vb.addItem(curve)
                    base_item.legend.addItem(curve, y_field)
                    base_vb.sigResized.connect(lambda mvb=base_vb, v=vb, yy=y_vals: self.link_viewbox_y(mvb, v, yy))
            else:
                base_item.getAxis("left").setTicks(None)
                for y_field in y_fields:
                    mode = self.plot_style_map[i].get(y_field, "Line")
                    color = next(color_cycle)
                    pen = pg.mkPen(color, width=2)
                    x_vals, y_vals = iter_xy(y_field)
                    if not x_vals:
                        continue
                    plot.plot(
                        x_vals, y_vals,
                        pen=pen if mode == "Line" else None,
                        name=y_field,
                        symbol="o" if mode == "Point" else None,
                        symbolSize=5,
                        symbolBrush=pen.color()
                    )

            self.vLines[i].hide()
            self.regions[i].hide()

    def link_viewbox_y(self, main_view, vb, y_vals):
        vb.setGeometry(main_view.sceneBoundingRect())
        if y_vals:
            vb.setYRange(min(y_vals), max(y_vals))

    # Selection, editing, shortcuts
    def point_clicked(self, event, i):
        if not self.data_points or not self._layer_ok(self.layer):
            return

        click_pos = event.scenePos()
        mouse_point = self.plotWidgets[i].plotItem.vb.mapSceneToView(click_pos)
        click_x = mouse_point.x()

        closest = min(self.data_points, key=lambda pt: abs(pt["x"] - click_x))
        nearest_x = closest["x"]
        self.last_clicked_fid = closest["fid"]

        if event.button() == QtCore.Qt.LeftButton:
            if event.modifiers() & QtCore.Qt.ShiftModifier:
                self.selection_end_x = nearest_x
                if self.selection_start_x is not None:
                    lo, hi = sorted([self.selection_start_x, self.selection_end_x])
                    for region in self.regions:
                        region.setRegion((lo, hi))
                        region.show()
                    fids = [pt["fid"] for pt in self.data_points if lo <= pt["x"] <= hi]
                    self.layer.selectByIds(list(dict.fromkeys(fids)))
            else:
                self.selection_start_x = nearest_x
                self.selection_end_x = None
                self.layer.selectByIds([closest["fid"]])
                for v in self.vLines:
                    v.setValue(nearest_x)
                    v.show()
                for r in self.regions:
                    r.hide()

    def region_changed(self):
        if not self.data_points or not self._layer_ok(self.layer):
            return

        lo, hi = self.regions[0].getRegion()
        fids = [pt["fid"] for pt in self.data_points if lo <= pt["x"] <= hi]
        self.layer.selectByIds(list(dict.fromkeys(fids)))

    def apply_value_to_selected(self):
        if not self.mask_field or not self.layer:
            return
        text_val = self.value_edit.text().strip()
        if not text_val:
            return
        try:
            value = float(text_val)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Invalid Value", "Please enter a valid number.")
            return
        selected = self.layer.selectedFeatureIds()
        if not selected:
            return

        mask_idx = self.layer.fields().indexOf(self.mask_field)
        if mask_idx < 0:
            return

        updates = {fid: {mask_idx: value} for fid in selected}
        self.layer.blockSignals(True)
        try:
            with edit(self.layer):
                self.layer.dataProvider().changeAttributeValues(updates)
        finally:
            self.layer.blockSignals(False)
        self.refresh_plot_all()
        self.value_edit.clear()

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress:
            key = event.key()

            if key == QtCore.Qt.Key_Space:
                if not self.mask_field or not self.layer:
                    return False
                text_val = self.value_edit.text().strip()
                selected = self.layer.selectedFeatureIds()
                if not selected:
                    return False
                mask_idx = self.layer.fields().indexOf(self.mask_field)
                if mask_idx < 0:
                    return False

                if text_val:
                    try:
                        value = float(text_val)
                        updates = {fid: {mask_idx: value} for fid in selected}
                    except ValueError:
                        QtWidgets.QMessageBox.warning(self, "Invalid Value", "Please enter a valid numeric value.")
                        return True
                else:
                    updates = {fid: {mask_idx: None} for fid in selected}

                self.layer.blockSignals(True)
                try:
                    with edit(self.layer):
                        self.layer.dataProvider().changeAttributeValues(updates)
                finally:
                    self.layer.blockSignals(False)
                self.refresh_plot_all()
                return True

            # Ctrl+J = zoom to selected
            if (key == QtCore.Qt.Key_J and (event.modifiers() & QtCore.Qt.ControlModifier)):
                if self.layer and self.layer.selectedFeatureIds():
                    self._iface.mapCanvas().zoomToSelected(self.layer)
                return True

            # F5 = refresh
            if key == QtCore.Qt.Key_F5:
                self.refresh_plot_all()
                return True

            # Digits = focus value_edit
            if QtCore.Qt.Key_0 <= key <= QtCore.Qt.Key_9:
                if not self.value_edit.hasFocus():
                    self.value_edit.setFocus()
                    self.value_edit.clear()
                self.value_edit.setText(self.value_edit.text() + event.text())
                self.value_edit.setCursorPosition(len(self.value_edit.text()))
                return True

        return False
