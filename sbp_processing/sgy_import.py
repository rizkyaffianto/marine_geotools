# -*- coding: utf-8 -*-
"""
SGY Bulk Import (ObsPy) → NPY + GPKG + JSON

- Reliable for marine SBP SEG-Y (variable trace length, vendor quirks)
- Applies SEG-Y coordinate scalar (scalar_to_be_applied_to_all_coordinates)
- Auto-detects coordinate encoding (degrees / arcsec / arcsec*100 / meters)
- Writes per-trace Timestamp (if header fields present)
- Optional duplicate removal + linear interpolation of invalid gaps
- Projects Input (raw) CRS → Output CRS and adds distance field
"""

import os
import json
import numpy as np
from datetime import datetime, timedelta

from obspy import read

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterCrs,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsWkbTypes,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from PyQt5.QtCore import QVariant, QDateTime


class SGYBulkImportObsPy(QgsProcessingAlgorithm):
    INPUT_FILES = "INPUT_FILES"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    RAW_CRS = "RAW_CRS"      # CRS of coordinates after scalar is applied
    GEOM_CRS = "GEOM_CRS"    # Output CRS
    CLEAN_INTERP = "CLEAN_INTERP"

    def createInstance(self):
        return SGYBulkImportObsPy()

    def name(self):
        return "sgy_bulk_import_obspy_add"

    def displayName(self):
        return "SGY Bulk Import"

    def group(self):
        return "SBP - File Import"

    def groupId(self):
        return "sbp_import"

    def shortHelpString(self):
        return (
            "Imports all SEG-Y files in a folder using ObsPy (robust for SBP).\n"
            "For each file, saves:\n"
            "  • <name>.npy      (float32, padded [n_traces × max_samples])\n"
            "  • <name>.gpkg     (per-trace points, with Timestamp, distance)\n"
            "  • <name>.json     (metadata incl. coord scale & scalar used)\n"
            "\n"
            "Options:\n"
            "  • Remove duplicate points and linearly interpolate invalid coordinate gaps\n"
            "\n"
            "CRS:\n"
            "  • Input (raw) CRS: CRS of SEG-Y navigation after applying SEG-Y scalar\n"
            "  • Output CRS: CRS of the exported navigation layer\n"
            "All outputs are written into the chosen output folder."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_FILES,
                "Input SEG-Y Files",
                layerType=QgsProcessing.TypeFile,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                "Output Folder (all files saved here)"
            )
        )
        # Input/raw CRS (navigation as it is in SEG-Y headers after scalar)
        self.addParameter(
            QgsProcessingParameterCrs(
                self.RAW_CRS,
                "Input (raw) CRS for navigation (after SEG-Y scalar)",
                defaultValue=QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        )
        # Output CRS
        self.addParameter(
            QgsProcessingParameterCrs(
                self.GEOM_CRS,
                "Output CRS for point layer",
                defaultValue=QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CLEAN_INTERP,
                "Remove duplicate geometry and linearly interpolate invalid gaps",
                defaultValue=True
            )
        )

    def _normalize_coords(self, x_arr, y_arr, coord_units_hint=None, raw_crs=None):
        """
        Coordinate normalization with FIXED rule:

          - coordinate_units == 2  → arcseconds ×100  → divide by (3600 * 100)
          - coordinate_units == 1  → length units     → leave unchanged
          - otherwise              → leave unchanged

        This matches legacy SEG-Y SBP practice and your working trackplot script.
        """
        x = np.asarray(x_arr, dtype=float)
        y = np.asarray(y_arr, dtype=float)

        # no valid coords → return as-is
        mask = np.isfinite(x) & np.isfinite(y) & (x != 0) & (y != 0)
        if not np.any(mask):
            return x, y, "unknown"

        # --- STRICT rule: arcseconds are ALWAYS arcsec*100 ---
        if coord_units_hint == 2:
            return (
                x / 3600,
                y / 3600, "arcsec"
            )

        # --- length units: trust SEG-Y scalar only ---
        if coord_units_hint == 1:
            return x, y, "length_units"

        # --- no unit info: do nothing ---
        return x, y, "unknown_no_unit_hint"

    def _extract_timestamp_qdatetime(self, hdr):
        """
        Build QDateTime from SEG-Y trace header (if possible).
        Uses: year_data_recorded, day_of_year, hour_of_day, minute_of_hour, second_of_minute
        Returns QDateTime() (invalid) if insufficient info.
        """
        try:
            y = int(getattr(hdr, "year_data_recorded", 0) or 0)
            d = int(getattr(hdr, "day_of_year", 0) or 0)
            H = int(getattr(hdr, "hour_of_day", 0) or 0)
            M = int(getattr(hdr, "minute_of_hour", 0) or 0)
            S = int(getattr(hdr, "second_of_minute", 0) or 0)
        except Exception as err:
            return QDateTime()  # invalid

        if y > 0 and 1 <= d <= 366:
            try:
                dt = datetime(y, 1, 1) + timedelta(days=d - 1, hours=H, minutes=M, seconds=S)
                return QDateTime.fromString(dt.strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss")
            except Exception as err:
                return QDateTime()

        return QDateTime()

    def _project_coords(self, x_src, y_src, in_crs, out_crs):
        """
        Generic projection: (x_src, y_src) in in_crs → (X, Y) in out_crs.

        - If in_crs is EPSG:4326 and coords are degrees, this is a normal lon/lat → projected transform.
        - If in_crs is UTM or local grid, coords are treated as that CRS and transformed accordingly.
        """
        x_src = np.asarray(x_src, dtype=float)
        y_src = np.asarray(y_src, dtype=float)

        if in_crs == out_crs:
            # No transformation needed
            return x_src.copy(), y_src.copy()

        transform = QgsCoordinateTransform(in_crs, out_crs, QgsProject.instance())
        X = np.empty_like(x_src, dtype=float)
        Y = np.empty_like(y_src, dtype=float)

        for i, (xx, yy) in enumerate(zip(x_src, y_src)):
            if np.isfinite(xx) and np.isfinite(yy) and xx != 0.0 and yy != 0.0:
                pt = transform.transform(QgsPointXY(xx, yy))
                X[i], Y[i] = pt.x(), pt.y()
            else:
                X[i], Y[i] = np.nan, np.nan
        return X, Y

    def _remove_duplicates_and_interpolate(self, X, Y, enable=True, dup_tol=1e-3):
        """
        Remove consecutive duplicate/near-duplicate points (within dup_tol meters),
        linearly interpolate geometry across invalid runs bounded by valid points,
        and linearly extrapolate at the edges if the start or end points are invalid.
        """
        Xc = X.copy()
        Yc = Y.copy()
        if not enable or len(Xc) == 0:
            return Xc, Yc

        n = len(Xc)

        # 1) Mark near-duplicate consecutive points as invalid
        last_valid_idx = None
        for i in range(n):
            xi, yi = Xc[i], Yc[i]
            if np.isfinite(xi) and np.isfinite(yi):
                if last_valid_idx is not None:
                    dx = xi - Xc[last_valid_idx]
                    dy = yi - Yc[last_valid_idx]
                    if np.hypot(dx, dy) <= dup_tol:
                        Xc[i], Yc[i] = np.nan, np.nan
                    else:
                        last_valid_idx = i
                else:
                    last_valid_idx = i

        # 2) Interpolate between valid segments
        i = 0
        while i < n:
            if not np.isfinite(Xc[i]) or not np.isfinite(Yc[i]):
                start = i - 1
                j = i
                while j < n and (not np.isfinite(Xc[j]) or not np.isfinite(Yc[j])):
                    j += 1
                end = j

                if start >= 0 and end < n and np.isfinite(Xc[start]) and np.isfinite(Xc[end]):
                    length = end - start
                    for k in range(1, length):
                        t = k / length
                        Xc[start + k] = (1 - t) * Xc[start] + t * Xc[end]
                        Yc[start + k] = (1 - t) * Yc[start] + t * Yc[end]

                i = end
            else:
                i += 1

        # 3) Extrapolate at edges if invalids remain at start or end
        # --- leading invalids ---
        valid_mask = np.isfinite(Xc) & np.isfinite(Yc)
        if np.any(valid_mask):
            first_valid = np.where(valid_mask)[0][0]
        else:
            first_valid = None

        if first_valid is not None and first_valid > 0:
            if first_valid + 1 < n and np.isfinite(Xc[first_valid + 1]):
                dx = Xc[first_valid + 1] - Xc[first_valid]
                dy = Yc[first_valid + 1] - Yc[first_valid]
                for i in range(first_valid - 1, -1, -1):
                    Xc[i] = Xc[i + 1] - dx
                    Yc[i] = Yc[i + 1] - dy

        # --- trailing invalids ---
        valid_mask = np.isfinite(Xc) & np.isfinite(Yc)
        if np.any(valid_mask):
            last_valid = np.where(valid_mask)[0][-1]
            if last_valid < n - 1 and last_valid - 1 >= 0 and np.isfinite(Xc[last_valid - 1]):
                dx = Xc[last_valid] - Xc[last_valid - 1]
                dy = Yc[last_valid] - Yc[last_valid - 1]
                for i in range(last_valid + 1, n):
                    Xc[i] = Xc[i - 1] + dx
                    Yc[i] = Yc[i - 1] + dy

        return Xc, Yc

    def _cumulative_distance(self, X, Y):
        """
        Cumulative distance (meters), starting at first valid point = 0.0.
        NaN segments (unbounded) are skipped—distance only accumulates between valid neighbors.
        """
        n = len(X)
        D = np.full(n, np.nan, dtype=float)

        # find first valid
        first = None
        for i in range(n):
            if np.isfinite(X[i]) and np.isfinite(Y[i]):
                first = i
                break
        if first is None:
            return D  # all invalid

        D[first] = 0.0
        prev = first
        for i in range(first + 1, n):
            if np.isfinite(X[i]) and np.isfinite(Y[i]) and np.isfinite(X[prev]) and np.isfinite(Y[prev]):
                D[i] = D[prev] + np.hypot(X[i] - X[prev], Y[i] - Y[prev])
                prev = i
            else:
                prev = i if np.isfinite(X[i]) and np.isfinite(Y[i]) else prev
        return D

    def _extract_t0_ms(self, st, fallback_from_traces=True):
        """
        Try to extract recording delay (t0) in milliseconds.

        Priority:
          1) Binary file header (if present)
          2) First trace header delay recording time
          3) Most common non-zero value across traces (optional)

        Returns: float (ms), default 0.0
        """
        # --- 1) Binary file header (best if consistent per file) ---
        try:
            segy0 = getattr(st[0].stats, "segy", None)
            bfh = getattr(segy0, "binary_file_header", None)
            if bfh is not None:
                v = getattr(bfh, "delay_recording_time", None)
                if v is None:
                    # Some variants might use slightly different naming
                    v = getattr(bfh, "delay_recording_time_in_ms", None)

                if v is not None:
                    v = float(v)
                    if np.isfinite(v) and abs(v) < 1e7:
                        return float(v)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        # --- 2) First trace header ---
        try:
            th = getattr(getattr(st[0].stats, "segy", None), "trace_header", None)
            if th is not None:
                v = getattr(th, "delay_recording_time", None)
                if v is None:
                    v = getattr(th, "delay_recording_time_in_ms", None)

                if v is not None:
                    v = float(v)
                    if np.isfinite(v) and abs(v) < 1e7:
                        return float(v)
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        # --- 3) Optional: scan traces for a stable non-zero delay ---
        if fallback_from_traces:
            vals = []
            try:
                for tr in st[: min(len(st), 2000)]:  # cap for speed
                    th = getattr(getattr(tr.stats, "segy", None), "trace_header", None)
                    if th is None:
                        continue
                    v = getattr(th, "delay_recording_time", None)
                    if v is None:
                        continue
                    v = float(v)
                    if np.isfinite(v) and v != 0.0 and abs(v) < 1e7:
                        vals.append(v)
                if vals:
                    # use the most common (mode-ish) value by rounding to 0.1 ms bins
                    vv = np.array(vals, dtype=float)
                    bins = np.round(vv * 10.0) / 10.0
                    uniq, cnt = np.unique(bins, return_counts=True)
                    return float(uniq[np.argmax(cnt)])
            except Exception as e:
                QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        return 0.0


    def processAlgorithm(self, parameters, context, feedback):
        files = self.parameterAsFileList(parameters, self.INPUT_FILES, context)
        if not files:
            raise QgsProcessingException("No SEG-Y files selected.")

        # IMPORTANT: use parameterAsString for FolderDestination
        out_dir = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)

        # If user picked a temporary directory or left it empty, QGIS may pass an empty string.
        # In that case, just use the directory of the first input file as the output folder.
        if not out_dir:
            out_dir = os.path.dirname(files[0])
            feedback.pushInfo("No explicit Output Folder chosen → using input file directory as output directory.")

        feedback.pushInfo(f"Output folder resolved to: {out_dir}")

        raw_crs = self.parameterAsCrs(parameters, self.RAW_CRS, context)
        out_crs = self.parameterAsCrs(parameters, self.GEOM_CRS, context)
        do_clean_interp = self.parameterAsBool(parameters, self.CLEAN_INTERP, context)

        os.makedirs(out_dir, exist_ok=True)

        nfiles = len(files)
        for fi, sgy_path in enumerate(files, start=1):
            if feedback.isCanceled():
                break

            fname = os.path.basename(sgy_path)
            base = os.path.splitext(fname)[0]
            amp_path = os.path.join(out_dir, f"{base}.npy")
            nav_path = os.path.join(out_dir, f"{base}.gpkg")
            meta_path = os.path.join(out_dir, f"{base}.json")

            feedback.pushInfo(f"[{fi}/{nfiles}] Loading {fname} with ObsPy…")

            # ... keep the rest of the function exactly as in the previous version ...

            try:
                st = read(sgy_path, format="SEGY", unpack_trace_headers=True)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to read {fname}: {e}")
                continue

            # -------- Collect traces, headers, coords --------
            traces = []
            xs_raw, ys_raw = [], []   # values AFTER SEG-Y scalar, before unit normalization
            qdatetimes = []
            coord_units_hint = None
            first_scalar_logged = False

            for ti, tr in enumerate(st):
                traces.append(tr.data.astype(np.float32, copy=False))
                hdr = getattr(tr.stats, "segy", None)
                th = getattr(hdr, "trace_header", None)

                if th is None:
                    xs_raw.append(0.0)
                    ys_raw.append(0.0)
                    qdatetimes.append(QDateTime())
                    continue

                # ---- raw coordinates from header ----
                x_hdr = getattr(th, "source_coordinate_x", 0) or getattr(th, "group_coordinate_x", 0) or 0
                y_hdr = getattr(th, "source_coordinate_y", 0) or getattr(th, "group_coordinate_y", 0) or 0

                # ---- SEG-Y scalar_to_be_applied_to_all_coordinates ----
                scalar = getattr(th, "scalar_to_be_applied_to_all_coordinates", 1)
                try:
                    scalar = int(scalar)
                except Exception as err:
                    scalar = 1

                if scalar > 0:
                    scale_fac = float(scalar)
                elif scalar < 0:
                    scale_fac = 1.0 / abs(scalar)
                else:
                    scale_fac = 1.0

                # Apply the scalar (common case: scalar = -100 → divide by 100)
                x = float(x_hdr) * scale_fac
                y = float(y_hdr) * scale_fac

                if not first_scalar_logged:
                    feedback.pushInfo(
                        f"  scalar_to_be_applied_to_all_coordinates={scalar} → scale_fac={scale_fac}"
                    )
                    first_scalar_logged = True

                xs_raw.append(x)
                ys_raw.append(y)

                # Timestamp
                qdatetimes.append(self._extract_timestamp_qdatetime(th))

                # Coordinate units hint: 1 = length, 2 = arcseconds (SEG-Y spec)
                if coord_units_hint is None:
                    cu = getattr(th, "coordinate_units", None)
                    if cu in (1, 2):
                        coord_units_hint = int(cu)

            # -------- Normalize units, then transform Input CRS → Output CRS --------
            x_norm, y_norm, scale_used = self._normalize_coords(xs_raw, ys_raw, coord_units_hint, raw_crs)
            feedback.pushInfo(
                f"  {fname}: coord scale detected = {scale_used} → transforming "
                f"{raw_crs.authid()} → {out_crs.authid()}"
            )

            X_proj, Y_proj = self._project_coords(x_norm, y_norm, raw_crs, out_crs)

            # -------- Build amplitude matrix (pad uneven) --------
            n_traces = len(traces)
            max_len = max((len(t) for t in traces), default=0)
            amp = np.zeros((n_traces, max_len), dtype=np.float32)
            ns_per_trace = []
            for i, tr in enumerate(traces):
                n = len(tr)
                ns_per_trace.append(n)
                if n > 0:
                    amp[i, :n] = tr

            # -------- dt (ms) best effort --------
            dt_ms = None
            try:
                if len(st) > 0:
                    dt_ms = st[0].stats.delta * 1000.0
                    feedback.pushInfo(f"  Detected sample interval = {dt_ms:.4f} ms")
            except Exception as err:
                feedback.pushWarning("  !! Could not determine sample interval (dt_ms)")
            
            t0_ms = self._extract_t0_ms(st)
            feedback.pushInfo(f"  Detected t0 (delay recording time) = {t0_ms:.3f} ms")
            
            
            # -------- Save amplitudes --------
            try:
                np.save(amp_path, amp)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to save amplitudes for {fname}: {e}")
                continue

            # -------- Clean duplicates & interpolate invalid gaps (optional) --------
            dup_tol = 1e-3
            if out_crs.isGeographic():
                dup_tol = 1e-8  # ~0.0011 m in latitude (very small)
            Xc, Yc = self._remove_duplicates_and_interpolate(X_proj, Y_proj, enable=do_clean_interp, dup_tol=dup_tol)


            # -------- Distance (cumulative from first valid) --------
            Dm = self._cumulative_distance(Xc, Yc)

            # -------- Build and save nav GPKG (per-trace point) --------
            fields = QgsFields()
            fields.append(QgsField("TraceID", QVariant.Int))
            fields.append(QgsField("LineName", QVariant.String))
            fields.append(QgsField("X", QVariant.Double))
            fields.append(QgsField("Y", QVariant.Double))
            fields.append(QgsField("distance", QVariant.Double))
            fields.append(QgsField("NumSamples", QVariant.Int))
            #fields.append(QgsField("SampleInt_ms", QVariant.Double))
            fields.append(QgsField("Timestamp", QVariant.DateTime))

            mem = QgsVectorLayer(f"Point?crs={out_crs.authid()}", base, "memory")
            pr = mem.dataProvider()
            pr.addAttributes(fields)
            mem.updateFields()

            for i, (x, y, dist) in enumerate(zip(Xc, Yc, Dm)):
                feat = QgsFeature()
                if np.isfinite(x) and np.isfinite(y):
                    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                qdt = qdatetimes[i] if i < len(qdatetimes) else QDateTime()
                feat.setAttributes([
                    i + 1,
                    base,
                    float(x) if np.isfinite(x) else None,
                    float(y) if np.isfinite(y) else None,
                    float(dist) if np.isfinite(dist) else None,
                    int(ns_per_trace[i]) if i < len(ns_per_trace) else None,
                    #float(dt_ms) if dt_ms is not None else None,
                    qdt
                ])
                pr.addFeature(feat)

            QgsVectorFileWriter.writeAsVectorFormatV3(
                mem,
                nav_path,
                QgsCoordinateTransformContext(),
                QgsVectorFileWriter.SaveVectorOptions()
            )

            # ---- Load into QGIS project ----
            try:
                nav_layer = QgsVectorLayer(nav_path, base, "ogr")
                if nav_layer and nav_layer.isValid():
                    QgsProject.instance().addMapLayer(nav_layer)
                    feedback.pushInfo(f"  → Loaded layer: {base}")
                else:
                    feedback.pushWarning(f"  !! Failed to load nav layer for {base}")
            except Exception as e:
                feedback.pushWarning(f"  !! Could not add {base} to project: {e}")

            # -------- Write metadata JSON --------
            meta = {
                "line_name": base,
                "source_file": fname,
                "created": datetime.now().isoformat(timespec="seconds"),
                "num_traces": int(n_traces),
                "max_samples_per_trace": int(max_len),
                "sample_interval_ms": float(dt_ms) if dt_ms is not None else None,
                "t0_ms": float(t0_ms),
                "coord_units_hint": coord_units_hint,
                "coord_scale_detected": scale_used,
                "input_crs": raw_crs.authid(),
                "output_crs": out_crs.authid(),
                "clean_interpolate": bool(do_clean_interp),
                # velocities kept for future depth conversion
                "sound_velocity_water_m_s": 1500.0,
                "sound_velocity_sediment_m_s": 1500.0,
            }

            try:
                with open(meta_path, "w", encoding="utf-8") as jf:
                    json.dump(meta, jf, indent=2)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to write meta for {fname}: {e}")

            feedback.pushInfo(
                f"  ✓ Saved: {os.path.basename(amp_path)}, {os.path.basename(nav_path)}, {os.path.basename(meta_path)}"
            )
            feedback.setProgress(int(fi / nfiles * 100))

        feedback.pushInfo("✅ Finished SGY bulk import (ObsPy).")
        return {}
