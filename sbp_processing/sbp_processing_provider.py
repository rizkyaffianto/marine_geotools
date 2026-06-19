# -*- coding: utf-8 -*-
from qgis.core import QgsProcessingProvider, QgsMessageLog, Qgis

class SBPToolsProvider(QgsProcessingProvider):
    """Processing provider for SBP Tools."""

    def id(self):
        return "sbptools"

    def name(self):
        return "SBP Tools"

    def longName(self):
        return "SBP Tools (Import, Settings, Vertical Shift, Depth & Thickness)"

    def loadAlgorithms(self):
        from .sgy_import import SGYBulkImportObsPy
        from .xtf_import import XTFSubbottomBulkImport_SGYCompat
        from .copy_setting import SBPCopySettingsToMany
        from .sound_velocity import SBPUpdateVelocitiesFromGpkg
        from .bathy_correction import SBPComputeDtShiftFromRaster
        from .tide_correction import SBPComputeDtTideShiftFromTable
        from .constant_offset import SBPVerticalConstantOffsetMeters
        from .compute_depth import SBPComputeReflectorDepthFromMetaMulti
        from .compute_thickness import SBPComputeThicknessFromTwoDepthFieldsMulti
        from .import_borehole import SBPImportBoreholeCSV

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
