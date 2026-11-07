"""Tool handlers for Joust (Parallel Git-Worktree Subagent Arena & Benchmark Judge)."""

from __future__ import annotations

import json
from typing import Any, Dict

from . import worktree_engine

_CTX_REF: Any = None


def set_context(ctx: Any) -> None:
    global _CTX_REF
    _CTX_REF = ctx


def _default_timeout() -> int:
    if _CTX_REF is not None:
        try:
            return int(_CTX_REF.get_config("default_timeout_seconds", 60))
        except Exception:
            return 60
    return 60


def _keep_backups() -> bool:
    if _CTX_REF is not None:
        try:
            return bool(_CTX_REF.get_config("keep_backup_patches", True))
        except Exception:
            return True
    return True


def joust_launch(args: Dict[str, Any], **kwargs: Any) -> str:
    """Launch a new Joust bout with isolated Git worktrees for 2-4 contenders."""
    try:
        title = str(args.get("title") or "").strip()
        goal = str(args.get("goal") or "").strip()
        contenders = args.get("contenders") or []
        repo_path = args.get("repo_path")
        test_command = str(args.get("test_command") or "python3 -m unittest").strip()
        spawn_subagents = bool(args.get("spawn_subagents", True))

        if not title or not goal:
            return json.dumps({"ok": False, "error": "Both 'title' and 'goal' are required."})
        if not isinstance(contenders, list) or len(contenders) < 2:
            return json.dumps({"ok": False, "error": "Provide 2 to 4 contenders in 'contenders'."})

        bout = worktree_engine.create_bout(
            title=title,
            goal=goal,
            contenders=contenders,
            repo_path=repo_path,
            test_command=test_command,
            ctx=_CTX_REF,
            spawn_subagents=spawn_subagents,
        )
        return json.dumps(
            {
                "ok": True,
                "bout_id": bout["bout_id"],
                "directive": bout["directive"],
                "message": (
                    f"Joust bout {bout['bout_id']} launched with {len(bout['contenders'])} isolated Git worktrees. "
                    f"Include {bout['directive']} on its own paragraph in your reply to render the live arena card."
                ),
                "bout": bout,
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_evaluate(args: Dict[str, Any], **kwargs: Any) -> str:
    """Run the verification & benchmark judge across contenders in a bout."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        if not bout_id:
            return json.dumps({"ok": False, "error": "'bout_id' is required."})
        contender_id = args.get("contender_id")
        test_command = args.get("test_command")
        timeout_seconds = int(args.get("timeout_seconds") or _default_timeout())

        bout = worktree_engine.evaluate_bout(
            bout_id=bout_id,
            contender_id=str(contender_id).strip() if contender_id else None,
            test_command_override=str(test_command).strip() if test_command else None,
            timeout_seconds=timeout_seconds,
        )
        return json.dumps(
            {
                "ok": True,
                "bout_id": bout["bout_id"],
                "winner_id": bout["winner_id"],
                "directive": bout["directive"],
                "bout": bout,
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_diff(args: Dict[str, Any], **kwargs: Any) -> str:
    """Inspect unified Git diffs across contender worktrees."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        if not bout_id:
            return json.dumps({"ok": False, "error": "'bout_id' is required."})
        contender_id = args.get("contender_id")
        max_chars = int(args.get("max_chars") or 8000)

        payload = worktree_engine.get_bout_diffs(
            bout_id=bout_id,
            contender_id=str(contender_id).strip() if contender_id else None,
            max_chars=max_chars,
        )
        return json.dumps({"ok": True, **payload})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_steer(args: Dict[str, Any], **kwargs: Any) -> str:
    """Steer a contender mid-flight and/or apply file updates in its isolated worktree."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        contender_id = str(args.get("contender_id") or "").strip()
        guidance = str(args.get("guidance") or "").strip()
        write_files = args.get("write_files")

        if not bout_id or not contender_id or not guidance:
            return json.dumps({"ok": False, "error": "'bout_id', 'contender_id', and 'guidance' are required."})

        res = worktree_engine.steer_contender(
            bout_id=bout_id,
            contender_id=contender_id,
            guidance=guidance,
            write_files=write_files if isinstance(write_files, dict) else None,
            ctx=_CTX_REF,
        )
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_merge(args: Dict[str, Any], **kwargs: Any) -> str:
    """Apply the winning contender's patch into the main repo, dry-run, rollback, or cleanup."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        if not bout_id:
            return json.dumps({"ok": False, "error": "'bout_id' is required."})
        contender_id = args.get("contender_id")
        action = str(args.get("action") or "apply").strip().lower()

        res = worktree_engine.merge_winner(
            bout_id=bout_id,
            contender_id=str(contender_id).strip() if contender_id else None,
            action=action,
            keep_backup=_keep_backups(),
        )
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_status(args: Dict[str, Any], **kwargs: Any) -> str:
    """Inspect a specific bout, list recent bouts, or run the built-in 3-contender demo tournament."""
    try:
        if args.get("run_demo"):
            bout = worktree_engine.run_demo_bout()
            return json.dumps(
                {
                    "ok": True,
                    "demo": True,
                    "bout_id": bout["bout_id"],
                    "winner_id": bout["winner_id"],
                    "directive": bout["directive"],
                    "message": (
                        f"Demo bout {bout['bout_id']} completed! Winner: {bout['winner_id']}. "
                        f"Include {bout['directive']} on its own paragraph to render the interactive scoreboard."
                    ),
                    "bout": bout,
                }
            )

        bout_id = str(args.get("bout_id") or "").strip()
        if bout_id:
            bout = worktree_engine.get_bout_details(bout_id)
            return json.dumps({"ok": True, "bout": bout, "directive": bout["directive"]})

        summary = worktree_engine.list_bouts(limit=20)
        return json.dumps({"ok": True, **summary})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})
