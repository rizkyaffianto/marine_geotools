from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsMessageLog,
    Qgis
)

class MultiLayerDeleteFields(QgsProcessingAlgorithm):
    """
    Delete one or more fields from several vector layers at once.
    """

    INPUT_LAYERS = "INPUT_LAYERS"
    FIELDS = "FIELDS"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input vector layers",
                layerType=QgsProcessing.TypeVector
            )
        )

        # Dropdown for field selection, allow multiple
        self.addParameter(
            QgsProcessingParameterField(
                self.FIELDS,
                "Select fields to delete",
                parentLayerParameterName=self.INPUT_LAYERS,
                type=QgsProcessingParameterField.Any,
                allowMultiple=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        field_names = self.parameterAsFields(parameters, self.FIELDS, context)

        for layer in layers:
            if not layer.isEditable():
                layer.startEditing()

            existing_fields = [field.name() for field in layer.fields()]
            to_delete = [existing_fields.index(field) for field in field_names if field in existing_fields]

            if to_delete:
                layer.dataProvider().deleteAttributes(to_delete)
                layer.updateFields()
                feedback.pushInfo(f"Deleted {len(to_delete)} fields from {layer.name()}: {', '.join(field_names)}")
            else:
                feedback.pushInfo(f"No matching fields found in {layer.name()}")

        return {}

    def name(self):
        return "multilayerdeletefields"

    def displayName(self):
        return "Delete Multiple Fields"

    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"

    def createInstance(self):
        return MultiLayerDeleteFields()
