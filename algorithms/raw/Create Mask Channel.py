from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsField
)
from qgis.PyQt.QtCore import QVariant


class CreateMaskField(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FIELD_NAME = "FIELD_NAME"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS, "Input vector layers",
            layerType=QgsProcessing.TypeVector))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_NAME, "Field name", defaultValue="MASK"))

    def name(self): return "create_mask_field"
    def displayName(self): return "Create MASK Field"
    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"
    def createInstance(self): return CreateMaskField()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        field_name = self.parameterAsString(parameters, self.FIELD_NAME, context)

        for layer in layers:
            feedback.pushInfo(f"Adding field '{field_name}' to {layer.name()}")
            dp = layer.dataProvider()

            # Add field if missing
            if field_name not in [f.name() for f in layer.fields()]:
                dp.addAttributes([QgsField(field_name, QVariant.Int)])
                layer.updateFields()

            field_index = layer.fields().indexFromName(field_name)

            # Build updates dictionary with value = 1 (very fast)
            updates = {f.id(): {field_index: 1} for f in layer.getFeatures()}

            if updates:
                dp.changeAttributeValues(updates)

            feedback.pushInfo(f"Done: {layer.name()}")

        return {}
