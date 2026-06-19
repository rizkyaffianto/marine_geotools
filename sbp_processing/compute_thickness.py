# -*- coding: utf-8 -*-

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsField,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant


class SBPComputeThicknessFromTwoDepthFieldsMulti(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    TOP_DEPTH_FIELD = "TOP_DEPTH_FIELD"
    BOTTOM_DEPTH_FIELD = "BOTTOM_DEPTH_FIELD"
    OUT_FIELD = "OUT_FIELD"
    CREATE_NEW = "CREATE_NEW"
    SKIP_NEGATIVE = "SKIP_NEGATIVE"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input SBP layers",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )

        # No defaults: user chooses
        self.addParameter(
            QgsProcessingParameterField(
                self.TOP_DEPTH_FIELD,
                "Top depth field (m)",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Any
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.BOTTOM_DEPTH_FIELD,
                "Bottom depth field (m)",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Any
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUT_FIELD,
                "Output thickness field name (m)"
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CREATE_NEW,
                "Create new field (unchecked = overwrite if exists)",
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SKIP_NEGATIVE,
                "Skip if (bottom - top) < 0",
                defaultValue=True
            )
        )

    def name(self):
        return "sbp_thickness_from_depth_fields_multi"

    def displayName(self):
        return "Calculate Thickness"

    def group(self):
        return "SBP - Depth and Thickness"

    def groupId(self):
        return "sbp_depth"

    def createInstance(self):
        return SBPComputeThicknessFromTwoDepthFieldsMulti()

    def _to_float(self, value):
        try:
            parsed_float = float(value)
            return parsed_float if parsed_float == parsed_float else None
        except (ValueError, TypeError):
            return None

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        if not layers:
            raise QgsProcessingException("No input layers provided.")

        top_field = self.parameterAsString(parameters, self.TOP_DEPTH_FIELD, context)
        bot_field = self.parameterAsString(parameters, self.BOTTOM_DEPTH_FIELD, context)
        out_field = self.parameterAsString(parameters, self.OUT_FIELD, context)
        create_new = self.parameterAsBool(parameters, self.CREATE_NEW, context)
        skip_negative = self.parameterAsBool(parameters, self.SKIP_NEGATIVE, context)

        if not top_field or not bot_field:
            raise QgsProcessingException("Top and Bottom depth fields are required.")
        if not out_field:
            raise QgsProcessingException("Output field name is required.")

        total_layers = len(layers)

        for layer_index, layer in enumerate(layers, start=1):
            if feedback.isCanceled():
                break

            if layer is None:
                continue

            idx_top = layer.fields().indexOf(top_field)
            idx_bot = layer.fields().indexOf(bot_field)

            if idx_top == -1 or idx_bot == -1:
                feedback.reportError(
                    f"[{layer_index}/{total_layers}] {layer.name()}: Missing fields "
                    f"('{top_field}' and/or '{bot_field}'). Skipping."
                )
                continue

            data_provider = layer.dataProvider()
            names = [field.name() for field in layer.fields()]

            if out_field not in names:
                data_provider.addAttributes([QgsField(out_field, QVariant.Double)])
                layer.updateFields()

            idx_out = layer.fields().indexFromName(out_field)
            if idx_out == -1:
                feedback.reportError(f"{layer.name()}: Failed to create/find output field '{out_field}'.")
                continue

            feedback.pushInfo(f"[{layer_index}/{total_layers}] Computing thickness on {layer.name()}")

            updates = {}
            request = QgsFeatureRequest().setSubsetOfAttributes([top_field, bot_field], layer.fields())

            feature_count = layer.featureCount() or 1
            for feature_index, feature in enumerate(layer.getFeatures(request), start=1):
                if feedback.isCanceled():
                    break
                if feature_index % 2000 == 0:
                    feedback.setProgress(int(100.0 * feature_index / feature_count))

                z_top = self._to_float(feature.attribute(idx_top))
                z_bot = self._to_float(feature.attribute(idx_bot))
                if z_top is None or z_bot is None:
                    continue

                thickness = z_bot - z_top
                if skip_negative and thickness < 0:
                    continue

                updates[feature.id()] = {idx_out: thickness}

            if updates:
                data_provider.changeAttributeValues(updates)

            feedback.pushInfo(f"Done: {layer.name()} (updated {len(updates)} features)")

        return {}
