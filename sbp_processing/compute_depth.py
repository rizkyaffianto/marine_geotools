# -*- coding: utf-8 -*-

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingException,
    QgsVectorLayer,
    QgsField,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant
import os
import json


class SBPComputeReflectorDepthFromMetaMulti(QgsProcessingAlgorithm):

    INPUT_LAYERS = "INPUT_LAYERS"
    REF_TWT_FIELD = "REF_TWT_FIELD"
    OUT_DEPTH_FIELD = "OUT_DEPTH_FIELD"

    SEABED_TWT_FIELD = "seabed_twt"
    DT_SHIFT_FIELD = "dt_shift_ms"

    META_VW = "sound_velocity_water_m_s"
    META_VS = "sound_velocity_sediment_m_s"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input reflector layers",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.REF_TWT_FIELD,
                "Reflector TWT field (ms)",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Any
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUT_DEPTH_FIELD,
                "Output reflector depth field (m)"
            )
        )

    def name(self):
        return "sbp_compute_reflector_depth_multi"

    def displayName(self):
        return "Compute Reflector Dept"

    def group(self):
        return "SBP - Depth and Thickness"

    def groupId(self):
        return "sbp_depth"

    def createInstance(self):
        return SBPComputeReflectorDepthFromMetaMulti()

    def _to_float(self, v):
        try:
            x = float(v)
            return x if x == x else None
        except Exception as err:
            return None

    def _get_gpkg_path(self, layer: QgsVectorLayer):
        return (layer.source() or "").split("|")[0]

    def _read_meta(self, layer: QgsVectorLayer):
        gpkg = self._get_gpkg_path(layer)
        if not gpkg or not os.path.exists(gpkg):
            return None, None, None

        base, _ = os.path.splitext(gpkg)
        meta_path = base + ".json"
        if not os.path.exists(meta_path):
            return meta_path, None, None

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as err:
            return meta_path, None, None

        vw = self._to_float(meta.get(self.META_VW))
        vs = self._to_float(meta.get(self.META_VS))
        return meta_path, vw, vs

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        if not layers:
            raise QgsProcessingException("No input layers provided.")

        ref_field = self.parameterAsString(parameters, self.REF_TWT_FIELD, context)
        out_field = self.parameterAsString(parameters, self.OUT_DEPTH_FIELD, context)

        if not ref_field:
            raise QgsProcessingException("Reflector TWT field is required.")
        if not out_field:
            raise QgsProcessingException("Output field name is required.")

        total = len(layers)

        for li, layer in enumerate(layers, start=1):
            if feedback.isCanceled():
                break

            if not isinstance(layer, QgsVectorLayer):
                feedback.reportError(f"Skipping non-vector layer: {layer}")
                continue

            idx_ref = layer.fields().indexOf(ref_field)
            idx_sb = layer.fields().indexOf(self.SEABED_TWT_FIELD)
            idx_dt = layer.fields().indexOf(self.DT_SHIFT_FIELD)

            if idx_ref == -1 or idx_sb == -1:
                feedback.reportError(
                    f"[{layer.name()}] Missing required fields "
                    f"({ref_field}, {self.SEABED_TWT_FIELD})"
                )
                continue

            # --- Read meta
            meta_path, vw, vs = self._read_meta(layer)
            if vw is None or vs is None:
                raise QgsProcessingException(
                    f"[{layer.name()}] Invalid or missing velocities in meta.json: {meta_path}"
                )

            feedback.pushInfo(
                f"[{li}/{total}] {layer.name()} → vw={vw}, vs={vs}"
            )

            # --- Add output field
            if layer.fields().indexOf(out_field) == -1:
                layer.dataProvider().addAttributes([QgsField(out_field, QVariant.Double)])
                layer.updateFields()

            idx_out = layer.fields().indexOf(out_field)

            # --- Compute depth
            layer.startEditing()

            req_fields = [ref_field, self.SEABED_TWT_FIELD]
            if idx_dt != -1:
                req_fields.append(self.DT_SHIFT_FIELD)

            req = QgsFeatureRequest().setSubsetOfAttributes(req_fields, layer.fields())

            n = layer.featureCount() or 1
            for i, f in enumerate(layer.getFeatures(req), start=1):
                if feedback.isCanceled():
                    break
                if i % 2000 == 0:
                    feedback.setProgress(int(100.0 * i / n))

                t_ref = self._to_float(f.attribute(idx_ref))
                t_sb = self._to_float(f.attribute(idx_sb))
                if t_ref is None or t_sb is None:
                    continue

                dt_ms = 0.0
                if idx_dt != -1:
                    v = self._to_float(f.attribute(idx_dt))
                    dt_ms = v if v is not None else 0.0

                # seabed depth (water column uses dt_shift)
                z_sb = (vw * ((t_sb + dt_ms) / 1000.0)) / 2.0

                # sediment thickness (dt cancels)
                dt_sed = t_ref - t_sb
                if dt_sed < 0:
                    continue

                z_below = (vs * (dt_sed / 1000.0)) / 2.0
                depth_m = z_sb + z_below

                layer.changeAttributeValue(f.id(), idx_out, depth_m)

            layer.commitChanges()

        return {}
