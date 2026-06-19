""" 
Analytic Signal (FFT Derivatives)
Diffusion Fill + Plane Detrend + Gaussian Smoothing (metres) + Optional Cosine Edge Taper

Workflow:
1) Auto-detect NaN/NoData (e.g., -999999 from metadata)
2) Fill missing cells using fast diffusion (distance transform + Gaussian blur)
3) Remove first-order (planar) trend surface
4) Apply Gaussian smoothing (radius in map units)
5) Optional cosine edge taper (~5%) before FFT
6) FFT derivatives: ∂f/∂x, ∂f/∂y, ∂f/∂z
7) Analytic Signal Amplitude (ASA)
8) Reapply original mask
9) Export the filled raster used for FFT (QC)

Author: Rizky Affianto 
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsMessageLog,
    Qgis
)
from osgeo import gdal
import numpy as np
from numpy.fft import fft2, ifft2, fftfreq
from scipy.ndimage import gaussian_filter, distance_transform_edt


class AnalyticSignalFFT_FastFill(QgsProcessingAlgorithm):
    INPUT_RASTER   = "INPUT_RASTER"
    SMOOTH_RADIUS  = "SMOOTH_RADIUS"
    EDGE_TAPER     = "EDGE_TAPER"
    REMASK_OUTPUTS = "REMASK_OUTPUTS"
    OUTPUT_FILLED  = "OUTPUT_FILLED"
    OUTPUT_DX      = "OUTPUT_DX"
    OUTPUT_DY      = "OUTPUT_DY"
    OUTPUT_DZ      = "OUTPUT_DZ"
    OUTPUT_ASA     = "OUTPUT_ASA"

    def name(self): return "analytic_signal_fft_fastfill"
    def displayName(self): return "Analytic Signal (FFT)"
    def group(self): return "MAG"
    def groupId(self): return "MAG"
    def createInstance(self): return AnalyticSignalFFT_FastFill()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_RASTER, "Input Raster (single band)"))

        self.addParameter(QgsProcessingParameterNumber(
            self.SMOOTH_RADIUS, "Gaussian smoothing radius (map units, e.g. metres)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0, minValue=0.0, maxValue=1000.0))

        self.addParameter(QgsProcessingParameterBoolean(
            self.EDGE_TAPER, "Apply cosine edge taper (5%) before FFT?",
            defaultValue=True))

        self.addParameter(QgsProcessingParameterBoolean(
            self.REMASK_OUTPUTS, "Re-mask original NoData in outputs?",
            defaultValue=True))

        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_FILLED, "QC: Filled raster used for FFT"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_DX, "Output ∂f/∂x Raster"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_DY, "Output ∂f/∂y Raster"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_DZ, "Output ∂f/∂z Raster"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_ASA, "Output Analytic Signal Amplitude Raster"))

    def _snap_px(self, v: float) -> float:
        a = abs(v)
        if a < 1: return round(v, 3)
        elif a < 10: return round(v, 2)
        else: return round(v, 1)

    def _plane_detrend(self, Z, mask_valid):
        Yi, Xi = np.indices(Z.shape)
        x = Xi[mask_valid].ravel()
        y = Yi[mask_valid].ravel()
        z = Z[mask_valid].ravel()
        G = np.c_[x, y, np.ones_like(x)]
        m, *_ = np.linalg.lstsq(G, z, rcond=None)
        trend = (m[0]*Xi + m[1]*Yi + m[2])
        return Z - trend

    def _apply_cosine_taper(self, Z, pct=5):
        """Apply 2-D cosine (Hanning) edge taper."""
        ny, nx = Z.shape
        wx = np.ones(nx)
        wy = np.ones(ny)
        nedge_x = max(1, int(nx * pct / 100))
        nedge_y = max(1, int(ny * pct / 100))
        wx[:nedge_x] = 0.5 * (1 - np.cos(np.linspace(0, np.pi, nedge_x)))
        wx[-nedge_x:] = wx[:nedge_x][::-1]
        wy[:nedge_y] = 0.5 * (1 - np.cos(np.linspace(0, np.pi, nedge_y)))
        wy[-nedge_y:] = wy[:nedge_y][::-1]
        window = np.outer(wy, wx)
        return Z * window

    def _fast_diffusion_fill(self, Z, mask_valid, smooth=5):
        """Fast diffusion-based fill for NaN/NoData regions."""
        feedback_info = f"Applying fast diffusion fill (σ={smooth})..."
        QgsMessageLog.logMessage(feedback_info, "marine_geotools", Qgis.Info)
        dist, idx = distance_transform_edt(~mask_valid, return_indices=True)
        Z_filled = Z[tuple(idx)]
        if smooth > 0:
            Z_filled = gaussian_filter(Z_filled, sigma=smooth)
        return Z_filled

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

    def processAlgorithm(self, parameters, context, feedback):
        raster = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster is None:
            raise QgsProcessingException("Invalid raster layer")

        smooth_radius = float(self.parameterAsDouble(parameters, self.SMOOTH_RADIUS, context))
        apply_taper   = bool(self.parameterAsBoolean(parameters, self.EDGE_TAPER, context))
        remask        = bool(self.parameterAsBoolean(parameters, self.REMASK_OUTPUTS, context))

        out_filled = self.parameterAsOutputLayer(parameters, self.OUTPUT_FILLED, context)
        out_dx = self.parameterAsOutputLayer(parameters, self.OUTPUT_DX, context)
        out_dy = self.parameterAsOutputLayer(parameters, self.OUTPUT_DY, context)
        out_dz = self.parameterAsOutputLayer(parameters, self.OUTPUT_DZ, context)
        out_asa = self.parameterAsOutputLayer(parameters, self.OUTPUT_ASA, context)

        dataset = gdal.Open(raster.source(), gdal.GA_ReadOnly)
        band = dataset.GetRasterBand(1)
        raster_data = band.ReadAsArray().astype(float)
        nodata = band.GetNoDataValue()

        if nodata is not None:
            feedback.pushInfo(f"Detected NoData value: {nodata}")
            mask_valid = ~(np.isnan(raster_data) | np.isclose(raster_data, nodata))
        else:
            mask_valid = ~np.isnan(raster_data)
        feedback.pushInfo(f"Valid cells: {mask_valid.sum()} / {mask_valid.size}")

        geo_transform = dataset.GetGeoTransform()
        dx = self._snap_px(abs(geo_transform[1]))
        dy = self._snap_px(abs(geo_transform[5]))
        rows, cols = raster_data.shape
        feedback.pushInfo(f"Raster {cols}×{rows}, pixel size dx={dx}, dy={dy}")

        feedback.pushInfo("Filling missing cells using fast diffusion method...")
        raster_data_filled = self._fast_diffusion_fill(raster_data, mask_valid, smooth=0)
        feedback.pushInfo("Fast diffusion fill complete.")

        feedback.pushInfo("Removing planar trend…")
        raster_data_detrend = self._plane_detrend(raster_data_filled, mask_valid)

        if smooth_radius > 0:
            sigma_x = smooth_radius / dx
            sigma_y = smooth_radius / dy
            feedback.pushInfo(f"Applying Gaussian smoothing (radius={smooth_radius} m; σx={sigma_x:.2f}, σy={sigma_y:.2f})")
            raster_data_detrend = gaussian_filter(raster_data_detrend, sigma=(sigma_y, sigma_x))
        else:
            feedback.pushInfo("Skipping smoothing (radius=0).")

        if apply_taper:
            feedback.pushInfo("Applying 5% cosine edge taper before FFT…")
            raster_data_detrend = self._apply_cosine_taper(raster_data_detrend, pct=5)
        else:
            feedback.pushInfo("Skipping edge taper.")

        feedback.pushInfo("Computing FFT derivatives…")
        kx = fftfreq(cols, dx) * 2 * np.pi
        ky = fftfreq(rows, dy) * 2 * np.pi
        KX, KY = np.meshgrid(kx, ky)
        K = np.sqrt(KX**2 + KY**2)

        FZ = fft2(raster_data_detrend)
        dfdx = np.real(ifft2(1j * KX * FZ))
        dfdy = np.real(ifft2(1j * KY * FZ))
        dfdz = np.real(ifft2(K * FZ))
        asa  = np.sqrt(dfdx**2 + dfdy**2 + dfdz**2)

        if remask:
            for arr in (dfdx, dfdy, dfdz, asa):
                arr[~mask_valid] = np.nan

        self._save_array_as_raster(raster_data_filled, dataset, out_filled)
        self._save_array_as_raster(dfdx, dataset, out_dx)
        self._save_array_as_raster(dfdy, dataset, out_dy)
        self._save_array_as_raster(dfdz, dataset, out_dz)
        self._save_array_as_raster(asa,  dataset, out_asa)
        dataset = None

        feedback.pushInfo("Analytic Signal computation complete.")
        return {
            self.OUTPUT_FILLED: out_filled,
            self.OUTPUT_DX: out_dx,
            self.OUTPUT_DY: out_dy,
            self.OUTPUT_DZ: out_dz,
            self.OUTPUT_ASA: out_asa
        }
