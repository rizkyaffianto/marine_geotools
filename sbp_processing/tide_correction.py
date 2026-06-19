# -*- coding: utf-8 -*-

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsVectorLayer,
    QgsField,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant, QDateTime
from datetime import datetime, timedelta
import bisect


class SBPComputeDtTideShiftFromTable(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    TIDE_LAYER = "TIDE_LAYER"

    SBP_TIME_FIELD = "SBP_TIME_FIELD"
    TIDE_TIME_FIELD = "TIDE_TIME_FIELD"
    TIDE_VALUE_FIELD = "TIDE_VALUE_FIELD"

    OUT_FIELD = "OUT_FIELD"
    MATCH_METHOD = "MATCH_METHOD"
    TIME_OFFSET_HOURS = "TIME_OFFSET_HOURS"
    VW = "VW"
    TIDE_SIGN = "TIDE_SIGN"

    METHOD_NEAREST = 0
    METHOD_LINEAR = 1

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input SBP vector layers (nav / picks)",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.TIDE_LAYER,
                "Tide layer (table / points)",
                types=[QgsProcessing.TypeVector]
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.SBP_TIME_FIELD,
                "SBP timestamp field (DateTime)",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Any,
                defaultValue="timestamp"
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.TIDE_TIME_FIELD,
                "Tide timestamp field (DateTime)",
                parentLayerParameterName=self.TIDE_LAYER,
                type=QgsProcessingParameterField.Any,
                defaultValue="timestamp"
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.TIDE_VALUE_FIELD,
                "Tide value field (meters)",
                parentLayerParameterName=self.TIDE_LAYER,
                type=QgsProcessingParameterField.Any,
                defaultValue="tide_m"
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUT_FIELD,
                "Output tide dt_shift field (ms)",
                defaultValue="dt_shift_ms"
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.MATCH_METHOD,
                "Time matching method",
                options=["Nearest", "Linear interpolation"],
                defaultValue=self.METHOD_LINEAR
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TIME_OFFSET_HOURS,
                "Time offset applied to TIDE timestamps (hours)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.VW,
                "Water sound velocity vw (m/s)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1500.0
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TIDE_SIGN,
                "Tide sign (+1 normally)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0
            )
        )

    def name(self):
        return "sbp_tide_shift_from_table"

    def displayName(self):
        return "Tide Shift"

    def group(self):
        return "SBP - Vertical Shift"

    def groupId(self):
        return "vertical_shift"

    def createInstance(self):
        return SBPComputeDtTideShiftFromTable()

    def _to_datetime(self, v):
        if v is None:
            return None
        if isinstance(v, QDateTime):
            return v.toPyDateTime() if v.isValid() else None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception as err:
            return None

    def _to_float(self, v):
        try:
            x = float(v)
            return x if x == x else None
        except Exception as err:
            return None

    def processAlgorithm(self, parameters, context, feedback):
        sbp_layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        tide_layer = self.parameterAsVectorLayer(parameters, self.TIDE_LAYER, context)

        sbp_time_field = self.parameterAsString(parameters, self.SBP_TIME_FIELD, context)
        tide_time_field = self.parameterAsString(parameters, self.TIDE_TIME_FIELD, context)
        tide_value_field = self.parameterAsString(parameters, self.TIDE_VALUE_FIELD, context)
        out_field = self.parameterAsString(parameters, self.OUT_FIELD, context)

        method = self.parameterAsEnum(parameters, self.MATCH_METHOD, context)
        offset_hours = self.parameterAsDouble(parameters, self.TIME_OFFSET_HOURS, context)
        vw = self.parameterAsDouble(parameters, self.VW, context)
        tide_sign = self.parameterAsDouble(parameters, self.TIDE_SIGN, context)

        if not sbp_layers or tide_layer is None:
            raise QgsProcessingException("Invalid input layers.")

        # ---- Build tide time series (with offset applied)
        t_idx = tide_layer.fields().indexOf(tide_time_field)
        v_idx = tide_layer.fields().indexOf(tide_value_field)

        tide_series = []
        td_offset = timedelta(hours=offset_hours)

        for f in tide_layer.getFeatures(QgsFeatureRequest().setSubsetOfAttributes(
                [tide_time_field, tide_value_field], tide_layer.fields())):
            dt = self._to_datetime(f.attribute(t_idx))
            val = self._to_float(f.attribute(v_idx))
            if dt is None or val is None:
                continue
            tide_series.append((dt + td_offset, val))

        tide_series.sort(key=lambda x: x[0])
        times = [x[0] for x in tide_series]
        vals = [x[1] for x in tide_series]

        if not times:
            raise QgsProcessingException("No valid tide records.")

        for layer in sbp_layers:
            if not isinstance(layer, QgsVectorLayer):
                continue

            sbp_idx = layer.fields().indexOf(sbp_time_field)
            if sbp_idx == -1:
                feedback.reportError(f"SBP time field not found in {layer.name()}")
                continue

            if layer.fields().indexOf(out_field) == -1:
                layer.dataProvider().addAttributes([QgsField(out_field, QVariant.Double)])
                layer.updateFields()

            out_idx = layer.fields().indexOf(out_field)

            layer.startEditing()

            for f in layer.getFeatures(QgsFeatureRequest().setSubsetOfAttributes(
                    [sbp_time_field], layer.fields())):
                dt_sbp = self._to_datetime(f.attribute(sbp_idx))
                if dt_sbp is None:
                    continue

                j = bisect.bisect_left(times, dt_sbp)
                if j == 0 or j >= len(times):
                    continue

                if method == self.METHOD_NEAREST:
                    tide_m = vals[j - 1] if (dt_sbp - times[j - 1]) <= (times[j] - dt_sbp) else vals[j]
                else:
                    t0, t1 = times[j - 1], times[j]
                    v0, v1 = vals[j - 1], vals[j]
                    w = (dt_sbp - t0).total_seconds() / (t1 - t0).total_seconds()
                    tide_m = (1 - w) * v0 + w * v1

                dt_tide_ms = (2.0 * tide_m * tide_sign / vw) * 1000.0
                layer.changeAttributeValue(f.id(), out_idx, dt_tide_ms)

            layer.commitChanges()

        return {}
