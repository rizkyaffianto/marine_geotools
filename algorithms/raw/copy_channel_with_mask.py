from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsField,
    QgsProcessing,
)
from qgis.PyQt.QtCore import QVariant
import numpy as np
import math


class CopyFieldWithMask(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FROM_FIELD = "FROM_FIELD"
    TO_FIELD = "TO_FIELD"
    MASK_FIELD = "MASK_FIELD"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FROM_FIELD,
            "Copy From Field",
            parentLayerParameterName=self.INPUT_LAYERS
        ))

        self.addParameter(QgsProcessingParameterString(
            self.TO_FIELD,
            "To",
            defaultValue="copied"
        ))

        self.addParameter(QgsProcessingParameterField(
            self.MASK_FIELD,
            "Mask Field",
            parentLayerParameterName=self.INPUT_LAYERS
        ))

    def name(self):
        return "copy_field_with_mask"

    def displayName(self):
        return "Copy Field With Mask"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return CopyFieldWithMask()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        from_field = self.parameterAsString(parameters, self.FROM_FIELD, context)
        to_field = self.parameterAsString(parameters, self.TO_FIELD, context)
        mask_field = self.parameterAsString(parameters, self.MASK_FIELD, context)

        for layer_index, layer in enumerate(layers):
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            source_field = layer.fields().field(from_field)
            if to_field not in [f.name() for f in layer.fields()]:
                new_field = QgsField(
                    to_field,
                    source_field.type(),
                    source_field.typeName(),
                    source_field.length(),
                    source_field.precision()
                )
                layer.dataProvider().addAttributes([new_field])
                layer.updateFields()

            from_idx = layer.fields().indexFromName(from_field)
            to_idx = layer.fields().indexFromName(to_field)
            mask_idx = layer.fields().indexFromName(mask_field)

            features = list(layer.getFeatures())
            total = len(features)

            fids = np.array([feature.id() for feature in features])
            from_vals = np.array([feature[from_idx] for feature in features], dtype=object)
            mask_vals = np.array([feature[mask_idx] for feature in features], dtype=object)

            # Apply mask: if mask is NULL/empty → None, else copy from_field
            mask_nulls = np.array([
                (val is None) or (isinstance(val, QVariant) and val.isNull()) or
                (isinstance(val, str) and val.strip() == '') or
                (isinstance(val, float) and math.isnan(val))
                for val in mask_vals
            ])

            result_vals = np.where(mask_nulls, None, from_vals)

            updates = {int(fid): {to_idx: val} for fid, val in zip(fids, result_vals)}

            layer.dataProvider().changeAttributeValues(updates)
            layer.updateFields()

            feedback.setProgress(100)
            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
