from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsField,
    QgsProcessing,
    QgsVectorLayer,
    QgsFeature,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant
import numpy as np
 

def padding(arr, pad_width):
    arr = np.asarray(arr)
    left_slope = arr[1] - arr[0]
    right_slope = arr[-1] - arr[-2]

    left_pad = [arr[0] - left_slope * i for i in range(pad_width, 0, -1)]
    right_pad = [arr[-1] + right_slope * i for i in range(1, pad_width + 1)]

    return np.array(left_pad + arr.tolist() + right_pad)


def convolve_padding(data, kernel):
    data = np.asarray(data)
    kernel = np.asarray(kernel)

    if len(kernel) % 2 == 0:
        raise ValueError("Only odd-length kernels are supported for symmetric convolution.")

    pad_width = (len(kernel) - 1) // 2
    padded_data = padding(data, pad_width)
    return np.convolve(padded_data, kernel, mode='valid')


class FieldConvolution1D(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    KERNEL = "KERNEL"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.INPUT_FIELD,
            "Input Field",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterString(
            self.OUTPUT_FIELD,
            "Output Field Name",
            defaultValue="convolved"
        ))

        self.addParameter(QgsProcessingParameterString(
            self.KERNEL,
            "Kernel)",
            defaultValue="1,1,1"
        ))

    def name(self):
        return "field_convolution_1d"

    def displayName(self):
        return "Convolution"

    def group(self):
        return "Filters"

    def groupId(self):
        return "filters"

    def createInstance(self):
        return FieldConvolution1D()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        kernel_str = self.parameterAsString(parameters, self.KERNEL, context)

        # Parse kernel input
        try:
            kernel = np.array([float(k_val.strip()) for k_val in kernel_str.split(",")])
        except Exception as e:
            QgsMessageLog.logMessage(f"Invalid kernel input: {e}", "marine_geotools", Qgis.Critical)
            raise ValueError(f"Invalid kernel input: {e}")

        for layer_index, layer in enumerate(layers):
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            if output_field not in [f.name() for f in layer.fields()]:
                new_field = QgsField(output_field, QVariant.Double, 'double')
                layer.dataProvider().addAttributes([new_field])
                layer.updateFields()

            input_idx = layer.fields().indexFromName(input_field)
            output_idx = layer.fields().indexFromName(output_field)

            features = list(layer.getFeatures())
            ids = [feature.id() for feature in features]
            values = [feature[input_idx] for feature in features]

            valid_values = [float(val) if val is not None else np.nan for val in values]
            if any(np.isnan(valid_values)):
                warning_msg = "Missing values detected; they will be interpolated before convolution."
                feedback.reportError(warning_msg)
                QgsMessageLog.logMessage(warning_msg, "marine_geotools", Qgis.Warning)

            clean_values = np.array(valid_values)
            mask = np.isnan(clean_values)
            if np.any(mask):
                clean_values[mask] = np.interp(np.flatnonzero(mask), np.flatnonzero(~mask), clean_values[~mask])

            convolved = convolve_padding(clean_values, kernel)

            updates = {fid: {output_idx: float(val)} for fid, val in zip(ids, convolved)}
            layer.dataProvider().changeAttributeValues(updates)
            layer.updateFields()
            feedback.setProgress(100)
            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
