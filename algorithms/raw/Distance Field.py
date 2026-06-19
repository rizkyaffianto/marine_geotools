from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsField,
    QgsFeature
)
from qgis.PyQt.QtCore import QVariant
import math


class DistanceChannelCalculator(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FIELD_X = "FIELD_X"
    FIELD_Y = "FIELD_Y"
    FIELD_Z = "FIELD_Z"
    OUTPUT_FIELD = "OUTPUT_FIELD"
    USE_CARTESIAN = "USE_CARTESIAN"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_X,
            "X Field (from first layer)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_Y,
            "Y Field (from first layer)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_Z,
            "Z Field (optional, from first layer)",
            parentLayerParameterName=self.INPUT_LAYERS,
            type=QgsProcessingParameterField.Numeric,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterString(
            self.OUTPUT_FIELD,
            "Output Distance Field",
            defaultValue="distance"
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_CARTESIAN,
            "Use Cartesian Direction Mode",
            defaultValue=False
        ))

    def name(self):
        return "distance_channel_calculator"

    def displayName(self):
        return "Distance Channel Calculator"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return DistanceChannelCalculator()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        field_x = self.parameterAsString(parameters, self.FIELD_X, context)
        field_y = self.parameterAsString(parameters, self.FIELD_Y, context)
        field_z = self.parameterAsString(parameters, self.FIELD_Z, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)
        use_cartesian = self.parameterAsBool(parameters, self.USE_CARTESIAN, context)

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")
            features = list(layer.getFeatures())

            if output_field not in [f.name() for f in layer.fields()]:
                layer.dataProvider().addAttributes([QgsField(name=output_field, type=QVariant.Double)])
                layer.updateFields()

            idx_x = layer.fields().indexFromName(field_x)
            idx_y = layer.fields().indexFromName(field_y)
            idx_z = layer.fields().indexFromName(field_z) if field_z else -1
            idx_out = layer.fields().indexFromName(output_field)

            # Sort features if needed (optional: group by line, etc.)
            coords = []
            for feat in features:
                try:
                    x = float(feat[idx_x])
                    y = float(feat[idx_y])
                    z = float(feat[idx_z]) if idx_z >= 0 else 0.0
                    coords.append((feat.id(), x, y, z))
                except:
                    continue

            # Optionally sort in Cartesian direction
            if use_cartesian and len(coords) >= 2:
                x0, y0 = coords[0][1], coords[0][2]
                x1, y1 = coords[-1][1], coords[-1][2]
                dx, dy = x1 - x0, y1 - y0
                angle = math.degrees(math.atan2(dy, dx))
                if abs(angle) <= 45:
                    coords.sort(key=lambda t: t[1])  # sort by X
                else:
                    coords.sort(key=lambda t: t[2])  # sort by Y

            # Calculate distance
            prev = None
            cumulative = 0.0
            updates = {}

            for i, (fid, x, y, z) in enumerate(coords):
                if prev is not None:
                    dx = x - prev[0]
                    dy = y - prev[1]
                    dz = z - prev[2] if idx_z >= 0 else 0
                    dist = math.sqrt(dx**2 + dy**2 + dz**2)
                    cumulative += dist
                else:
                    cumulative = 0.0
                updates[fid] = {idx_out: cumulative}
                prev = (x, y, z)

                if i % 100 == 0:
                    feedback.setProgress(int(i / len(coords) * 100))
                    if feedback.isCanceled():
                        return {}

            layer.dataProvider().changeAttributeValues(updates)
            layer.updateFields()
            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}
