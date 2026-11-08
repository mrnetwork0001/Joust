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

    def test_launch_binds_child_sessions_from_subagent_start(self) -> None:
        bout = self._launch()
        self.assertEqual(len(self.ctx.subagent_lifecycle.launched), 2)
        goals = [r["goal"] for r in self.ctx.subagent_lifecycle.launched]
        self.assertTrue(all("joust:bout=" in g for g in goals))
        for c in bout["contenders"]:
            self.assertEqual(c["status"], "running")
            self.assertTrue(c["subagent_session_id"].startswith("child-sess-"), c)
            self.assertEqual(worktree_engine.get_session_worktree(c["subagent_session_id"]), c["worktree_path"])

    def test_middleware_jails_child_session_tools_into_its_worktree(self) -> None:
        bout = self._launch()
        a, b = bout["contenders"]
        # Relative path from contender A's child session lands in A's worktree
        out = joust_pkg._on_tool_request(
            tool_name="write_file", args={"path": "calc.py", "content": "x"},
            session_id=a["subagent_session_id"], task_id="", tool_call_id="tc1", turn_id="", api_request_id="",
        )
        self.assertEqual(out["args"]["path"], str(Path(a["worktree_path"]) / "calc.py"))
        # Absolute path into the MAIN repo from contender B is redirected into B's worktree
        out_b = joust_pkg._on_tool_request(
            tool_name="read_file", args={"path": str(self.repo / "calc.py")},
            session_id=b["subagent_session_id"],
        )
        self.assertEqual(out_b["args"]["path"], str(Path(b["worktree_path"]) / "calc.py"))
        self.assertNotEqual(out["args"]["path"], out_b["args"]["path"])
        # Terminal gets its workdir pinned; an unbound session is left untouched
        term = joust_pkg._on_tool_request(tool_name="terminal", args={"command": "ls"}, session_id=a["subagent_session_id"])
        self.assertEqual(term["args"]["workdir"], a["worktree_path"])
        self.assertIsNone(joust_pkg._on_tool_request(tool_name="write_file", args={"path": "calc.py"}, session_id="someone-else"))

    def test_main_repo_path_rewrite_passes_main_repo_root(self) -> None:
        bout = self._launch()
        a = bout["contenders"][0]
        # Escape attempts are clamped inside the worktree by the middleware AND vetoed by pre_tool_call
        sid = a["subagent_session_id"]
        out = joust_pkg._on_tool_request(
            tool_name="write_file", args={"path": "../../outside/escaped.txt", "content": "x"},
            session_id=sid, tool_call_id="tc-escape",
        )
        self.assertTrue(out["args"]["path"].startswith(a["worktree_path"] + "/"))
        veto = joust_pkg._on_pre_tool_call(tool_name="write_file", args=out["args"], session_id=sid, tool_call_id="tc-escape")
        self.assertEqual(veto["action"], "block")
        self.assertIn("outside contender", veto["message"])
        # A clean call is not vetoed, and the veto was consumed
        self.assertIsNone(joust_pkg._on_pre_tool_call(tool_name="write_file", args={"path": "calc.py"}, session_id=sid, tool_call_id="tc-ok"))

    def test_pre_tool_call_asks_for_human_approval_before_merge(self) -> None:
        approve = joust_pkg._on_pre_tool_call(tool_name="joust_merge", args={"bout_id": "bout-x", "action": "apply"})
        self.assertEqual(approve["action"], "approve")
        self.assertEqual(approve["rule_key"], "joust:merge")
        self.assertIsNone(joust_pkg._on_pre_tool_call(tool_name="joust_merge", args={"bout_id": "bout-x", "action": "dry_run"}))
        self.assertIsNone(joust_pkg._on_pre_tool_call(tool_name="joust_evaluate", args={"bout_id": "bout-x"}))

    def test_evaluate_routes_test_command_through_hermes_terminal_tool(self) -> None:
        calls = []

        def dispatch_tool(name, args, **kw):
            calls.append((name, dict(args)))
            return json.dumps({"output": "ok\nJOUST_BENCHMARK_MS=7.5", "exit_code": 0, "error": None, "cwd": args["workdir"]})

        self.ctx.dispatch_tool = dispatch_tool
        bout = self._launch()
        res = json.loads(tools.joust_evaluate({"bout_id": bout["bout_id"], "timeout_seconds": 1}))
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 2)
        for (name, args), c in zip(calls, bout["contenders"]):
            self.assertEqual(name, "terminal")
            self.assertEqual(args["workdir"], c["worktree_path"])
            self.assertEqual(args["timeout"], 5)  # clamped to the 5s floor
            self.assertIn("unittest", args["command"])
        judged = {c["contender_id"]: c for c in res["bout"]["contenders"]}
        self.assertTrue(all(c["tests_passed"] for c in judged.values()))
        self.assertEqual({c["duration_ms"] for c in judged.values()}, {7.5})

    def test_terminal_tool_errors_count_as_failures_without_fallback(self) -> None:
        self.ctx.dispatch_tool = lambda name, args, **kw: json.dumps({"output": "", "exit_code": None, "error": "blocked by policy", "status": "blocked"})
        bout = self._launch()
        res = json.loads(tools.joust_evaluate({"bout_id": bout["bout_id"]}))
        self.assertTrue(res["ok"], res)
        for c in res["bout"]["contenders"]:
            self.assertFalse(c["tests_passed"])
            self.assertIn("blocked", c["test_output"])

    def test_subagent_stop_auto_evaluates_contender(self) -> None:
        bout = self._launch()
        a = bout["contenders"][0]
        (Path(a["worktree_path"]) / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        joust_pkg._on_subagent_stop(
            parent_session_id="parent-sess", parent_turn_id="", child_session_id=a["subagent_session_id"],
            child_role="leaf", child_summary="done", child_status="completed", tool_call_history=[], duration_ms=10,
        )
        joust_pkg.wait_for_auto_judges()  # the benchmark runs off Hermes' hook thread
        details = worktree_engine.get_bout_details(bout["bout_id"])
        judged = {c["contender_id"]: c for c in details["contenders"]}
        self.assertTrue(judged["plus-operator"]["tests_passed"])
        self.assertGreaterEqual(judged["plus-operator"]["score"], 60.0)
        # The rival has not run yet: it keeps its live status instead of being marked failed
        self.assertEqual(judged["sum-builtin"]["status"], "running")
        self.assertEqual(judged["sum-builtin"]["exit_code"], -1)
        self.assertEqual(details["status"], "partial")

    def test_post_tool_call_counts_contender_activity(self) -> None:
        bout = self._launch()
        a = bout["contenders"][0]
        for _ in range(3):
            joust_pkg._on_post_tool_call(tool_name="read_file", args={}, result="", session_id=a["subagent_session_id"])
        details = worktree_engine.get_bout_details(bout["bout_id"])
        counts = {c["contender_id"]: c["tool_calls_count"] for c in details["contenders"]}
        self.assertEqual(counts["plus-operator"], 3)
        self.assertEqual(counts["sum-builtin"], 0)

    def test_steer_reaches_live_subagent_via_handle(self) -> None:
        bout = self._launch()
        res = json.loads(tools.joust_steer({"bout_id": bout["bout_id"], "contender_id": "sum-builtin", "guidance": "Use sum()"}))
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["steered_live_subagent"])
        self.assertEqual(self.ctx.subagent_lifecycle.steered, [("sub-2", "Use sum()")])

    def test_binding_survives_in_memory_loss_via_db_fallback(self) -> None:
        bout = self._launch()
        a = bout["contenders"][0]
        with worktree_engine._LOCK:
            worktree_engine._ACTIVE_SESSION_WORKTREES.clear()
            worktree_engine._ACTIVE_SESSION_CONTENDERS.clear()
        self.assertEqual(worktree_engine.get_session_worktree(a["subagent_session_id"]), a["worktree_path"])

    def test_launch_without_parent_session_leaves_worktrees_ready(self) -> None:
        tools.set_context(None)
        bout = self._launch()
        for c in bout["contenders"]:
            self.assertEqual(c["status"], "ready")
            self.assertEqual(c["subagent_session_id"], "")


if __name__ == "__main__":
    unittest.main()
