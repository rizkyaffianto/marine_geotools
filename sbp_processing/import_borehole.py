# -*- coding: utf-8 -*-
"""
SBP Tools – Borehole CSV Import
================================
Imports borehole data from a simplified CSV template into a QGIS point layer.

CSV Format
----------
Fixed columns (left side):
    Name, Easting, Northing, Longitude, Latitude, Width, StickToSeabed, Depth

Repeating lithology quadruplets (right side, as many as needed):
    Name, Color, Description, End, Name, Color, Description, End, ...
    (where "End" is the depth to the bottom of that lithology in meters,
     relative to the top of the previous lithology / borehole top)

The importer detects column positions by name (case-insensitive) and
automatically indexes repeating headers as:
    l1_name / l1_color / l1_desc / l1_end
    l2_name / l2_color / l2_desc / l2_end  ...etc.

Notes
-----
- X/Y fields are selected by the user in the dialog (can be Easting+Northing, or Lon+Lat, etc.)
- Colors must be standard QGIS color names (e.g. Red, Blue, Yellow, etc.)
- StickToSeabed: 1/true/yes = True, 0/false/no = False
- Width is in meters
- Depth is the absolute depth of the borehole top below sea surface (0 if StickToSeabed)
- Description is free text shown as a hover tooltip in the SBP Viewer
"""

import csv
import os

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsProcessingOutputVectorLayer,
    QgsProcessing,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsFields,
    QgsVectorLayer,
    QgsVectorLayerExporter,
    QgsProject,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


_FIXED_COLS = {
    "name":          ("Name",          QVariant.String),
    "easting":       ("Easting",       QVariant.Double),
    "northing":      ("Northing",      QVariant.Double),
    "longitude":     ("Longitude",     QVariant.Double),
    "latitude":      ("Latitude",      QVariant.Double),
    "width":         ("Width",         QVariant.Double),
    "sticktoSeabed": ("StickToSeabed", QVariant.Int),
    "depth":         ("Depth",         QVariant.Double),
}
_FIXED_LOWER = {k.lower(): v for k, v in _FIXED_COLS.items()}
_FIXED_KEY_LOWER = set(_FIXED_LOWER.keys())

# Repeating lithology quadruplet column names (case-insensitive)
_LITHO_NAMES  = {"name"}
_LITHO_COLORS = {"color"}
_LITHO_DESCS  = {"description", "desc"}
_LITHO_ENDS   = {"end"}


def _parse_bool(value: str) -> int:
    return 1 if str(value).strip().lower() in ("1", "true", "yes", "y") else 0

def _safe_float(value: str):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _parse_headers(raw_headers: list) -> tuple:
    fixed_map  = {}
    litho_cols = []

    name_count  = 0
    color_count = 0
    desc_count  = 0
    end_count   = 0

    name_consumed_as_fixed = False

    for index, header in enumerate(raw_headers):
        header_lower_str = header.strip().lower()

        if header_lower_str in _FIXED_KEY_LOWER and header_lower_str not in fixed_map:
            if header_lower_str == "name" and not name_consumed_as_fixed:
                fixed_map[header_lower_str] = index
                name_consumed_as_fixed = True
                continue
            elif header_lower_str != "name":
                fixed_map[header_lower_str] = index
                continue

        if header_lower_str in _LITHO_NAMES:
            name_count += 1
            litho_cols.append((index, name_count, "name"))
        elif header_lower_str in _LITHO_COLORS:
            color_count += 1
            litho_cols.append((index, color_count, "color"))
        elif header_lower_str in _LITHO_DESCS:
            desc_count += 1
            litho_cols.append((index, desc_count, "desc"))
        elif header_lower_str in _LITHO_ENDS:
            end_count += 1
            litho_cols.append((index, end_count, "end"))

    max_litho = max(name_count, color_count, desc_count, end_count) if litho_cols else 0
    return fixed_map, litho_cols, max_litho


def _build_qgs_fields(max_litho: int) -> QgsFields:
    """Build the full QgsFields definition for the output layer."""
    fields = QgsFields()

    fields.append(QgsField("Name",          QVariant.String))
    fields.append(QgsField("Width",         QVariant.Double))
    fields.append(QgsField("StickToSeabed", QVariant.Int))
    fields.append(QgsField("Depth",         QVariant.Double))
    fields.append(QgsField("Longitude",     QVariant.Double))
    fields.append(QgsField("Latitude",      QVariant.Double))
    fields.append(QgsField("Easting",       QVariant.Double))
    fields.append(QgsField("Northing",      QVariant.Double))

    for i in range(1, max_litho + 1):
        fields.append(QgsField(f"l{i}_name",  QVariant.String))
        fields.append(QgsField(f"l{i}_color", QVariant.String))
        fields.append(QgsField(f"l{i}_desc",  QVariant.String))
        fields.append(QgsField(f"l{i}_end",   QVariant.Double))

    return fields


def import_borehole_csv(csv_path: str,
                        x_col: str,
                        y_col: str,
                        src_crs_authid: str,
                        layer_name: str = "Boreholes") -> QgsVectorLayer:
    """
    Parse the CSV and return a new in-memory QGIS point vector layer.

    Parameters
    ----------
    csv_path      : Absolute path to the CSV file.
    x_col         : Header name of the X / Easting / Longitude column.
    y_col         : Header name of the Y / Northing / Latitude column.
    src_crs_authid: Authority ID of the CRS of the coordinates (e.g. 'EPSG:4326').
    layer_name    : Name for the output layer.

    Returns
    -------
    QgsVectorLayer (memory layer, already added to QgsProject)
    """
    src_crs = QgsCoordinateReferenceSystem(src_crs_authid)
    if not src_crs.isValid():
        raise ValueError(f"Invalid CRS: {src_crs_authid}")

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            raw_headers = next(reader)
            rows = [r for r in reader if any(c.strip() for c in r)]
    except (OSError, IOError) as error:
        raise ValueError(f"Failed to read CSV file {csv_path}: {error}")

    fixed_map, litho_cols, max_litho = _parse_headers(raw_headers)

    x_col_lower = x_col.strip().lower()
    y_col_lower = y_col.strip().lower()
    header_lower = [h.strip().lower() for h in raw_headers]

    try:
        x_idx = header_lower.index(x_col_lower)
    except ValueError:
        raise ValueError(f"X column '{x_col}' not found in CSV headers.")
    try:
        y_idx = header_lower.index(y_col_lower)
    except ValueError:
        raise ValueError(f"Y column '{y_col}' not found in CSV headers.")

    fields = _build_qgs_fields(max_litho)

    mem_layer = QgsVectorLayer(
        f"Point?crs={src_crs_authid}&index=yes",
        layer_name,
        "memory"
    )
    provider = mem_layer.dataProvider()
    provider.addAttributes(fields)
    mem_layer.updateFields()

    # 5. Build a litho lookup: triplet_no -> {role: col_idx}
    litho_lookup = {}  # {triplet_no: {role: col_idx}}
    for col_idx, triplet_no, role in litho_cols:
        litho_lookup.setdefault(triplet_no, {})[role] = col_idx

    # 6. Parse rows -> QgsFeature
    features = []
    for row in rows:
        # Pad short rows
        while len(row) < len(raw_headers):
            row.append("")

        # --- Coordinates ---
        raw_x = _safe_float(row[x_idx])
        raw_y = _safe_float(row[y_idx])
        if raw_x is None or raw_y is None:
            continue  # skip rows with missing coordinates

        pt = QgsPointXY(raw_x, raw_y)
        geom = QgsGeometry.fromPointXY(pt)

        def get_fixed(col_lower_key, default=""):
            col_idx = fixed_map.get(col_lower_key)
            if col_idx is None:
                return default
            return row[col_idx].strip() if col_idx < len(row) else default

        name_val  = get_fixed("name", "")
        width_val = _safe_float(get_fixed("width", "")) or 0.0
        stick_val = _parse_bool(get_fixed("sticktoSeabed".lower(), "0"))
        depth_val = _safe_float(get_fixed("depth", "")) or 0.0
        lon_val   = _safe_float(get_fixed("longitude", ""))
        lat_val   = _safe_float(get_fixed("latitude", ""))
        east_val  = _safe_float(get_fixed("easting", ""))
        north_val = _safe_float(get_fixed("northing", ""))

        # --- Build attribute list matching QgsFields order ---
        attrs = [
            name_val,
            width_val,
            stick_val,
            depth_val,
            lon_val,
            lat_val,
            east_val,
            north_val,
        ]

        # Lithology attributes (fill with None if quadruplet is missing)
        for i in range(1, max_litho + 1):
            litho_data = litho_lookup.get(i, {})
            lname_val = row[litho_data["name"]].strip() if "name" in litho_data and litho_data["name"] < len(row) else None
            lcolor_val = row[litho_data["color"]].strip() if "color" in litho_data and litho_data["color"] < len(row) else None
            ldesc_val = row[litho_data["desc"]].strip() if "desc" in litho_data and litho_data["desc"] < len(row) else None
            lend_val = _safe_float(row[litho_data["end"]]) if "end" in litho_data and litho_data["end"] < len(row) else None
            attrs.extend([lname_val, lcolor_val, ldesc_val, lend_val])

        feat = QgsFeature(fields)
        feat.setGeometry(geom)
        feat.setAttributes(attrs)
        features.append(feat)

    provider.addFeatures(features)
    mem_layer.updateExtents()

    # 7. Add to project
    QgsProject.instance().addMapLayer(mem_layer)
    return mem_layer


# QGIS Processing Algorithm wrapper
class SBPImportBoreholeCSV(QgsProcessingAlgorithm):
    INPUT_CSV  = "INPUT_CSV"
    X_FIELD    = "X_FIELD"
    Y_FIELD    = "Y_FIELD"
    CRS        = "CRS"
    LAYER_NAME = "LAYER_NAME"
    OUTPUT     = "OUTPUT"

    def name(self):
        return "sbp_import_borehole_csv"

    def displayName(self):
        return "Borehole Import (CSV)"

    def group(self):
        return "SBP - File Import"

    def groupId(self):
        return "sbp_import"

    def shortHelpString(self):
        return (
            "Import borehole / core data from a simplified CSV file.\n\n"
            "CSV column format:\n"
            "  Fixed: Name, Easting, Northing, Longitude, Latitude, Width, StickToSeabed, Depth\n"
            "  Lithology (repeating quadruplets): Name, Color, Description, End  [Name, Color, Description, End ...]\n\n"
            "- X Field / Y Field: select which CSV column contains the X and Y coordinates.\n"
            "- CRS: the coordinate reference system of those coordinates.\n"
            "- Color names must be QGIS color names (Red, Blue, Yellow, etc.).\n"
            "- Description: free text, shown as a hover tooltip in the SBP Viewer.\n"
            "- StickToSeabed: 1/true/yes → borehole top anchors to seabed reflector in SBP Viewer.\n"
            "- End values are depths in meters relative to the TOP of that lithology layer."
        )

    def createInstance(self):
        return SBPImportBoreholeCSV()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_CSV,
                "CSV File",
                extension="csv"
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.X_FIELD,
                "X Field (column name)",
                defaultValue="Easting"
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.Y_FIELD,
                "Y Field (column name)",
                defaultValue="Northing"
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.CRS,
                "Coordinate CRS",
                defaultValue="EPSG:4326"
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.LAYER_NAME,
                "Output Layer Name",
                defaultValue="Boreholes"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        csv_path   = self.parameterAsFile(parameters, self.INPUT_CSV, context)
        x_col      = self.parameterAsString(parameters, self.X_FIELD, context).strip()
        y_col      = self.parameterAsString(parameters, self.Y_FIELD, context).strip()
        crs        = self.parameterAsCrs(parameters, self.CRS, context)
        layer_name = self.parameterAsString(parameters, self.LAYER_NAME, context).strip() or "Boreholes"

        if not os.path.isfile(csv_path):
            raise QgsProcessingException(f"CSV file not found: {csv_path}")

        feedback.pushInfo(f"Importing boreholes from: {csv_path}")
        feedback.pushInfo(f"X column: {x_col}  |  Y column: {y_col}  |  CRS: {crs.authid()}")

        layer = import_borehole_csv(
            csv_path=csv_path,
            x_col=x_col,
            y_col=y_col,
            src_crs_authid=crs.authid(),
            layer_name=layer_name,
        )

        feedback.pushInfo(f"Created layer '{layer.name()}' with {layer.featureCount()} boreholes.")
        return {self.OUTPUT: layer.id()}
