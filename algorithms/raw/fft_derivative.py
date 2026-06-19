# -*- coding: utf-8 -*-
"""
FFT Derivatives (Tx, Ty, Tz)
Diffusion Fill + Plane Detrend + Gaussian Smoothing (metres) + Optional Cosine Edge Taper

Outputs:
- QC filled raster (used for FFT)
- Tx = ∂f/∂x
- Ty = ∂f/∂y
- Tz = ∂f/∂z

Author: Rizky Affianto /
"""

from qgis.core import (
    QgsMessageLog,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingException
)
from osgeo import gdal
import numpy as np
from numpy.fft import fft2, ifft2, fftfreq
from scipy.ndimage import gaussian_filter, distance_transform_edt


class FFTDerivatives_FastFill(QgsProcessingAlgorithm):
    INPUT_RASTER   = "INPUT_RASTER"
    SMOOTH_RADIUS  = "SMOOTH_RADIUS"
    EDGE_TAPER     = "EDGE_TAPER"
    REMASK_OUTPUTS = "REMASK_OUTPUTS"
    OUTPUT_FILLED  = "OUTPUT_FILLED"
    OUTPUT_TX      = "OUTPUT_TX"
    OUTPUT_TY      = "OUTPUT_TY"
    OUTPUT_TZ      = "OUTPUT_TZ"

    def name(self): return "fft_derivatives_fastfill"
    def displayName(self): return "FFT Derivatives (Tx, Ty, Tz)"
    def group(self): return "MAG"
    def groupId(self): return "MAG"
    def createInstance(self): return FFTDerivatives_FastFill()

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
            self.OUTPUT_TX, "Output Tx = ∂f/∂x Raster"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_TY, "Output Ty = ∂f/∂y Raster"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_TZ, "Output Tz = ∂f/∂z Raster"))

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
        dist, idx = distance_transform_edt(~mask_valid, return_indices=True)
        Z_filled = Z[tuple(idx)]
        if smooth > 0:
            Z_filled = gaussian_filter(Z_filled, sigma=smooth)
        return Z_filled

    def _save_array_as_raster(self, arr, ref_ds, out_path):
        driver = gdal.GetDriverByName("GTiff")
        gt = ref_ds.GetGeoTransform()
        crs = ref_ds.GetProjection()
        width, height = ref_ds.RasterXSize, ref_ds.RasterYSize
        out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(crs)
        band = out_ds.GetRasterBand(1)
        band.WriteArray(arr.astype(np.float32))
        band.SetNoDataValue(np.nan)
        band.FlushCache()
        out_ds.FlushCache()
        out_ds = None

    def processAlgorithm(self, parameters, context, feedback):
        raster = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster is None:
            raise QgsProcessingException("Invalid raster layer")

        smooth_radius = float(self.parameterAsDouble(parameters, self.SMOOTH_RADIUS, context))
        apply_taper   = bool(self.parameterAsBoolean(parameters, self.EDGE_TAPER, context))
        remask        = bool(self.parameterAsBoolean(parameters, self.REMASK_OUTPUTS, context))

        out_filled = self.parameterAsOutputLayer(parameters, self.OUTPUT_FILLED, context)
        out_tx     = self.parameterAsOutputLayer(parameters, self.OUTPUT_TX, context)
        out_ty     = self.parameterAsOutputLayer(parameters, self.OUTPUT_TY, context)
        out_tz     = self.parameterAsOutputLayer(parameters, self.OUTPUT_TZ, context)

        ds = gdal.Open(raster.source(), gdal.GA_ReadOnly)
        if ds is None:
            raise QgsProcessingException("GDAL failed to open raster source")

        band = ds.GetRasterBand(1)
        Z = band.ReadAsArray().astype(float)
        nodata = band.GetNoDataValue()

        if nodata is not None:
            feedback.pushInfo(f"Detected NoData value: {nodata}")
            mask_valid = ~(np.isnan(Z) | np.isclose(Z, nodata))
        else:
            mask_valid = ~np.isnan(Z)
        feedback.pushInfo(f"Valid cells: {mask_valid.sum()} / {mask_valid.size}")

        gt = ds.GetGeoTransform()
        dx = self._snap_px(abs(gt[1]))
        dy = self._snap_px(abs(gt[5]))
        ny, nx = Z.shape
        feedback.pushInfo(f"Raster {nx}×{ny}, pixel size dx={dx}, dy={dy}")

        # Fill
        feedback.pushInfo("Filling missing cells (fast diffusion)...")
        Z_filled = self._fast_diffusion_fill(Z, mask_valid, smooth=0)

        # Detrend
        feedback.pushInfo("Removing planar trend...")
        Z_work = self._plane_detrend(Z_filled, mask_valid)

        # Smooth
        if smooth_radius > 0:
            sigma_x = smooth_radius / dx
            sigma_y = smooth_radius / dy
            feedback.pushInfo(f"Gaussian smoothing radius={smooth_radius} (σx={sigma_x:.2f}, σy={sigma_y:.2f})")
            Z_work = gaussian_filter(Z_work, sigma=(sigma_y, sigma_x))
        else:
            feedback.pushInfo("Skipping smoothing (radius=0).")

        # Taper
        if apply_taper:
            feedback.pushInfo("Applying 5% cosine edge taper...")
            Z_work = self._apply_cosine_taper(Z_work, pct=5)

        # FFT derivatives
        feedback.pushInfo("Computing FFT derivatives...")
        kx = fftfreq(nx, dx) * 2 * np.pi
        ky = fftfreq(ny, dy) * 2 * np.pi
        KX, KY = np.meshgrid(kx, ky)
        K = np.sqrt(KX**2 + KY**2)

        FZ = fft2(Z_work)
        tx = np.real(ifft2(1j * KX * FZ))
        ty = np.real(ifft2(1j * KY * FZ))
        tz = np.real(ifft2(K * FZ))

        if remask:
            tx[~mask_valid] = np.nan
            ty[~mask_valid] = np.nan
            tz[~mask_valid] = np.nan

        # Save
        self._save_array_as_raster(Z_filled, ds, out_filled)
        self._save_array_as_raster(tx, ds, out_tx)
        self._save_array_as_raster(ty, ds, out_ty)
        self._save_array_as_raster(tz, ds, out_tz)
        ds = None

        feedback.pushInfo("FFT Derivatives complete.")
        return {
            self.OUTPUT_FILLED: out_filled,
            self.OUTPUT_TX: out_tx,
            self.OUTPUT_TY: out_ty,
            self.OUTPUT_TZ: out_tz
        }
