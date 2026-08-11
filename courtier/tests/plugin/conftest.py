"""Shared fixtures for plugin tests.

At runtime the PluginManager runs each plugin in its own subprocess and
adds only that plugin's directory to sys.path, so ``from tools import ...``
inside ``entry.py`` resolves correctly.

During testing all plugins share one process, so we CANNOT add every
plugin directory to sys.path globally — the ``tools`` module from the
first plugin loaded would be cached in ``sys.modules`` and every
subsequent plugin would get the wrong ``tools``.

Instead, each plugin test file is responsible for prepending its own
plugin directory to ``sys.path`` before importing the entry module.
"""

import sys
from pathlib import Path

_PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "plugins"


def _plugin_dir(name: str) -> str:
    """Return the absolute path to a plugin directory.

    Supports the restructured layout:
    - plugins/shared/{name}/ (shared plugins)
    - plugins/docaudit/{name}/ (domain plugins, e.g. parse)
    - plugins/docaudit/audit/{name}/ (docaudit audit plugins)
    """
    direct = _PLUGINS_ROOT / name
    if direct.is_dir():
        return str(direct)
    for sub in ("shared", "docaudit", "docaudit/audit"):
        nested = _PLUGINS_ROOT / sub / name
        if nested.is_dir():
            return str(nested)
    # Fall back to direct path so the error message is clear.
    return str(direct)


def _ensure_plugin_path(name: str) -> str:
    """Prepend *name* plugin directory to sys.path and clear any stale
    ``tools`` module cached from a different plugin's import."""
    path_str = _plugin_dir(name)
    sys.path.insert(0, path_str)
    # If a previous plugin test imported ``tools``, that module is still
    # cached in sys.modules and would shadow the current plugin's tools.
    sys.modules.pop("tools", None)
    return path_str
