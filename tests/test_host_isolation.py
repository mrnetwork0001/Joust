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


