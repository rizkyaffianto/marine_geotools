# -*- coding: utf-8 -*-
"""
Analytic Signal Amplitude (ASA)
Input rasters: Tx, Ty, Tz (derivatives)
ASA = sqrt(Tx^2 + Ty^2 + Tz^2)

Author: Rizky Affianto / ChatGPT
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsMessageLog,
    Qgis
)
from osgeo import gdal
import numpy as np


class AnalyticSignal_FromDerivatives(QgsProcessingAlgorithm):
    TX = "TX"
    TY = "TY"
    TZ = "TZ"
    REMASK_OUTPUTS = "REMASK_OUTPUTS"
    OUTPUT_ASA = "OUTPUT_ASA"

    def name(self): return "analytic_signal_from_derivatives"
    def displayName(self): return "Analytic Signal (from Tx, Ty, Tz)"
    def group(self): return "MAG"
    def groupId(self): return "MAG"
    def createInstance(self): return AnalyticSignal_FromDerivatives()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.TX, "Input Tx raster (∂f/∂x)"))
        self.addParameter(QgsProcessingParameterRasterLayer(self.TY, "Input Ty raster (∂f/∂y)"))
        self.addParameter(QgsProcessingParameterRasterLayer(self.TZ, "Input Tz raster (∂f/∂z)"))

        self.addParameter(QgsProcessingParameterBoolean(
            self.REMASK_OUTPUTS, "Re-mask NoData (if any input is NoData)?",
            defaultValue=True))

        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_ASA, "Output Analytic Signal Amplitude (ASA) raster"))

    def _save_array_as_raster(self, arr, ref_dataset, out_path):
        driver = gdal.GetDriverByName("GTiff")
        geo_transform = ref_dataset.GetGeoTransform()
        coord_ref_sys = ref_dataset.GetProjection()
        width, height = ref_dataset.RasterXSize, ref_dataset.RasterYSize
        out_dataset = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
        out_dataset.SetGeoTransform(geo_transform)
        out_dataset.SetProjection(coord_ref_sys)
        band = out_dataset.GetRasterBand(1)
        band.WriteArray(arr.astype(np.float32))
        band.SetNoDataValue(np.nan)
        band.FlushCache()
        out_dataset.FlushCache()
        out_dataset = None

    def _read_single_band(self, raster_layer, feedback, label):
        dataset = gdal.Open(raster_layer.source(), gdal.GA_ReadOnly)
        if dataset is None:
            QgsMessageLog.logMessage(f"GDAL failed to open {label}", "marine_geotools", Qgis.Critical)
            raise QgsProcessingException(f"GDAL failed to open {label}")
        band = dataset.GetRasterBand(1)
        raster_array = band.ReadAsArray().astype(float)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            mask_valid = ~(np.isnan(raster_array) | np.isclose(raster_array, nodata))
        else:
            mask_valid = ~np.isnan(raster_array)
        return dataset, raster_array, mask_valid

    def _same_grid(self, dataset_a, dataset_b):
        return (
            dataset_a.RasterXSize == dataset_b.RasterXSize and
            dataset_a.RasterYSize == dataset_b.RasterYSize and
            dataset_a.GetGeoTransform() == dataset_b.GetGeoTransform() and
            dataset_a.GetProjection() == dataset_b.GetProjection()
        )

    def processAlgorithm(self, parameters, context, feedback):
        tx_layer = self.parameterAsRasterLayer(parameters, self.TX, context)
        ty_layer = self.parameterAsRasterLayer(parameters, self.TY, context)
        tz_layer = self.parameterAsRasterLayer(parameters, self.TZ, context)
        if tx_layer is None or ty_layer is None or tz_layer is None:
            raise QgsProcessingException("Tx/Ty/Tz inputs are required")

        remask = bool(self.parameterAsBoolean(parameters, self.REMASK_OUTPUTS, context))
        out_asa = self.parameterAsOutputLayer(parameters, self.OUTPUT_ASA, context)

        dataset_x, tx, mask_x = self._read_single_band(tx_layer, feedback, "Tx")
        dataset_y, ty, mask_y = self._read_single_band(ty_layer, feedback, "Ty")
        dataset_z, tz, mask_z = self._read_single_band(tz_layer, feedback, "Tz")

        if not self._same_grid(dataset_x, dataset_y) or not self._same_grid(dataset_x, dataset_z):
            error_msg = "Tx, Ty, Tz rasters are not on the same grid (size/geotransform/projection differ)."
            QgsMessageLog.logMessage(error_msg, "marine_geotools", Qgis.Critical)
            raise QgsProcessingException(error_msg)

        feedback.pushInfo("Computing ASA = sqrt(Tx^2 + Ty^2 + Tz^2)...")
        asa = np.sqrt(tx**2 + ty**2 + tz**2)

        if remask:
            mask_all = mask_x & mask_y & mask_z
            asa[~mask_all] = np.nan

        self._save_array_as_raster(asa, dataset_x, out_asa)

        dataset_x = dataset_y = dataset_z = None
        feedback.pushInfo("Analytic Signal (from derivatives) complete.")
        return {self.OUTPUT_ASA: out_asa}
