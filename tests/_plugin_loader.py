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


# ---- a stand-in for the Hermes ``terminal`` tool ------------------------------------------------------------
# Outside Hermes there is no ``ctx.dispatch_tool``, and the engine now refuses to run a bout's command without
# one. Unit tests that call the tools with no context get this runner instead: it returns the terminal tool's
# JSON shape ({output, exit_code, error, status}), so the tests exercise the same parsing path as production.
import json as _json
import os as _os
import subprocess as _subprocess

real_make_terminal_runner = worktree_engine.make_terminal_runner


def fake_terminal_runner(command: str, workdir: str, timeout: int) -> str:
    env = {"PATH": _os.environ.get("PATH", "/usr/bin:/bin"), "HOME": _os.environ.get("HOME", ""),
           "PYTHONPATH": workdir, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = _subprocess.run(["/bin/sh", "-c", command], cwd=workdir, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, check=False)
        return _json.dumps({"output": (proc.stdout or "") + (proc.stderr or ""), "exit_code": proc.returncode,
                            "error": None, "status": "ok"})
    except _subprocess.TimeoutExpired:
        return _json.dumps({"output": f"Command timed out after {timeout}s", "exit_code": 124, "error": None,
                            "status": "timeout"})


def _make_terminal_runner_for_tests(ctx):
    runner = real_make_terminal_runner(ctx)
    return runner if runner is not None else fake_terminal_runner


worktree_engine.make_terminal_runner = _make_terminal_runner_for_tests
