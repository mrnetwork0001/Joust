"""Crossfire, Flake Shield, kept-loser branches, bout reports, and model-vs-model metadata."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine as eng  # noqa: E402

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=False)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    for cmd in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base")):
        _git(repo, *cmd)
    return repo


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-feat-")
        os.environ["HERMES_HOME"] = self._tmp.name
        self.repo = _make_repo(Path(self._tmp.name))
        tools.set_context(None)

    def tearDown(self) -> None:
        eng.close_cached_dbs()
        self._tmp.cleanup()

    def launch(self, names=("alpha", "beta"), **extra):
        contenders = [{"name": n, "strategy": f"{n} way", **extra.get(n, {})} for n in names]
        res = json.loads(tools.joust_launch({
            "title": "t", "goal": "g", "repo_path": str(self.repo), "spawn_subagents": False,
            "test_command": f"{sys.executable} -m unittest -q", "contenders": contenders,
        }))
        self.assertTrue(res["ok"], res)
        return res["bout"]

    def wt(self, bout, cid) -> Path:
        return Path(next(c["worktree_path"] for c in bout["contenders"] if c["contender_id"] == cid))


class TestCrossfire(_Base):
