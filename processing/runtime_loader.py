# -*- coding: utf-8 -*-
from qgis.core import QgsMessageLog, Qgis

import importlib.util
import sys
from pathlib import Path

from qgis.core import QgsProcessingAlgorithm


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_algorithm_classes():
    """Load QgsProcessingAlgorithm subclasses from bundled scripts.

    We load from source files at runtime so you can keep your existing
    Processing scripts mostly unchanged (including filenames with spaces).
    """

    algo_dir = _plugin_root() / 'algorithms' / 'raw'
    if not algo_dir.exists():
        return []

    alg_classes = []

    for path in sorted(algo_dir.glob('*.py')):
        if 'Profile' in path.name:
            continue

        mod_name = f"marine_geotools_alg_{path.stem.replace(' ', '_').replace('-', '_')}"

        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(path))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            for obj in module.__dict__.values():
                try:
                    if isinstance(obj, type) and issubclass(obj, QgsProcessingAlgorithm) and obj is not QgsProcessingAlgorithm:
                        alg_classes.append(obj)
                except Exception as err:
                    QgsMessageLog.logMessage(f"Error checking obj in {mod_name}: {err}", "MarineGeoTools", Qgis.Warning)
                    continue
        except Exception as err:
            QgsMessageLog.logMessage(f"Failed to import algorithm module from {path}: {err}", "MarineGeoTools", Qgis.Warning)
            continue

    uniq = []
    seen = set()
    for cls in alg_classes:
        key = f"{cls.__module__}.{cls.__name__}"
        if key not in seen:
            seen.add(key)
            uniq.append(cls)
    return uniq
