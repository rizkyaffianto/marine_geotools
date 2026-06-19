# Marine GeoTools - User Guide

Welcome to the **Marine GeoTools** QGIS Plugin! This unified plugin provides an advanced geoprocessing environment and interactive viewers specifically designed for analogue marine geophysics, including Magnetic (MAG) and Sub-Bottom Profiler (SBP) data processing.

---

## 1. Prerequisites & Dependency Installation

Marine GeoTools relies on several external Python libraries for advanced data processing, array manipulation, and interactive plotting. These must be installed within your QGIS Python environment before the plugin can function correctly.

### Detected External Dependencies:
* `obspy`: Used for reading and processing standard seismic data formats like SEG-Y.
* `pyqtgraph`: Used for the interactive, high-performance visualization in the SBP and Profile viewers.
* `scipy`: Used for advanced interpolation, signal processing, and filtering algorithms.
* `numpy`: Used for large-scale numerical operations and matrix manipulations (typically pre-installed with QGIS).

### Installation Instructions (Windows OSGeo4W)

To install these dependencies on Windows, you must use the OSGeo4W Shell provided by QGIS:

1. **Close QGIS** if it is currently running.
2. Open the Windows Start Menu, search for **OSGeo4W Shell**, right-click it, and select **Run as Administrator**.
3. In the command prompt that appears, type the following command to ensure the QGIS Python environment is active:
   ```cmd
   py3_env
   ```
4. Run the following `pip` command to install the required libraries:
   ```cmd
   python -m pip install obspy pyqtgraph scipy numpy
   ```
5. Wait for the installation to complete. Once finished, you can safely close the OSGeo4W Shell.

---

## 2. Manual Plugin Installation

To install the Marine GeoTools plugin manually from a `.zip` archive, follow these step-by-step instructions:

**Method A: Install via QGIS Plugin Manager (Recommended)**
1. Open QGIS.
2. In the top menu bar, navigate to **Plugins** > **Manage and Install Plugins...**
3. Select the **Install from ZIP** tab on the left panel.
4. Click the `...` button and browse to your `marine_geotools.zip` file.
5. Click **Install Plugin**. QGIS will automatically extract and install it.
6. Once installed, ensure the checkbox next to **Marine GeoTools** is checked in the **Installed** tab to enable it.

**Method B: Manual Extraction**
1. Locate your QGIS plugins folder. On Windows, this is typically located at:
   `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   *(You can paste this path directly into your Windows File Explorer address bar).*
2. Extract the contents of `marine_geotools.zip` into this folder. Ensure the extracted folder is named `marine_geotools` (not nested inside another folder).
3. Open QGIS.
4. Go to **Plugins** > **Manage and Install Plugins...** > **Installed**.
5. Check the box next to **Marine GeoTools** to enable it.

---

## 3. Workflows & User Guide

Marine GeoTools provides dedicated workflows and tools for processing both Magnetic (MAG) and Sub-Bottom Profiler (SBP) data.

### 3.1. Magnetic (MAG) Workflow

The MAG processing workflow revolves around importing point data, processing it through standard algorithms, and refining it using the interactive Profile Viewer.

**Step 1: Data Import & GPKG Creation**
- Start by performing a bulk CSV import of your raw magnetic survey data using the **CSV Import** tool, which can be found under the **Database Tools** subgroup within the QGIS Processing Toolbox. This creates a spatially-aware GeoPackage (GPKG) point layer representing your survey tracks.

**Step 2: Processing & Data Enhancement**
- While you can work within the layer's Attribute Table using QGIS native Field Calculators, you can also use the **Field Calculator** found in the **Database Tools** subgroup of the Processing Toolbox. This custom algorithm allows for efficient calculations across multiple input layers simultaneously.
- Apply other advanced geoprocessing algorithms found in the Processing Toolbox, such as:
  - Applying Filters (Low-pass, Non-linear, Rolling Stats)
  - Spike Detection and removal
  - Gridding and IDW Interpolation
  - Calculating Analytic Signal, FFT, and derivatives for interpretation.

**Step 3: Interactive Data Cleaning with Profile Viewer**
- Click the **Profile Viewer** button (magnet icon) on the toolbar to open the interactive profile dock. This viewer allows you to plot up to 5 profiles simultaneously.
- **Key Features of the Profile Viewer:**
  - **Masking:** Assign a **Mask Field** from your attribute table.
  - **Space Button Function:** When inspecting data anomalies on the graph, use the **Space** key on your keyboard to instantly write a specific value (or NULL) to the selected data points in your Mask Field.
  - **Selection:** Click to select individual points, or `Shift+Click` and drag to select regions. Selected points will highlight in the QGIS map canvas. Use `Ctrl+J` to zoom to the selection.
  - **Freeze Layer:** Lock the graph to a specific layer so it doesn't change when you select different layers in the QGIS legend.
  - **Refresh:** Press `F5` to refresh the plot after running processing algorithms.

---

### 3.2. Sub-Bottom Profiler (SBP) Workflow

The SBP workflow provides tools for importing acoustic profiles, applying corrections, picking reflectors, and converting time-series data into depth models.

**Step 1: Data Import**
- Navigate to **SBP Tools** > **File Import**:
  - **XTF Import**: Bulk import eXtended Triton Format (XTF) sub-bottom profiler data (with SGY compatibility).
  - **SBP Import (SGY)**: Bulk import standard SEG-Y seismic files.
  - **Borehole Import (CSV)**: Import borehole point data from CSV files for ground-truthing and correlation.

**Step 2: Data Visualization & Enhancement (SBP Viewer)**
- Click the **SBP Tools** dropdown button or navigate to **SBP Tools** > **SBP Viewer (Dock)**. This opens a dedicated right-side dock widget for interactive sub-bottom profile visualization.
- **Key Features of the SBP Viewer:**
  - **Gain Settings:** Enhance your acoustic signal visually by applying Automatic Gain Control (AGC), Time-Variant Gain (TVG), Band-pass filtering, trace stacking, or water column blanking.
  - **Color Scales:** Adjust your display range (Min/Max), scale to Best/Data, and toggle between colormaps (e.g., Grey, Grey bipolar) and inverted color schemes.
  - **TWT / Depth Display:** Toggle between viewing your profiles in Two-Way Travel Time (ms) or absolute Depth (m), depending on the applied sound velocities.
  - **Markers:** Load QGIS point layers to project vertical borehole/correlation markers directly onto the SBP trace profiles with an adjustable tolerance buffer.
  - **Bathymetry Raster:** Overlay QGIS raster bathymetry layers directly onto the viewer. Raster depths are dynamically sampled along the navigation track and plotted as a continuous line, with options to invert the depth signs to match different coordinate systems.

**Step 3: Picking Reflectors**
- Open the **Picking Dialog** to digitize sub-bottom reflectors (e.g., Seabed, geological horizons).
- Features manual polyline picking, eraser tools, box tracking, and a threshold-based **Auto Tracker** to snap picks directly to peaks or zero-crossings. You can customize the colors and visibility for multiple reflectors.

**Step 4: Vertical Shifts & Corrections**
- Correct your picked SBP data to standard datums using the **Vertical Shift** menu tools:
  - **Bathymetry**: Apply shifts based on bathymetric depth models.
  - **Tide**: Apply tidal corrections using tide table data.
  - **Constant**: Apply a flat, constant vertical offset in meters.

**Step 5: Settings & Depth/Thickness Interpretation**
- **Sound velocity setting**: Update sound velocity parameters directly into your SBP Geopackage databases.
- Translate your time-based SBP reflections into physical depths using the **Depth and Thickness** menu:
  - **Compute Reflector Depth**: Calculate the absolute depth of digitized reflectors using applied vertical shifts and sound velocities.
  - **Calculate Thickness**: Compute the true thickness of geological units between bounded reflector horizons.

---
*Generated automatically by Marine GeoTools Plugin analyzer.*
