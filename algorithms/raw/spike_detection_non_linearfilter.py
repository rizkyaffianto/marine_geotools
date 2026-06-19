from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessing,
)
import numpy as np
 

def shift_nan(a: np.ndarray, k: int) -> np.ndarray:
    """Shift array by k samples without wrap-around; pad with NaN."""
    out = np.full_like(a, np.nan, dtype=float)
    if k > 0:
        out[k:] = a[:-k]
    elif k < 0:
        out[:k] = a[-k:]
    else:
        out[:] = a
    return out


class NonLinearSpikeMask(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FIELD_CHECK = "FIELD_CHECK"
    FIELD_MASK = "FIELD_MASK"
    WIDTH = "WIDTH"
    TOLERANCE = "TOLERANCE"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CHECK,
            "Field to Check (numeric)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_MASK,
            "Field to Mask (set to NULL if spike detected)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Any
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.WIDTH,
            "Maximum Filter Width (A–E distance)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=3,
            minValue=1
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE,
            "Amplitude Tolerance (abs(Yq − Yc))",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.5,
            minValue=0.0
        ))

    def name(self):
        return "nonlinear_spike_mask"

    def displayName(self):
        return "Spike Detection"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return NonLinearSpikeMask()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        field_check = self.parameterAsString(parameters, self.FIELD_CHECK, context)
        field_mask = self.parameterAsString(parameters, self.FIELD_MASK, context)
        max_width = self.parameterAsInt(parameters, self.WIDTH, context)
        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            idx_check = layer.fields().indexFromName(field_check)
            idx_mask = layer.fields().indexFromName(field_mask)
            if idx_check < 0 or idx_mask < 0:
                feedback.reportError(f"Field(s) not found in {layer.name()}")
                continue

            feats = list(layer.getFeatures())
            if not feats:
                feedback.pushInfo(f"{layer.name()}: no features.")
                continue

            vals = np.array(
                [f[idx_check] if f[idx_check] is not None else np.nan for f in feats],
                dtype=float
            )
            n = len(vals)

            # OPTION B: mirror padding
            pad = 2 * max_width
            if n < 3:
                feedback.pushInfo(f"{layer.name()}: too few samples ({n}).")
                continue

            # if pad too large relative to n, reflect padding can fail; clamp pad safely
            # (reflect requires pad < n)
            pad = min(pad, max(1, n - 1))

            # Mirror pad: reflect values around edges (no wrap). NaNs remain NaNs.
            vp = np.pad(vals, (pad, pad), mode="reflect")
            Np = len(vp)

            is_spike_padded = np.zeros(Np, dtype=bool)

            for width in range(max_width, 0, -1):
                Ya = shift_nan(vp, -2 * width)
                Yb = shift_nan(vp, -width)
                Yc = vp
                Yd = shift_nan(vp,  width)
                Ye = shift_nan(vp,  2 * width)

                valid = (
                    np.isfinite(Ya) &
                    np.isfinite(Yb) &
                    np.isfinite(Yc) &
                    np.isfinite(Yd) &
                    np.isfinite(Ye)
                )

                S = Yc - (Yb + Yd) / 2.0
                nonzero = ~np.isclose(S, 0.0)
                cond_valid = valid & nonzero

                Yavg = (Ya + Ye) / 2.0
                T = (Yb - Yavg) + (Yc - Yavg) + (Yd - Yavg)
                R = np.round(T / S, 1)

                Yq_pos = (2.0 / 3.0) * (Yb + Yd) - (1.0 / 6.0) * (Ya + Ye)
                Yq_neg = 0.5 * Yc + 0.25 * (Yb + Yd)

                diff_pos = np.abs(Yq_pos - Yc)
                diff_neg = np.abs(Yq_neg - Yc)

                cond_pos = cond_valid & (R >= 0) & (R < 2) & (diff_pos >= tol)
                cond_neg = cond_valid & (R < 0) & (R > -2) & (diff_neg >= tol)

                is_spike_padded |= (cond_pos | cond_neg)

            # map back to original indices
            is_spike = is_spike_padded[pad:pad + n]

            changes = {f.id(): {idx_mask: None} for i, f in enumerate(feats) if is_spike[i]}
            if changes:
                layer.dataProvider().changeAttributeValues(changes)

            layer.updateFields()
            layer.triggerRepaint()
            feedback.pushInfo(f"{layer.name()}: masked {int(is_spike.sum())} spikes detected.")

        feedback.pushInfo("All layers processed.")
        return {}
