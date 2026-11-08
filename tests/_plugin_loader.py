"""Load the Joust plugin package by *path* so the tests work from any checkout or install directory name.

Hermes installs catalog plugins as ``<HERMES_HOME>/plugins/joust`` (lower-case), while a clone may be named
``Joust``; importing by directory name would break on case-sensitive filesystems. ``unittest discover -s tests``
puts this directory on ``sys.path``, so test modules simply ``from _plugin_loader import joust_pkg, tools, worktree_engine``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "joust_plugin_under_test"

if PACKAGE_NAME in sys.modules:
    joust_pkg = sys.modules[PACKAGE_NAME]
else:
    _spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)]
    )
    assert _spec is not None and _spec.loader is not None
    joust_pkg = importlib.util.module_from_spec(_spec)
    sys.modules[PACKAGE_NAME] = joust_pkg
    _spec.loader.exec_module(joust_pkg)

tools = joust_pkg.tools  # arena_tools, re-exported by the package as ``tools``
worktree_engine = joust_pkg.worktree_engine
