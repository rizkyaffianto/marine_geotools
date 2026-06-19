from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterCrs,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateTransform,
    QgsProject,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant


class MultiLayerUpdateGeometryCRS(QgsProcessingAlgorithm):
    """
    Update point geometry of multiple layers based on X and Y attribute fields.
    If X or Y is NULL/invalid, geometry is set to NULL.
    Optionally reproject to a new CRS.
    """

    INPUT_LAYERS = "INPUT_LAYERS"
    X_FIELD = "X_FIELD"
    Y_FIELD = "Y_FIELD"
    TARGET_CRS = "TARGET_CRS"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input point layers",
                layerType=QgsProcessing.TypeVectorPoint
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.X_FIELD,
                "X coordinate field",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Numeric
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.Y_FIELD,
                "Y coordinate field",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Numeric
            )
        )

        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                "Target CRS (optional, leave empty to keep layer CRS)",
                optional=True
            )
        )

    def name(self):
        return "multi_layer_update_geometry_crs"

    def displayName(self):
        return "Update Geometry from Fields"

    def group(self):
        return "Coordinates"

    def groupId(self):
        return "coordinates"


    def createInstance(self):
        return MultiLayerUpdateGeometryCRS()

    # --- helpers ---
    @staticmethod
    def _to_float(value):
        """Convert QVariant/str/number to float or return None if invalid/NULL."""
        if value is None:
            return None
        if isinstance(value, QVariant):
            if value.isNull():
                return None
            value = value.value()
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None
            if "," in s and "." in s:
                s = s.replace(",", "")
            elif "," in s and "." not in s:
                s = s.replace(",", ".")
            try:
                return float(s)
            except ValueError as e:
                QgsMessageLog.logMessage(f"Failed to convert string '{s}' to float: {e}", "marine_geotools", Qgis.Warning)
                return None
        try:
            return float(value)
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to convert {value} to float: {e}", "marine_geotools", Qgis.Warning)
            return None

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        x_field = self.parameterAsString(parameters, self.X_FIELD, context)
        y_field = self.parameterAsString(parameters, self.Y_FIELD, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)

        for layer in layers:
            feedback.pushInfo(f"Updating geometry of: {layer.name()}")

            provider = layer.dataProvider()
            updates = {}
            updated = 0

            # Prepare transform if target CRS is set
            transform = None
            if target_crs.isValid() and target_crs != layer.crs():
                transform = QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())
                feedback.pushInfo(f"  Reprojecting {layer.name()} to {target_crs.authid()}")

            for feature in layer.getFeatures():
                x_val = self._to_float(feature[x_field])
                y_val = self._to_float(feature[y_field])

                if x_val is None or y_val is None:
                    new_geometry = QgsGeometry()  # NULL geometry
                else:
                    new_geometry = QgsGeometry.fromPointXY(QgsPointXY(x_val, y_val))

                if transform and not new_geometry.isEmpty():
                    try:
                        new_geometry.transform(transform)
                    except Exception as e:
                        error_msg = f"Transform failed for FID {feature.id()}: {e}"
                        feedback.reportError(error_msg)
                        QgsMessageLog.logMessage(error_msg, "marine_geotools", Qgis.Warning)
                        new_geometry = QgsGeometry()

                updates[feature.id()] = new_geometry
                updated += 1

            if updates:
                if not provider.changeGeometryValues(updates):
                    error_msg = "Provider refused geometry update (read-only or unsupported provider)."
                    QgsMessageLog.logMessage(error_msg, "marine_geotools", Qgis.Critical)
                    raise Exception(error_msg)

            # If CRS changed, update layer CRS metadata
            if transform:
                layer.setCrs(target_crs)

            feedback.pushInfo(f"  Features updated: {updated}")

        return {}
