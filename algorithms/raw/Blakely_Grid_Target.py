from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsFeatureSink,
    QgsGeometry,
    QgsPointXY,
    QgsMessageLog,
    Qgis
)
from qgis.PyQt.QtCore import QVariant
import numpy as np
from osgeo import gdal
import scipy.ndimage


class DetectGridPeaks(QgsProcessingAlgorithm):
    INPUT_GRID = "INPUT_GRID"
    SMOOTHING_PASSES = "SMOOTHING_PASSES"
    PEAK_SENSITIVITY = "PEAK_SENSITIVITY"
    VALUE_CUTOFF = "VALUE_CUTOFF"
    OUTPUT_POINTS = "OUTPUT_POINTS"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_GRID, "Input Grid"))
        self.addParameter(QgsProcessingParameterNumber(
            self.SMOOTHING_PASSES, "Smoothing Passes", defaultValue=3, minValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.PEAK_SENSITIVITY, "Peak Sensitivity (Blakely Method)",
            options=["All ridge peaks (1)", "Even more peaks (2)", "More peaks (3)", "Normal (4)"],
            defaultValue=3))
        self.addParameter(QgsProcessingParameterNumber(
            self.VALUE_CUTOFF, "Grid Value Cutoff", defaultValue=0, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POINTS, "Detected Peaks Points"))

    def processAlgorithm(self, parameters, context, feedback):
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_GRID, context)
        smoothing_passes = self.parameterAsInt(parameters, self.SMOOTHING_PASSES, context)
        sensitivity = self.parameterAsEnum(parameters, self.PEAK_SENSITIVITY, context) + 1
        value_cutoff = self.parameterAsDouble(parameters, self.VALUE_CUTOFF, context)

        path = raster_layer.dataProvider().dataSourceUri()
        dataset = gdal.Open(path)
        if dataset is None:
            QgsMessageLog.logMessage(f"Failed to open raster: {path}", "marine_geotools", Qgis.Critical)
            raise Exception("Failed to open raster")
            
        band = dataset.GetRasterBand(1)
        raster_data = band.ReadAsArray().astype(float)
        nodata = band.GetNoDataValue()

        if nodata is not None:
            raster_data[raster_data == nodata] = np.nan

        hanning_kernel = np.array([[0.06, 0.10, 0.06],
                                   [0.10, 0.36, 0.10],
                                   [0.06, 0.10, 0.06]])
        smoothed = raster_data.copy()
        for _ in range(smoothing_passes):
            smoothed = scipy.ndimage.convolve(smoothed, hanning_kernel, mode='nearest')
        peaks = np.ones_like(smoothed, dtype=bool)
        for shift in [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
            shifted = np.roll(np.roll(smoothed, shift[0], axis=0), shift[1], axis=1)
            comparison = smoothed > shifted
            direction_peaks = comparison & ~np.isnan(shifted)

            if sensitivity == 4:  # Normal
                peaks &= direction_peaks
            elif sensitivity == 3:  # More peaks
                peaks &= np.sum([smoothed > np.roll(np.roll(smoothed, s[0], axis=0), s[1], axis=1)
                                 for s in [(-1, 0), (1, 0), (0, -1), (0, 1)]], axis=0) >= 3
            elif sensitivity == 2:  # Even more peaks
                peaks &= np.sum([smoothed > np.roll(np.roll(smoothed, s[0], axis=0), s[1], axis=1)
                                 for s in [(-1, 0), (1, 0), (0, -1), (0, 1)]], axis=0) >= 2
            elif sensitivity == 1:  # All ridge peaks
                peaks &= np.sum([smoothed > np.roll(np.roll(smoothed, s[0], axis=0), s[1], axis=1)
                                 for s in [(-1, 0), (1, 0), (0, -1), (0, 1)]], axis=0) >= 1

        if value_cutoff is not None:
            peaks &= smoothed >= value_cutoff

        peak_indices = np.argwhere(peaks)

        # Prepare output point fields
        output_fields = QgsFields()
        output_fields.append(QgsField("X", QVariant.Double))
        output_fields.append(QgsField("Y", QVariant.Double))
        output_fields.append(QgsField("value", QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT_POINTS, context,
                                               output_fields, QgsWkbTypes.Point, raster_layer.crs())

        transform = dataset.GetGeoTransform()
        origin_x, pixel_width, _, origin_y, _, pixel_height = transform

        for row, col in peak_indices:
            value = smoothed[row, col]
            if np.isnan(value):
                continue
            point_x = origin_x + col * pixel_width + pixel_width / 2
            point_y = origin_y + row * pixel_height + pixel_height / 2
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point_x, point_y)))
            feature.setAttributes([point_x, point_y, float(value)])
            sink.addFeature(feature, QgsFeatureSink.FastInsert)

        return {self.OUTPUT_POINTS: dest_id}

    def name(self):
        return "detect_grid_peaks"

    def displayName(self):
        return "Detect Grid Peaks"

    def group(self): return "MAG"
    def groupId(self): return "MAG"

    def createInstance(self):
        return DetectGridPeaks()
