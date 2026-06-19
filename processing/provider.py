# -*- coding: utf-8 -*-
from qgis.core import QgsMessageLog, Qgis, QgsProcessingProvider

from .runtime_loader import load_algorithm_classes

class MarineGeoToolsProvider(QgsProcessingProvider):
    """Unified Processing provider that loads both MAG and SBP algorithms."""

    def id(self):
        return 'marine_geotools'

    def name(self):
        return 'Marine GeoTools'

    def longName(self):
        return 'Marine GeoTools (MAG & SBP)'

    def loadAlgorithms(self):
        for AlgClass in load_algorithm_classes():
            try:
                self.addAlgorithm(AlgClass())
            except Exception as err:
                QgsMessageLog.logMessage(f"Failed to load algorithm {AlgClass}: {err}", "MarineGeoTools", Qgis.Warning)
                continue
        
        from ..sbp_processing.sgy_import import SGYBulkImportObsPy
        from ..sbp_processing.xtf_import import XTFSubbottomBulkImport_SGYCompat
        from ..sbp_processing.copy_setting import SBPCopySettingsToMany
        from ..sbp_processing.sound_velocity import SBPUpdateVelocitiesFromGpkg
        from ..sbp_processing.bathy_correction import SBPComputeDtShiftFromRaster
        from ..sbp_processing.tide_correction import SBPComputeDtTideShiftFromTable
        from ..sbp_processing.constant_offset import SBPVerticalConstantOffsetMeters
        from ..sbp_processing.compute_depth import SBPComputeReflectorDepthFromMetaMulti
        from ..sbp_processing.compute_thickness import SBPComputeThicknessFromTwoDepthFieldsMulti
        from ..sbp_processing.import_borehole import SBPImportBoreholeCSV

        self.addAlgorithm(XTFSubbottomBulkImport_SGYCompat())
        self.addAlgorithm(SGYBulkImportObsPy())
        self.addAlgorithm(SBPUpdateVelocitiesFromGpkg())
        self.addAlgorithm(SBPCopySettingsToMany())
        self.addAlgorithm(SBPComputeDtShiftFromRaster())
        self.addAlgorithm(SBPComputeDtTideShiftFromTable())
        self.addAlgorithm(SBPVerticalConstantOffsetMeters())
        self.addAlgorithm(SBPComputeReflectorDepthFromMetaMulti())
        self.addAlgorithm(SBPComputeThicknessFromTwoDepthFieldsMulti())
        self.addAlgorithm(SBPImportBoreholeCSV())
