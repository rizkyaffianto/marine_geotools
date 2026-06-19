from qgis.core import QgsMessageLog, Qgis
import os
import glob

files = glob.glob('d:/Antigravity/QGIS/QGIS Plugin Tools/marine_geotools/**/*.py', recursive=True)
count = 0
for f in files:
    with open(f, 'r', encoding='utf-8') as file:
        content = file.read()
    if '"MAG"' in content or "'MAG'" in content:
        content = content.replace('"MAG"', '"MAG"').replace("'MAG'", "'MAG'")
        with open(f, 'w', encoding='utf-8') as file:
            file.write(content)
        count += 1
        QgsMessageLog.logMessage(f"Updated {f}", "PluginLogger", Qgis.Info)
QgsMessageLog.logMessage(f"Total files updated: {count}", "PluginLogger", Qgis.Info)
