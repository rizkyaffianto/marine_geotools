from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsField,
    QgsProcessing,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant
import numpy as np


class FraserLowPassBest(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    CUTOFF_WAVELENGTH = "CUTOFF_WAVELENGTH"
    DX = "DX"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.INPUT_FIELD,
            "Field to Filter",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterString(
            self.OUTPUT_FIELD,
            "Output Field Name",
            defaultValue="lowpass"
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.CUTOFF_WAVELENGTH,
            "Cutoff wavelength (fiducials)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=20.0
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.DX,
            "Data spacing Δx (same unit as cutoff)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0
        ))

    def name(self):
        return "fraser_lowpass"

    def displayName(self):
        return "Low-Pass Filter"

    def group(self):
        return "Filters"

    def groupId(self):
        return "filters"


    def createInstance(self):
        return FraserLowPassBest()

    # --- Helper: global linear detrend ---
    def _lintrend(self, x_indices):
        n = len(x_indices)
        t = np.arange(n)
        mask = ~np.isnan(x_indices)
        if mask.sum() < 2:
            return np.zeros_like(x_indices)
        coef = np.polyfit(t[mask], x_indices[mask], 1)
        return np.polyval(coef, t)

    # --- Helper: mirror pad with optional detrend ---
    def pad_mirror_with_detrend(self, vals, half_length, detrend=True):
        residual_values = vals.copy()
        idx = np.arange(len(residual_values))
        nanmask = np.isnan(residual_values)
        if nanmask.any():
            residual_values[nanmask] = np.interp(idx[nanmask], idx[~nanmask], residual_values[~nanmask])

        # Detrend (global linear)
        trend = self._lintrend(residual_values) if detrend else np.zeros_like(residual_values)
        residuals = residual_values - trend  # residual

        # Mirror pad residual
        left = residuals[1:half_length+1][::-1] if len(residuals) > 1 else np.repeat(residuals[0], half_length)
        right = residuals[-half_length-1:-1][::-1] if len(residuals) > 1 else np.repeat(residuals[-1], half_length)
        r_pad = np.concatenate([left, residuals, right])

        # Mirror pad trend too
        t_left = trend[1:half_length+1][::-1] if len(trend) > 1 else np.repeat(trend[0], half_length)
        t_right = trend[-half_length-1:-1][::-1] if len(trend) > 1 else np.repeat(trend[-1], half_length)
        trend_pad = np.concatenate([t_left, trend, t_right])

        return r_pad, trend_pad, nanmask

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        cutoff_wavelength = self.parameterAsDouble(parameters, self.CUTOFF_WAVELENGTH, context)
        dx = self.parameterAsDouble(parameters, self.DX, context)

        # --- Compute filter params ---
        half_length = int(round(cutoff_wavelength / dx))
        cutoff_freq = 1.0 / cutoff_wavelength
        feedback.pushInfo(f"Filter half-length N = {half_length}, cutoff frequency = {cutoff_freq:.6f} cycles/unit")

        # --- Build convolution kernel ---
        k = np.arange(-half_length, half_length + 1)
        x_indices = k * dx
        sinc_term = np.sinc(2 * cutoff_freq * x_indices)  # normalized sinc
        window = 0.5 * (1 + np.cos(np.pi * x_indices / (half_length * dx)))
        window[np.abs(x_indices) > half_length * dx] = 0
        weights = sinc_term * window
        weights /= np.sum(weights)  # normalize to unity gain

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            if output_field not in [f.name() for f in layer.fields()]:
                layer.dataProvider().addAttributes([QgsField(output_field, QVariant.Double)])
                layer.updateFields()

            input_idx = layer.fields().indexFromName(input_field)
            output_idx = layer.fields().indexFromName(output_field)

            feats = list(layer.getFeatures())
            vals = []
            fids = []
            for feature in feats:
                try:
                    vals.append(float(feature[input_idx]) if feature[input_idx] is not None else np.nan)
                    fids.append(feature.id())
                except Exception as e:
                    QgsMessageLog.logMessage(f"Failed to parse value for feature {feature.id()}: {e}", "MarineGeoTools", Qgis.Warning)
                    vals.append(np.nan)
                    fids.append(feature.id())
            vals = np.array(vals, dtype=float)
            fids = np.array(fids)

            if np.sum(~np.isnan(vals)) < len(weights):
                feedback.reportError("Not enough data points for filter length.")
                continue

            # --- Pad with mirror + detrend ---
            r_pad, trend_pad, nanmask = self.pad_mirror_with_detrend(vals, half_length, detrend=True)

            # --- Convolution ---
            y_pad = np.convolve(r_pad, weights, mode="same")

            # --- Trim to original length & add trend back ---
            smoothed_values = y_pad[half_length:-half_length] + trend_pad[half_length:-half_length]

            # Restore NaNs
            smoothed_values[nanmask] = np.nan

            # --- Update output field ---
            updates = {
                int(fid): {output_idx: float(val)}
                for fid, val in zip(fids, smoothed_values)
                if not np.isnan(val)
            }
            if updates:
                layer.dataProvider().changeAttributeValues(updates)

            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
