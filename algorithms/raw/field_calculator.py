from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterExpression,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsExpressionContextScope,
    QgsField,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant


class MultiLayerFieldCalculatorFast(QgsProcessingAlgorithm):
    INPUT_LAYERS = "INPUT_LAYERS"
    FIELD_NAME = "FIELD_NAME"
    FIELD_TYPE = "FIELD_TYPE"
    FIELD_LENGTH = "FIELD_LENGTH"
    FIELD_PRECISION = "FIELD_PRECISION"
    EXPRESSION = "EXPRESSION"
    CREATE_NEW = "CREATE_NEW"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS, "Input vector layers", layerType=QgsProcessing.TypeVector))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_NAME, "Result field name", defaultValue="calc"))
        self.addParameter(QgsProcessingParameterEnum(
            self.FIELD_TYPE, "Result field type",
            options=["Double", "Integer", "String"], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.FIELD_LENGTH, "Result field length",
            type=QgsProcessingParameterNumber.Integer, defaultValue=20))
        self.addParameter(QgsProcessingParameterNumber(
            self.FIELD_PRECISION, "Result field precision",
            type=QgsProcessingParameterNumber.Integer, defaultValue=6))
        self.addParameter(QgsProcessingParameterExpression(
            self.EXPRESSION, "Field calculator expression", defaultValue="@row_number"))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CREATE_NEW, "Create new field (unchecked = overwrite if exists)",
            defaultValue=True))

    def name(self):
        return "multi_layer_field_calculator_native"

    def displayName(self):
        return "Field Calculator"
        
    def group(self): return "Database Tools"
    def groupId(self): return "databasetools"
    def createInstance(self): return MultiLayerFieldCalculatorFast()

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        field_name = self.parameterAsString(parameters, self.FIELD_NAME, context)
        expr_text = self.parameterAsExpression(parameters, self.EXPRESSION, context)
        create_new = self.parameterAsBool(parameters, self.CREATE_NEW, context)

        ftype_index = self.parameterAsInt(parameters, self.FIELD_TYPE, context)
        type_map = {0: QVariant.Double, 1: QVariant.Int, 2: QVariant.String}
        field_type = type_map.get(ftype_index, QVariant.Double)

        for layer in layers:
            feedback.pushInfo(f"Calculating on {layer.name()}")
            dp = layer.dataProvider()

            if create_new and field_name not in [f.name() for f in layer.fields()]:
                dp.addAttributes([QgsField(field_name, field_type)])
                layer.updateFields()

            field_index = layer.fields().indexFromName(field_name)

            expr = QgsExpression(expr_text)
            context_expr = QgsExpressionContext()

            # Add standard scopes manually (works on all QGIS versions)
            context_expr.appendScope(QgsExpressionContextUtils.globalScope())
            context_expr.appendScope(QgsExpressionContextUtils.projectScope(context.project()))
            context_expr.appendScope(QgsExpressionContextUtils.layerScope(layer))

            updates = {}
            total = layer.featureCount() or 1
            for i, f in enumerate(layer.getFeatures(QgsFeatureRequest())):
                context_expr.setFeature(f)
                val = expr.evaluate(context_expr)
                if expr.hasEvalError():
                    feedback.reportError(expr.evalErrorString())
                    val = None
                updates[f.id()] = {field_index: val}

                if i % 1000 == 0:
                    feedback.setProgress(i / total * 100)

            if updates:
                dp.changeAttributeValues(updates)

            feedback.pushInfo(f"Done: {layer.name()}")

        return {}
