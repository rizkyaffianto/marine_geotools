# -*- coding: utf-8 -*-

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

import os
import csv


class BulkExportSelectedFieldsCSV(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FIELD_NAMES = "FIELD_NAMES"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    MERGE_TO_SINGLE = "MERGE_TO_SINGLE"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input vector layers",
                layerType=QgsProcessing.TypeVector
            )
        )

        # Comma-separated field names
        self.addParameter(
            QgsProcessingParameterString(
                self.FIELD_NAMES,
                "Field names to export (comma-separated)",
                defaultValue=""
            )
        )

        # Output folder for CSV files
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                "Output folder for CSV files"
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MERGE_TO_SINGLE,
                "Merge all layers into one CSV file",
                defaultValue=False
            )
        )

    def name(self):
        # internal ID
        return "bulk_export_selected_fields_csv"

    def displayName(self):
        return "Bulk Export Selected Fields to CSV"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return BulkExportSelectedFieldsCSV()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        if not layers:
            raise QgsProcessingException("No input layers provided.")

        field_string = self.parameterAsString(parameters, self.FIELD_NAMES, context)
        field_names = [f.strip() for f in field_string.split(",") if f.strip()]

        if not field_names:
            raise QgsProcessingException(
                "No field names specified. Please enter names separated by commas."
            )

        out_folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if not out_folder:
            raise QgsProcessingException("Output folder is not valid.")

        if not os.path.exists(out_folder):
            os.makedirs(out_folder, exist_ok=True)

        merge_to_single = self.parameterAsBool(parameters, self.MERGE_TO_SINGLE, context)

        total_layers = len(layers) or 1

        # MODE 1: MERGE TO SINGLE CSV
        if merge_to_single:
            merged_path = os.path.join(out_folder, "merged_layers.csv")
            feedback.pushInfo(f"Merging all layers into: {merged_path}")

            with open(merged_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)

                # Add 'layer_name' + user-requested fields as header
                header = ["layer_name"] + field_names
                writer.writerow(header)

                for i, layer in enumerate(layers):
                    if feedback.isCanceled():
                        break

                    if not isinstance(layer, QgsVectorLayer):
                        feedback.pushInfo(f"Skipping non-vector layer: {layer.name()}")
                        continue

                    feedback.pushInfo(f"Processing layer: {layer.name()}")

                    layer_fields = layer.fields()
                    missing_names = []

                    # Check which fields exist (for logging only; missing ones become empty values)
                    for name in field_names:
                        idx = layer_fields.indexFromName(name)
                        if idx == -1:
                            missing_names.append(name)

                    if missing_names:
                        feedback.pushInfo(
                            "  Warning: these fields do not exist in layer '{}': {}".format(
                                layer.name(), ", ".join(missing_names)
                            )
                        )

                    for feat in layer.getFeatures():
                        row = [layer.name()]
                        for name in field_names:
                            if name in layer_fields.names():
                                val = feat[name]
                            else:
                                val = None
                            row.append(val)
                        writer.writerow(row)

                    feedback.setProgress(int((i + 1) / float(total_layers) * 100))

            feedback.pushInfo("Merged CSV created: {}".format(merged_path))
            return {}

        for i, layer in enumerate(layers):
            if feedback.isCanceled():
                break

            if not isinstance(layer, QgsVectorLayer):
                feedback.pushInfo(f"Skipping non-vector layer: {layer.name()}")
                continue

            feedback.pushInfo(f"Processing layer: {layer.name()}")

            fields = layer.fields()
            attr_indices = []
            found_names = []
            missing_names = []

            for name in field_names:
                idx = fields.indexFromName(name)
                if idx != -1:
                    attr_indices.append(idx)
                    found_names.append(name)
                else:
                    missing_names.append(name)

            if not attr_indices:
                feedback.pushInfo(
                    f"  No specified fields found in layer '{layer.name()}'. Skipping."
                )
                continue

            if missing_names:
                feedback.pushInfo(
                    "  Warning: these fields do not exist in layer '{}': {}".format(
                        layer.name(), ", ".join(missing_names)
                    )
                )

            safe_layer_name = "".join(
                c if c.isalnum() or c in ("_", "-") else "_"
                for c in layer.name()
            )
            out_path = os.path.join(out_folder, safe_layer_name + ".csv")

            # CSV save options
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "CSV"
            options.fileEncoding = "UTF-8"
            options.onlySelectedFeatures = False
            options.attributes = attr_indices
            options.includeWkt = False  # no geometry

            feedback.pushInfo(
                "  Exporting fields {} to: {}".format(", ".join(found_names), out_path)
            )

            # Safe handling of different QGIS return signatures
            result = QgsVectorFileWriter.writeAsVectorFormatV2(
                layer,
                out_path,
                context.transformContext(),
                options
            )

            error = None
            error_message = ""
            new_path = ""

            if isinstance(result, tuple):
                if len(result) == 3:
                    error, error_message, new_path = result
                elif len(result) == 2:
                    error, new_path = result
                    error_message = ""
                else:
                    error = QgsVectorFileWriter.ErrCreateDataSource
                    error_message = "Unexpected return from writeAsVectorFormatV2"
                    new_path = out_path
            else:
                error = result
                new_path = out_path

            if error != QgsVectorFileWriter.NoError:
                feedback.reportError(
                    "  Error writing layer '{}' to CSV: {}".format(
                        layer.name(), error_message
                    )
                )
            else:
                feedback.pushInfo("  Done: {}".format(new_path))

            feedback.setProgress(int((i + 1) / float(total_layers) * 100))

        return {}
