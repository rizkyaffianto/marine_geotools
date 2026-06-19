# -*- coding: utf-8 -*-

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsVectorLayer,
    QgsField,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant


class SBPVerticalConstantOffsetMeters(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    OUT_FIELD = "OUT_FIELD"
    OFFSET_M = "OFFSET_M"
    VW = "VW"
    MODE = "MODE"

    MODE_REPLACE = 0
    MODE_ADD = 1

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input SBP vector layers (nav/picks)",
                layerType=QgsProcessing.TypeVector
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUT_FIELD,
                "Output shift field name (ms)",
                defaultValue="dt_shift_ms"
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.OFFSET_M,
                "Constant vertical offset (meters)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.VW,
                "Water sound velocity vw (m/s)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1500.0,
                minValue=500.0,
                maxValue=5000.0
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.MODE,
                "Write mode",
                options=["Replace (set constant value)", "Add (existing + constant)"],
                defaultValue=self.MODE_REPLACE
            )
        )

    def name(self):
        return "sbp_constant_vertical_offset_meters"

    def displayName(self):
        return "Constant Offset"

    def group(self):
        return "SBP - Vertical Shift"

    def groupId(self):
        return "vertical_shift"

    def createInstance(self):
        return SBPVerticalConstantOffsetMeters()

    def _to_float_or_none(self, value):
        try:
            if value is None:
                return None
            if hasattr(value, "isNull") and value.isNull():
                return None
            parsed_float = float(value)
            if parsed_float != parsed_float:
                return None
            return parsed_float
        except (ValueError, TypeError):
            return None

    def _ensure_field(self, layer, field_name):
        idx = layer.fields().indexOf(field_name)
        if idx != -1:
            return idx

        ok = layer.dataProvider().addAttributes([QgsField(field_name, QVariant.Double)])
        layer.updateFields()
        if not ok:
            raise QgsProcessingException(
                "Failed to add field '{}' to layer '{}'".format(field_name, layer.name())
            )

        idx = layer.fields().indexOf(field_name)
        if idx == -1:
            raise QgsProcessingException(
                "Field '{}' still not found after add in '{}'".format(field_name, layer.name())
            )
        return idx

    def _flush_updates(self, layer, updates, feedback):
        if not updates:
            return True
        ok = layer.dataProvider().changeAttributeValues(updates)
        if not ok:
            feedback.reportError("  - Bulk changeAttributeValues failed.")
            return False
        updates.clear()
        return True

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        out_field = (self.parameterAsString(parameters, self.OUT_FIELD, context) or "dt_const_ms").strip()

        offset_meters = float(self.parameterAsDouble(parameters, self.OFFSET_M, context))
        velocity_water = float(self.parameterAsDouble(parameters, self.VW, context))
        mode = int(self.parameterAsEnum(parameters, self.MODE, context))

        if not layers:
            raise QgsProcessingException("No input layers provided.")
        if not out_field:
            raise QgsProcessingException("Output field name is empty.")
        if velocity_water <= 0:
            raise QgsProcessingException("vw must be > 0.")

        delta_time_constant_ms = (2.0 * offset_meters / velocity_water) * 1000.0

        CHUNK_SIZE = 5000
        total_layers = len(layers) or 1

        for layer_index, layer in enumerate(layers):
            if feedback.isCanceled():
                break
            if not isinstance(layer, QgsVectorLayer):
                continue

            feedback.pushInfo("[{}/{}] Layer: {}".format(layer_index + 1, total_layers, layer.name()))
            feedback.pushInfo("  - Constant offset: {:.4f} m -> {:.4f} ms (vw={:.1f} m/s)".format(
                offset_meters, delta_time_constant_ms, velocity_water
            ))

            out_idx = self._ensure_field(layer, out_field)

            if not layer.isEditable():
                layer.startEditing()

            feature_count = layer.featureCount() or 0
            if feature_count == 0:
                layer.commitChanges()
                layer.startEditing()
                feedback.pushInfo("  - No features.")
                continue

            step = max(1, feature_count // 100)
            updates = {}
            written_count = 0

            request = QgsFeatureRequest()
            if mode == self.MODE_ADD:
                request.setSubsetOfAttributes([out_field], layer.fields())
            else:
                request.setSubsetOfAttributes([])

            for feature_index, feature in enumerate(layer.getFeatures(request)):
                if feedback.isCanceled():
                    break
                if (feature_index % step) == 0:
                    feedback.setProgress(int(100 * feature_index / float(max(1, feature_count))))

                if mode == self.MODE_REPLACE:
                    new_value = delta_time_constant_ms
                else:
                    current_value = self._to_float_or_none(feature.attribute(out_idx))
                    new_value = (current_value if current_value is not None else 0.0) + delta_time_constant_ms

                updates[feature.id()] = {out_idx: float(new_value)}
                written_count += 1

                if len(updates) >= CHUNK_SIZE:
                    if not self._flush_updates(layer, updates, feedback):
                        layer.rollBack()
                        feedback.reportError("  - Rolled back due to update failure.")
                        break

            if updates:
                if not self._flush_updates(layer, updates, feedback):
                    layer.rollBack()
                    feedback.reportError("  - Rolled back due to update failure.")
                    continue

            if not layer.commitChanges():
                layer.rollBack()
                feedback.reportError("  - Commit failed (rolled back).")
                continue

            layer.startEditing()
            feedback.pushInfo("  - Wrote {} / {}".format(written_count, feature_count))

        return {}
