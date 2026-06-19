# -*- coding: utf-8 -*-
"""
Layback Along Track (Combined: Fiducial + Distance) - FIELD BASED (X/Y), SAME LAYER OUTPUT
EXTRAPOLATION: Linear regression using first/last K points (K >= 10)

- Multiple input layers (loops)
- Uses X/Y fields (NOT geometry)
- Builds original track per group by sorting with an ORDER field
- Computes chainage (distance along track) internally
- Optional Fiducial layback: fid_target = fid - N  -> mapped to chainage via fid->chainage (interp / linear-reg extrap)
- Optional Distance layback: s_final = s_base - L(m)  (positive L = backward)
- Converts s_final back to X/Y along the original track (interp / linear-reg extrap)
- Writes output fields into SAME layer:
    (Xfield)<suffix>, (Yfield)<suffix>
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsField,
    QgsMessageLog,
    Qgis
)
import math
from bisect import bisect_left


class LaybackAlongTrackLinearExtrap(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    GROUP_FIELD = "GROUP_FIELD"
    ORDER_FIELD = "ORDER_FIELD"
    X_FIELD = "X_FIELD"
    Y_FIELD = "Y_FIELD"

    USE_FID = "USE_FID"
    FID_FIELD = "FID_FIELD"
    N_CONST = "N_CONST"
    N_FIELD = "N_FIELD"

    USE_DIST = "USE_DIST"
    DIST_CONST = "DIST_CONST"
    DIST_FIELD = "DIST_FIELD"

    EXTRAP_K = "EXTRAP_K"   # minimum 10, used for regression extrap at ends

    # --- output ---
    SUFFIX = "SUFFIX"
    COMMIT = "COMMIT"

    def name(self):
        return "layback_along_track_linear_extrap_k10"

    def displayName(self):
        return "Layback Along Track"

    def group(self):
        return "Coordinates"

    def groupId(self):
        return "coordinates"
        
    def createInstance(self):
        return LaybackAlongTrackLinearExtrap()

    def initAlgorithm(self, config=None):
        # ======================
        # 1) INPUT / ORDERING
        # ======================
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input layers",
        ))

        self.addParameter(QgsProcessingParameterField(
            self.GROUP_FIELD,
            "Group / Line field (optional)",
            parentLayerParameterName=self.INPUT_LAYERS,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterField(
            self.ORDER_FIELD,
            "Order field (REQUIRED: fid/time/ping/trace) - defines original track order",
            parentLayerParameterName=self.INPUT_LAYERS,
            optional=False
        ))

        self.addParameter(QgsProcessingParameterField(
            self.X_FIELD,
            "X field",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterField(
            self.Y_FIELD,
            "Y field",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        # ======================
        # ======================
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_FID,
            "Apply fiducial layback (fid - N)",
            defaultValue=True
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FID_FIELD,
            "Fiducial field (numeric) (required if fid layback enabled)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.N_CONST,
            "N constant (fiducials) (used if N field empty)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=0
        ))

        self.addParameter(QgsProcessingParameterField(
            self.N_FIELD,
            "N field (optional, numeric)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_DIST,
            "Apply distance layback along track (meters): s_final = s_base - L",
            defaultValue=True
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.DIST_CONST,
            "Distance layback L constant (meters) (used if L field empty). Positive = backward",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0
        ))

        self.addParameter(QgsProcessingParameterField(
            self.DIST_FIELD,
            "Distance layback L field (meters) (optional)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric,
            optional=True
        ))

        # ======================
        # ======================
        self.addParameter(QgsProcessingParameterNumber(
            self.EXTRAP_K,
            "Extrapolation K points (linear regression)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=2,
            minValue=2
        ))

        # ======================
        # 4) OUTPUT
        # ======================
        self.addParameter(QgsProcessingParameterString(
            self.SUFFIX,
            "Output suffix (creates (Xfield)<suffix>, (Yfield)<suffix>)",
            defaultValue="_lb"
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.COMMIT,
            "Commit changes automatically",
            defaultValue=True
        ))

    def _to_float(self, value):
        try:
            return float(value)
        except Exception as e:
            QgsMessageLog.logMessage(f"Conversion to float failed for value {value}: {e}", "MarineGeoTools", Qgis.Warning)
            return None

    def _sort_key(self, value):
        try:
            return (0, float(value))
        except Exception as e:
            QgsMessageLog.logMessage(f"Conversion to float failed for sort key {value}: {e}", "MarineGeoTools", Qgis.Warning)
            return (1, str(value))

    def _chainage(self, x_coords, y_coords):
        chainage = [0.0]
        for i in range(1, len(x_coords)):
            chainage.append(chainage[-1] + math.hypot(x_coords[i] - x_coords[i - 1], y_coords[i] - y_coords[i - 1]))
        return chainage

    def _linreg_predict(self, x_list, y_list, target_x):
        """
        Simple least-squares linear regression y = a + b*x
        Returns y(x). Falls back safely if degenerate.
        """
        n = len(x_list)
        if n < 2:
            return y_list[0]

        sx = sum(x_list)
        sy = sum(y_list)
        sxx = sum(v * v for v in x_list)
        sxy = sum(x_list[i] * y_list[i] for i in range(n))

        denom = n * sxx - sx * sx
        if denom == 0:
            return sy / n

        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n
        return a + b * target_x

    def _interp_extrap_1d(self, xvals, yvals, target_x, k):
        """
        y(x) mapping.
        Inside range: linear interpolation between neighbors.
        Outside range: linear regression using first/last K points (K>=10).
        """
        n = len(xvals)
        if n == 0:
            return None
        if n == 1:
            return yvals[0]

        # inside -> linear interpolation
        if xvals[0] <= target_x <= xvals[-1]:
            j = bisect_left(xvals, target_x)
            if j <= 0:
                return yvals[0]
            if j >= n:
                return yvals[-1]
            x0, x1 = xvals[j - 1], xvals[j]
            y0, y1 = yvals[j - 1], yvals[j]
            if x1 == x0:
                return y0
            a = (target_x - x0) / (x1 - x0)
            return y0 + a * (y1 - y0)

        k = max(10, int(k))
        k = min(k, n)

        if target_x < xvals[0]:
            xx = xvals[:k]
            yy = yvals[:k]
        else:
            xx = xvals[-k:]
            yy = yvals[-k:]

        return float(self._linreg_predict(xx, yy, target_x))

    def _interp_extrap_xy_from_s(self, svals, xs, ys, s_target, k):
        """
        X(s), Y(s) mapping along the track.
        Inside range: linear along segments.
        Outside range: regression extrapolation on X(s) and Y(s) using first/last K points.
        """
        x = self._interp_extrap_1d(svals, xs, s_target, k)
        y = self._interp_extrap_1d(svals, ys, s_target, k)
        return x, y

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)

        group_field = self.parameterAsString(parameters, self.GROUP_FIELD, context).strip()
        order_field = self.parameterAsString(parameters, self.ORDER_FIELD, context).strip()
        x_field = self.parameterAsString(parameters, self.X_FIELD, context).strip()
        y_field = self.parameterAsString(parameters, self.Y_FIELD, context).strip()

        use_fid = self.parameterAsBool(parameters, self.USE_FID, context)
        fid_field = self.parameterAsString(parameters, self.FID_FIELD, context).strip()
        n_const = self.parameterAsInt(parameters, self.N_CONST, context)
        n_field = self.parameterAsString(parameters, self.N_FIELD, context).strip()

        use_dist = self.parameterAsBool(parameters, self.USE_DIST, context)
        dist_const = self.parameterAsDouble(parameters, self.DIST_CONST, context)
        dist_field = self.parameterAsString(parameters, self.DIST_FIELD, context).strip()

        extrap_k = self.parameterAsInt(parameters, self.EXTRAP_K, context)
        extrap_k = max(2, int(extrap_k))

        suffix = self.parameterAsString(parameters, self.SUFFIX, context).strip() or "_lb"
        do_commit = self.parameterAsBool(parameters, self.COMMIT, context)

        for layer in layers:
            if feedback.isCanceled():
                return {}

            idx_group = layer.fields().indexFromName(group_field) if group_field else -1
            idx_order = layer.fields().indexFromName(order_field)
            idx_x = layer.fields().indexFromName(x_field)
            idx_y = layer.fields().indexFromName(y_field)

            idx_fid = layer.fields().indexFromName(fid_field) if fid_field else -1
            idx_n = layer.fields().indexFromName(n_field) if n_field else -1
            idx_dist = layer.fields().indexFromName(dist_field) if dist_field else -1

            if idx_order < 0 or idx_x < 0 or idx_y < 0:
                feedback.pushWarning(f"Skip {layer.name()}: missing ORDER/X/Y field")
                continue
            if use_fid and idx_fid < 0:
                feedback.pushWarning(f"Skip {layer.name()}: USE_FID is on, but fid field not found")
                continue

            out_x = f"{x_field}{suffix}"
            out_y = f"{y_field}{suffix}"

            prov = layer.dataProvider()
            layer.startEditing()

            add = []
            if layer.fields().indexFromName(out_x) < 0:
                add.append(QgsField(out_x, QVariant.Double))
            if layer.fields().indexFromName(out_y) < 0:
                add.append(QgsField(out_y, QVariant.Double))
            if add:
                prov.addAttributes(add)
                layer.updateFields()

            idx_outx = layer.fields().indexFromName(out_x)
            idx_outy = layer.fields().indexFromName(out_y)

            feats = list(layer.getFeatures())
            if not feats:
                continue

            # group
            groups = {}
            for f in feats:
                key = f[idx_group] if idx_group >= 0 else "__ALL__"
                groups.setdefault(key, []).append(f)

            updates = {}

            for _, flist in groups.items():
                # collect ordered valid points
                rows = []
                for f in flist:
                    x = self._to_float(f[idx_x])
                    y = self._to_float(f[idx_y])
                    if x is None or y is None:
                        continue
                    rows.append((self._sort_key(f[idx_order]), f, x, y))

                if len(rows) < 2:
                    continue

                rows.sort(key=lambda t: t[0])
                x_coords = [t[2] for t in rows]
                y_coords = [t[3] for t in rows]
                chainage_values = self._chainage(x_coords, y_coords)

                # feature id -> chainage at its ordered index
                feat_to_s = {t[1].id(): chainage_values[i] for i, t in enumerate(rows)}

                # build fid -> chainage control points (dedupe by fid, last wins)
                if use_fid:
                    fid_to_s = {}
                    for i, (_, f, _, _) in enumerate(rows):
                        fv = self._to_float(f[idx_fid])
                        if fv is None:
                            continue
                        fid_to_s[fv] = chainage_values[i]
                    if len(fid_to_s) >= 2:
                        fid_vals = sorted(fid_to_s.keys())
                        s_at_fid = [fid_to_s[v] for v in fid_vals]
                    else:
                        fid_vals = None
                        s_at_fid = None
                else:
                    fid_vals = None
                    s_at_fid = None

                # apply to each feature (in ordered rows)
                for _, f, _, _ in rows:
                    base_s = feat_to_s[f.id()]

                    # 1) fiducial layback -> compute base_s from fid-N
                    if use_fid and fid_vals is not None:
                        fv = self._to_float(f[idx_fid])
                        if fv is not None:
                            if idx_n >= 0:
                                nv = self._to_float(f[idx_n])
                                N = int(round(nv)) if nv is not None else n_const
                            else:
                                N = n_const
                            fid_target = fv - float(N)  # backward
                            base_s = self._interp_extrap_1d(fid_vals, s_at_fid, fid_target, extrap_k)

                    if use_dist:
                        if idx_dist >= 0:
                            lv = self._to_float(f[idx_dist])
                            L = lv if lv is not None else dist_const
                        else:
                            L = dist_const
                        final_s = base_s - float(L)  # positive L = backward
                    else:
                        final_s = base_s

                    # 3) chainage -> XY on track (with regression extrap ends)
                    newx, newy = self._interp_extrap_xy_from_s(chainage_values, x_coords, y_coords, final_s, extrap_k)

                    updates[f.id()] = {idx_outx: float(newx), idx_outy: float(newy)}

            prov.changeAttributeValues(updates)
            layer.updateFields()

            if do_commit:
                layer.commitChanges()
            else:
                layer.triggerRepaint()

            feedback.pushInfo(f"{layer.name()} updated → {out_x}, {out_y}")

        return {}
