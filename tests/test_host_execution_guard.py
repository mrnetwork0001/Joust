"""Model-chosen commands never run on the host without the Hermes terminal tool.

The bout's test command comes from the model (``joust_launch`` or ``persist_test_command``). Inside Hermes it
runs through ``ctx.dispatch_tool("terminal", ...)``, so the dangerous-command guard, the approval prompt and the
configured sandbox backend apply. Every path without that runner must refuse without running the command;
only the built-in demo, whose commands the plugin writes itself, may use a bounded subprocess.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, real_make_terminal_runner, tools, worktree_engine as eng  # noqa: E402
from test_engine_edges import _PY, _EngineEdgeCase, _make_repo  # noqa: E402


class TestHostExecutionGuard(_EngineEdgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = _make_repo(self.tmp, broken=False)
        self.marker = self.tmp / "ran-on-host"
        # The command a model might have stored: it would leave a marker file if it ever ran.
        self.bout = self.launch(self.repo, test_command=f"touch {self.marker} && {_PY} -m unittest -q")
        self.bid = self.bout["bout_id"]

    def assert_not_run(self) -> None:
        self.assertFalse(self.marker.exists(), "the model-chosen command ran on the host")
        rows = {c["contender_id"]: c for c in eng.get_bout_details(self.bid)["contenders"]}
        self.assertTrue(all(r["exit_code"] == -1 for r in rows.values()), rows)  # still never-run rows

    def test_cli_judge_refuses_and_points_to_hermes(self) -> None:
        err = self._capture_stderr(lambda: joust_pkg._handle_cli_command(argparse.Namespace(joust_cmd="judge", bout_id=self.bid)))
        self.assertEqual(err[0], 2)
        self.assertIn(f"/joust judge {self.bid}", err[1])
        self.assertIn("ask the agent", err[1])
        self.assert_not_run()

    def test_engine_refuses_evaluate_without_runner(self) -> None:
        with self.assertRaises(eng.HostExecutionRefused):
            eng.evaluate_bout(self.bid)
        with self.assertRaises(eng.HostExecutionRefused):
            eng.evaluate_bout(self.bid, test_command_override=f"touch {self.marker}", persist_command=True)
        self.assertEqual(eng.get_bout_details(self.bid)["test_command"], self.bout["test_command"])  # not persisted
        self.assert_not_run()

    def test_engine_refuses_crossfire_without_runner(self) -> None:
        wt = Path(self.bout["contenders"][0]["worktree_path"])
        (wt / "test_extra.py").write_text(f"import pathlib\npathlib.Path({str(self.marker)!r}).touch()\n", encoding="utf-8")
        with self.assertRaises(eng.HostExecutionRefused):
            eng.run_crossfire(self.bid)
        self.assert_not_run()

    def test_low_level_runner_refuses_without_opt_in(self) -> None:
        wt = Path(self.bout["contenders"][0]["worktree_path"])
        code, _, out, _ = eng._run_verification_command(f"touch {self.marker}", wt)
        self.assertEqual(code, 125)
        self.assertIn("not run", out)
        self.assertFalse(self.marker.exists())

    def test_tool_without_dispatch_tool_returns_an_error_not_a_subprocess(self) -> None:
        saved = eng.make_terminal_runner
        eng.make_terminal_runner = real_make_terminal_runner  # no ctx -> no runner, as in production
        try:
            res = json.loads(tools.joust_evaluate({"bout_id": self.bid}))
        finally:
            eng.make_terminal_runner = saved
        self.assertFalse(res["ok"])
        self.assertIn("outside Hermes", res["error"])
        self.assert_not_run()

    @staticmethod
    def _capture_stderr(fn):
        import io
        buf, old = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            fn()
            code = 0
        except SystemExit as exc:
            code = exc.code
        finally:
            sys.stderr = old
        return code, buf.getvalue()


class TestReportWritePath(_EngineEdgeCase):
    def test_write_path_refuses_dot_git(self) -> None:
        repo = _make_repo(self.tmp, broken=False)
        bid = self.launch(repo)["bout_id"]
        for bad in (".git/hooks/post-checkout", ".git/joust.md", "sub/.GIT/x.md"):
            res = json.loads(tools.joust_report({"bout_id": bid, "write_path": bad}))
            self.assertFalse(res["ok"], bad)
            self.assertIn(".git", res["error"])
        self.assertFalse((repo / ".git" / "joust.md").exists())
        ok = json.loads(tools.joust_report({"bout_id": bid, "write_path": "docs/gitlog.md"}))
        self.assertTrue(ok["ok"], ok)


if __name__ == "__main__":
    unittest.main()
