from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsField,
    QgsProcessing,
    QgsVectorLayer,
    QgsFeature,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant
import numpy as np

class NaudyDreyerSpikeFilter(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    WIDTH = "WIDTH"
    TOLERANCE = "TOLERANCE"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.INPUT_FIELD,
            "Input Field (from 1st Layer)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterString(
            self.OUTPUT_FIELD,
            "Output Field Name",
            defaultValue="despiked"
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.WIDTH,
            "Width",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=3,
            minValue=1
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE,
            "Tolerance",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.5,
            minValue=0.0
        ))

    def name(self):
        return "naudy_dreyer_spike_filter_2"

    def displayName(self):
        return "Non Linier Filter"

    def group(self):
        return "Filters"

    def groupId(self):
        return "filters"


    def createInstance(self):
        return NaudyDreyerSpikeFilter()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        max_width = self.parameterAsInt(parameters, self.WIDTH, context)
        tolerance = self.parameterAsDouble(parameters, self.TOLERANCE, context)

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            # Add output field if missing
            if output_field not in [field.name() for field in layer.fields()]:
                layer.dataProvider().addAttributes([QgsField(output_field, QVariant.Double)])
                layer.updateFields()

            in_idx = layer.fields().indexFromName(input_field)
            out_idx = layer.fields().indexFromName(output_field)

            feats = list(layer.getFeatures())
            attribute_values = []
            for feature in feats:
                try:
                    attribute_values.append(float(feature[in_idx]) if feature[in_idx] is not None else np.nan)
                except Exception as e:
                    QgsMessageLog.logMessage(f"Failed to parse value for feature {feature.id()}: {e}", "MarineGeoTools", Qgis.Warning)
                    attribute_values.append(np.nan)
            values = np.array(attribute_values, dtype=float)
            filtered = values.copy()

            # Loop only over widths, but vectorize inside
            for width in range(max_width, 0, -1):
                feedback.pushInfo(f"Applying filter with width {width}...")
                y_a = np.roll(filtered, -2*width)
                y_b = np.roll(filtered, -width)
                y_c = filtered
                y_d = np.roll(filtered, width)
                y_e = np.roll(filtered, 2*width)

                # restrict to valid indices
                valid_mask = np.zeros(len(values), dtype=bool)
                valid_mask[2*width: len(values)-2*width] = True

                # compute S, skip near-zero
                s_val = y_c - (y_b + y_d) / 2.0
                nonzero_mask = ~np.isclose(s_val, 0.0)

                # compute T, R
                y_avg = (y_a + y_e) / 2.0
                t_val = (y_b - y_avg) + (y_c - y_avg) + (y_d - y_avg)
                r_ratio = np.round(t_val / s_val, 1)

                # condition masks
                cond_valid = valid_mask & nonzero_mask
                cond_r_pos = cond_valid & (r_ratio >= 0) & (r_ratio < 2)
                cond_r_neg = cond_valid & (r_ratio < 0) & (r_ratio > -2)

                # candidate replacements
                yq_pos = (2/3.0) * (y_b + y_d) - (1/6.0) * (y_a + y_e)
                yq_neg = 0.5 * y_c + 0.25 * (y_b + y_d)

                # apply tolerance check
                diff_pos = np.abs(yq_pos - values)
                diff_neg = np.abs(yq_neg - values)

                update_pos = cond_r_pos & (diff_pos >= tolerance)
                update_neg = cond_r_neg & (diff_neg >= tolerance)

                filtered[update_pos] = yq_pos[update_pos]
                filtered[update_neg] = yq_neg[update_neg]

            # push back to QGIS
            updates = {feature.id(): {out_idx: float(filtered[i])} for i, feature in enumerate(feats)}
            layer.dataProvider().changeAttributeValues(updates)
            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}

