# -*- coding: utf-8 -*-
"""
XTF SBP Bulk Import → NPY + GPKG + JSON (SGY-compatible outputs)

✅ SGY-compatible outputs
- Output files per XTF:
    <base>_CH#.npy, <base>_CH#.gpkg, <base>_CH#.json
- Output GPKG fields (same as SGY Bulk Import):
    TraceID, LineName, X, Y, distance, NumSamples, Timestamp
- JSON keys aligned to SGY Bulk Import:
    line_name, source_file, created, num_traces, max_samples_per_trace,
    sample_interval_ms, t0_ms, coord_units_hint, coord_scale_detected,
    input_crs, output_crs, clean_interpolate,
    sound_velocity_water_m_s, sound_velocity_sediment_m_s

✅ XTF-specific additions
- SBP Channel selector (Channel 1/2/3) → counts SUBBOTTOM channels only (SSS excluded)
- Computes:
    t0_ms from PingChanHeader.TimeDelay (seconds → ms)
    sample_interval_ms from PingChanHeader.TimeDuration / NumSamples (seconds/sample → ms)
"""

import os
import json
import struct
import numpy as np
from datetime import datetime

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterCrs,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from qgis.PyQt.QtCore import QVariant, QDateTime



XTF_MAGIC = 0xFACE
XTF_HEADER_SONAR = 0

# CHANINFO TypeOfChannel (Rev.42 commonly: 0=SUBBOTTOM, 1=PORT, 2=STBD, 3=BATHY)
TYPE_SUBBOTTOM = 0

PING_HEADER_SIZE = 256
PING_CHAN_HEADER_SIZE = 64
CHANINFO_SIZE = 128
CHANINFO_OFFSET = 256

# Ping header offsets (Rev.42)
OFF_NUM_BYTES_THIS_RECORD = 10   # WORD
OFF_YEAR = 14                    # WORD
OFF_MONTH = 16                   # BYTE
OFF_DAY = 17                     # BYTE
OFF_HOUR = 18                    # BYTE
OFF_MINUTE = 19                  # BYTE
OFF_SECOND = 20                  # BYTE
OFF_HSEC = 21                    # BYTE
OFF_SENSOR_Y = 160               # double
OFF_SENSOR_X = 168               # double
OFF_NUM_CHANS_TO_FOLLOW = 4      # WORD

# Ping channel header offsets (Rev.42)
OFF_CHAN_NUMBER = 0              # WORD
OFF_TIMEDELAY = 12               # float (seconds)
OFF_TIMEDURATION = 16            # float (seconds)
OFF_NUMSAMPLES = 42              # DWORD



def _clean_cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

def _u8(b, off):   return b[off]
def _u16(b, off):  return struct.unpack_from("<H", b, off)[0]
def _u32(b, off):  return struct.unpack_from("<I", b, off)[0]
def _f32(b, off):  return struct.unpack_from("<f", b, off)[0]
def _f64(b, off):  return struct.unpack_from("<d", b, off)[0]

def _qdatetime_from_parts(y, m, d, H, M, S, hsec):
    try:
        ms = int(hsec) * 10  # hundredths → ms
        qdt = QDateTime.fromString(
            f"{y:04d}-{m:02d}-{d:02d} {H:02d}:{M:02d}:{S:02d}",
            "yyyy-MM-dd HH:mm:ss"
        )
        if qdt.isValid():
            qdt = qdt.addMSecs(ms)
        return qdt
    except Exception as err:
        return QDateTime()

def _project_coords(x_src, y_src, in_crs, out_crs):
    x_src = np.asarray(x_src, dtype=float)
    y_src = np.asarray(y_src, dtype=float)

    if in_crs == out_crs:
        return x_src.copy(), y_src.copy()

    tr = QgsCoordinateTransform(in_crs, out_crs, QgsProject.instance())
    X = np.full_like(x_src, np.nan, dtype=float)
    Y = np.full_like(y_src, np.nan, dtype=float)

    for i, (x, y) in enumerate(zip(x_src, y_src)):
        if np.isfinite(x) and np.isfinite(y) and x != 0.0 and y != 0.0:
            p = tr.transform(QgsPointXY(float(x), float(y)))
            X[i], Y[i] = p.x(), p.y()
    return X, Y

def _remove_duplicates_and_interpolate(X, Y, enable=True, dup_tol=1e-3):
    Xc = X.copy()
    Yc = Y.copy()
    if not enable or len(Xc) == 0:
        return Xc, Yc

    n = len(Xc)

    # 1) mark near-duplicate consecutive points invalid
    last_valid = None
    for i in range(n):
        if np.isfinite(Xc[i]) and np.isfinite(Yc[i]):
            if last_valid is None:
                last_valid = i
            else:
                if np.hypot(Xc[i] - Xc[last_valid], Yc[i] - Yc[last_valid]) <= dup_tol:
                    Xc[i], Yc[i] = np.nan, np.nan
                else:
                    last_valid = i

    # 2) interpolate bounded gaps
    i = 0
    while i < n:
        if not (np.isfinite(Xc[i]) and np.isfinite(Yc[i])):
            start = i - 1
            j = i
            while j < n and not (np.isfinite(Xc[j]) and np.isfinite(Yc[j])):
                j += 1
            end = j
            if start >= 0 and end < n and np.isfinite(Xc[start]) and np.isfinite(Xc[end]):
                L = end - start
                for k in range(1, L):
                    t = k / L
                    Xc[start + k] = (1 - t) * Xc[start] + t * Xc[end]
                    Yc[start + k] = (1 - t) * Yc[start] + t * Yc[end]
            i = end
        else:
            i += 1

    valid = np.isfinite(Xc) & np.isfinite(Yc)
    if np.any(valid):
        first = np.where(valid)[0][0]
        last = np.where(valid)[0][-1]

        if first > 0 and first + 1 < n and np.isfinite(Xc[first + 1]):
            dx = Xc[first + 1] - Xc[first]
            dy = Yc[first + 1] - Yc[first]
            for ii in range(first - 1, -1, -1):
                Xc[ii] = Xc[ii + 1] - dx
                Yc[ii] = Yc[ii + 1] - dy

        if last < n - 1 and last - 1 >= 0 and np.isfinite(Xc[last - 1]):
            dx = Xc[last] - Xc[last - 1]
            dy = Yc[last] - Yc[last - 1]
            for ii in range(last + 1, n):
                Xc[ii] = Xc[ii - 1] + dx
                Yc[ii] = Yc[ii - 1] + dy

    return Xc, Yc

def _cumulative_distance(X, Y):
    n = len(X)
    D = np.full(n, np.nan, dtype=float)

    first = None
    for i in range(n):
        if np.isfinite(X[i]) and np.isfinite(Y[i]):
            first = i
            break
    if first is None:
        return D

    D[first] = 0.0
    prev = first
    for i in range(first + 1, n):
        if np.isfinite(X[i]) and np.isfinite(Y[i]) and np.isfinite(X[prev]) and np.isfinite(Y[prev]):
            D[i] = D[prev] + np.hypot(X[i] - X[prev], Y[i] - Y[prev])
            prev = i
    return D

def _read_chaninfo_list(f):
    # counts at offsets 166/168
    f.seek(166)
    num_sonar = struct.unpack("<H", f.read(2))[0]
    num_bathy = struct.unpack("<H", f.read(2))[0]
    total = num_sonar + num_bathy

    f.seek(CHANINFO_OFFSET)
    chans = []
    for i in range(total):
        raw = f.read(CHANINFO_SIZE)
        if len(raw) < CHANINFO_SIZE:
            raise EOFError("Unexpected EOF while reading CHANINFO.")
        type_of_channel = struct.unpack_from("<B", raw, 0)[0]
        subch = struct.unpack_from("<B", raw, 1)[0]
        bps = struct.unpack_from("<H", raw, 6)[0]
        name_b = struct.unpack_from("<16s", raw, 12)[0]
        freq = struct.unpack_from("<f", raw, 32)[0]
        sample_format = struct.unpack_from("<B", raw, 74)[0]
        chans.append({
            "index": i,
            "TypeOfChannel": int(type_of_channel),
            "SubChannelNumber": int(subch),
            "BytesPerSample": int(bps),
            "ChannelName": _clean_cstr(name_b),
            "Frequency": float(freq),
            "SampleFormat": int(sample_format),
        })
    return num_sonar, num_bathy, chans

def _dtype_for_samples(bytes_per_sample, sample_format):
    # Best-effort mapping using SampleFormat + byte size
    if sample_format == 5 and bytes_per_sample == 4:
        return np.dtype("<f4")
    if sample_format == 2 and bytes_per_sample == 4:
        return np.dtype("<i4")
    if sample_format == 3 and bytes_per_sample == 2:
        return np.dtype("<i2")
    if sample_format == 8 and bytes_per_sample == 1:
        return np.dtype("<i1")

    if bytes_per_sample == 2:
        return np.dtype("<i2")
    if bytes_per_sample == 4:
        return np.dtype("<i4")
    return np.dtype("<u1")

def _compute_header_size(total_channels):
    # 1024 bytes header blocks; >6 channels extends header by 1024 blocks
    blocks = (total_channels + 5) // 6  # 6 CHANINFO per 1024 block
    return 1024 * max(1, blocks)

def _compute_t0_ms_from_timedelay(timedelay_s_list):
    """
    Compute file-level t0_ms from per-trace TimeDelay (seconds → ms).
    Robust: mode-ish (0.1 ms bins) if stable, else median.
    """
    td = np.asarray(timedelay_s_list, dtype=float)
    td = td[np.isfinite(td)]
    if td.size == 0:
        return 0.0

    t0_ms = td * 1000.0
    nonzero = t0_ms[np.abs(t0_ms) > 1e-9]
    if nonzero.size > 0:
        t0_ms = nonzero

    bins = np.round(t0_ms * 10.0) / 10.0
    uniq, cnt = np.unique(bins, return_counts=True)
    mode_val = float(uniq[np.argmax(cnt)])

    if cnt.max() >= max(3, int(0.2 * t0_ms.size)):
        return mode_val

    return float(np.median(t0_ms))

def _compute_dt_ms_from_duration(ns_list, timeduration_s_list):
    """
    Compute file-level sample_interval_ms from TimeDuration/NumSamples.
    Returns None if not enough valid data.
    """
    ns = np.asarray(ns_list, dtype=float)
    td = np.asarray(timeduration_s_list, dtype=float)
    good = np.isfinite(ns) & np.isfinite(td) & (ns > 0) & (td > 0)

    if not np.any(good):
        return None

    dt_ms = (td[good] / ns[good]) * 1000.0
    # optional sanity filter (0 < dt < 1000 ms)
    dt_ms = dt_ms[(dt_ms > 0.0) & (dt_ms < 1000.0)]
    if dt_ms.size == 0:
        return None

    # robust single value
    return float(np.median(dt_ms))


def _extract_sbp_from_xtf(path, chaninfo, sbp_target_index):
    """
    Extract SBP samples + nav/time from SONAR packets for selected SUBBOTTOM channel.

    Returns (9 values):
      xs, ys, qdts, ns_list, samples_list, timedelay_s_list, timeduration_s_list,
      selected_file_chan, selected_chaninfo
    """
    sbp_file_chan_indices = [c["index"] for c in chaninfo if c["TypeOfChannel"] == TYPE_SUBBOTTOM]
    if not sbp_file_chan_indices:
        raise QgsProcessingException("No SUBBOTTOM channels found in CHANINFO.")

    if sbp_target_index < 0 or sbp_target_index >= len(sbp_file_chan_indices):
        raise QgsProcessingException(
            f"Selected SBP Channel {sbp_target_index+1} not available. "
            f"File has {len(sbp_file_chan_indices)} SBP channel(s)."
        )

    selected_file_chan = sbp_file_chan_indices[sbp_target_index]
    ci = chaninfo[selected_file_chan]
    bps = int(ci["BytesPerSample"])
    dt = _dtype_for_samples(bps, int(ci["SampleFormat"]))

    xs, ys, qdts = [], [], []
    ns_list, samples_list = [], []
    timedelay_s_list, timeduration_s_list = [], []

    with open(path, "rb") as f:
        # jump to first packet after header
        total = len(chaninfo)
        f.seek(_compute_header_size(total))

        while True:
            start = f.tell()
            b2 = f.read(2)
            if not b2 or len(b2) < 2:
                break

            magic = struct.unpack("<H", b2)[0]
            if magic != XTF_MAGIC:
                f.seek(start + 1)
                continue

            rest = f.read(PING_HEADER_SIZE - 2)
            if len(rest) < (PING_HEADER_SIZE - 2):
                break
            ph = b2 + rest

            header_type = _u8(ph, 2)
            nbytes = _u16(ph, OFF_NUM_BYTES_THIS_RECORD)
            if nbytes < PING_HEADER_SIZE:
                f.seek(start + 1)
                continue

            if header_type != XTF_HEADER_SONAR:
                f.seek(start + nbytes)
                continue

            num_chans = _u16(ph, OFF_NUM_CHANS_TO_FOLLOW)

            y = _u16(ph, OFF_YEAR)
            m = _u8(ph, OFF_MONTH)
            d = _u8(ph, OFF_DAY)
            H = _u8(ph, OFF_HOUR)
            M = _u8(ph, OFF_MINUTE)
            S = _u8(ph, OFF_SECOND)
            hs = _u8(ph, OFF_HSEC)
            qdt = _qdatetime_from_parts(y, m, d, H, M, S, hs)

            sy = _f64(ph, OFF_SENSOR_Y)
            sx = _f64(ph, OFF_SENSOR_X)

            chosen = None
            chosen_ns = None
            chosen_tdelay = None
            chosen_tdur = None

            for _ in range(num_chans):
                chh = f.read(PING_CHAN_HEADER_SIZE)
                if len(chh) < PING_CHAN_HEADER_SIZE:
                    break

                ch_num = struct.unpack_from("<H", chh, OFF_CHAN_NUMBER)[0]
                ns = struct.unpack_from("<I", chh, OFF_NUMSAMPLES)[0]
                tdelay = struct.unpack_from("<f", chh, OFF_TIMEDELAY)[0]
                tdur = struct.unpack_from("<f", chh, OFF_TIMEDURATION)[0]

                payload_nbytes = int(ns) * bps
                payload = f.read(payload_nbytes)
                if len(payload) < payload_nbytes:
                    break

                if 0 <= ch_num < len(chaninfo) and chaninfo[ch_num]["TypeOfChannel"] == TYPE_SUBBOTTOM:
                    if ch_num == selected_file_chan:
                        chosen = np.frombuffer(payload, dtype=dt, count=int(ns)).astype(np.float32, copy=False)
                        chosen_ns = int(ns)
                        chosen_tdelay = float(tdelay)
                        chosen_tdur = float(tdur)

            if chosen is None:
                f.seek(start + nbytes)
                continue

            xs.append(float(sx))
            ys.append(float(sy))
            qdts.append(qdt)
            ns_list.append(int(chosen_ns))
            samples_list.append(chosen)
            timedelay_s_list.append(chosen_tdelay if chosen_tdelay is not None else 0.0)
            timeduration_s_list.append(chosen_tdur if chosen_tdur is not None else 0.0)

            f.seek(start + nbytes)

    return (
        xs, ys, qdts, ns_list, samples_list,
        timedelay_s_list, timeduration_s_list,
        selected_file_chan, ci
    )



class XTFSubbottomBulkImport_SGYCompat(QgsProcessingAlgorithm):
    INPUT_FILES = "INPUT_FILES"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    RAW_CRS = "RAW_CRS"
    GEOM_CRS = "GEOM_CRS"
    CLEAN_INTERP = "CLEAN_INTERP"
    SBP_CHOICE = "SBP_CHOICE"

    def createInstance(self):
        return XTFSubbottomBulkImport_SGYCompat()

    def name(self):
        return "xtf_sbp_bulk_import_sgy_compat"

    def displayName(self):
        return "XTF SBP Bulk Import"

    def group(self):
        return "SBP - File Import"

    def groupId(self):
        return "sbp_import"

    def shortHelpString(self):
        return (
            "Imports all XTF files in a folder (Subbottom/SBP only) and exports outputs matching SGY Bulk Import.\n"
            "Per file saves:\n"
            "  • <name>_CH#.npy  (float32, padded [n_traces × max_samples])\n"
            "  • <name>_CH#.gpkg (per-trace points with TraceID/LineName/X/Y/distance/NumSamples/Timestamp)\n"
            "  • <name>_CH#.json (metadata keys aligned with SGY script)\n"
            "\n"
            "SBP Channel 1/2/3 refers to the 1st/2nd/3rd SUBBOTTOM channel in CHANINFO (SSS excluded).\n"
            "CRS workflow:\n"
            "  • Input (raw) CRS: CRS of XTF SensorX/Y\n"
            "  • Output CRS: CRS of exported point layer\n"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_FILES,
                "Input XTF Files",
                layerType=QgsProcessing.TypeFile,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                "Output Folder (all files saved here)"
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SBP_CHOICE,
                "SBP Channel (SUBBOTTOM only)",
                options=["Channel 1", "Channel 2", "Channel 3"],
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.RAW_CRS,
                "Input (raw) CRS for navigation (XTF SensorX/Y)",
                defaultValue="EPSG:4326",
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.GEOM_CRS,
                "Output CRS for point layer",
                defaultValue="EPSG:4326",
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CLEAN_INTERP,
                "Remove duplicate geometry and linearly interpolate invalid gaps",
                defaultValue=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        files = self.parameterAsFileList(parameters, self.INPUT_FILES, context)
        if not files:
            raise QgsProcessingException("No XTF files selected.")

        out_dir = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if not out_dir:
            out_dir = os.path.dirname(files[0])
            feedback.pushInfo("No explicit Output Folder chosen → using input file directory as output directory.")

        raw_crs = self.parameterAsCrs(parameters, self.RAW_CRS, context)
        out_crs = self.parameterAsCrs(parameters, self.GEOM_CRS, context)
        do_clean_interp = self.parameterAsBool(parameters, self.CLEAN_INTERP, context)
        sbp_target_index = int(self.parameterAsEnum(parameters, self.SBP_CHOICE, context))  # 0..2

        os.makedirs(out_dir, exist_ok=True)

        nfiles = len(files)
        for fi, xtf_path in enumerate(files, start=1):
            if feedback.isCanceled():
                break

            fname = os.path.basename(xtf_path)
            base = os.path.splitext(fname)[0]

            ch_tag = f"CH{sbp_target_index+1}"
            amp_path = os.path.join(out_dir, f"{base}_{ch_tag}.npy")
            nav_path = os.path.join(out_dir, f"{base}_{ch_tag}.gpkg")
            meta_path = os.path.join(out_dir, f"{base}_{ch_tag}.json")

            feedback.pushInfo(f"[{fi}/{nfiles}] Loading {fname} …")

            # Read CHANINFO
            try:
                with open(xtf_path, "rb") as f:
                    num_sonar, num_bathy, chaninfo = _read_chaninfo_list(f)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to read header/CHANINFO: {e}")
                continue

            sbp_count = sum(1 for c in chaninfo if c["TypeOfChannel"] == TYPE_SUBBOTTOM)
            if sbp_count == 0:
                feedback.pushWarning("  !! No SUBBOTTOM channels found. Skipping.")
                continue
            if sbp_target_index >= sbp_count:
                feedback.pushWarning(f"  !! Selected Channel {sbp_target_index+1} not available (only {sbp_count}). Skipping.")
                continue

            # Extract SBP traces (+ timedelay + duration)
            try:
                (xs_raw, ys_raw, qdts, ns_per_trace, traces_list,
                 timedelay_s_list, timeduration_s_list,
                 selected_file_chan, selected_ci) = _extract_sbp_from_xtf(xtf_path, chaninfo, sbp_target_index)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to read SBP packets: {e}")
                continue

            n_traces = len(traces_list)
            if n_traces == 0:
                feedback.pushWarning("  !! No SBP traces extracted. Skipping.")
                continue

            # Transform coords
            feedback.pushInfo(f"  Transforming {raw_crs.authid()} → {out_crs.authid()}")
            X_proj, Y_proj = _project_coords(xs_raw, ys_raw, raw_crs, out_crs)

            dup_tol = 1e-3
            if out_crs.isGeographic():
                dup_tol = 1e-8
            Xc, Yc = _remove_duplicates_and_interpolate(X_proj, Y_proj, enable=do_clean_interp, dup_tol=dup_tol)

            # Distance
            Dm = _cumulative_distance(Xc, Yc)

            # Build padded amplitude matrix like SGY
            max_len = max(ns_per_trace) if ns_per_trace else 0
            amp = np.zeros((n_traces, max_len), dtype=np.float32)
            for i, tr in enumerate(traces_list):
                n = len(tr)
                if n > 0:
                    amp[i, :n] = tr[:n]

            # Compute dt_ms + t0_ms from XTF channel header values
            sample_interval_ms = _compute_dt_ms_from_duration(ns_per_trace, timeduration_s_list)
            if sample_interval_ms is not None:
                feedback.pushInfo(f"  Detected sample_interval_ms = {sample_interval_ms:.6f} ms (from TimeDuration/NumSamples)")
            else:
                feedback.pushWarning("  !! sample_interval_ms could not be computed (missing/invalid TimeDuration/NumSamples)")

            t0_ms = _compute_t0_ms_from_timedelay(timedelay_s_list)
            feedback.pushInfo(f"  Detected t0_ms = {t0_ms:.3f} ms (from TimeDelay)")

            # Save amplitudes
            try:
                np.save(amp_path, amp)
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to save amplitudes: {e}")
                continue

            # Build and save nav GPKG with SAME fields as SGY
            fields = QgsFields()
            fields.append(QgsField("TraceID", QVariant.Int))
            fields.append(QgsField("LineName", QVariant.String))
            fields.append(QgsField("X", QVariant.Double))
            fields.append(QgsField("Y", QVariant.Double))
            fields.append(QgsField("distance", QVariant.Double))
            fields.append(QgsField("NumSamples", QVariant.Int))
            fields.append(QgsField("Timestamp", QVariant.DateTime))

            mem = QgsVectorLayer(f"Point?crs={out_crs.authid()}", f"{base}_{ch_tag}", "memory")
            pr = mem.dataProvider()
            pr.addAttributes(fields)
            mem.updateFields()

            for i in range(n_traces):
                feat = QgsFeature()
                x = Xc[i] if i < len(Xc) else np.nan
                y = Yc[i] if i < len(Yc) else np.nan
                if np.isfinite(x) and np.isfinite(y):
                    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))

                qdt = qdts[i] if i < len(qdts) else QDateTime()

                feat.setAttributes([
                    int(i + 1),
                    base,
                    float(x) if np.isfinite(x) else None,
                    float(y) if np.isfinite(y) else None,
                    float(Dm[i]) if i < len(Dm) and np.isfinite(Dm[i]) else None,
                    int(ns_per_trace[i]) if i < len(ns_per_trace) else None,
                    qdt
                ])
                pr.addFeature(feat)

            QgsVectorFileWriter.writeAsVectorFormatV3(
                mem,
                nav_path,
                QgsCoordinateTransformContext(),
                QgsVectorFileWriter.SaveVectorOptions()
            )

            # Load into QGIS (same behavior)
            try:
                nav_layer = QgsVectorLayer(nav_path, f"{base}_{ch_tag}", "ogr")
                if nav_layer and nav_layer.isValid():
                    QgsProject.instance().addMapLayer(nav_layer)
                    feedback.pushInfo(f"  → Loaded layer: {base}_{ch_tag}")
                else:
                    feedback.pushWarning(f"  !! Failed to load nav layer for {base}_{ch_tag}")
            except Exception as e:
                feedback.pushWarning(f"  !! Could not add {base}_{ch_tag} to project: {e}")

            meta = {
                "line_name": base,
                "source_file": fname,
                "created": datetime.now().isoformat(timespec="seconds"),
                "num_traces": int(n_traces),
                "max_samples_per_trace": int(max_len),
                "sample_interval_ms": float(sample_interval_ms) if sample_interval_ms is not None else None,
                "t0_ms": float(t0_ms),
                "coord_units_hint": None,          # SEG-Y specific; not used in XTF
                "coord_scale_detected": "xtf_raw", # no SEG-Y scalar workflow
                "input_crs": raw_crs.authid(),
                "output_crs": out_crs.authid(),
                "clean_interpolate": bool(do_clean_interp),
                "sound_velocity_water_m_s": 1500.0,
                "sound_velocity_sediment_m_s": 1500.0,

                "xtf_selected_subbottom_channel_choice": int(sbp_target_index + 1),
                "xtf_selected_file_channel_index": int(selected_file_chan),
                "xtf_selected_chaninfo": selected_ci,
                "xtf_header_counts": {"sonar": int(num_sonar), "bathy": int(num_bathy)},
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

        feedback.pushInfo("✅ Finished XTF SBP bulk import (SGY-compatible).")
        return {}
