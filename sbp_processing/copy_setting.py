# -*- coding: utf-8 -*-
"""
SBP: Copy settings from one file to many (via meta JSON), using selected GPKG layers.

- Source: one GPKG layer (reads <base>.json next to it)
- Targets: multiple GPKG layers (writes each <base>.json next to it)
- Copies selected blocks:
    velocities: sound_velocity_water_m_s, sound_velocity_sediment_m_s
    processing: processing (AGC/TVG/Stacking/blank_water + any other processing items)
    viewer: viewer (min/max/colormap/invert/active_reflector)
    reflectors: reflectors (theme colors/show)
- Preserves per-file identity keys (line_name, source_file, counts, CRS, etc.)
- Optional: create .bak backups
"""

import os
import json
from datetime import datetime

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsMessageLog,
    Qgis,
)

class SBPCopySettingsToMany(QgsProcessingAlgorithm):
    SOURCE_GPKG = "SOURCE_GPKG"
    TARGET_GPKG = "TARGET_GPKG"

    COPY_VELOCITIES = "COPY_VELOCITIES"
    COPY_PROCESSING = "COPY_PROCESSING"
    COPY_VIEWER = "COPY_VIEWER"
    COPY_REFLECTORS = "COPY_REFLECTORS"

    MAKE_BACKUP = "MAKE_BACKUP"
    STRICT = "STRICT"

    def createInstance(self):
        return SBPCopySettingsToMany()

    def name(self):
        return "sbp_copy_settings_to_many"

    def displayName(self):
        return "Copy Settings"

    def group(self):
        return "SBP - Settings"

    def groupId(self):
        return "sbp_setting"

    def shortHelpString(self):
        return (
            "Copies SBP processing/viewer/reflector/velocity settings from one line to many lines.\n"
            "Select a SOURCE GPKG (template) and TARGET GPKGs.\n"
            "Edits the meta JSON beside each GPKG.\n"
        )

    def _layer_source_path(self, layer: QgsMapLayer) -> str:
        src = layer.source() or ""
        if "|" in src:
            src = src.split("|", 1)[0]
        return src

    def _json_for_gpkg(self, gpkg_path: str) -> str:
        base = os.path.splitext(os.path.basename(gpkg_path))[0]
        folder = os.path.dirname(gpkg_path)
        return os.path.join(folder, base + ".json")

    def _load_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)

    def _backup_once(self, json_path: str):
        bak = json_path + ".bak"
        if not os.path.exists(bak):
            with open(json_path, "rb") as fr, open(bak, "wb") as fw:
                fw.write(fr.read())

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMapLayer(
                self.SOURCE_GPKG,
                "Source SBP GPKG (template settings)",types=[QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.TARGET_GPKG,
                "Target SBP GPKG layer(s) (apply settings to)",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )
        self.addParameter(QgsProcessingParameterBoolean(self.COPY_VELOCITIES, "Copy sound velocities", True))
        self.addParameter(QgsProcessingParameterBoolean(self.COPY_PROCESSING, "Copy processing (AGC/TVG/Stacking/Blank water, etc.)", True))
        self.addParameter(QgsProcessingParameterBoolean(self.COPY_VIEWER, "Copy viewer (color scale/min/max/colormap/invert/active_reflector)", True))
        self.addParameter(QgsProcessingParameterBoolean(self.COPY_REFLECTORS, "Copy reflectors theme (colors/show)", True))

        self.addParameter(QgsProcessingParameterBoolean(self.MAKE_BACKUP, "Create .json.bak backup (once)", True))
        self.addParameter(QgsProcessingParameterBoolean(self.STRICT, "Strict mode: error if a target JSON is missing", False))

    def processAlgorithm(self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback):
        src_layer = self.parameterAsLayer(parameters, self.SOURCE_GPKG, context)
        tgt_layers = self.parameterAsLayerList(parameters, self.TARGET_GPKG, context)

        if src_layer is None:
            raise QgsProcessingException("Source layer is not valid.")
        if not tgt_layers:
            raise QgsProcessingException("No target layers selected.")

        copy_vel = bool(self.parameterAsBool(parameters, self.COPY_VELOCITIES, context))
        copy_proc = bool(self.parameterAsBool(parameters, self.COPY_PROCESSING, context))
        copy_view = bool(self.parameterAsBool(parameters, self.COPY_VIEWER, context))
        copy_refl = bool(self.parameterAsBool(parameters, self.COPY_REFLECTORS, context))
        make_backup = bool(self.parameterAsBool(parameters, self.MAKE_BACKUP, context))
        strict = bool(self.parameterAsBool(parameters, self.STRICT, context))

        src_gpkg = self._layer_source_path(src_layer)
        if not src_gpkg.lower().endswith(".gpkg") or not os.path.exists(src_gpkg):
            raise QgsProcessingException(f"Source is not a valid .gpkg path: {src_gpkg}")

        src_json = self._json_for_gpkg(src_gpkg)
        if not os.path.exists(src_json):
            raise QgsProcessingException(f"Source meta JSON not found: {src_json}")

        src_meta = self._load_json(src_json)

        template = {}

        if copy_vel:
            template["sound_velocity_water_m_s"] = src_meta.get("sound_velocity_water_m_s", 1500.0)
            template["sound_velocity_sediment_m_s"] = src_meta.get("sound_velocity_sediment_m_s", 1500.0)

        if copy_proc:
            template["processing"] = src_meta.get("processing", {})

        if copy_view:
            template["viewer"] = src_meta.get("viewer", {})

        if copy_refl:
            template["reflectors"] = src_meta.get("reflectors", {})

        feedback.pushInfo("Template blocks prepared from source:")
        feedback.pushInfo("  " + ", ".join(template.keys()) if template else "  (nothing selected)")

        updated_count, skipped_count, missing_count, error_count = 0, 0, 0, 0

        for layer_index, tgt_layer in enumerate(tgt_layers, start=1):
            if feedback.isCanceled():
                break

            tgt_gpkg = self._layer_source_path(tgt_layer)
            if not tgt_gpkg.lower().endswith(".gpkg") or not os.path.exists(tgt_gpkg):
                skipped_count += 1
                feedback.pushWarning(f"[{layer_index}] Skip: not a valid .gpkg file source → {tgt_gpkg}")
                continue

            tgt_json = self._json_for_gpkg(tgt_gpkg)
            if not os.path.exists(tgt_json):
                missing_count += 1
                msg = f"[{layer_index}] Target meta JSON not found: {tgt_json}"
                if strict:
                    raise QgsProcessingException(msg)
                feedback.pushWarning(msg)
                continue

            if os.path.abspath(tgt_json) == os.path.abspath(src_json):
                feedback.pushInfo(f"[{layer_index}] Target is source template itself → skipped (no change).")
                skipped_count += 1
                continue

            try:
                if make_backup:
                    self._backup_once(tgt_json)

                tgt_meta = self._load_json(tgt_json)

                for key, value in template.items():
                    tgt_meta[key] = value

                tgt_meta["settings_copied_from"] = os.path.basename(src_json)
                tgt_meta["settings_copied_at"] = datetime.now().isoformat(timespec="seconds")

                self._save_json(tgt_json, tgt_meta)

                updated_count += 1
                feedback.pushInfo(f"[{layer_index}] Updated: {os.path.basename(tgt_json)}")

            except (OSError, IOError, json.JSONDecodeError) as error:
                error_count += 1
                QgsMessageLog.logMessage(f"[{layer_index}] ERROR processing {tgt_json}: {error}", "SBP Plugin", Qgis.Warning)
                feedback.pushWarning(f"[{layer_index}] ERROR: {tgt_json}: {error}")

            feedback.setProgress(int(layer_index / max(1, len(tgt_layers)) * 100))

        feedback.pushInfo("✅ Done.")
        feedback.pushInfo(f"Updated: {updated_count}, Missing JSON: {missing_count}, Skipped: {skipped_count}, Errors: {error_count}")
        return {}
