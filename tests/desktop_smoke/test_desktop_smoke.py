"""unittest wrapper around the Node desktop smoke harness (tests/desktop_smoke/smoke.mjs).

The harness needs Node plus react / react-dom (and optionally happy-dom) installed OUTSIDE the
repo — by default under ``$TMPDIR/joust-desktop-smoke/node_modules`` or wherever
``JOUST_SMOKE_NODE_MODULES`` points (see README.md). When any of that is missing the test is
SKIPPED, never failed, so ``python3 -m unittest discover -s tests`` stays green on a bare machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _node_modules_dir() -> Path:
    configured = os.environ.get("JOUST_SMOKE_NODE_MODULES")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "joust-desktop-smoke" / "node_modules"


class DesktopPluginSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        # The harness never reads HERMES_HOME, but every test in this repo runs against a fresh one.
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-desktop-smoke-")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_desktop_plugin_smoke(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        node_modules = _node_modules_dir()
        for dep in ("react", "react-dom"):
            if not (node_modules / dep / "package.json").exists():
                self.skipTest(f"{dep} not installed under {node_modules}; see tests/desktop_smoke/README.md")

        env = {**os.environ, "JOUST_SMOKE_NODE_MODULES": str(node_modules)}
        proc = subprocess.run(
            [node, str(_HERE / "smoke.mjs")],
            cwd=str(_HERE.parent.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode == 2:
            self.skipTest(f"smoke harness prerequisites missing:\n{proc.stderr}")
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"desktop smoke harness failed (exit {proc.returncode})\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )
        self.assertIn("[desktop-smoke]", proc.stdout)
        self.assertIn(" 0 failed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
