from qgis.core import QgsMessageLog, Qgis
# -*- coding: utf-8 -*-

def classFactory(iface):
    """Factory for QGIS plugin."""
    from .marine_geotools_plugin import MarineGeoToolsPlugin
    return MarineGeoToolsPlugin(iface)
