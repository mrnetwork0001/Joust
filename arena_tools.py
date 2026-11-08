"""Tool handlers for Joust (Parallel Git-Worktree Subagent Arena & Benchmark Judge)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

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


def _auto_evaluate_on_stop() -> bool:
    if _CTX_REF is not None:
        try:
            return bool(_CTX_REF.get_config("auto_evaluate_on_stop", True))
        except Exception:
            return True
    return True


_SYNC_AUTO_JUDGE = {"value": False}


def auto_judge_synchronously() -> bool:
    """Tests flip this so subagent_stop judges inline instead of on a daemon thread."""
    return bool(_SYNC_AUTO_JUDGE["value"])


def terminal_runner() -> Any:
    """Runner that executes verification commands through the Hermes terminal tool (None outside Hermes)."""
    return worktree_engine.make_terminal_runner(_CTX_REF)


def _default_runs() -> int:
    if _CTX_REF is not None:
        try:
            return max(1, min(worktree_engine.MAX_RUNS, int(_CTX_REF.get_config("default_runs", 1))))
        except Exception:
            return 1
    return 1


def _keep_loser_branches() -> bool:
    if _CTX_REF is not None:
        try:
            return bool(_CTX_REF.get_config("keep_loser_branches", True))
        except Exception:
            return True
    return True


def evaluate_with_context(
    bout_id: str,
    contender_id: Optional[str] = None,
    test_command_override: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    persist_command: bool = False,
    runs: Optional[int] = None,
) -> Dict[str, Any]:
    """evaluate_bout with the plugin's runner (terminal tool when a Hermes context exists) and config defaults."""
    return worktree_engine.evaluate_bout(
        bout_id=bout_id,
        contender_id=contender_id,
        test_command_override=test_command_override,
        timeout_seconds=worktree_engine.clamp_timeout(timeout_seconds if timeout_seconds else _default_timeout()),
        runner=terminal_runner(),
        persist_command=persist_command,
        runs=int(runs) if runs else _default_runs(),
    )


def joust_crossfire(args: Dict[str, Any], **kwargs: Any) -> str:
    """Fire every contender's new/changed tests at every rival worktree; survival becomes the first tiebreak."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        if not bout_id:
            return json.dumps({"ok": False, "error": "'bout_id' is required."})
        globs = args.get("test_globs")
        bout = worktree_engine.run_crossfire(
            bout_id=bout_id,
            timeout_seconds=worktree_engine.clamp_timeout(args.get("timeout_seconds") or _default_timeout()),
            runner=terminal_runner(),
            test_globs=[str(g) for g in globs] if isinstance(globs, list) and globs else None,
            command_template=str(args.get("command_template")).strip() if args.get("command_template") else None,
        )
        summary = bout.get("crossfire_summary") or {}
        return json.dumps(
            {
                "ok": True,
                "bout_id": bout_id,
                "winner_id": bout["winner_id"],
                "directive": bout["directive"],
                "crossfire": summary,
                "leaderboard": [
                    {"contender_id": c["contender_id"], "survived": c["crossfire_survived"], "total": c["crossfire_total"], "score": c["score"]}
                    for c in bout["contenders"]
                ],
                "message": (
                    f"Crossfire fired {summary.get('volleys', 0)} test volley(s) from {len(summary.get('armed') or [])} contender(s). "
                    f"Leader: {bout['winner_id'] or 'none'}. Include {bout['directive']} on its own paragraph."
                ),
                "bout": bout,
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def joust_report(args: Dict[str, Any], **kwargs: Any) -> str:
    """Keep every contender as a joust/<bout>/<contender> branch and write a markdown bout report."""
    try:
        bout_id = str(args.get("bout_id") or "").strip()
        if not bout_id:
            return json.dumps({"ok": False, "error": "'bout_id' is required."})
        kept: Dict[str, Any] = {"branches": {}, "skipped": {}}
        if bool(args.get("keep_branches", True)):
            kept = worktree_engine.snapshot_contender_branches(bout_id)
        rep = worktree_engine.generate_report(bout_id, write_path=str(args.get("write_path") or "").strip() or None)
        return json.dumps(
            {
                "ok": True,
                "bout_id": bout_id,
                "report_paths": rep["paths"],
                "kept_branches": kept["branches"],
                "skipped_branches": kept["skipped"],
                "markdown": rep["markdown"] if bool(args.get("include_markdown", False)) else rep["markdown"][:1200],
                "message": f"Report saved to {rep['paths'][-1]}; {len(kept['branches'])} contender branch(es) kept.",
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


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
        if any(not isinstance(c, dict) or not str(c.get("name") or "").strip() for c in contenders):
            return json.dumps({"ok": False, "error": "Each contender must be an object with a non-empty 'name' and a 'strategy'."})

        bout = worktree_engine.create_bout(
            title=title,
            goal=goal,
            contenders=contenders,
            repo_path=repo_path,
            test_command=test_command,
            ctx=_CTX_REF,
            spawn_subagents=spawn_subagents,
        )
        spawned = int(bout.get("subagents_spawned") or 0)
        if spawn_subagents and spawned == 0:
            reasons = "; ".join(sorted(set((bout.get("spawn_errors") or {}).values()))) or "unknown"
            launch_note = (
                f"No subagents were launched ({reasons}). The {len(bout['contenders'])} worktrees are ready: "
                "implement each strategy with joust_steer(write_files=...) or run joust_evaluate when ready."
            )
        elif spawn_subagents and spawned < len(bout["contenders"]):
            launch_note = f"{spawned}/{len(bout['contenders'])} contender subagents launched; see spawn_errors."
        else:
            launch_note = f"{spawned} contender subagent(s) launched in isolated Git worktrees." if spawn_subagents else "Worktrees provisioned (no subagents requested)."
        return json.dumps(
            {
                "ok": True,
                "bout_id": bout["bout_id"],
                "directive": bout["directive"],
                "subagents_spawned": spawned,
                "spawn_errors": bout.get("spawn_errors") or {},
                "carry_over": bout.get("carry_over"),
                "message": (
                    f"Joust bout {bout['bout_id']}: {launch_note} "
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
        timeout_seconds = args.get("timeout_seconds")

        bout = evaluate_with_context(
            bout_id=bout_id,
            contender_id=str(contender_id).strip() if contender_id else None,
            test_command_override=str(test_command).strip() if test_command else None,
            timeout_seconds=int(timeout_seconds) if timeout_seconds else None,
            persist_command=bool(args.get("persist_test_command", False)),
            runs=int(args.get("runs")) if args.get("runs") else None,
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

        keep_losers = args.get("keep_losers")
        res = worktree_engine.merge_winner(
            bout_id=bout_id,
            contender_id=str(contender_id).strip() if contender_id else None,
            action=action,
            keep_backup=_keep_backups(),
            keep_losers=bool(keep_losers) if keep_losers is not None else _keep_loser_branches(),
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
