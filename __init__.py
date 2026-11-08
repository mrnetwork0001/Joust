"""Joust — Parallel Git-Worktree Subagent Arena, Benchmark Judge & 1-Click Merge Studio."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import arena_tools as tools, schemas, worktree_engine

logger = logging.getLogger(__name__)
_PLUGIN_DIR = Path(__file__).resolve().parent


def _on_tool_request(**kwargs: Any) -> Dict[str, Any]:
    """Middleware: scope file and terminal tool arguments to a contender's isolated Git worktree."""
    tool_name = str(kwargs.get("tool_name") or "")
    args = kwargs.get("args")
    if not isinstance(args, dict):
        return {"args": args}

    session_id = str(kwargs.get("session_id") or "")
    task_id = str(kwargs.get("task_id") or "")
    wt_path = worktree_engine.get_session_worktree(session_id, task_id)
    if not wt_path:
        return {"args": args}

    rewritten, reason = worktree_engine.rewrite_tool_args_for_worktree(tool_name, args, wt_path)
    if reason:
        return {"args": rewritten, "source": "joust", "reason": reason}
    return {"args": args}


def _on_post_tool_call(**kwargs: Any) -> None:
    """Observer hook: track tool activity inside active contender subagent sessions."""
    session_id = str(kwargs.get("session_id") or "")
    task_id = str(kwargs.get("task_id") or "")
    bound = worktree_engine.get_session_contender(session_id, task_id)
    if not bound:
        return
    bout_id, contender_id = bound
    try:
        conn = worktree_engine.get_db()
        conn.execute(
            "UPDATE contenders SET tool_calls_count = tool_calls_count + 1 WHERE bout_id = ? AND contender_id = ?",
            (bout_id, contender_id),
        )
        conn.commit()
        worktree_engine.notify_desktop("bout.updated", {"bout_id": bout_id})
    except Exception as exc:
        logger.debug("Joust post_tool_call update skipped: %s", exc)


def _on_subagent_stop(**kwargs: Any) -> None:
    """Lifecycle hook: when a contender subagent finishes, optionally auto-evaluate its worktree."""
    corr_id = str(kwargs.get("correlation_id") or "")
    session_id = str(kwargs.get("child_session_id") or kwargs.get("session_id") or "")
    bout_id = ""
    contender_id = ""

    if ":" in corr_id and corr_id.startswith("bout-"):
        bout_id, _, contender_id = corr_id.partition(":")
    else:
        bound = worktree_engine.get_session_contender(session_id)
        if bound:
            bout_id, contender_id = bound

    if not bout_id or not contender_id:
        return

    try:
        worktree_engine.evaluate_bout(bout_id=bout_id, contender_id=contender_id)
    except Exception as exc:
        logger.debug("Joust auto-evaluate on subagent_stop skipped: %s", exc)


def _handle_slash_joust(raw_args: str, **kwargs: Any) -> str:
    """In-session /joust slash command handler."""
    parts = (raw_args or "").strip().split()
    sub = parts[0].lower() if parts else "status"

    try:
        if sub == "demo":
            bout = worktree_engine.run_demo_bout()
            lines = [
                f"⚔️ **Joust Demo Tournament Completed** (`{bout['bout_id']}`)",
                f"**Title:** {bout['title']}",
                f"**Winner:** `{bout['winner_id']}`",
                "",
                "| Rank | Contender | Status | Score | Latency | Diff (+/-) |",
                "|---|---|---|---|---|---|",
            ]
            for c in bout["contenders"]:
                badge = "🏆 PASS" if c["is_winner"] else ("✅ PASS" if c["tests_passed"] else "❌ FAIL")
                lines.append(
                    f"| #{c['rank']} | `{c['contender_id']}` | {badge} | **{c['score']:.1f}** | "
                    f"{c['duration_ms']:.1f}ms | +{c['insertions']}/-{c['deletions']} |"
                )
            lines.append("")
            lines.append(bout["directive"])
            return "\n".join(lines)

        if sub == "judge" and len(parts) >= 2:
            bout = worktree_engine.evaluate_bout(parts[1])
            return f"Evaluated `{bout['bout_id']}` — Winner: `{bout['winner_id'] or 'none'}`\n\n{bout['directive']}"

        if sub == "merge" and len(parts) >= 2:
            cid = parts[2] if len(parts) >= 3 else None
            res = worktree_engine.merge_winner(parts[1], contender_id=cid, action="apply")
            return f"Merged `{res['merged_contender_id']}` into `{res['repo_path']}` (backup: `{res['backup_patch_path']}`)."

        if sub == "rollback" and len(parts) >= 2:
            worktree_engine.merge_winner(parts[1], action="rollback")
            return f"Rolled back merge for `{parts[1]}`."

        summary = worktree_engine.list_bouts(limit=10)
        bouts = summary.get("bouts") or []
        if not bouts:
            return "No Joust bouts recorded yet. Run `/joust demo` or ask Hermes to launch a `joust_launch` tournament."
        lines = [
            f"⚔️ **Joust Arena** — {summary['total_bouts']} bout(s), {summary['merged_bouts']} merged:",
            "",
        ]
        for b in bouts[:5]:
            lines.append(
                f"- `{b['bout_id']}` **{b['title']}** — status: `{b['status']}`, "
                f"winner: `{b['winner_id'] or 'pending'}` ({len(b['contenders'])} contenders)"
            )
        lines.append("\nSubcommands: `/joust status`, `/joust demo`, `/joust judge <bout_id>`, `/joust merge <bout_id> [contender]`, `/joust rollback <bout_id>`")
        return "\n".join(lines)
    except Exception as exc:
        return f"Joust command error: {exc}"


def _setup_cli_parser(parser: Any) -> None:
    sub = parser.add_subparsers(dest="joust_cmd")
    sub.add_parser("list", help="List recent Joust bouts and contender leaderboards")
    sub.add_parser("demo", help="Run a self-contained 3-contender Joust tournament in a demo repo")
    judge_p = sub.add_parser("judge", help="Evaluate contenders in a bout")
    judge_p.add_argument("bout_id", help="Bout ID to evaluate")
    merge_p = sub.add_parser("merge", help="Merge winning contender into base repository")
    merge_p.add_argument("bout_id", help="Bout ID")
    merge_p.add_argument("--contender", default="", help="Optional contender ID override")
    merge_p.add_argument("--rollback", action="store_true", help="Rollback a previously merged bout")


def _handle_cli_command(args: Any) -> None:
    cmd = getattr(args, "joust_cmd", None) or "list"
    if cmd == "demo":
        bout = worktree_engine.run_demo_bout()
        print(json.dumps(bout, indent=2))
    elif cmd == "judge":
        bout = worktree_engine.evaluate_bout(args.bout_id)
        print(json.dumps(bout, indent=2))
    elif cmd == "merge":
        action = "rollback" if getattr(args, "rollback", False) else "apply"
        res = worktree_engine.merge_winner(args.bout_id, contender_id=getattr(args, "contender", "") or None, action=action)
        print(json.dumps(res, indent=2))
    else:
        print(json.dumps(worktree_engine.list_bouts(limit=20), indent=2))


def register(ctx: Any) -> None:
    """Register Joust tools, middleware, hooks, slash commands, CLI subcommand, and bundled skill."""
    tools.set_context(ctx)

    ctx.register_tool(
        name="joust_launch",
        toolset="joust",
        schema=schemas.JOUST_LAUNCH,
        handler=tools.joust_launch,
        description="Launch parallel Git-worktree contenders for a coding task",
        emoji="⚔️",
    )
    ctx.register_tool(
        name="joust_evaluate",
        toolset="joust",
        schema=schemas.JOUST_EVALUATE,
        handler=tools.joust_evaluate,
        description="Run benchmark and verification suite across contender worktrees",
        emoji="🏁",
    )
    ctx.register_tool(
        name="joust_diff",
        toolset="joust",
        schema=schemas.JOUST_DIFF,
        handler=tools.joust_diff,
        description="Inspect unified diffs across contender worktrees",
        emoji="🔍",
    )
    ctx.register_tool(
        name="joust_steer",
        toolset="joust",
        schema=schemas.JOUST_STEER,
        handler=tools.joust_steer,
        description="Steer a contender mid-flight or update files in its worktree",
        emoji="🧭",
    )
    ctx.register_tool(
        name="joust_merge",
        toolset="joust",
        schema=schemas.JOUST_MERGE,
        handler=tools.joust_merge,
        description="Merge winning contender diff into main repo with rollback backup",
        emoji="🏆",
    )
    ctx.register_tool(
        name="joust_status",
        toolset="joust",
        schema=schemas.JOUST_STATUS,
        handler=tools.joust_status,
        description="List Joust bouts, leaderboards, or run a live 3-way demo tournament",
        emoji="📊",
    )

    ctx.register_middleware("tool_request", _on_tool_request)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("subagent_stop", _on_subagent_stop)

    ctx.register_command(
        "joust",
        _handle_slash_joust,
        description="Manage Joust parallel Git-worktree tournaments (/joust status|demo|judge|merge|rollback)",
        args_hint="[status|demo|judge|merge|rollback]",
    )
    ctx.register_cli_command(
        "joust",
        "Parallel Git-worktree subagent arena and benchmark judge",
        _setup_cli_parser,
        _handle_cli_command,
    )

    skill_md = _PLUGIN_DIR / "skills" / "joust-arena" / "SKILL.md"
    if skill_md.is_file():
        ctx.register_skill(
            "joust-arena",
            skill_md,
            description="Launch parallel Git-worktree coding tournaments and benchmark competing implementations",
        )
