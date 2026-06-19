# -*- coding: utf-8 -*-
"""
QGIS Processing Algorithm: Euler Depth Estimation (UXO-style) — MAG_Depth only

Inputs:
- Total field grid (T)
- Derivative grids Tx, Ty, Tz
- Target point layer with a numeric target-size field (map units)
- Target type dropdown -> Structural Index (SI) (custom list below)
- Instrument height (kept as input for compatibility; not used in output)
- Minimum samples

Solves per target in a local window:
(x-x0)Tx + (y-y0)Ty + (z-z0)Tz = N (B - T)
Unknowns: x0, y0, z0, B

Assume observation plane z = 0.
Depth below sensor = -z0 (positive down). We output MAG_Depth as a positive value.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsFeatureSink,
    QgsWkbTypes,
    QgsGeometry,
    QgsPointXY,
    QgsRaster,
)

import math
import numpy as np


class EulerDepthFromDerivatives(QgsProcessingAlgorithm):
    MAG = "MAG"
    TX = "TX"
    TY = "TY"
    TZ = "TZ"
    TARGETS = "TARGETS"
    SIZE_FIELD = "SIZE_FIELD"
    TARGET_TYPE = "TARGET_TYPE"
    INSTRUMENT_HEIGHT = "INSTRUMENT_HEIGHT"
    MIN_SAMPLES = "MIN_SAMPLES"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return EulerDepthFromDerivatives()

    def name(self):
        return "euler_depth_from_derivatives"

    def displayName(self):
        return "Calculate Target Depth using Euler..."

    def group(self): return "MAG"
    def groupId(self): return "MAG"

    def shortHelpString(self):
        return (
            "Estimate apparent depth per target point using Euler deconvolution.\n\n"
            "Uses total-field grid (T) and derivative grids (Tx, Ty, Tz) in a local\n"
            "square window per target (window size from target size field).\n\n"
            "Output field:\n"
            "  - MAG_Depth : depth below sensor (positive value)\n\n"
            "Assumes observation plane z=0. Internally depth below sensor = -z0.\n"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.MAG, "Total field grid (T)"
        ))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.TX, "Derivative grid Tx = dT/dx"
        ))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.TY, "Derivative grid Ty = dT/dy"
        ))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.TZ, "Derivative grid Tz = dT/dz"
        ))

        self.addParameter(QgsProcessingParameterFeatureSource(
            self.TARGETS, "Target points", [QgsProcessing.TypeVectorPoint]
        ))

        self.addParameter(QgsProcessingParameterField(
            self.SIZE_FIELD,
            "Target size field (map units, e.g. meters)",
            parentLayerParameterName=self.TARGETS,
            type=QgsProcessingParameterField.Numeric
        ))

        # Custom SI dropdown (as requested)
        self._target_labels = [
            "Magnetic ordnance (SI=3)",
            "Magnetic sphere (SI=3)",
            "Magnetic barrel (SI=3)",
            "Magnetic ordnance (SI=2.7)",
            "Magnetic projectile (SI=2.5)",
            "Magnetic cylinder (SI=2)",
            "Magnetic pipe (SI=2)",
            "Magnetic sheet (SI=1)",
            "Magnetic sill (SI=1)",
            "Magnetic step (SI=0.5)",
            "Magnetic contact (SI=0)",
        ]
        self._target_SI = [3.0, 3.0, 3.0, 2.7, 2.5, 2.0, 2.0, 1.0, 1.0, 0.5, 0.0]

        self.addParameter(QgsProcessingParameterEnum(
            self.TARGET_TYPE,
            "Target type (Structural Index, SI)",
            options=self._target_labels,
            defaultValue=0  # default to first item (SI=3)
        ))

        # Kept for backward compatibility / future extension (not used in output)
        self.addParameter(QgsProcessingParameterNumber(
            self.INSTRUMENT_HEIGHT,
            "Instrument height (m) (kept; not used in MAG_Depth-only output)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_SAMPLES,
            "Minimum samples (after nodata filtering)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=25,
            minValue=10
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Euler depth results (MAG_Depth only)"
        ))

    # -------- helpers --------

    def _identify_val(self, rlayer, x, y):
        """Sample raster at x,y. Returns float or None."""
        dp = rlayer.dataProvider()
        res = dp.identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue)
        if not res.isValid():
            return None
        d = res.results()
        if not d:
            return None
        v = d.get(1, None)  # band 1
        if v is None:
            return None
        try:
            fv = float(v)
        except Exception as err:
            return None
        if math.isnan(fv):
            return None
        return fv

    def _window_samples(self, cx, cy, half_size, step):
        """Square window centered at (cx,cy)."""
        if half_size <= 0:
            return
        if step <= 0:
            step = max(half_size / 5.0, 1e-6)
        n = max(1, int(math.ceil(half_size / step)))
        for i in range(-n, n + 1):
            x = cx + i * step
            for j in range(-n, n + 1):
                y = cy + j * step
                yield x, y

    def _solve_euler_ls(self, N, samples):
        """
        samples: list (x, y, T, Tx, Ty, Tz), observation z=0
        Solve: [Tx Ty Tz N] [x0 y0 z0 B]^T = x*Tx + y*Ty + N*T
        """
        if len(samples) < 4:
            return None

        A = np.zeros((len(samples), 4), dtype=float)
        b = np.zeros((len(samples),), dtype=float)

        for i, (x, y, T, Tx, Ty, Tz) in enumerate(samples):
            A[i, 0] = Tx
            A[i, 1] = Ty
            A[i, 2] = Tz
            A[i, 3] = N
            b[i] = x * Tx + y * Ty + N * T

        try:
            u, residuals, rank, s = np.linalg.lstsq(A, b, rcond=None)
        except Exception as err:
            return None

        if rank < 4:
            return None

        x0, y0, z0, B = u.tolist()
        return x0, y0, z0, B

    # -------- main --------

    def processAlgorithm(self, parameters, context, feedback):
        mag = self.parameterAsRasterLayer(parameters, self.MAG, context)
        tx = self.parameterAsRasterLayer(parameters, self.TX, context)
        ty = self.parameterAsRasterLayer(parameters, self.TY, context)
        tz = self.parameterAsRasterLayer(parameters, self.TZ, context)

        targets = self.parameterAsSource(parameters, self.TARGETS, context)
        size_field = self.parameterAsString(parameters, self.SIZE_FIELD, context)

        if mag is None or tx is None or ty is None or tz is None:
            raise QgsProcessingException("All rasters (T, Tx, Ty, Tz) must be set.")
        if targets is None:
            raise QgsProcessingException("Invalid target point layer.")
        if not size_field:
            raise QgsProcessingException("Target size field is required.")

        si_idx = self.parameterAsEnum(parameters, self.TARGET_TYPE, context)
        N = float(self._target_SI[si_idx])

        # kept for compatibility (currently not used in output)
        _inst_h = float(self.parameterAsDouble(parameters, self.INSTRUMENT_HEIGHT, context))
        min_samples = int(self.parameterAsInt(parameters, self.MIN_SAMPLES, context))

        try:
            step = float(min(abs(mag.rasterUnitsPerPixelX()), abs(mag.rasterUnitsPerPixelY())))
            if step <= 0:
                step = 1.0
        except Exception as err:
            step = 1.0

        # output fields: original + MAG_Depth only
        out_fields = QgsFields()
        for f in targets.fields():
            out_fields.append(f)
        out_fields.append(QgsField("MAG_Depth", QVariant.Double))

        (sink, sink_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            QgsWkbTypes.Point,
            targets.sourceCrs()
        )
        if sink is None:
            raise QgsProcessingException("Could not create output sink.")

        total = targets.featureCount()
        if total == 0:
            return {self.OUTPUT: sink_id}

        for idx, feat in enumerate(targets.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(100 * idx / max(1, total)))

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            pt = geom.asPoint()
            cx, cy = pt.x(), pt.y()

            # read size
            try:
                sz = feat[size_field]
                if sz is None:
                    raise ValueError()
                sz = float(sz)
            except Exception as err:
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cx, cy)))
                out_feat.setAttributes(feat.attributes() + [None])
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            if sz <= 0:
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cx, cy)))
                out_feat.setAttributes(feat.attributes() + [None])
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            half = 0.5 * sz

            # collect samples
            samples = []
            for x, y in self._window_samples(cx, cy, half, step):
                T = self._identify_val(mag, x, y)
                Tx = self._identify_val(tx, x, y)
                Ty = self._identify_val(ty, x, y)
                Tz = self._identify_val(tz, x, y)
                if T is None or Tx is None or Ty is None or Tz is None:
                    continue
                samples.append((x, y, T, Tx, Ty, Tz))

            if len(samples) < min_samples:
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cx, cy)))
                out_feat.setAttributes(feat.attributes() + [None])
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            sol = self._solve_euler_ls(N, samples)
            if sol is None:
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cx, cy)))
                out_feat.setAttributes(feat.attributes() + [None])
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            x0, y0, z0, B = sol

            # depth below sensor: want positive value always
            # nominal: depth = -z0 (positive down). Force positive magnitude:
            MAG_Depth = abs(-float(z0))

            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cx, cy)))
            out_feat.setAttributes(feat.attributes() + [MAG_Depth])
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

        return {self.OUTPUT: sink_id}
