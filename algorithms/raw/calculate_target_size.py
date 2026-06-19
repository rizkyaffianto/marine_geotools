# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsFeatureSink,
    QgsGeometry,
    QgsPointXY,
    QgsRaster,
    QgsMessageLog,
    Qgis
)
import math
import statistics


class CalculateTargetSizeInflection(QgsProcessingAlgorithm):
    """
    Estimate target size from raster by distance from peak point to first inflection
    in 8 directions. Final target size = MAXIMUM inflection distance (d_max).
    """

    RASTER = "RASTER"
    POINTS = "POINTS"
    MAX_DISTANCE = "MAX_DISTANCE"
    STEP_MULT = "STEP_MULT"
    OUTPUT = "OUTPUT"

    def name(self):
        return "calculate_target_size_inflection"

    def displayName(self):
        return "Calculate Target Size"
    def group(self): return "MAG"
    def groupId(self): return "MAG"


    def shortHelpString(self):
        return (
            "For each input point, sample the raster in 8 directions and find the first inflection point\n"
            "using 4 successive samples. Target size is the MAXIMUM inflection distance (d_max).\n"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.RASTER, "Grid (raster) to define target sizes"
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POINTS, "Target points (peaks)", [QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_DISTANCE,
            "Maximum search distance (map units)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=50.0,
            minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.STEP_MULT,
            "Step multiplier (1.0 ≈ cell size step)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0,
            minValue=0.1
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Output (points with tgt_size)"
        ))

    def createInstance(self):
        return CalculateTargetSizeInflection()

    # --- Raster sampling helper ---
    def _sample_raster(self, provider, point_xy, band=1):
        ident = provider.identify(point_xy, QgsRaster.IdentifyFormatValue)
        if not ident.isValid():
            return None
        res = ident.results()
        if band not in res:
            return None
        try:
            v = float(res[band])
            if math.isnan(v):
                return None
            return v
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sampling raster: {e}", "marine_geotools", Qgis.Warning)
            return None

    def processAlgorithm(self, parameters, context, feedback):
        raster = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        source = self.parameterAsSource(parameters, self.POINTS, context)
        max_dist = self.parameterAsDouble(parameters, self.MAX_DISTANCE, context)
        step_mult = self.parameterAsDouble(parameters, self.STEP_MULT, context)

        if raster is None:
            raise QgsProcessingException("Invalid raster input.")
        if source is None:
            raise QgsProcessingException("Invalid point layer input.")

        provider = raster.dataProvider()
        if provider is None:
            raise QgsProcessingException("Raster provider not available.")

        rx = abs(raster.rasterUnitsPerPixelX())
        ry = abs(raster.rasterUnitsPerPixelY())
        cell = (rx + ry) / 2.0 if (rx > 0 and ry > 0) else max(rx, ry, 1.0)

        h = cell * step_mult
        if h <= 0:
            h = cell

        # 8 normalized directions
        dirs = [
            (1, 0), (1, 1), (0, 1), (-1, 1),
            (-1, 0), (-1, -1), (0, -1), (1, -1)
        ]
        dirs = [(dx / math.hypot(dx, dy), dy / math.hypot(dx, dy)) for dx, dy in dirs]

        out_fields = QgsFields()
        for f in source.fields():
            out_fields.append(f)
        out_fields.append(QgsField("tgt_size", QVariant.Double))

        (sink, sink_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields,
            source.wkbType(),
            source.sourceCrs()
        )
        if sink is None:
            raise QgsProcessingException("Could not create output sink.")

        total = source.featureCount()
        if total == 0:
            return {self.OUTPUT: sink_id}

        # Process each point
        for i, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue

            pt = geom.asPoint()
            p0 = QgsPointXY(pt.x(), pt.y())

            distances = []

            nmax = int(max_dist / h) if max_dist > 0 else 0
            if nmax < 4:
                nmax = 4

            for (ux, uy) in dirs:
                v = []
                d = []
                found = None

                for k in range(0, nmax + 1):
                    dist_k = k * h
                    xk = p0.x() + ux * dist_k
                    yk = p0.y() + uy * dist_k
                    val = self._sample_raster(provider, QgsPointXY(xk, yk), band=1)

                    if val is None:
                        break

                    v.append(val)
                    d.append(dist_k)

                    if len(v) >= 4:
                        j = len(v) - 1
                        v0, v1, v2, v3 = v[j - 3], v[j - 2], v[j - 1], v[j]

                        s1 = (v1 - v0) / h
                        s2 = (v2 - v1) / h
                        s3 = (v3 - v2) / h

                        dd1 = s2 - s1
                        dd2 = s3 - s2

                        if (s2 < 0.0) and (dd1 < 0.0) and (dd2 > 0.0):
                            denom = (dd1 - dd2)
                            t = (dd1 / denom) if denom != 0.0 else 0.5
                            t = max(0.0, min(1.0, t))

                            found = d[j - 2] + t * (d[j - 1] - d[j - 2])
                            break

                if found is not None:
                    distances.append(found)

            target_size = max(distances) if distances else None

            out_feature = QgsFeature(out_fields)
            out_feature.setGeometry(QgsGeometry.fromPointXY(p0))
            attributes = feat.attributes()
            attributes.append(target_size)
            out_feature.setAttributes(attributes)
            sink.addFeature(out_feature, QgsFeatureSink.FastInsert)

            if i % 50 == 0:
                feedback.setProgress(int(100.0 * i / max(1, total)))

        return {self.OUTPUT: sink_id}
