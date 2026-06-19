from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingMultiStepFeedback,
    QgsProcessingParameterMultipleLayers, QgsProcessingParameterField,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
    QgsProcessingException, QgsVectorLayer, QgsFields, QgsField, QgsFeature,
    QgsVariantUtils
)
from qgis.PyQt.QtCore import QVariant
import processing
import math


class Model(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            'input_point', 'Input Point',
            layerType=QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterField(
            'z_field', 'Z field',
            type=QgsProcessingParameterField.Numeric,
            parentLayerParameterName='input_point'))
        self.addParameter(QgsProcessingParameterNumber(
            'weighting_power', 'Weighting Power',
            type=QgsProcessingParameterNumber.Double, defaultValue=2))
        self.addParameter(QgsProcessingParameterNumber(
            'radius', 'Radius',
            type=QgsProcessingParameterNumber.Double, defaultValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            'max_number_of_data_points_to_use', 'Max number of data points to use',
            type=QgsProcessingParameterNumber.Double, defaultValue=12))
        self.addParameter(QgsProcessingParameterNumber(
            'min_number_of_data_points_to_use', 'Min number of data points to use',
            type=QgsProcessingParameterNumber.Double, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'nodata_value', 'NODATA Value',
            type=QgsProcessingParameterNumber.Integer, defaultValue=-999999))
        self.addParameter(QgsProcessingParameterNumber(
            'smoothing', 'Smoothing',
            type=QgsProcessingParameterNumber.Double, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'cell_size', 'Cell size',
            type=QgsProcessingParameterNumber.Double, defaultValue=0.5))
        self.addParameter(QgsProcessingParameterRasterDestination(
            'RasterGrid', 'Raster Grid', createByDefault=True))

    @staticmethod
    def _to_float_or_none(val):
        """
        Convert various QGIS attribute representations to float.
        Return None if val is NULL/invalid/empty/NaN.
        """
        # QGIS nulls can be QVariant NULL or Python None
        if val is None:
            return None
        try:
            if QgsVariantUtils.isNull(val):
                return None
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "PluginLogger", Qgis.Critical)

        # Sometimes numeric fields may come as empty string
        if isinstance(val, str):
            if val.strip() == "":
                return None

        try:
            fval = float(val)
        except Exception as err:
            return None

        # Guard NaN / inf
        if math.isnan(fval) or math.isinf(fval):
            return None

        return fval

    def processAlgorithm(self, parameters, context, model_feedback):
        feedback = QgsProcessingMultiStepFeedback(3, model_feedback)
        results, outputs = {}, {}

        # === 1. Build temporary combined point layer ===
        feedback.setCurrentStep(0)
        feedback.pushInfo("Building temporary combined point layer in memory...")
        input_layers = self.parameterAsLayerList(parameters, 'input_point', context)
        if not input_layers:
            raise QgsProcessingException("No valid input layers provided.")

        crs = input_layers[0].crs().authid()
        fields = QgsFields()
        fields.append(QgsField('layer', QVariant.String))
        fields.append(QgsField('Z', QVariant.Double))

        mem_layer = QgsVectorLayer(f'Point?crs={crs}', 'combined', 'memory')
        provider = mem_layer.dataProvider()
        provider.addAttributes(fields)
        mem_layer.updateFields()

        z_field = self.parameterAsString(parameters, 'z_field', context)

        total = sum(lyr.featureCount() for lyr in input_layers if lyr and lyr.isValid())
        if total == 0:
            raise QgsProcessingException("No features found in input layers.")

        processed = 0
        kept = 0
        skipped_null_z = 0
        skipped_bad_geom = 0

        for lyr in input_layers:
            if not lyr or not lyr.isValid():
                feedback.reportError("Invalid layer encountered, skipping.")
                continue

            idx = lyr.fields().indexFromName(z_field)
            if idx == -1:
                feedback.reportError(f"Field {z_field} missing in {lyr.name()}, skipping.")
                continue

            layer_name = lyr.name()

            for f in lyr.getFeatures():
                if feedback.isCanceled():
                    break

                # Progress is based on processed source features
                processed += 1
                feedback.setProgress(int(33 * processed / max(total, 1)))

                geom = f.geometry()
                if geom is None or geom.isEmpty():
                    skipped_bad_geom += 1
                    continue

                raw_val = f.attribute(idx)
                z = self._to_float_or_none(raw_val)
                if z is None:
                    skipped_null_z += 1
                    continue

                feat = QgsFeature(mem_layer.fields())
                feat.setGeometry(geom)
                feat.setAttributes([layer_name, z])
                provider.addFeature(feat)
                kept += 1

        mem_layer.updateExtents()

        feedback.pushInfo(
            f"Combined layer created with {kept} features "
            f"(skipped NULL/invalid Z: {skipped_null_z}, skipped empty geom: {skipped_bad_geom})."
        )

        if kept == 0:
            raise QgsProcessingException(
                "All input features were skipped (NULL/invalid Z or empty geometry). Nothing to grid."
            )

        feedback.setCurrentStep(1)
        r = self.parameterAsDouble(parameters, 'radius', context)
        extent = mem_layer.extent()
        minx, maxx = extent.xMinimum() - r, extent.xMaximum() + r
        miny, maxy = extent.yMinimum() - r, extent.yMaximum() + r

        cell = self.parameterAsDouble(parameters, 'cell_size', context)
        if cell <= 0:
            raise QgsProcessingException("Cell size must be > 0.")

        width = max(1, int((maxx - minx) / cell))
        height = max(1, int((maxy - miny) / cell))

        gdal_extra = f"-txe {minx} {maxx} -tye {miny} {maxy} -outsize {width} {height}"
        feedback.pushInfo(f"EXTRA options: {gdal_extra}")
        feedback.setProgress(66)

        feedback.setCurrentStep(2)
        feedback.pushInfo("Running IDW interpolation...")
        alg_params = {
            'DATA_TYPE': 5,
            'INPUT': mem_layer,
            'Z_FIELD': 'Z',
            'POWER': self.parameterAsDouble(parameters, 'weighting_power', context),
            'RADIUS': self.parameterAsDouble(parameters, 'radius', context),
            'SMOOTHING': self.parameterAsDouble(parameters, 'smoothing', context),
            'NODATA': self.parameterAsInt(parameters, 'nodata_value', context),
            'MAX_POINTS': self.parameterAsInt(parameters, 'max_number_of_data_points_to_use', context),
            'MIN_POINTS': self.parameterAsInt(parameters, 'min_number_of_data_points_to_use', context),
            'EXTRA': gdal_extra,
            'OUTPUT': parameters['RasterGrid']
        }

        outputs['GridIdw'] = processing.run(
            'gdal:gridinversedistancenearestneighbor',
            alg_params, context=context, feedback=feedback, is_child_algorithm=True
        )

        feedback.setProgress(100)
        feedback.pushInfo("✅ Finished IDW interpolation successfully.")

        results['RasterGrid'] = outputs['GridIdw']['OUTPUT']
        return results

    def name(self):
        return 'idw_interpolation_simple'

    def displayName(self):
        return 'IDW Interpolation'

    def group(self):
        return "Gridding"

    def groupId(self):
        return "gridding"

    def createInstance(self):
        return Model()
