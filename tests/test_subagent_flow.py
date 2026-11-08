"""Tests for the live-subagent path: session binding, middleware jailing, and auto-judge on stop.

These exercise the exact kwargs Hermes 0.23 passes to ``tool_request`` middleware
(``tool_name``, ``args``, ``session_id``, ``task_id``), to the ``subagent_start`` hook
(``child_session_id``, ``child_subagent_id``, ``child_goal``) and to ``subagent_stop``
(``child_session_id``, ``child_status``), using a fake ``ctx.subagent_lifecycle`` whose
``launch()`` mirrors the real service: the start hook fires *before* the handle is returned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine  # noqa: E402


@dataclass(frozen=True)
class _Handle:
    contract_version: int
    subagent_id: str
    parent_session_id: Optional[str]
    correlation_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _FakeLifecycle:
    """Mimics agent.subagent_lifecycle.SubagentLifecycleService ordering and shapes."""

    def __init__(self) -> None:
        self.launched: List[Any] = []
        self.steered: List[tuple] = []
        self.counter = 0

    def launch(self, request: Any) -> _Handle:
        self.counter += 1
        sid = f"child-sess-{self.counter}"
        sub_id = f"sub-{self.counter}"
        # Real Hermes fires subagent_start inside launch(), before the handle exists.
        req = dict(request)  # real service: SubagentLaunchRequest | Mapping
        assert req["role"] == "leaf" and req["goal"] and req["correlation_id"]
        joust_pkg._on_subagent_start(
            parent_session_id="parent-sess",
            child_session_id=sid,
            child_subagent_id=sub_id,
            child_role=req["role"],
            child_goal=req["goal"],
        )
        self.launched.append(req)
        return _Handle(1, sub_id, "parent-sess", req["correlation_id"])

    def steer(self, handle: Any, text: str) -> bool:
        self.steered.append((dict(handle)["subagent_id"], text))
        return True


class _FakeCtx:
    def __init__(self) -> None:
        self.subagent_lifecycle = _FakeLifecycle()

    def get_config(self, key: str, default: Any = None) -> Any:
        return default


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    return repo


class TestSubagentFlow(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-sub-")
        os.environ["HERMES_HOME"] = self._tmp.name
        self.repo = _make_repo(Path(self._tmp.name))
        self.ctx = _FakeCtx()
        tools.set_context(self.ctx)

    def tearDown(self) -> None:
        tools.set_context(None)
        worktree_engine.close_cached_dbs()
        self._tmp.cleanup()

    def _launch(self) -> Dict[str, Any]:
        raw = tools.joust_launch({
            "title": "Fix add",
            "goal": "Make add() return the sum.",
            "repo_path": str(self.repo),
            "test_command": f"{sys.executable} -m unittest -q",
            "contenders": [
                {"name": "plus-operator", "strategy": "use a + b"},
                {"name": "sum-builtin", "strategy": "use sum((a, b))"},
            ],
        })
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res["bout"]

