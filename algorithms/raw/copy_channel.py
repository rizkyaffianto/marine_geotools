from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsField,
    QgsProcessing,
    QgsVectorLayer,
    QgsFeature
)
from qgis.PyQt.QtCore import QVariant


class CopyField(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    INPUT_FIELD = "INPUT_FIELD"
    OUTPUT_FIELD = "OUTPUT_FIELD"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS,
            "Input Layers",
            layerType=QgsProcessing.TypeVector
        ))

        self.addParameter(QgsProcessingParameterField(
            self.INPUT_FIELD,
            "Copy From Field",
            parentLayerParameterName=self.INPUT_LAYERS
        ))

        self.addParameter(QgsProcessingParameterString(
            self.OUTPUT_FIELD,
            "To",
            defaultValue="copied"
        ))

    def name(self):
        return "copy_field"

    def displayName(self):
        return "Copy Field"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return CopyField()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        input_field = self.parameterAsString(parameters, self.INPUT_FIELD, context)
        output_field = self.parameterAsString(parameters, self.OUTPUT_FIELD, context)

        for layer in layers:
            feedback.pushInfo(f"Processing layer: {layer.name()}")

            # Ensure output field exists (same type as input)
            if output_field not in [f.name() for f in layer.fields()]:
                src_field = layer.fields().field(input_field)
                new_field = QgsField(
                    output_field,
                    src_field.type(),
                    src_field.typeName(),
                    src_field.length(),
                    src_field.precision()
                )
                layer.dataProvider().addAttributes([new_field])
                layer.updateFields()

            in_idx = layer.fields().indexFromName(input_field)
            out_idx = layer.fields().indexFromName(output_field)

            features = layer.getFeatures()
            updates = {feature.id(): {out_idx: feature[in_idx]} for feature in features}

            # Single provider update
            layer.dataProvider().changeAttributeValues(updates)

            feedback.pushInfo(f"Finished layer: {layer.name()}")

        return {}

