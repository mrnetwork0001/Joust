"""Integration tests: Joust driven through REAL Hermes 0.23 plumbing, not through the plugin's own functions.

Skipped cleanly when ``hermes_cli`` is not importable (plain ``python3 -m unittest discover -s tests``).
Run it with an interpreter that has Hermes installed, e.g. the hermes venv python.

What is exercised, and through which Hermes entry point:

* install + enable: the repo is copied into ``$HERMES_HOME/plugins/joust`` (fresh temp HERMES_HOME) and
  enabled with the real ``hermes plugins enable joust`` CLI;
* load: ``hermes_cli.plugins.discover_plugins()`` -> ``PluginManager`` imports the plugin as
  ``hermes_plugins.joust`` and calls ``register(ctx)`` with a real ``PluginContext``;
* tools: ``tools.registry.registry.dispatch(...)`` with the dispatcher's context kwargs, and
  ``PluginContext.dispatch_tool``;
* middleware: ``hermes_cli.middleware.apply_tool_request_middleware`` (the agent-loop call site);
* hooks: ``hermes_cli.lifecycle.invoke_hook`` (what ``tools/delegate_tool.py`` fires for ``subagent_start``
  and ``model_tools`` fires for ``post_tool_call``) and ``hermes_cli.plugins.invoke_hook`` (what
  ``tools/delegate_tool_results.py`` fires for ``subagent_stop``);
* slash command: ``hermes_cli.plugin_command_dispatch.invoke_plugin_command`` +
  ``resolve_plugin_command_result`` exactly as ``cli.py``/the gateway call it;
* CLI subcommand: the ``register_cli_command`` entry wired into an argparse subparser.

Nothing here needs an LLM: the real ``ctx.subagent_lifecycle`` is only exercised on its no-parent failure
path (``SubagentLifecycleError``), which is what Joust hits outside an active parent turn.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERMES_AVAILABLE = importlib.util.find_spec("hermes_cli") is not None
_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_TOOLS = ["joust_launch", "joust_evaluate", "joust_diff", "joust_steer", "joust_merge", "joust_status", "joust_crossfire", "joust_report"]
_EXPECTED_HOOKS = ["pre_tool_call", "post_tool_call", "subagent_start", "subagent_stop"]


def _hermes_cli_argv() -> List[str]:
    """The ``hermes`` console script next to this interpreter, else the equivalent ``-c`` launcher."""
    sibling = Path(sys.executable).with_name("hermes")
    if sibling.is_file():
        return [str(sibling)]
    return [sys.executable, "-c", "import sys; from hermes_cli.main import main; sys.exit(main())"]


def _make_repo(root: Path) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=str(root)))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    return repo


@unittest.skipUnless(_HERMES_AVAILABLE, "Hermes (hermes_cli) is not importable in this interpreter")
class TestHermesChain(unittest.TestCase):
    """One fresh HERMES_HOME per class: plugin discovery is process-global and the CLI enable costs seconds."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_home = os.environ.get("HERMES_HOME")
        cls._tmp = tempfile.TemporaryDirectory(prefix="joust-hermes-chain-")
        cls.home = Path(cls._tmp.name) / "home"
        (cls.home / "plugins").mkdir(parents=True)
        shutil.copytree(
            _REPO_ROOT, cls.home / "plugins" / "joust",
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        os.environ["HERMES_HOME"] = str(cls.home)

        # 1. Enable through the real CLI (writes plugins.enabled into $HERMES_HOME/config.yaml).
        proc = subprocess.run(
            [*_hermes_cli_argv(), "plugins", "enable", "joust"],
            env={**os.environ, "HERMES_HOME": str(cls.home)}, capture_output=True, text=True, timeout=300,
        )
        cls.enable_output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise AssertionError(f"`hermes plugins enable joust` failed ({proc.returncode}):\n{cls.enable_output}")

        # 2. Load through the real plugin manager: register(ctx) runs with a real PluginContext.
        from hermes_cli import plugins as hp

        from tools.registry import discover_builtin_tools
        discover_builtin_tools()  # the terminal tool must exist for joust_evaluate's dispatch_tool routing
        hp.discover_plugins()
        cls.hp = hp
        cls.manager = hp.get_plugin_manager()
        loaded = cls.manager._plugins.get("joust")
        if loaded is None or not loaded.enabled:
            raise AssertionError(f"joust did not load through PluginManager: {loaded and loaded.error!r}")
        cls.loaded = loaded
        cls.plugin = loaded.module  # the hermes_plugins.joust package instance Hermes imported
        cls.engine = loaded.module.worktree_engine

    @classmethod
    def tearDownClass(cls) -> None:
        with contextlib.suppress(Exception):
            cls.engine.close_cached_dbs()
        with contextlib.suppress(Exception):
            cls.hp._reset_plugin_managers_for_tests()
        if cls._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = cls._prev_home
        cls._tmp.cleanup()

    def setUp(self) -> None:
        # Other test modules move HERMES_HOME per test; Hermes keys its plugin manager by resolved home.
        os.environ["HERMES_HOME"] = str(self.home)

    # ---- helpers -------------------------------------------------------------------------------------

    def _dispatch(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch like ``model_tools._execute_tool`` does: registry.dispatch + the dispatcher's kwargs."""
        from tools.registry import registry

        raw = registry.dispatch(tool, args, scope=self.manager.scope_key, task_id="task-1", session_id="parent-sess", user_task=None)
        self.assertIsInstance(raw, str, raw)
        return json.loads(raw)

    def _launch(self, repo: Path, *, spawn_subagents: bool = False, test_command: Optional[str] = None) -> Dict[str, Any]:
        res = self._dispatch("joust_launch", {
            "title": "Fix add", "goal": "Make add() return the sum.", "repo_path": str(repo),
            "test_command": test_command or f"{sys.executable} -m unittest -q", "spawn_subagents": spawn_subagents,
            "contenders": [
                {"name": "plus-operator", "strategy": "use a + b"},
                {"name": "sum-builtin", "strategy": "use sum((a, b))"},
            ],
        })
        self.assertTrue(res.get("ok"), res)
        return res

    def _start_hook(self, bout_id: str, contender_id: str, child_session_id: str, child_subagent_id: str) -> List[Any]:
        """Fire subagent_start with the exact kwargs tools/delegate_tool.py passes, through hermes_cli.lifecycle."""
        from hermes_cli.lifecycle import invoke_hook

        marker = self.engine.contender_marker(bout_id, contender_id)
        return invoke_hook(
            "subagent_start", parent_session_id="parent-sess", parent_turn_id="turn-1", parent_subagent_id=None,
            child_session_id=child_session_id, child_subagent_id=child_subagent_id, child_role="leaf",
            child_goal=f"[Joust Contender: {contender_id}] ({marker})\nProblem Goal: make add() return the sum.",
        )

    def _stop_hook(self, child_session_id: str, status: str = "completed") -> List[Any]:
        """Fire subagent_stop with the exact kwargs tools/delegate_tool_results.py passes, through hermes_cli.plugins."""
        from hermes_cli.plugins import invoke_hook

        return invoke_hook(
            "subagent_stop", parent_session_id="parent-sess", parent_turn_id="turn-1", child_session_id=child_session_id,
            child_role="leaf", child_summary="done", child_status=status, tool_call_history=[], duration_ms=10,
        )

    @staticmethod
    def _contenders(details: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {c["contender_id"]: c for c in details["contenders"]}

    # ---- 1. registration through the real PluginManager ------------------------------------------------

    def test_01_register_ctx_populates_every_hermes_registry(self) -> None:
        from tools.registry import registry

        self.assertIn("enabled", self.enable_output.lower(), self.enable_output)
        self.assertIsNone(self.loaded.error)
        self.assertTrue(self.plugin.__name__.startswith("hermes_plugins."), self.plugin.__name__)

        # Tools: ledger attribution and the global tool registry (profile scope) agree.
        self.assertEqual(self.loaded.tools_registered, _EXPECTED_TOOLS)
        for name in _EXPECTED_TOOLS:
            entry = registry.get_entry(name, scope=self.manager.scope_key)
            self.assertIsNotNone(entry, name)
            self.assertEqual(entry.toolset, "joust")
            self.assertTrue(entry.handler.__module__.startswith(self.plugin.__name__), entry.handler.__module__)
            self.assertIn(name, self.manager._plugin_tool_names)
        self.assertEqual(sorted(self.loaded.manifest.provides_tools), sorted(_EXPECTED_TOOLS))

        # Hooks + middleware.
        self.assertEqual(self.loaded.hooks_registered, _EXPECTED_HOOKS)
        for hook in _EXPECTED_HOOKS:
            self.assertTrue(any(cb.__module__ == self.plugin.__name__ for cb in self.manager._hooks.get(hook, [])), hook)
            self.assertTrue(self.hp.has_hook(hook), hook)
        self.assertEqual(self.loaded.middleware_registered, ["tool_request"])
        self.assertTrue(any(cb.__module__ == self.plugin.__name__ for cb in self.manager._middleware.get("tool_request", [])))
        self.assertTrue(self.hp.has_middleware("tool_request"))

        # Slash command, CLI subcommand, skill.
        self.assertEqual(self.loaded.commands_registered, ["joust"])
        cmd = self.manager._plugin_commands["joust"]
        self.assertEqual(cmd["plugin_key"], "joust")
        self.assertTrue(callable(cmd["handler"]))
        self.assertTrue(cmd["args_hint"])
        self.assertIs(self.hp.get_plugin_command_handler("joust"), cmd["handler"])
        self.assertIn("joust", self.manager._cli_commands)
        skill_path = self.manager.find_plugin_skill("joust:joust-arena")
        self.assertIsNotNone(skill_path)
        self.assertTrue(Path(skill_path).is_file())
        self.assertEqual(self.manager.list_plugin_skills("joust"), ["joust-arena"])
        self.assertIn("joust:joust-arena", [m["name"] for m in self.manager.list_plugin_skill_metadata()])

        # The ctx the plugin kept is the real PluginContext, and its config path resolves to manifest defaults.
        ctx = self.plugin.tools._CTX_REF
        self.assertIsInstance(ctx, self.hp.PluginContext)
        self.assertEqual(ctx.plugin_id, "joust")
        self.assertEqual(ctx.get_config("default_timeout_seconds", 60), 60)

    def test_02_cli_enable_wrote_config_and_plugins_list_sees_it(self) -> None:
        from hermes_cli.config import load_config_readonly

        enabled = (load_config_readonly() or {}).get("plugins", {}).get("enabled")
        self.assertIn("joust", enabled or [], enabled)
        proc = subprocess.run(
            [*_hermes_cli_argv(), "plugins", "list"],
            env={**os.environ, "HERMES_HOME": str(self.home)}, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("joust", proc.stdout)
        self.assertIn("enabled", proc.stdout)

    # ---- 2. tool dispatch through the registry / ctx.dispatch_tool -------------------------------------

    def test_03_registry_dispatch_joust_launch_provisions_git_worktrees(self) -> None:
        repo = _make_repo(Path(self._tmp.name))
        res = self._launch(repo)
        self.assertEqual(len(res["bout"]["contenders"]), 2)
        self.assertEqual(res["directive"], f'::joust-bout{{id="{res["bout_id"]}"}}')
        for c in res["bout"]["contenders"]:
            self.assertEqual(c["status"], "ready")
            wt = Path(c["worktree_path"])
            self.assertTrue(wt.is_dir(), wt)
            self.assertTrue((wt / ".git").is_file(), "expected a `git worktree add` worktree, not a copytree fallback")
            self.assertTrue(str(wt).startswith(str((self.home / "plugin-data" / "joust").resolve())), wt)
        # A second dispatch surface: ctx.dispatch_tool reaches the same handler.
        ctx = self.plugin.tools._CTX_REF
        status = json.loads(ctx.dispatch_tool("joust_status", {"bout_id": res["bout_id"]}))
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["bout"]["bout_id"], res["bout_id"])

    def test_04_ctx_dispatch_tool_joust_status_run_demo(self) -> None:
        ctx = self.plugin.tools._CTX_REF
        demo = json.loads(ctx.dispatch_tool("joust_status", {"run_demo": True}))
        self.assertTrue(demo["ok"], demo)
        self.assertTrue(demo["demo"])
        self.assertEqual(demo["winner_id"], "hash-bucket-index")
        self.assertEqual(demo["directive"], f'::joust-bout{{id="{demo["bout_id"]}"}}')
        ranked = demo["bout"]["contenders"]
        self.assertEqual([c["contender_id"] for c in ranked], ["hash-bucket-index", "bisect-sorted-book", "naive-filter-scan"])
        self.assertEqual([c["tests_passed"] for c in ranked], [True, True, False])
        self.assertTrue(ranked[0]["is_winner"])
        # Unknown-tool and invalid-arg paths stay JSON, never exceptions.
        bad = self._dispatch("joust_evaluate", {})
        self.assertFalse(bad["ok"])
        self.assertIn("bout_id", bad["error"])

    # ---- 3. tool_request middleware through hermes_cli.middleware ---------------------------------------

    def test_05_apply_tool_request_middleware_jails_bound_session(self) -> None:
        from hermes_cli.middleware import apply_tool_request_middleware

        repo = _make_repo(Path(self._tmp.name))
        bout = self._launch(repo)["bout"]
        a = self._contenders(bout)["plus-operator"]
        self.assertTrue(self.engine.bind_contender_subagent(bout["bout_id"], "plus-operator", "chain-sess-A", "sa-A"))

        args = {"path": "calc.py", "content": "x"}
        res = apply_tool_request_middleware(
            "write_file", args, session_id="chain-sess-A", task_id="", tool_call_id="t1", turn_id="", api_request_id="",
        )
        self.assertEqual(res.payload["path"], str(Path(a["worktree_path"]) / "calc.py"))
        self.assertEqual(res.payload["content"], "x")
        self.assertTrue(res.changed)
        self.assertEqual(len(res.trace), 1, res.trace)
        self.assertEqual(res.trace[0]["source"], "joust")
        self.assertTrue(res.trace[0]["reason"].startswith("Joust isolated write_file"), res.trace)
        self.assertEqual(res.original_payload, {"path": "calc.py", "content": "x"})
        self.assertEqual(args, {"path": "calc.py", "content": "x"}, "Hermes hands the plugin a copy; caller args stay intact")

        # Absolute path into the MAIN repo is redirected into the contender's worktree.
        res_abs = apply_tool_request_middleware(
            "read_file", {"path": str(repo / "calc.py")}, session_id="chain-sess-A", task_id="", tool_call_id="t2",
            turn_id="", api_request_id="",
        )
        self.assertEqual(res_abs.payload["path"], str(Path(a["worktree_path"]) / "calc.py"))
        self.assertEqual(res_abs.trace[0]["source"], "joust")

        # Terminal gets its workdir pinned to the worktree.
        res_term = apply_tool_request_middleware(
            "terminal", {"command": "ls"}, session_id="chain-sess-A", task_id="", tool_call_id="t3", turn_id="", api_request_id="",
        )
        self.assertEqual(res_term.payload, {"command": "ls", "workdir": a["worktree_path"]})
        self.assertEqual(res_term.trace[0]["source"], "joust")

        # An unbound session's arguments come back untouched.
        res_other = apply_tool_request_middleware(
            "write_file", {"path": "calc.py", "content": "x"}, session_id="someone-else", task_id="", tool_call_id="t4",
            turn_id="", api_request_id="",
        )
        self.assertEqual(res_other.payload, {"path": "calc.py", "content": "x"})
        self.assertNotIn("joust", [t.get("source") for t in res_other.trace])

    def test_06_unbound_session_is_a_no_op_for_hermes(self) -> None:
        """KNOWN DEFECT (documented, see the hermes-chain report): for a session Joust is not tracking the
        middleware returns ``{"args": args}`` instead of ``None``. Hermes' ``_apply_request_chain`` treats
        ANY dict carrying an ``args`` dict as a rewrite, so every tool call of every non-Joust session is
        reported as ``changed=True`` with a ``{"source": "plugin"}`` trace entry. The fix is to return
        ``None`` on the no-op path; flip this decorator off once it lands."""
        from hermes_cli.middleware import apply_tool_request_middleware

        for sid in ("someone-else", ""):
            res = apply_tool_request_middleware(
                "write_file", {"path": "calc.py", "content": "x"}, session_id=sid, task_id="", tool_call_id="t5",
                turn_id="", api_request_id="",
            )
            self.assertFalse(res.changed, (sid, res.trace))
            self.assertEqual(res.trace, [], (sid, res.trace))

    # ---- 4. lifecycle hooks through Hermes invoke_hook -------------------------------------------------

    def test_07_subagent_start_via_lifecycle_invoke_hook_binds_child_session(self) -> None:
        repo = _make_repo(Path(self._tmp.name))
        bout = self._launch(repo)["bout"]
        b = self._contenders(bout)["sum-builtin"]

        results = self._start_hook(bout["bout_id"], "sum-builtin", "chain-sess-B", "sa-B")
        self.assertEqual(results, [], "subagent_start is an observer; the plugin must return None")
        self.assertEqual(self.engine.get_session_worktree("chain-sess-B"), b["worktree_path"])
        self.assertEqual(self.engine.get_session_contender("chain-sess-B"), (bout["bout_id"], "sum-builtin"))
        details = self.engine.get_bout_details(bout["bout_id"])
        judged = self._contenders(details)["sum-builtin"]
        self.assertEqual(judged["subagent_session_id"], "chain-sess-B")
        self.assertEqual(judged["status"], "running")
        self.assertIn("contender.subagent_started", [e["event_type"] for e in details["events"]])

        # A child whose goal carries no Joust marker is ignored without error.
        from hermes_cli.lifecycle import invoke_hook

        out = invoke_hook(
            "subagent_start", parent_session_id="parent-sess", parent_turn_id="", parent_subagent_id=None,
            child_session_id="unrelated-sess", child_subagent_id="sa-X", child_role="leaf", child_goal="Summarize the README.",
        )
        self.assertEqual(out, [])
        self.assertIsNone(self.engine.get_session_worktree("unrelated-sess"))

    def _wait_judges(self) -> None:
        import importlib
        pkg = importlib.import_module(self.engine.__name__.rsplit(".", 1)[0])
        pkg.wait_for_auto_judges()

    def test_08_subagent_stop_via_plugins_invoke_hook_auto_evaluates_on_caller_thread(self) -> None:
        from hermes_cli import plugins_dispatch

        # Contract pinned by Hermes: subagent_stop never moves to a timeout worker (hooks.md).
        self.assertIn("subagent_stop", plugins_dispatch._HOOK_CALLER_THREAD_HOOKS)
        self.assertNotIn("subagent_stop", plugins_dispatch._HOOK_TIMEOUT_BOUNDED_HOOKS)

        repo = _make_repo(Path(self._tmp.name))
        bout = self._launch(repo)["bout"]
        a = self._contenders(bout)["plus-operator"]
        self._start_hook(bout["bout_id"], "plus-operator", "chain-sess-C", "sa-C")
        (Path(a["worktree_path"]) / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        seen: Dict[str, Any] = {}

        def spy(**kwargs: Any) -> None:
            seen["thread"] = threading.current_thread()
            seen["kwargs"] = sorted(kwargs)

        self.manager._hooks["subagent_stop"].append(spy)
        try:
            results = self._stop_hook("chain-sess-C")
        finally:
            self.manager._hooks["subagent_stop"].remove(spy)
        self.assertEqual(results, [])
        self.assertIs(seen["thread"], threading.current_thread(), "subagent_stop callbacks run on the firing thread")
        self.assertIn("telemetry_schema_version", seen["kwargs"])

        self._wait_judges()  # bookkeeping is synchronous; the benchmark runs on a daemon thread
        details = self.engine.get_bout_details(bout["bout_id"])
        judged = self._contenders(details)
        self.assertTrue(judged["plus-operator"]["tests_passed"], judged["plus-operator"]["test_output"])
        self.assertGreaterEqual(judged["plus-operator"]["score"], 60.0)
        self.assertEqual(judged["plus-operator"]["status"], "passed")
        self.assertFalse(judged["sum-builtin"]["tests_passed"])
        self.assertIn(judged["sum-builtin"]["status"], ("ready", "running"))  # rival not judged yet: never marked failed
        self.assertEqual(details["winner_id"], "plus-operator")
        events = [e["event_type"] for e in details["events"]]
        self.assertIn("contender.subagent_stopped", events)
        self.assertIn("bout.evaluated", events)

    def test_09_subagent_stop_offloads_the_benchmark_from_the_firing_thread(self) -> None:
        """subagent_stop is in _HOOK_CALLER_THREAD_HOOKS (no timeout, caller thread), so Joust must not run the
        benchmark inline: the hook returns immediately and a daemon thread judges the contender."""
        repo = _make_repo(Path(self._tmp.name))
        slow = f'{sys.executable} -c "import time; time.sleep(1.2)"'
        bout = self._launch(repo, test_command=slow)["bout"]
        self._start_hook(bout["bout_id"], "sum-builtin", "chain-sess-D", "sa-D")
        t0 = time.perf_counter()
        self._stop_hook("chain-sess-D")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.8, f"subagent_stop blocked the firing thread for {elapsed:.2f}s")
        self._wait_judges()
        judged = self._contenders(self.engine.get_bout_details(bout["bout_id"]))["sum-builtin"]
        self.assertTrue(judged["tests_passed"])
        self.assertGreaterEqual(judged["duration_ms"], 1000.0)

