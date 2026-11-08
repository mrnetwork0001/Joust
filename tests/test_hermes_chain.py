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

