"""Contracts Joust relies on under both ``plugins.isolation`` modes of Hermes 0.23 (``in_process`` and ``host``).

With ``plugins.isolation: host`` third-party plugin code runs in a separate plugin-host process:

* ``ctx.subagent_lifecycle`` is a remote facade; every call returns plain data, so ``launch()`` hands back a
  dict-like ``Record`` (no ``to_dict``) and ``steer()`` receives the dict Joust persisted;
* hook and middleware payloads/results cross the wire as JSON (one document per line);
* ``ctx.register_cli_command`` is a no-op (``hermes joust`` is not wired), desktop broadcasts from inside the
  host reach no client, and the host's in-memory maps die with a host restart, so SQLite rows must be enough to
  re-bind a child session.

These tests pin the plugin-side behaviour with fakes and never need an LLM. The two codec tests that import
Hermes itself are skipped under a bare system python.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine  # noqa: E402


def _hermes_importable() -> bool:
    try:
        return (importlib.util.find_spec("hermes_cli.plugin_host_wire") is not None
                and importlib.util.find_spec("agent.subagent_lifecycle") is not None)
    except (ImportError, ValueError):
        return False


HERMES_AVAILABLE = _hermes_importable()

# The exact field set of agent.subagent_lifecycle.SubagentHandle in Hermes 0.23.
_HANDLE_FIELDS = ("contract_version", "subagent_id", "parent_session_id", "correlation_id", "created_at",
                  "provider", "model", "role", "depth", "capability")


def _plain_handle(sub_id: str, correlation_id: Optional[str]) -> Dict[str, Any]:
    return {"contract_version": 1, "subagent_id": sub_id, "parent_session_id": "parent-sess",
            "correlation_id": correlation_id, "created_at": 1700000000.0, "provider": None, "model": None,
            "role": "leaf", "depth": 1, "capability": "cap-" + sub_id}


class _RecordLike(dict):
    """What a dataclass looks like after crossing the plugin-host wire: a dict with attribute access and no
    methods (``hermes_cli.plugin_host_wire.Record``)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class _SubagentLifecycleError(ValueError):
    """Shape of agent.subagent_lifecycle.SubagentLifecycleError (a ValueError subclass)."""


class _FakeLifecycle:
    """``launch()`` fires ``subagent_start`` synchronously on the caller's thread before returning (the real
    service does this at the end of ``_build_child_preserving_parent_tools``) and returns ``handle_factory``'s
    value; ``steer`` records the handle it was given."""

    def __init__(self, handle_factory=_plain_handle, launch_error: Optional[BaseException] = None,
                 steer_error: Optional[BaseException] = None) -> None:
        self.handle_factory = handle_factory
        self.launch_error = launch_error
        self.steer_error = steer_error
        self.launched: List[Dict[str, Any]] = []
        self.steered: List[tuple] = []
        self.counter = 0

    def launch(self, request: Any) -> Any:
        if self.launch_error is not None:
            raise self.launch_error
        req = dict(request)
        self.counter += 1
        sid, sub_id = f"child-sess-{self.counter}", f"sub-{self.counter}"
        joust_pkg._on_subagent_start(
            parent_session_id="parent-sess", parent_turn_id="turn-1", parent_subagent_id="",
            child_session_id=sid, child_subagent_id=sub_id, child_role=req["role"], child_goal=req["goal"],
        )
        self.launched.append(req)
        return self.handle_factory(sub_id, req.get("correlation_id"))

    def steer(self, handle: Any, text: str) -> bool:
        if self.steer_error is not None:
            raise self.steer_error
        self.steered.append((handle, text))
        return True


class _FakeCtx:
    def __init__(self, lifecycle: Any = None, config: Optional[Dict[str, Any]] = None) -> None:
        if lifecycle is not None:
            self.subagent_lifecycle = lifecycle
        self._config = config or {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    return repo


def _reset_memory_maps() -> None:
    with worktree_engine._LOCK:
        worktree_engine._ACTIVE_SESSION_WORKTREES.clear()
        worktree_engine._ACTIVE_SESSION_CONTENDERS.clear()
        worktree_engine._PENDING_SUBAGENT_SESSIONS.clear()


class _JoustCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-host-iso-")
        os.environ["HERMES_HOME"] = self._tmp.name
        self.repo = _make_repo(Path(self._tmp.name))
        _reset_memory_maps()

    def tearDown(self) -> None:
        tools.set_context(None)
        _reset_memory_maps()
        worktree_engine.close_cached_dbs()
        self._tmp.cleanup()

    def _launch(self, ctx: Any, spawn: bool = True) -> Dict[str, Any]:
        tools.set_context(ctx)
        raw = tools.joust_launch({
            "title": "Fix add",
            "goal": "Make add() return the sum.",
            "repo_path": str(self.repo),
            "test_command": f"{sys.executable} -m unittest -q",
            "spawn_subagents": spawn,
            "contenders": [
                {"name": "plus-operator", "strategy": "use a + b"},
                {"name": "sum-builtin", "strategy": "use sum((a, b))"},
            ],
        })
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res["bout"]

    def _handle_rows(self, bout_id: str) -> Dict[str, Dict[str, Any]]:
        rows = worktree_engine.get_db().execute(
            "SELECT contender_id, status, subagent_session_id, subagent_handle_json FROM contenders WHERE bout_id = ?",
            (bout_id,),
        ).fetchall()
        return {r["contender_id"]: dict(r) for r in rows}


class TestRemoteFacadeContracts(_JoustCase):
    """``ctx.subagent_lifecycle`` as a host remote facade: plain-data handles in, plain-data handles out."""

    def test_create_bout_accepts_plain_dict_handle_and_steers_with_that_dict(self) -> None:
        lifecycle = _FakeLifecycle(handle_factory=_plain_handle)
        bout = self._launch(_FakeCtx(lifecycle))
        self.assertEqual(len(lifecycle.launched), 2)
        rows = self._handle_rows(bout["bout_id"])
        for cid, row in rows.items():
            self.assertEqual(row["status"], "running", row)
            self.assertTrue(row["subagent_session_id"].startswith("child-sess-"), row)
            stored = json.loads(row["subagent_handle_json"])
            self.assertEqual(set(stored), set(_HANDLE_FIELDS))
            self.assertEqual(stored["correlation_id"], f"{bout['bout_id']}:{cid}")
        res = json.loads(tools.joust_steer({"bout_id": bout["bout_id"], "contender_id": "sum-builtin",
                                            "guidance": "Use sum()"}))
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["steered_live_subagent"])
        (handle, text), = lifecycle.steered
        self.assertIsInstance(handle, dict)
        self.assertEqual(handle, json.loads(rows["sum-builtin"]["subagent_handle_json"]))
        self.assertEqual(text, "Use sum()")

    def test_create_bout_accepts_record_like_handle_without_to_dict(self) -> None:
        lifecycle = _FakeLifecycle(handle_factory=lambda sid, cid: _RecordLike(_plain_handle(sid, cid)))
        bout = self._launch(_FakeCtx(lifecycle))
        rows = self._handle_rows(bout["bout_id"])
        for row in rows.values():
            self.assertEqual(json.loads(row["subagent_handle_json"])["parent_session_id"], "parent-sess")
            self.assertEqual(row["status"], "running")

    def test_launch_raising_lifecycle_error_falls_back_to_ready(self) -> None:
        # The real service raises SubagentLifecycleError("No active Hermes parent session is available.")
        # outside a parent turn; from inside the host it is re-raised as a plain ValueError.
        for exc in (_SubagentLifecycleError("No active Hermes parent session is available."),
                    ValueError("No active Hermes parent session is available."),
                    RuntimeError("the plugin host for this profile exited (code -9)")):
            with self.subTest(error=type(exc).__name__):
                bout = self._launch(_FakeCtx(_FakeLifecycle(launch_error=exc)))
                for c in bout["contenders"]:
                    self.assertEqual(c["status"], "ready", c)
                    self.assertEqual(c["subagent_session_id"], "")
                    self.assertTrue(Path(c["worktree_path"]).is_dir())
                rows = self._handle_rows(bout["bout_id"])
                self.assertTrue(all(r["subagent_handle_json"] == "{}" for r in rows.values()), rows)

    def test_ctx_without_lifecycle_facade_leaves_contenders_ready(self) -> None:
        bout = self._launch(_FakeCtx(lifecycle=None))
        self.assertEqual({c["status"] for c in bout["contenders"]}, {"ready"})

    def test_steer_tolerates_facade_raising_and_still_records_guidance(self) -> None:
        lifecycle = _FakeLifecycle(steer_error=RuntimeError("the plugin host for this profile exited"))
        bout = self._launch(_FakeCtx(lifecycle))
        res = json.loads(tools.joust_steer({"bout_id": bout["bout_id"], "contender_id": "plus-operator",
                                            "guidance": "Try harder", "write_files": {"NOTES.md": "hint"}}))
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["steered_live_subagent"])
        self.assertEqual(res["written_files"], ["NOTES.md"])
        notes = [c["steering_notes"] for c in res["bout"]["contenders"] if c["contender_id"] == "plus-operator"][0]
        self.assertEqual([n["guidance"] for n in notes], ["Try harder"])


class TestWireSafety(_JoustCase):
    """Hook and middleware callbacks accept the full Hermes 0.23 payloads and return JSON-serialisable data."""

    def test_middleware_result_is_json_round_trippable(self) -> None:
        bout = self._launch(_FakeCtx(_FakeLifecycle()))
        a = bout["contenders"][0]
        payload = {
            "tool_name": "write_file",
            "args": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
            "original_args": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
            "task_id": "", "session_id": a["subagent_session_id"], "tool_call_id": "tc-1", "turn_id": "turn-1",
            "api_request_id": "api-1", "telemetry_schema_version": 1, "middleware_schema_version": 1,
        }
        out = joust_pkg._on_tool_request(**payload)
        wire = json.loads(json.dumps(out))  # what the plugin-host channel does to the return value
        self.assertEqual(wire, out)
        self.assertIsInstance(wire["args"]["path"], str)
        self.assertEqual(wire["args"]["path"], str(Path(a["worktree_path"]) / "calc.py"))
        self.assertEqual(wire["source"], "joust")
        self.assertIn("reason", wire)
        # The payload must not have been mutated in place (Hermes hands each callback a copy, but the wire
        # codec makes that copy for us in host mode, so the original dict must come back unchanged).
        self.assertEqual(payload["args"]["path"], "calc.py")

    def test_hooks_accept_full_payloads_and_return_none(self) -> None:
        bout = self._launch(_FakeCtx(_FakeLifecycle(), config={"auto_evaluate_on_stop": False}))
        a = bout["contenders"][0]
        sid = a["subagent_session_id"]
        self.assertIsNone(joust_pkg._on_post_tool_call(
            tool_name="read_file", args={"path": "calc.py"}, result="...", session_id=sid, task_id="",
            tool_call_id="tc-1", turn_id="turn-1", api_request_id="api-1", telemetry_schema_version=1))
        self.assertIsNone(joust_pkg._on_subagent_start(
            parent_session_id="parent-sess", parent_turn_id="turn-1", parent_subagent_id="", child_session_id="",
            child_subagent_id="", child_role="leaf", child_goal="no marker", telemetry_schema_version=1))
        self.assertIsNone(joust_pkg._on_subagent_stop(
            parent_session_id="parent-sess", parent_turn_id="turn-1", child_session_id=sid, child_role="leaf",
            child_summary="done", child_status="completed", tool_call_history=[], duration_ms=12,
            telemetry_schema_version=1))
        details = worktree_engine.get_bout_details(bout["bout_id"])
        counts = {c["contender_id"]: c["tool_calls_count"] for c in details["contenders"]}
        self.assertEqual(counts["plus-operator"], 0)  # post_tool_call never counts (middleware does)
        kinds = [e["event_type"] for e in details["events"]]
        self.assertIn("contender.subagent_stopped", kinds)
        # auto_evaluate_on_stop=False must leave the contender unjudged.
        self.assertEqual([c["status"] for c in details["contenders"] if c["contender_id"] == "plus-operator"], ["running"])

    def test_non_dict_args_pass_through_untouched(self) -> None:
        self.assertIsNone(joust_pkg._on_tool_request(tool_name="terminal", args=None, session_id="x"))
        self.assertIsNone(joust_pkg._on_tool_request(tool_name="terminal", args="ls", session_id="x"))


class TestHostRestartRecovery(_JoustCase):
    """A plugin-host restart (or a reload) wipes every module-level map; SQLite must carry the binding."""

    def test_bindings_recover_from_db_after_simulated_host_restart(self) -> None:
        bout = self._launch(_FakeCtx(_FakeLifecycle()))
        a, b = bout["contenders"]
        _reset_memory_maps()
        worktree_engine.close_cached_dbs()  # a new host process opens a new connection
        self.assertEqual(worktree_engine.get_session_worktree(a["subagent_session_id"]), a["worktree_path"])
        self.assertEqual(worktree_engine.get_session_contender(b["subagent_session_id"]),
                         (bout["bout_id"], b["contender_id"]))
        out = joust_pkg._on_tool_request(tool_name="write_file", args={"path": "calc.py", "content": "x"},
                                         session_id=a["subagent_session_id"], task_id="")
        self.assertEqual(out["args"]["path"], str(Path(a["worktree_path"]) / "calc.py"))
        joust_pkg._on_tool_request(tool_name="read_file", args={"path": "calc.py"}, session_id=b["subagent_session_id"])
        details = worktree_engine.get_bout_details(bout["bout_id"])
        counts = {c["contender_id"]: c["tool_calls_count"] for c in details["contenders"]}
        self.assertEqual(counts, {"plus-operator": 1, "sum-builtin": 1})

    def test_unknown_session_after_restart_stays_unbound(self) -> None:
        self._launch(_FakeCtx(_FakeLifecycle()))
        _reset_memory_maps()
        worktree_engine.close_cached_dbs()
        self.assertIsNone(worktree_engine.get_session_worktree("stranger", "task-stranger"))
        out = joust_pkg._on_tool_request(tool_name="write_file", args={"path": "calc.py"}, session_id="stranger")
        self.assertIsNone(out)

    def test_stale_binding_to_removed_worktree_is_ignored(self) -> None:
        bout = self._launch(_FakeCtx(_FakeLifecycle()))
        a = bout["contenders"][0]
        json.loads(tools.joust_merge({"bout_id": bout["bout_id"], "action": "cleanup"}))
        _reset_memory_maps()
        self.assertFalse(Path(a["worktree_path"]).exists())
        self.assertIsNone(worktree_engine.get_session_worktree(a["subagent_session_id"]))


class TestRelayedHookThreading(_JoustCase):
    """In host mode the ``subagent_start`` hook that Hermes fires *inside* ``launch()`` is relayed back to the
    plugin host and served on a different thread than the one running ``create_bout``."""

    def _relaying_lifecycle(self, wait_seconds: float) -> tuple:
        threads: List[threading.Thread] = []
        finished_before_return: List[bool] = []

        class Relayed(_FakeLifecycle):
            def launch(inner, request: Any) -> Any:
                req = dict(request)
                inner.counter += 1
                sid, sub_id = f"child-sess-{inner.counter}", f"sub-{inner.counter}"
                t = threading.Thread(
                    target=joust_pkg._on_subagent_start, kwargs=dict(
                        parent_session_id="parent-sess", parent_turn_id="turn-1", parent_subagent_id="",
                        child_session_id=sid, child_subagent_id=sub_id, child_role="leaf", child_goal=req["goal"]),
                    daemon=True, name="relayed-subagent-start")
                threads.append(t)
                t.start()
                t.join(timeout=wait_seconds)  # Hermes would block here until the hook returns
                finished_before_return.append(not t.is_alive())
                inner.launched.append(req)
                return _plain_handle(sub_id, req.get("correlation_id"))

        return Relayed(), threads, finished_before_return

    def test_relayed_subagent_start_binding_is_eventually_consistent(self) -> None:
        lifecycle, threads, _ = self._relaying_lifecycle(wait_seconds=1.0)
        bout = self._launch(_FakeCtx(lifecycle))
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "relayed subagent_start never completed")
        rows = self._handle_rows(bout["bout_id"])
        sessions = {cid: row["subagent_session_id"] for cid, row in rows.items()}
        self.assertEqual(set(sessions.values()), {"child-sess-1", "child-sess-2"}, sessions)
        for cid, sid in sessions.items():
            self.assertEqual(worktree_engine.get_session_contender(sid), (bout["bout_id"], cid))

    def test_known_hazard_relayed_hook_completes_while_launch_waits(self) -> None:
        """Documents a known host-mode hazard: ``create_bout`` holds ``worktree_engine._LOCK`` across
        ``service.launch()``, so a ``subagent_start`` served on another thread blocks in ``get_db()`` until
        the lock is released — with the real plugin host that is a deadlock (launch() waits for the hook, the
        hook waits for the lock). Remove the ``expectedFailure`` once launch() runs outside the lock."""
        lifecycle, threads, finished = self._relaying_lifecycle(wait_seconds=1.0)
        self._launch(_FakeCtx(lifecycle))
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(finished, [True, True], "subagent_start could not complete while launch() waited")


class TestRegisterInHostLikeContext(unittest.TestCase):
    """``register(ctx)`` against a context shaped like the host's ``RemotePluginContext``."""

    class _RecordingCtx:
        def __init__(self) -> None:
            self.calls: Dict[str, List[Any]] = {}

        def _record(self, kind: str, *args: Any, **kwargs: Any) -> None:
            self.calls.setdefault(kind, []).append((args, kwargs))

        def register_tool(self, **kwargs: Any) -> None:
            self._record("tool", **kwargs)

        def register_middleware(self, kind: str, cb: Any) -> None:
            self._record("middleware", kind, cb)

        def register_hook(self, name: str, cb: Any) -> None:
            self._record("hook", name, cb)

        def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
            self._record("command", name, handler, **kwargs)

        def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
            # RemotePluginContext: logs "ctx.register_cli_command() is skipped in the plugin host" and returns None.
            self._record("cli_skipped")

        def register_skill(self, name: str, path: Any, **kwargs: Any) -> None:
            self._record("skill", name, str(path), **kwargs)

        def get_config(self, key: str, default: Any = None) -> Any:
            return default

    def test_register_skips_cli_but_wires_tools_hooks_middleware_and_slash(self) -> None:
        ctx = self._RecordingCtx()
        try:
            joust_pkg.register(ctx)
            self.assertIs(tools._CTX_REF, ctx)
        finally:
            tools.set_context(None)
        tool_names = sorted(kw["name"] for _, kw in ctx.calls["tool"])
        self.assertEqual(tool_names, ["joust_crossfire", "joust_diff", "joust_evaluate", "joust_launch", "joust_merge",
                                      "joust_report", "joust_status", "joust_steer"])
        self.assertEqual([a[0] for a, _ in ctx.calls["middleware"]], ["tool_request"])
        self.assertEqual(sorted(a[0] for a, _ in ctx.calls["hook"]), ["post_tool_call", "pre_tool_call", "subagent_start", "subagent_stop"])
        self.assertEqual([a[0] for a, _ in ctx.calls["command"]], ["joust"])
        self.assertEqual(len(ctx.calls["cli_skipped"]), 1)
        self.assertEqual([a[0] for a, _ in ctx.calls.get("skill", [])], ["joust-arena"])
        for _, kw in ctx.calls["tool"]:
            self.assertTrue(callable(kw["handler"]))
            json.dumps(kw["schema"])  # schemas must be JSON (they cross the wire in host mode)


class TestDesktopNotify(unittest.TestCase):
    """``notify_desktop`` must never raise: inside the plugin host the broadcast has no client, and under a
    bare interpreter ``hermes_cli`` is not importable at all."""

    def _with_fake_plugin_events(self, broadcast):
        saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.plugin_events")}
        pkg = types.ModuleType("hermes_cli")
        pkg.__path__ = []  # type: ignore[attr-defined]
        mod = types.ModuleType("hermes_cli.plugin_events")
        mod.broadcast_plugin_event = broadcast  # type: ignore[attr-defined]
        pkg.plugin_events = mod  # type: ignore[attr-defined]
        sys.modules["hermes_cli"] = pkg
        sys.modules["hermes_cli.plugin_events"] = mod
        return saved

    def _restore(self, saved: Dict[str, Any]) -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_notify_is_silent_when_broadcast_raises(self) -> None:
        seen: List[tuple] = []

        def broadcast(plugin_id, event, payload=None):
            seen.append((plugin_id, event, payload))
            raise RuntimeError("no gateway in this process")

        saved = self._with_fake_plugin_events(broadcast)
        try:
            self.assertIsNone(worktree_engine.notify_desktop("bout.updated", {"bout_id": "bout-1"}))
        finally:
            self._restore(saved)
        self.assertEqual(seen, [("joust", "bout.updated", {"bout_id": "bout-1"})])

    def test_notify_is_silent_when_hermes_is_missing(self) -> None:
        saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.plugin_events")}
        sys.modules["hermes_cli"] = None  # type: ignore[assignment]  # forces ImportError on the lazy import
        sys.modules.pop("hermes_cli.plugin_events", None)
        try:
            self.assertIsNone(worktree_engine.notify_desktop("bout.updated"))
        finally:
            self._restore(saved)


@unittest.skipUnless(HERMES_AVAILABLE, "Hermes is not importable in this interpreter")
class TestHermesWireCodec(_JoustCase):
    """The real plugin-host codec: a SubagentHandle crosses as a Record, middleware results as JSON."""

    def test_subagent_handle_crosses_wire_as_record_and_create_bout_copes(self) -> None:
        from agent.subagent_lifecycle import SubagentHandle
        from hermes_cli.plugin_host_wire import Record, decode, encode

        def factory(sub_id: str, correlation_id: Optional[str]) -> Any:
            handle = SubagentHandle(1, sub_id, "parent-sess", correlation_id, time.time(), None, None, "leaf", 1, "cap")
            crossed = decode(encode(handle))
            assert isinstance(crossed, Record) and not hasattr(crossed, "to_dict")
            return crossed

        lifecycle = _FakeLifecycle(handle_factory=factory)
        bout = self._launch(_FakeCtx(lifecycle))
        rows = self._handle_rows(bout["bout_id"])
        for row in rows.values():
            stored = json.loads(row["subagent_handle_json"])
            self.assertEqual(SubagentHandle.from_dict(stored).parent_session_id, "parent-sess")
        res = json.loads(tools.joust_steer({"bout_id": bout["bout_id"], "contender_id": "plus-operator", "guidance": "go"}))
        self.assertTrue(res["steered_live_subagent"])
        (handle, _), = lifecycle.steered
        # Joust sends the persisted dict back; the real service coerces a Mapping with SubagentHandle.from_dict.
        self.assertEqual(SubagentHandle.from_dict(decode(encode(handle))).subagent_id, "sub-1")

    def test_middleware_result_round_trips_through_wire_codec(self) -> None:
        from hermes_cli.plugin_host_wire import decode, encode
        bout = self._launch(_FakeCtx(_FakeLifecycle()))
        a = bout["contenders"][0]
        out = joust_pkg._on_tool_request(tool_name="terminal", args={"command": "ls"}, session_id=a["subagent_session_id"])
        self.assertEqual(decode(encode(out)), out)
        self.assertEqual(out["args"]["workdir"], a["worktree_path"])


if __name__ == "__main__":
    unittest.main()
