# -*- coding: utf-8 -*-
"""
Update SBP velocities inside meta .json files based on selected .gpkg layers.

- Input: multiple GPKG files (SBP nav layers)
- For each GPKG:
    <name>.gpkg  -> edits <name>.json in same folder (same basename)
- Updates JSON keys:
    sound_velocity_water_m_s
    sound_velocity_sediment_m_s
- Optional: create .bak backup

Notes:
- Works for SGY Bulk Import outputs (<base>.gpkg + <base>.json)
- Works for XTF SBP outputs (<base>_CH1.gpkg + <base>_CH1.json)
"""

import os
import json
from datetime import datetime

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsMapLayer,
)

class SBPUpdateVelocitiesFromGpkg(QgsProcessingAlgorithm):
    INPUT_GPKG = "INPUT_GPKG"
    VEL_WATER = "VEL_WATER"
    VEL_SED = "VEL_SED"
    MAKE_BACKUP = "MAKE_BACKUP"
    STRICT_MATCH = "STRICT_MATCH"

    def createInstance(self):
        return SBPUpdateVelocitiesFromGpkg()

    def name(self):
        return "sbp_update_velocities_from_gpkg"

    def displayName(self):
        return "Sound Velocity Setting"

    def group(self):
        return "SBP - Settings"

    def groupId(self):
        return "sbp_setting"

    def shortHelpString(self):
        return (
            "Updates sound velocities stored in the meta .json files produced by your SBP import tools.\n\n"
            "Inputs:\n"
            "  • Multiple GPKG files (SBP navigation layers)\n"
            "  • Water velocity (m/s)\n"
            "  • Sediment velocity (m/s)\n\n"
            "Behavior:\n"
            "  • For each <name>.gpkg, edits <name>.json in the same folder.\n"
            "  • Optionally writes a .bak backup before editing.\n"
            "  • Writes/overwrites keys: sound_velocity_water_m_s, sound_velocity_sediment_m_s\n"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_GPKG,
                "Input SBP GPKG layer(s) (nav outputs)",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.VEL_WATER,
                "Water sound velocity (m/s)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1500.0,
                minValue=1000.0,
                maxValue=2500.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.VEL_SED,
                "Sediment sound velocity (m/s)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1600.0,
                minValue=1000.0,
                maxValue=6000.0
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MAKE_BACKUP,
                "Create .json.bak backup",
                defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.STRICT_MATCH,
                "Strict mode: error if meta .json is missing",
                defaultValue=False
            )
        )

    def _layer_source_path(self, layer: QgsMapLayer) -> str:
        """
        Try to resolve a filesystem path for the layer.
        For OGR layers loaded from .gpkg, layer.source() often looks like:
          /path/file.gpkg|layername=xxx
        """
        src = layer.source() or ""
        if "|" in src:
            src = src.split("|", 1)[0]
        return src

    def _load_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)

    def processAlgorithm(self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_GPKG, context)
        if not layers:
            raise QgsProcessingException("No input layers selected.")

        v_water = float(self.parameterAsDouble(parameters, self.VEL_WATER, context))
        v_sed = float(self.parameterAsDouble(parameters, self.VEL_SED, context))
        make_backup = bool(self.parameterAsBool(parameters, self.MAKE_BACKUP, context))
        strict = bool(self.parameterAsBool(parameters, self.STRICT_MATCH, context))

        updated = 0
        skipped = 0
        missing = 0
        errors = 0

        feedback.pushInfo(f"Target values: water={v_water:.3f} m/s, sediment={v_sed:.3f} m/s")
        feedback.pushInfo(f"Layers selected: {len(layers)}")

        for idx, lyr in enumerate(layers, start=1):
            if feedback.isCanceled():
                break

            gpkg_path = self._layer_source_path(lyr)
            if not gpkg_path.lower().endswith(".gpkg") or not os.path.exists(gpkg_path):
                skipped += 1
                feedback.pushWarning(f"[{idx}] Skip: not a valid .gpkg file source → {gpkg_path}")
                continue

            base = os.path.splitext(os.path.basename(gpkg_path))[0]
            folder = os.path.dirname(gpkg_path)
            json_path = os.path.join(folder, base + ".json")

            if not os.path.exists(json_path):
                missing += 1
                msg = f"[{idx}] Meta JSON not found for {base}: {json_path}"
                if strict:
                    raise QgsProcessingException(msg)
                feedback.pushWarning(msg)
                continue

            try:
                if make_backup:
                    bak_path = json_path + ".bak"
                    # Only overwrite backup if it doesn't exist (safer)
                    if not os.path.exists(bak_path):
                        with open(json_path, "rb") as fr, open(bak_path, "wb") as fw:
                            fw.write(fr.read())

                meta = self._load_json(json_path)

                meta["sound_velocity_water_m_s"] = v_water
                meta["sound_velocity_sediment_m_s"] = v_sed

                # Optional: keep a small audit stamp (won't break your other tools)
                meta["velocities_updated"] = datetime.now().isoformat(timespec="seconds")

                self._save_json(json_path, meta)

                updated += 1
                feedback.pushInfo(f"[{idx}] Updated: {os.path.basename(json_path)}")

            except Exception as e:
                errors += 1
                feedback.pushWarning(f"[{idx}] ERROR updating {json_path}: {e}")

            feedback.setProgress(int(idx / max(1, len(layers)) * 100))

        feedback.pushInfo("✅ Done.")
        feedback.pushInfo(f"Updated: {updated}, Missing JSON: {missing}, Skipped: {skipped}, Errors: {errors}")
        return {}
