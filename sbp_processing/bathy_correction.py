# -*- coding: utf-8 -*-

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingException,
    QgsVectorLayer,
    QgsField,
    QgsPointXY,
    QgsProject,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant


class SBPComputeDtShiftFromRaster(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_RASTER = "INPUT_RASTER"
    SEABED_FIELD = "SEABED_FIELD"
    OUT_FIELD = "OUT_FIELD"
    VW = "VW"
    DEPTH_SIGN = "DEPTH_SIGN"
    BAND = "BAND"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input SBP vector layers (nav/picks)",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Depth raster (bathy/tide grid)"
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.SEABED_FIELD,
                "Seabed TWT field name (ms)",
                defaultValue="seabed_twt"
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUT_FIELD,
                "Output dt_shift field (ms)",
                defaultValue="dt_shift_ms"
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.VW,
                "Water sound velocity vw (m/s)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1500.0,
                minValue=500.0,
                maxValue=5000.0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DEPTH_SIGN,
                "Depth sign (+1 if raster is positive-down, -1 if raster is negative depth)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BAND,
                "Raster band (usually 1)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=1,
                maxValue=9999
            )
        )

    def name(self):
        return "sbp_bathy_shift"

    def displayName(self):
        return "Bathy Shift"

    def group(self):
        return "SBP - Vertical Shift"

    def groupId(self):
        return "vertical_shift"

    def createInstance(self):
        return SBPComputeDtShiftFromRaster()

    def _to_float_or_none(self, v):
        try:
            if v is None:
                return None
            if hasattr(v, "isNull") and v.isNull():
                return None
            x = float(v)
            if x != x:  # NaN
                return None
            return x
        except Exception as err:
            QgsMessageLog.logMessage(f"Error converting {v} to float: {err}", "MarineGeoTools", Qgis.Warning)
            return None

    def _feature_point(self, feat):
        g = feat.geometry()
        if g is None or g.isEmpty():
            return None
        try:
            if g.type() == 0:  # Point
                if g.isMultipart():
                    pts = g.asMultiPoint()
                    if pts:
                        p = pts[0]
                        return QgsPointXY(p.x(), p.y())
                else:
                    p = g.asPoint()
                    return QgsPointXY(p.x(), p.y())
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        # fallback: centroid
        try:
            c = g.centroid()
            if c and not c.isEmpty():
                p = c.asPoint()
                return QgsPointXY(p.x(), p.y())
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        return None

    def _ensure_field(self, layer, field_name):
        idx = layer.fields().indexOf(field_name)
        if idx != -1:
            return idx

        pr = layer.dataProvider()
        ok = pr.addAttributes([QgsField(field_name, QVariant.Double)])
        layer.updateFields()
        if not ok:
            raise QgsProcessingException(
                "Failed to add field '{}' to layer '{}'".format(field_name, layer.name())
            )

        idx = layer.fields().indexOf(field_name)
        if idx == -1:
            raise QgsProcessingException(
                "Field '{}' still not found after add in '{}'".format(field_name, layer.name())
            )
        return idx

    def _sample_raster_fast(self, provider, pt, band):
        # Much faster than identify(): returns (value, ok)
        try:
            val, ok = provider.sample(pt, band)
            if not ok:
                return None
            return self._to_float_or_none(val)
        except Exception as err:
            QgsMessageLog.logMessage(f"Error sampling raster: {err}", "MarineGeoTools", Qgis.Warning)
            return None

    def _flush_updates(self, layer, updates, feedback):
        if not updates:
            return True
        ok = layer.dataProvider().changeAttributeValues(updates)
        if not ok:
            feedback.reportError("  - Bulk changeAttributeValues failed.")
            return False
        updates.clear()
        return True

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        raster = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)

        seabed_field = (self.parameterAsString(parameters, self.SEABED_FIELD, context) or "seabed_twt").strip()
        out_field = (self.parameterAsString(parameters, self.OUT_FIELD, context) or "dt_shift_ms").strip()

        vw = float(self.parameterAsDouble(parameters, self.VW, context))
        depth_sign = float(self.parameterAsDouble(parameters, self.DEPTH_SIGN, context))
        band = int(self.parameterAsInt(parameters, self.BAND, context))

        if not layers:
            raise QgsProcessingException("No input layers provided.")
        if raster is None:
            raise QgsProcessingException("Raster is not valid.")
        if vw <= 0:
            raise QgsProcessingException("vw must be > 0.")
        if band < 1:
            raise QgsProcessingException("Band must be >= 1.")

        provider = raster.dataProvider()
        if provider is None:
            raise QgsProcessingException("Raster provider not available.")

        total_layers = len(layers) or 1
        CHUNK = 5000  # bulk update chunk size (tweak if needed)

        for i, layer in enumerate(layers):
            if feedback.isCanceled():
                break

            if not isinstance(layer, QgsVectorLayer):
                feedback.pushInfo("Skipping non-vector layer: {}".format(getattr(layer, "name", lambda: "<?>")()))
                continue

            feedback.pushInfo("[{}/{}] Layer: {}".format(i + 1, total_layers, layer.name()))

            seabed_idx = layer.fields().indexOf(seabed_field)
            if seabed_idx == -1:
                feedback.reportError("  - Skipped: seabed field '{}' not found.".format(seabed_field))
                continue

            shift_idx = self._ensure_field(layer, out_field)

            # CRS transform: layer CRS -> raster CRS
            xform = None
            try:
                if layer.crs().isValid() and raster.crs().isValid() and layer.crs() != raster.crs():
                    xform = QgsCoordinateTransform(layer.crs(), raster.crs(), QgsProject.instance())
            except Exception as err:
                QgsMessageLog.logMessage(f"Error creating CRS transform: {err}", "MarineGeoTools", Qgis.Warning)
                xform = None

            if not layer.isEditable():
                layer.startEditing()

            n = layer.featureCount() or 0
            if n == 0:
                layer.commitChanges()
                layer.startEditing()
                feedback.pushInfo("  - No features.")
                continue

            written = 0
            skipped_geom = 0
            skipped_raster = 0
            skipped_seabed = 0
            skipped_transform = 0

            step = max(1, n // 100)

            # Only request needed attribute (seabed) + geometry
            req = QgsFeatureRequest()
            req.setSubsetOfAttributes([seabed_field], layer.fields())

            updates = {}  # { fid: { fieldIndex: value } }

            for k, feat in enumerate(layer.getFeatures(req)):
                if feedback.isCanceled():
                    break
                if (k % step) == 0:
                    feedback.setProgress(int(100 * k / float(max(1, n))))

                pt = self._feature_point(feat)
                if pt is None:
                    skipped_geom += 1
                    continue

                if xform is not None:
                    try:
                        pt = xform.transform(pt)
                    except Exception as err:
                        QgsMessageLog.logMessage(f"Error transforming point: {err}", "MarineGeoTools", Qgis.Warning)
                        skipped_transform += 1
                        continue

                depth_val = self._sample_raster_fast(provider, pt, band)
                if depth_val is None:
                    skipped_raster += 1
                    continue

                seabed_twt = self._to_float_or_none(feat.attribute(seabed_idx))
                if seabed_twt is None:
                    skipped_seabed += 1
                    continue

                depth_m = float(depth_val) * depth_sign
                twt_bathy_ms = (2.0 * depth_m / vw) * 1000.0
                dt_shift_ms = float(twt_bathy_ms) - float(seabed_twt)

                updates[feat.id()] = {shift_idx: dt_shift_ms}
                written += 1

                if len(updates) >= CHUNK:
                    if not self._flush_updates(layer, updates, feedback):
                        layer.rollBack()
                        feedback.reportError("  - Rolled back due to update failure.")
                        break

            # flush remainder
            if updates:
                if not self._flush_updates(layer, updates, feedback):
                    layer.rollBack()
                    feedback.reportError("  - Rolled back due to update failure.")
                    continue

            if not layer.commitChanges():
                layer.rollBack()
                feedback.reportError("  - Commit failed (rolled back).")
                continue

            layer.startEditing()  # optional: keep editable

            feedback.pushInfo(
                "  - Wrote {} / {}. Skipped: geom={}, transform={}, raster={}, seabed_null={}".format(
                    written, n, skipped_geom, skipped_transform, skipped_raster, skipped_seabed
                )
            )

        return {}
