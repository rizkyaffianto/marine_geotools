# -*- coding: utf-8 -*-
"""
CSV / Delimited Text Bulk Import → GPKG
=========================================
Imports one or more delimited text files (.csv, .txt, .tsv, .dat) as
point layers and exports each to a GeoPackage (.gpkg) in a chosen folder.

Behaviour
---------
- Delimiter: auto-detected via csv.Sniffer, or user can override.
- X / Y columns: common names are auto-detected (case-insensitive).
  Falls back to user-specified column names if auto-detection fails.
- All other columns are imported as string attributes.
- Rows with missing or non-numeric X / Y values are skipped with a warning.
- All output layers are auto-loaded into the QGIS project after import.
- One GPKG is produced per input file; all saved in the output folder.

Auto-detected X column names (case-insensitive):
    longitude, lon, long, x, easting, east

Auto-detected Y column names (case-insensitive):
    latitude, lat, y, northing, north
"""

import csv
import io
import os
from datetime import datetime as _dt

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant, QDate, QTime, QDateTime


# Constants

# Auto-detect candidate column names (lower-cased for comparison)
_AUTO_X_NAMES = {"longitude", "lon", "long", "x", "easting", "east"}
_AUTO_Y_NAMES = {"latitude", "lat", "y", "northing", "north"}

# Delimiter choices presented to the user
_DELIMITERS = ["Auto-detect", ",", ";", "\t", " ", "|"]
_DELIMITER_LABELS = ["Auto-detect", "Comma (,)", "Semicolon (;)", "Tab (\\t)", "Space ( )", "Pipe (|)"]

# Date / Time format candidates (tried in order; first match wins per column)
_DATE_FORMATS = [
    "%Y-%m-%d",   # 2024-01-31
    "%d/%m/%Y",   # 31/01/2024
    "%m/%d/%Y",   # 01/31/2024
    "%d-%m-%Y",   # 31-01-2024
    "%Y%m%d",     # 20240131
    "%d.%m.%Y",   # 31.01.2024
]

_TIME_FORMATS = [
    "%H:%M:%S",      # 13:05:09
    "%H:%M:%S.%f",   # 13:05:09.123456 or 13:05:09.099 (1-6 digit sub-seconds)
    "%H:%M",         # 13:05
    "%I:%M:%S %p",   # 01:05:09 PM
    "%I:%M %p",      # 01:05 PM
]

_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",    # 2024-01-31 13:05:09
    "%Y-%m-%dT%H:%M:%S",    # 2024-01-31T13:05:09  (ISO 8601)
    "%Y-%m-%dT%H:%M:%SZ",   # 2024-01-31T13:05:09Z
    "%Y-%m-%d %H:%M:%S.%f", # 2024-01-31 13:05:09.123
    "%Y-%m-%dT%H:%M:%S.%f", # 2024-01-31T13:05:09.123
    "%d/%m/%Y %H:%M:%S",    # 31/01/2024 13:05:09
    "%m/%d/%Y %H:%M:%S",    # 01/31/2024 13:05:09
    "%d-%m-%Y %H:%M:%S",    # 31-01-2024 13:05:09
    "%d.%m.%Y %H:%M:%S",    # 31.01.2024 13:05:09
    "%Y%m%d%H%M%S",         # 20240131130509
]


# Helper utilities

def _sniff_delimiter(sample: str) -> str:
    """Use csv.Sniffer to detect the delimiter from the first few lines."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t |")
        return dialect.delimiter
    except csv.Error:
        return ","  # safe fallback


def _find_xy_columns(headers_lower: list, x_hint: str, y_hint: str):
    """
    Return (x_idx, y_idx) using this priority order:

      1. User-supplied hint (case-insensitive exact match) — highest priority.
         If the user typed a column name, that column is always used.
      2. Auto-detection from well-known names — only when no hint is given.

    Returns (None, None) if resolution fails for either axis.
    """
    def _find_by_hint(hint):
        h = hint.strip().lower()
        for i, col in enumerate(headers_lower):
            if col == h:
                return i
        return None

    def _find_by_autodetect(known_names):
        for i, col in enumerate(headers_lower):
            if col in known_names:
                return i
        return None

    # X axis
    if x_hint and x_hint.strip():
        x_idx = _find_by_hint(x_hint)
    else:
        x_idx = _find_by_autodetect(_AUTO_X_NAMES)

    # Y axis
    if y_hint and y_hint.strip():
        y_idx = _find_by_hint(y_hint)
    else:
        y_idx = _find_by_autodetect(_AUTO_Y_NAMES)

    return x_idx, y_idx


def _safe_float(val: str):
    """Return float or None."""
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _safe_int(val: str):
    """Return int or None (only for values that are strictly integer-shaped)."""
    s = str(val).strip()
    try:
        i = int(s)
        if '.' not in s and 'e' not in s.lower():
            return i
        return None
    except (ValueError, TypeError):
        return None


def _safe_date(val: str):
    """Try all _DATE_FORMATS; return (parsed_str, fmt) or None."""
    s = val.strip()
    for fmt in _DATE_FORMATS:
        try:
            d = _dt.strptime(s, fmt)
            return d, fmt
        except ValueError:
            continue
    return None


def _safe_time(val: str):
    """Try all _TIME_FORMATS; return (parsed_str, fmt) or None."""
    s = val.strip()
    for fmt in _TIME_FORMATS:
        try:
            t = _dt.strptime(s, fmt)
            return t, fmt
        except ValueError:
            continue
    return None


def _safe_datetime(val: str):
    """Try all _DATETIME_FORMATS; return (parsed_str, fmt) or None."""
    s = val.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            dt = _dt.strptime(s, fmt)
            return dt, fmt
        except ValueError:
            continue
    return None


# Type state constants (ordered from most specific to least)
_STATE_DATETIME = 'datetime'
_STATE_DATE     = 'date'
_STATE_TIME     = 'time'
_STATE_INT      = 'int'
_STATE_DOUBLE   = 'double'
_STATE_STRING   = 'string'

# Map from state → QVariant type
_STATE_TO_QTYPE = {
    _STATE_DATETIME: QVariant.DateTime,
    _STATE_DATE:     QVariant.Date,
    _STATE_TIME:     QVariant.Time,
    _STATE_INT:      QVariant.Int,
    _STATE_DOUBLE:   QVariant.Double,
    _STATE_STRING:   QVariant.String,
}

_QTYPE_LABEL = {
    QVariant.DateTime: 'DateTime',
    QVariant.Date:     'Date',
    QVariant.Time:     'Time (stored as DateTime with date 1900-01-01; combine with Date column later)',
    QVariant.Int:      'Int',
    QVariant.Double:   'Double',
    QVariant.String:   'String',
}

_NULL_STRINGS = {'', 'nan', 'null', 'none', 'na', 'n/a'}


def _infer_col_types(all_rows: list, n_cols: int) -> list:
    """
    Scan all data rows and decide the best QVariant type for each column.

    Priority waterfall (most specific first):
      DateTime > Date > Time > Int > Double > String

    Empty / null-like cells are ignored so sparse columns keep their type.
    Detection uses the first file only; the result is reused for all
    subsequent files in the same batch run.
    """
    # Start each column as 'int' — will demote as evidence accumulates.
    # We treat DateTime/Date/Time as separate tracks: a column starts
    # trying datetime; if that fails it tries date, then time, then numeric.
    # We use two parallel states per column:
    #   temporal_state: 'datetime' | 'date' | 'time' | None (failed temporal)
    #   numeric_state:  'int' | 'double' | 'string'
    temporal  = [_STATE_DATETIME] * n_cols  # optimistic temporal guess
    numeric   = [_STATE_INT]      * n_cols  # optimistic numeric guess
    has_value = [False]           * n_cols  # whether any non-null seen

    for row in all_rows:
        for ci in range(n_cols):
            raw = row[ci].strip() if ci < len(row) else ''
            if raw == '' or raw.lower() in _NULL_STRINGS:
                continue
            has_value[ci] = True

            # ---- Temporal track ----
            if temporal[ci] == _STATE_DATETIME:
                if _safe_datetime(raw) is None:
                    temporal[ci] = _STATE_DATE
            if temporal[ci] == _STATE_DATE:
                if _safe_date(raw) is None:
                    temporal[ci] = _STATE_TIME
            if temporal[ci] == _STATE_TIME:
                if _safe_time(raw) is None:
                    temporal[ci] = None  # no temporal match

            # ---- Numeric track ----
            if numeric[ci] != _STATE_STRING:
                if numeric[ci] == _STATE_INT:
                    if _safe_int(raw) is None:
                        if _safe_float(raw) is not None:
                            numeric[ci] = _STATE_DOUBLE
                        else:
                            numeric[ci] = _STATE_STRING
                elif numeric[ci] == _STATE_DOUBLE:
                    if _safe_float(raw) is None:
                        numeric[ci] = _STATE_STRING

    # ---- Resolve final type per column ----
    result = []
    for ci in range(n_cols):
        if not has_value[ci]:
            result.append(QVariant.String)  # all-empty column → String
            continue

        t = temporal[ci]
        if t in (_STATE_DATETIME, _STATE_DATE, _STATE_TIME):
            result.append(_STATE_TO_QTYPE[t])
        else:
            result.append(_STATE_TO_QTYPE.get(numeric[ci], QVariant.String))

    return result


def _coerce(val: str, qtype):
    """
    Cast a string cell value to the inferred QGIS type.
    Returns None for empty / unparseable values so QGIS stores NULL.
    """
    s = val.strip()
    if s == '' or s.lower() in _NULL_STRINGS:
        return None

    if qtype == QVariant.Int:
        v = _safe_int(s)
        return v if v is not None else (int(_safe_float(s)) if _safe_float(s) is not None else None)

    if qtype == QVariant.Double:
        return _safe_float(s)

    if qtype == QVariant.Date:
        result = _safe_date(s)
        if result:
            d, _ = result
            return QDate(d.year, d.month, d.day)
        return None

    if qtype == QVariant.Time:
        result = _safe_time(s)
        if result:
            t, _ = result
            # GPKG has no time-only type; store as DateTime with epoch date
            # 1900-01-01 so the date can be combined later with a Date column.
            return QDateTime(
                QDate(1900, 1, 1),
                QTime(t.hour, t.minute, t.second, t.microsecond // 1000)
            )
        return None

    if qtype == QVariant.DateTime:
        result = _safe_datetime(s)
        if result:
            dt, _ = result
            return QDateTime(
                QDate(dt.year, dt.month, dt.day),
                QTime(dt.hour, dt.minute, dt.second, dt.microsecond // 1000)
            )
        return None

    return s  # String


def _to_gpkg_type(qtype):
    """
    Map a detected QVariant type to one that GeoPackage actually supports.

    GeoPackage natively supports: Int, Double, String, Date, DateTime.
    Time-only (QVariant.Time) is NOT in the GPKG spec.
    We promote it to DateTime, storing the time against epoch date 1900-01-01.
    This preserves the time value in a proper temporal field that QGIS
    can filter/sort, and allows later combination with a Date column via:
        make_datetime("DateCol", to_time("TimeCol"))
    """
    if qtype == QVariant.Time:
        return QVariant.DateTime
    return qtype


# Algorithm

class CSVBulkImportToGpkg(QgsProcessingAlgorithm):
    """Batch-import delimited text files as point layers → GPKG."""

    INPUT_FILES  = "INPUT_FILES"
    DELIMITER    = "DELIMITER"
    X_FIELD      = "X_FIELD"
    Y_FIELD      = "Y_FIELD"
    INPUT_CRS    = "INPUT_CRS"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"


    def createInstance(self):
        return CSVBulkImportToGpkg()

    def name(self):
        return "csv_bulk_import_to_gpkg"

    def displayName(self):
        return "CSV Bulk Import"

    def group(self):
        return "Database Tools"

    def groupId(self):
        return "databasetools"

    def shortHelpString(self):
        return (
            "Batch-import delimited text files (.csv, .txt, .tsv, .dat) as "
            "point layers.\n\n"
            "For each input file, one GeoPackage (.gpkg) is written to the "
            "chosen output folder and automatically loaded into the QGIS "
            "project.\n\n"
            "X / Y columns\n"
            "─────────────\n"
            "Auto-detected from common names:\n"
            "  • X: longitude, lon, long, x, easting, east\n"
            "  • Y: latitude, lat, y, northing, north\n"
            "If no match is found, the names you type in X Field / Y Field "
            "are used (case-insensitive).\n\n"
            "Delimiter\n"
            "─────────\n"
            "Choose 'Auto-detect' to let the importer sniff the separator, "
            "or pick an explicit character.\n\n"
            "All other columns are imported as string attributes.\n"
            "Rows with invalid or missing coordinates are skipped with a "
            "warning; the rest of the file continues to be imported."
        )


    def initAlgorithm(self, config=None):
        # Multi-file picker
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_FILES,
                "Input CSV / Delimited Text Files",
                layerType=QgsProcessing.TypeFile,
            )
        )

        # Delimiter selector
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DELIMITER,
                "Delimiter",
                options=_DELIMITER_LABELS,
                defaultValue=0,  # Auto-detect
                optional=False,
            )
        )

        # X / Y column names (fallback if auto-detect misses)
        self.addParameter(
            QgsProcessingParameterString(
                self.X_FIELD,
                "X Column Name (used if auto-detect fails)",
                defaultValue="Longitude",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.Y_FIELD,
                "Y Column Name (used if auto-detect fails)",
                defaultValue="Latitude",
                optional=True,
            )
        )

        # CRS of input coordinates
        self.addParameter(
            QgsProcessingParameterCrs(
                self.INPUT_CRS,
                "Input CRS",
                defaultValue=QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        )

        # Output folder
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                "Output Folder (GPKGs saved here)",
            )
        )


    def processAlgorithm(self, parameters, context, feedback):
        files = self.parameterAsFileList(parameters, self.INPUT_FILES, context)
        if not files:
            raise QgsProcessingException("No input files selected.")

        # Output folder
        out_dir = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if not out_dir:
            out_dir = os.path.dirname(files[0])
            feedback.pushInfo(
                "No Output Folder specified → using input file directory."
            )
        os.makedirs(out_dir, exist_ok=True)
        feedback.pushInfo(f"Output folder: {out_dir}")

        # Delimiter
        delim_idx = self.parameterAsEnum(parameters, self.DELIMITER, context)
        chosen_delimiter = _DELIMITERS[delim_idx]  # may be "Auto-detect"

        x_hint = self.parameterAsString(parameters, self.X_FIELD, context).strip()
        y_hint = self.parameterAsString(parameters, self.Y_FIELD, context).strip()

        # CRS
        input_crs = self.parameterAsCrs(parameters, self.INPUT_CRS, context)

        nfiles = len(files)
        cached_schema = None  # (headers, col_types) inferred from first file

        for fi, csv_path in enumerate(files, start=1):
            if feedback.isCanceled():
                break

            fname = os.path.basename(csv_path)
            base  = os.path.splitext(fname)[0]
            gpkg_path = os.path.join(out_dir, f"{base}.gpkg")

            feedback.pushInfo(f"[{fi}/{nfiles}] Importing {fname} …")

            try:
                cached_schema = self._import_file(
                    csv_path, gpkg_path, base,
                    chosen_delimiter, x_hint, y_hint,
                    input_crs, feedback,
                    cached_schema=cached_schema,
                )
            except Exception as e:
                feedback.pushWarning(f"  !! Failed to import {fname}: {e}")
                continue

            feedback.setProgress(int(fi / nfiles * 100))

        feedback.pushInfo("✅ Finished CSV bulk import.")
        return {}


    def _import_file(
        self,
        csv_path: str,
        gpkg_path: str,
        layer_name: str,
        chosen_delimiter: str,
        x_hint: str,
        y_hint: str,
        input_crs,
        feedback,
        cached_schema=None,  # (headers_lower, x_idx, y_idx, col_types) or None
    ):
        """
        Import one file. Returns the detected schema tuple so the caller
        can pass it to subsequent files, skipping re-detection.
        """
        # ---- Read raw bytes, detect encoding ----
        with open(csv_path, "rb") as fh:
            raw = fh.read()

        # Try UTF-8-sig (BOM), then UTF-8, then latin-1
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise QgsProcessingException(
                f"Cannot decode {os.path.basename(csv_path)} — try saving as UTF-8."
            )

        # ---- Resolve delimiter ----
        if chosen_delimiter == "Auto-detect":
            # Feed first 8 KB to sniffer
            sample = text[:8192]
            delimiter = _sniff_delimiter(sample)
            feedback.pushInfo(f"  Auto-detected delimiter: {repr(delimiter)}")
        else:
            delimiter = chosen_delimiter
            feedback.pushInfo(f"  Using delimiter: {repr(delimiter)}")

        # ---- Parse CSV ----
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)

        try:
            raw_headers = next(reader)
        except StopIteration:
            raise QgsProcessingException(
                f"{os.path.basename(csv_path)} appears to be empty."
            )

        headers = [h.strip() for h in raw_headers]
        headers_lower = [h.lower() for h in headers]

        # ---- Read ALL data rows now (needed for type inference or writing) ----
        all_rows = []
        for row in reader:
            if len(row) == 0:
                continue
            while len(row) < len(headers):
                row.append("")
            all_rows.append(row)

        if not all_rows:
            feedback.pushWarning(
                f"  !! No data rows in {os.path.basename(csv_path)} — skipping."
            )
            return cached_schema

        # ---- Resolve X/Y column indices ----
        if cached_schema is not None:
            # Reuse schema from first file — just map column names
            cached_headers_lower, x_idx, y_idx, col_types = cached_schema
            feedback.pushInfo(
                "  Using schema detected from first file (skipping re-detection)."
            )
            # Sanity check: same number of columns?
            if len(headers) != len(cached_headers_lower):
                feedback.pushWarning(
                    f"  !! Column count mismatch ({len(headers)} vs "
                    f"{len(cached_headers_lower)} in first file). "
                    "Re-detecting schema for this file."
                )
                cached_schema = None  # fall through to fresh detection

        if cached_schema is None:
            x_idx, y_idx = _find_xy_columns(headers_lower, x_hint, y_hint)

            if x_idx is None:
                feedback.pushWarning(
                    f"  !! Cannot find X column in {os.path.basename(csv_path)} — "
                    f"tried auto-detect and hint '{x_hint}'. Skipping file."
                )
                return None
            if y_idx is None:
                feedback.pushWarning(
                    f"  !! Cannot find Y column in {os.path.basename(csv_path)} — "
                    f"tried auto-detect and hint '{y_hint}'. Skipping file."
                )
                return None

            feedback.pushInfo(
                f"  X → column '{headers[x_idx]}' (index {x_idx}), "
                f"Y → column '{headers[y_idx]}' (index {y_idx})"
            )
            feedback.pushInfo("  Detecting column types from this file …")

            # ---- Pass 1: infer types ----
            col_types = _infer_col_types(all_rows, len(headers))

            for hdr, qt in zip(headers, col_types):
                feedback.pushInfo(f"    '{hdr}' → {_QTYPE_LABEL.get(qt, '?')}")

            # Cache schema for all subsequent files
            cached_schema = (headers_lower, x_idx, y_idx, col_types)
        else:
            feedback.pushInfo(
                f"  X → column '{headers[x_idx]}' (index {x_idx}), "
                f"Y → column '{headers[y_idx]}' (index {y_idx})"
            )

        # ---- Build QGIS field list ----
        # col_types    → detected logical types (used for logging)
        # write_types  → GPKG-compatible types (Time → String, rest unchanged)
        write_types = [_to_gpkg_type(t) for t in col_types]

        fields = QgsFields()
        # Dedicated X / Y double fields (always Double)
        fields.append(QgsField("X", QVariant.Double))
        fields.append(QgsField("Y", QVariant.Double))
        # All original columns with GPKG-compatible types
        for i, hdr in enumerate(headers):
            safe_name = hdr if hdr else f"col_{i}"
            fields.append(QgsField(safe_name, write_types[i]))

        # ---- Memory layer ----
        mem = QgsVectorLayer(
            f"Point?crs={input_crs.authid()}", layer_name, "memory"
        )
        pr = mem.dataProvider()
        pr.addAttributes(fields)
        mem.updateFields()

        skipped = 0
        imported = 0

        # ---- Pass 2: write features with typed values ----
        for row_num, row in enumerate(all_rows, start=2):
            x_val = _safe_float(row[x_idx])
            y_val = _safe_float(row[y_idx])

            if x_val is None or y_val is None:
                feedback.pushWarning(
                    f"  Row {row_num}: invalid X ({row[x_idx]!r}) or "
                    f"Y ({row[y_idx]!r}) — skipped."
                )
                skipped += 1
                continue

            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x_val, y_val)))

            # [X double, Y double] + all original columns.
            # col_types[i] drives the coerce logic (e.g. Time → QDateTime with 1900-01-01);
            # write_types[i] is only used for the field definition above.
            typed_attrs = [_coerce(row[i], col_types[i]) for i in range(len(headers))]
            feat.setAttributes([x_val, y_val] + typed_attrs)
            pr.addFeature(feat)
            imported += 1

        feedback.pushInfo(
            f"  Imported {imported} point(s){f', skipped {skipped} invalid row(s)' if skipped else ''}."
        )

        if imported == 0:
            feedback.pushWarning(
                f"  !! No valid features for {os.path.basename(csv_path)} — "
                "GPKG not written."
            )
            return cached_schema

        # ---- Write to GPKG ----
        save_opts = QgsVectorFileWriter.SaveVectorOptions()
        save_opts.driverName = "GPKG"
        save_opts.layerName = layer_name

        err, msg = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem,
            gpkg_path,
            QgsCoordinateTransformContext(),
            save_opts,
        )[:2]

        if err != QgsVectorFileWriter.NoError:
            feedback.pushWarning(
                f"  !! Failed to write GPKG for {os.path.basename(csv_path)}: {msg}"
            )
            return

        feedback.pushInfo(f"  ✓ Saved: {os.path.basename(gpkg_path)}")

        # ---- Auto-load into QGIS project ----
        try:
            lyr = QgsVectorLayer(gpkg_path, layer_name, "ogr")
            if lyr and lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                feedback.pushInfo(f"  → Loaded layer: {layer_name}")
            else:
                feedback.pushWarning(
                    f"  !! Layer written but could not be loaded: {layer_name}"
                )
        except Exception as e:
            feedback.pushWarning(f"  !! Could not add {layer_name} to project: {e}")

        return cached_schema
