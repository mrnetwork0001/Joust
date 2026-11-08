"""Edge-case regression tests for the Joust engine (worktree_engine.py + arena_tools.py).

Every test runs against a fresh temporary ``HERMES_HOME`` and a throwaway Git repository, drives the
engine through the public tool handlers (``joust_launch`` / ``joust_evaluate`` / ``joust_merge`` ...)
or the engine helpers, and only asserts behaviour that is correct today. Scenarios that currently
misbehave are deliberately *not* locked in here; they live in the engine-edges bug report.

Groups: merge/rollback/cleanup lifecycle, the verification judge (timeouts, failures, telemetry,
encoding, ties, bounds), launch validation and dirty-tree carry-over, the middleware path jail,
and SQLite/data-dir handling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine as eng  # noqa: E402

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
_PY = sys.executable
_FIXED_CALC = "def add(a, b):\n    return a + b\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)


def _make_repo(root: Path, name: str = "repo", broken: bool = True) -> Path:
    """Create a one-commit repo whose test suite fails (broken=True) or passes at HEAD."""
    repo = root / name
    repo.mkdir(parents=True)
    body = "a - b" if broken else "a + b"
    (repo / "calc.py").write_text(f"def add(a, b):\n    return {body}\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


class _EngineEdgeCase(unittest.TestCase):
    """Fresh HERMES_HOME per test; module-level session bindings are cleared on teardown."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-edges-")
        self.tmp = Path(self._tmp.name)
        os.environ["HERMES_HOME"] = self._tmp.name
        tools.set_context(None)

    def tearDown(self) -> None:
        tools.set_context(None)
        eng.close_cached_dbs()
        with eng._LOCK:
            eng._ACTIVE_SESSION_WORKTREES.clear()
            eng._ACTIVE_SESSION_CONTENDERS.clear()
            eng._PENDING_SUBAGENT_SESSIONS.clear()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------------------------------
    def launch(self, repo: Path, names: Iterable[str] = ("alpha", "beta"), **extra: Any) -> Dict[str, Any]:
        raw = tools.joust_launch(
            {
                "title": "edge",
                "goal": "edge goal",
                "repo_path": str(repo),
                "test_command": extra.pop("test_command", f"{_PY} -m unittest -q"),
                "contenders": [{"name": n, "strategy": "s"} for n in names],
                "spawn_subagents": False,
                **extra,
            }
        )
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res["bout"]

    @staticmethod
    def worktrees(bout: Dict[str, Any]) -> Dict[str, Path]:
        return {c["contender_id"]: Path(c["worktree_path"]) for c in bout["contenders"]}

    def evaluate(self, bout_id: str, **kw: Any) -> Dict[str, Any]:
        raw = tools.joust_evaluate({"bout_id": bout_id, **kw})
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res

    @staticmethod
    def merge(bout_id: str, action: str, contender_id: Optional[str] = None) -> Dict[str, Any]:
        args: Dict[str, Any] = {"bout_id": bout_id, "action": action}
        if contender_id:
            args["contender_id"] = contender_id
        return json.loads(tools.joust_merge(args))

    @staticmethod
    def by_id(res: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {c["contender_id"]: c for c in res["bout"]["contenders"]}


# ====================================================================================================
# joust_merge: apply / dry_run / rollback / cleanup lifecycle
# ====================================================================================================
