from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsField,
    QgsVectorLayer,
    QgsFeature,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant
import numpy as np
from scipy import interpolate


class InterpolateField(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    METHOD = "METHOD"
    MAX_GAP = "MAX_GAP"
    EXTRAPOLATE = "EXTRAPOLATE"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input Layers",
                layerType=QgsProcessing.TypeVector
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.INPUT_FIELD,
                "Field to Interpolate (from 1st Layer)",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Numeric
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUTPUT_FIELD,
                "Output Field Name",
                defaultValue="interpolated"
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Interpolation Method",
                options=["Linear", "Minimum Curvature", "Akima", "Nearest"],
                defaultValue=0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_GAP,
                "Maximum Gap to Interpolate (leave blank for no limit)",
                type=QgsProcessingParameterNumber.Integer,
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.EXTRAPOLATE,
                "Extrapolation Method",
                options=["None", "Same", "Nearest", "Linear"],
                defaultValue=0
            )
        )

    def name(self):
        return "interpolate_field"

    def displayName(self):
        return "Interpolation"

    def group(self):
        return "Filters"

    def groupId(self):
        return "filters"

    def createInstance(self):
        return InterpolateField()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        method_index = self.parameterAsEnum(parameters, self.METHOD, context)
        extrap_index = self.parameterAsEnum(parameters, self.EXTRAPOLATE, context)

        max_gap = parameters[self.MAX_GAP]
        if max_gap is None:
            max_gap = float("inf")

        method_name = ["linear", "min_curvature", "akima", "nearest"][method_index]
        extrap_name = ["none", "same", "nearest", "linear"][extrap_index]

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            # Add output field if missing
            if output_field not in [f.name() for f in layer.fields()]:
                layer.dataProvider().addAttributes([QgsField(output_field, QVariant.Double)])
                layer.updateFields()

            input_idx = layer.fields().indexFromName(input_field)
            output_idx = layer.fields().indexFromName(output_field)

            features = list(layer.getFeatures())
            attribute_values = []
            feature_ids = []

            for feature in features:
                feature_ids.append(feature.id())
                try:
                    field_value = float(feature[input_idx]) if feature[input_idx] is not None else np.nan
                except Exception as e:
                    QgsMessageLog.logMessage(f"Failed to parse value for feature {feature.id()}: {e}", "MarineGeoTools", Qgis.Warning)
                    field_value = np.nan
                attribute_values.append(field_value)

            attribute_values = np.array(attribute_values)
            x_indices = np.arange(len(attribute_values))
            valid_mask = ~np.isnan(attribute_values)

            interpolated = attribute_values.copy()

            # Select interpolation method
            if method_name == "linear":
                f_interp = interpolate.interp1d(
                    x_indices[valid_mask], attribute_values[valid_mask], kind="linear", bounds_error=False
                )
            elif method_name == "nearest":
                f_interp = interpolate.interp1d(
                    x_indices[valid_mask], attribute_values[valid_mask], kind="nearest", bounds_error=False
                )
            elif method_name == "akima":
                f_interp = interpolate.Akima1DInterpolator(x_indices[valid_mask], attribute_values[valid_mask])
            elif method_name == "min_curvature":
                try:
                    f_interp = interpolate.CubicSpline(
                        x_indices[valid_mask], attribute_values[valid_mask], bc_type="natural"
                    )
                except Exception as e:
                    QgsMessageLog.logMessage(f"CubicSpline failed for layer {layer.name()}: {e}", "MarineGeoTools", Qgis.Warning)
                    feedback.reportError("CubicSpline failed: not enough data points.")
                    f_interp = None
            else:
                f_interp = None

            if f_interp is not None:
                nan_idx = np.where(np.isnan(attribute_values))[0]
                start = None
                for i in range(len(attribute_values)):
                    if np.isnan(attribute_values[i]):
                        if start is None:
                            start = i
                    elif start is not None:
                        end = i
                        gap_size = end - start
                        if gap_size <= max_gap:
                            interpolated[start:end] = f_interp(np.arange(start, end))
                        start = None

                if extrap_name != "none":
                    def get_extrap_func(kind):
                        if kind == "same":
                            return f_interp
                        elif kind == "nearest":
                            return interpolate.interp1d(
                                x_indices[valid_mask], attribute_values[valid_mask], kind="nearest", fill_value="extrapolate"
                            )
                        elif kind == "linear":
                            return interpolate.interp1d(
                                x_indices[valid_mask], attribute_values[valid_mask], kind="linear", fill_value="extrapolate"
                            )

                    extrap_func = get_extrap_func(extrap_name)
                    if extrap_func:
                        if np.isnan(interpolated[0]):
                            i = 0
                            while i < len(interpolated) and np.isnan(interpolated[i]):
                                interpolated[i] = extrap_func(i)
                                i += 1
                        if np.isnan(interpolated[-1]):
                            i = len(interpolated) - 1
                            while i >= 0 and np.isnan(interpolated[i]):
                                interpolated[i] = extrap_func(i)
                                i -= 1

            # Update output field
            updates = {}
            for i, val in enumerate(interpolated):
                if not np.isnan(val):
                    updates[feature_ids[i]] = {output_idx: float(val)}

            layer.dataProvider().changeAttributeValues(updates)
            layer.updateFields()

            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
