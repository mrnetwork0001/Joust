"""FastAPI backend routes for Joust (/api/plugins/joust/*) shared by Hermes Desktop and Dashboard.

Hermes imports this file by path (``importlib.util.spec_from_file_location``), outside the plugin package,
so the engine is loaded the same way under a namespaced module name. The plugin directory is never put on
``sys.path``: that would let a plugin file shadow a Hermes top-level package (e.g. ``tools``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_ENGINE_CACHE: Dict[str, Any] = {}


def _load_engine() -> Any:
    if "engine" in _ENGINE_CACHE:
        return _ENGINE_CACHE["engine"]
    spec = importlib.util.spec_from_file_location("hermes_plugin_joust_worktree_engine", _PLUGIN_ROOT / "worktree_engine.py")
    if spec is None or spec.loader is None:
        raise ImportError("Joust worktree_engine.py could not be located next to dashboard/")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # worktree_engine has no relative imports, so no sys.modules entry is needed
    _ENGINE_CACHE["engine"] = module
    return module


worktree_engine = _load_engine()

router = APIRouter()


@router.get("/bouts")
async def api_list_bouts(limit: int = 25) -> Dict[str, Any]:
    try:
        return {"ok": True, **worktree_engine.list_bouts(limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/bouts/{bout_id}")
async def api_get_bout(bout_id: str) -> Dict[str, Any]:
    try:
        return {"ok": True, "bout": worktree_engine.get_bout_details(bout_id)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/bouts/{bout_id}/diffs")
async def api_get_bout_diffs(bout_id: str) -> Dict[str, Any]:
    try:
        return {"ok": True, **worktree_engine.get_bout_diffs(bout_id)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/bouts/demo")
async def api_run_demo() -> Dict[str, Any]:
    try:
        bout = worktree_engine.run_demo_bout()
        return {"ok": True, "bout": bout}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bouts/{bout_id}/evaluate")
async def api_evaluate_bout(bout_id: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = body or {}
    try:
        bout = worktree_engine.evaluate_bout(
            bout_id=bout_id,
            contender_id=payload.get("contender_id"),
            test_command_override=payload.get("test_command"),
            timeout_seconds=int(payload.get("timeout_seconds") or 60),
        )
        return {"ok": True, "bout": bout}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bouts/{bout_id}/steer")
async def api_steer_contender(bout_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        contender_id = str(body.get("contender_id") or "").strip()
        guidance = str(body.get("guidance") or "").strip()
        write_files = body.get("write_files")
        res = worktree_engine.steer_contender(
            bout_id=bout_id,
            contender_id=contender_id,
            guidance=guidance,
            write_files=write_files if isinstance(write_files, dict) else None,
        )
        return res
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bouts/{bout_id}/merge")
async def api_merge_bout(bout_id: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = body or {}
    try:
        return worktree_engine.merge_winner(
            bout_id=bout_id,
            contender_id=payload.get("contender_id"),
            action=str(payload.get("action") or "apply"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
