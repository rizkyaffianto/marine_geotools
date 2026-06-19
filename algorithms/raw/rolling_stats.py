from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsField,
    QgsFeature
)
from qgis.PyQt.QtCore import QVariant
import numpy as np


def rolling_statistic(values, window, method="mean"):
    """
    Fast rolling mean/median with edge padding.
    values : 1D numpy array
    window : int, size of rolling window
    method : 'mean' or 'median'
    """
    n = len(values)
    half_w = window // 2

    # Pad edges with nearest values so edges are also calculated
    padded = np.pad(values, pad_width=half_w, mode="edge")

    # Build strided sliding windows
    shape = (n, window)
    strides = (padded.strides[0], padded.strides[0])
    windows = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)

    if method == "mean":
        return windows.mean(axis=1)
    elif method == "median":
        return np.median(windows, axis=1)
    else:
        raise ValueError("method must be 'mean' or 'median'")


class RollingStatistic(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    WINDOW = "WINDOW"
    METHOD = "METHOD"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input Layers",
                layerType=QgsProcessing.TypeVectorAnyGeometry
            )
        )

        # Field to smooth
        self.addParameter(
            QgsProcessingParameterField(
                self.INPUT_FIELD,
                "Field",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Numeric
            )
        )

        # Output field
        self.addParameter(
            QgsProcessingParameterString(
                self.OUTPUT_FIELD,
                "Output Field Name",
                defaultValue="rolling"
            )
        )

        # Window size
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WINDOW,
                "Window Size",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=3,
                minValue=1
            )
        )

        # Method (Mean / Median)
        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Statistic Method",
                options=["Mean", "Median"],
                defaultValue=0
            )
        )

    def name(self):
        return "rolling_statistic"

    def displayName(self):
        return "Rolling Statistic"

    def group(self):
        return "Filters"

    def groupId(self):
        return "filters"


    def createInstance(self):
        return RollingStatistic()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        window = self.parameterAsInt(parameters, self.WINDOW, context)
        method = self.parameterAsEnum(parameters, self.METHOD, context)

        method_str = "mean" if method == 0 else "median"

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            # Add output field if missing
            if output_field not in [f.name() for f in layer.fields()]:
                layer.dataProvider().addAttributes([QgsField(output_field, QVariant.Double)])
                layer.updateFields()

            in_idx = layer.fields().indexFromName(input_field)
            out_idx = layer.fields().indexFromName(output_field)

            feats = list(layer.getFeatures())
            values = np.array([f[in_idx] for f in feats], dtype=float)

            # Vectorized rolling statistic
            smoothed = rolling_statistic(values, window, method=method_str)

            # Write results back
            updates = {f.id(): {out_idx: float(smoothed[i])} for i, f in enumerate(feats)}
            layer.dataProvider().changeAttributeValues(updates)

            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
