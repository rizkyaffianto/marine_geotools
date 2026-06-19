from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingMultiStepFeedback,
    QgsProcessingParameterMultipleLayers, QgsProcessingParameterFeatureSink,
    QgsProcessingException, QgsVectorLayer, QgsFields, QgsField, QgsFeature,
    QgsGeometry, QgsPointXY, QgsWkbTypes
)
from qgis.PyQt.QtCore import QVariant


class MergePointLayersToPath(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            'input_point', 'Input Point',
            layerType=QgsProcessing.TypeVectorPoint))

        self.addParameter(QgsProcessingParameterFeatureSink(
            'OUTPUT_PATH', 'Output Path Lines', QgsProcessing.TypeVectorLine))

    def processAlgorithm(self, parameters, context, model_feedback):
        feedback = QgsProcessingMultiStepFeedback(2, model_feedback)
        results = {}

        input_layers = self.parameterAsLayerList(parameters, 'input_point', context)
        if not input_layers:
            raise QgsProcessingException("No valid input layers provided.")

        # Build memory layer to keep order (with NULL placeholders)
        crs = input_layers[0].crs()
        fields = QgsFields()
        fields.append(QgsField('layer', QVariant.String))
        fields.append(QgsField('has_geom', QVariant.Bool))  # mark valid/invalid
        mem_layer = QgsVectorLayer(f'Point?crs={crs.authid()}', 'combined_points', 'memory')
        provider = mem_layer.dataProvider()
        provider.addAttributes(fields)
        mem_layer.updateFields()

        for layer in input_layers:
            for feature in layer.getFeatures():
                new_feat = QgsFeature(mem_layer.fields())
                new_feat['layer'] = layer.name()
                geom = feature.geometry()

                # Handle null geometry explicitly
                if geom is None or geom.isEmpty():
                    new_feat.setGeometry(QgsGeometry())  # empty geometry placeholder
                    new_feat['has_geom'] = False
                else:
                    try:
                        point = geom.asPoint()
                        if point.x() is None or point.y() is None:
                            new_feat.setGeometry(QgsGeometry())
                            new_feat['has_geom'] = False
                        else:
                            new_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
                            new_feat['has_geom'] = True
                    except Exception as e:
                        QgsMessageLog.logMessage(f"Failed to process geometry for feature {feature.id()}: {e}", "MarineGeoTools", Qgis.Warning)
                        new_feat.setGeometry(QgsGeometry())
                        new_feat['has_geom'] = False

                provider.addFeature(new_feat)

        mem_layer.updateExtents()

        # Output sink (line)
        fields_line = QgsFields()
        fields_line.append(QgsField('layer', QVariant.String))
        sink, dest_id = self.parameterAsSink(parameters, 'OUTPUT_PATH', context,
                                             fields_line, QgsWkbTypes.LineString, crs)

        grouped = {}
        for feature in mem_layer.getFeatures():
            grouped.setdefault(feature['layer'], []).append(feature)

        for lyr_name, feats in grouped.items():
            feats.sort(key=lambda feat: feat.id())  # simple order
            current_points = []

            for feature in feats:
                has_geom = feature['has_geom']
                geom = feature.geometry()

                if not has_geom or geom is None or geom.isEmpty():
                    # break path if no geometry
                    if len(current_points) >= 2:
                        line_feat = QgsFeature(fields_line)
                        line_feat.setGeometry(QgsGeometry.fromPolylineXY(current_points))
                        line_feat['layer'] = lyr_name
                        sink.addFeature(line_feat)
                    current_points = []
                    continue

                point = geom.asPoint()
                current_points.append(QgsPointXY(point))

            # Add last segment if needed
            if len(current_points) >= 2:
                line_feat = QgsFeature(fields_line)
                line_feat.setGeometry(QgsGeometry.fromPolylineXY(current_points))
                line_feat['layer'] = lyr_name
                sink.addFeature(line_feat)

        feedback.pushInfo("✅ Finished — path breaks applied at NULL geometries.")
        results['OUTPUT_PATH'] = dest_id
        return results

    def name(self):
        return 'merge_point_layer'

    def displayName(self):
        return 'Merge Point Layers to Path'

    def group(self):
        return "Coordinates"

    def groupId(self):
        return "coordinates"

    def createInstance(self):
        return MergePointLayersToPath()
