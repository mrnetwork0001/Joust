"""Core Git-worktree arena engine, benchmark judge, and middleware path isolator for Joust."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import statistics
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_ID = "joust"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_LOCK = threading.RLock()

# In-memory fast lookup of active subagent session_id / task_id -> worktree_path
_ACTIVE_SESSION_WORKTREES: Dict[str, str] = {}
_ACTIVE_SESSION_CONTENDERS: Dict[str, Tuple[str, str]] = {}  # session_id -> (bout_id, contender_id)
_DB_CACHE: Dict[str, sqlite3.Connection] = {}


def get_data_dir() -> Path:
    """Resolve Joust's profile-scoped persistent data directory via Hermes public API."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        root = hermes_home / "plugin-data" / PLUGIN_ID
        root.mkdir(parents=True, exist_ok=True)
        return root


def get_db() -> sqlite3.Connection:
    """Open Joust's WAL-mode SQLite database via Hermes public API (cached per profile path)."""
    db_file = str((get_data_dir() / "joust.db").resolve())
    with _LOCK:
        cached = _DB_CACHE.get(db_file)
        if cached is not None:
            return cached
        try:
            from plugins.plugin_storage import plugin_db

            conn = plugin_db(PLUGIN_ID, "joust.db")
        except Exception:
            conn = sqlite3.connect(db_file, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        _DB_CACHE[db_file] = conn
        return conn


def close_cached_dbs() -> None:
    """Close any cached SQLite connections (used in test teardown)."""
    with _LOCK:
        for conn in _DB_CACHE.values():
            try:
                conn.close()
            except Exception:
                pass
        _DB_CACHE.clear()


_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)[-_](?:live|test|proj|ant|or)?[-_]?[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|password|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)


def _redact(text: str) -> str:
    """Best-effort secret scrubbing for captured benchmark output (stdlib only; catalog rule 9)."""
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def notify_desktop(event: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Broadcast a live plugin event to connected Hermes Desktop windows."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, event, payload or {})
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str, fallback: str = "contender") -> str:
    cleaned = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    return cleaned[:48] or fallback


def _init_schema(conn: sqlite3.Connection) -> None:
    with _LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bouts (
                bout_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                test_command TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                winner_id TEXT DEFAULT '',
                merged_contender_id TEXT DEFAULT '',
                backup_patch_path TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contenders (
                contender_id TEXT NOT NULL,
                bout_id TEXT NOT NULL,
                name TEXT NOT NULL,
                strategy TEXT NOT NULL,
                model TEXT DEFAULT '',
                worktree_path TEXT NOT NULL,
                subagent_session_id TEXT DEFAULT '',
                subagent_handle_json TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'ready',
                tests_passed INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER DEFAULT -1,
                duration_ms REAL NOT NULL DEFAULT 0.0,
                files_changed INTEGER NOT NULL DEFAULT 0,
                insertions INTEGER NOT NULL DEFAULT 0,
                deletions INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0.0,
                score_breakdown_json TEXT NOT NULL DEFAULT '{}',
                test_output TEXT DEFAULT '',
                steering_notes_json TEXT NOT NULL DEFAULT '[]',
                tool_calls_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bout_id, contender_id),
                FOREIGN KEY (bout_id) REFERENCES bouts(bout_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS bout_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bout_id TEXT NOT NULL,
                contender_id TEXT DEFAULT '',
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(contenders)").fetchall()}
        for col, decl in (
            ("crossfire_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("crossfire_survived", "INTEGER NOT NULL DEFAULT 0"),
            ("crossfire_total", "INTEGER NOT NULL DEFAULT 0"),
            ("runs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("flaky", "INTEGER NOT NULL DEFAULT 0"),
            ("branch_ref", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE contenders ADD COLUMN {col} {decl}")
        conn.commit()


def _run_git(args: List[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    if os.environ.get("USERPROFILE"):
        env["USERPROFILE"] = os.environ["USERPROFILE"]
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def resolve_git_repo(repo_path: Optional[str] = None) -> Tuple[Path, str]:
    """Validate and resolve the Git repository root and current HEAD commit SHA."""
    candidate = Path(repo_path or os.getcwd()).expanduser().resolve()
    if not candidate.exists():
        raise ValueError(f"Repository path does not exist: {candidate}")
    top = _run_git(["rev-parse", "--show-toplevel"], cwd=candidate)
    if top.returncode != 0 or not top.stdout.strip():
        raise ValueError(f"Not a Git repository: {candidate} ({top.stderr.strip()})")
    root = Path(top.stdout.strip()).resolve()
    head = _run_git(["rev-parse", "HEAD"], cwd=root)
    if head.returncode != 0 or not head.stdout.strip():
        raise ValueError(f"Repository at {root} has no commits yet (HEAD cannot be resolved).")
    return root, head.stdout.strip()


_PENDING_SUBAGENT_SESSIONS: Dict[str, str] = {}  # child_subagent_id / marker -> child_session_id
_ACTIVE_SESSION_REPOS: Dict[str, str] = {}  # session_id -> main repo root (for path redirection)
_UNBOUND_SESSIONS: set = set()  # negative cache: sessions known not to be Joust contenders
_PENDING_VIOLATIONS: Dict[str, str] = {}  # tool_call_id (or session_id) -> escape message for pre_tool_call
_MARKER_RE = re.compile(r"joust:bout=(bout-[0-9a-f]{8});contender=([a-z0-9][a-z0-9_\-]*)")
_UNBOUND_CACHE_MAX = 4096


def contender_marker(bout_id: str, contender_id: str) -> str:
    """Stable marker embedded in a contender's goal so the subagent_start hook can bind its session."""
    return f"joust:bout={bout_id};contender={contender_id}"


def parse_contender_marker(text: Optional[str]) -> Optional[Tuple[str, str]]:
    m = _MARKER_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def bind_session_worktree(
    session_id: str, bout_id: str, contender_id: str, worktree_path: str, repo_path: str = ""
) -> None:
    """Register a child subagent session/task ID so middleware scopes its tools to its worktree."""
    if not session_id:
        return
    with _LOCK:
        _ACTIVE_SESSION_WORKTREES[session_id] = str(Path(worktree_path).resolve())
        _ACTIVE_SESSION_CONTENDERS[session_id] = (bout_id, contender_id)
        if repo_path:
            _ACTIVE_SESSION_REPOS[session_id] = str(Path(repo_path).resolve())
        _UNBOUND_SESSIONS.discard(session_id)


def bind_contender_subagent(
    bout_id: str, contender_id: str, child_session_id: str, child_subagent_id: str = ""
) -> bool:
    """Bind a live child session to its contender worktree and persist the identity in the DB."""
    if not child_session_id:
        return False
    conn = get_db()
    with _LOCK:
        row = conn.execute(
            "SELECT c.worktree_path, b.repo_path FROM contenders c JOIN bouts b ON b.bout_id = c.bout_id "
            "WHERE c.bout_id = ? AND c.contender_id = ?",
            (bout_id, contender_id),
        ).fetchone()
        if row is None:
            return False
        bind_session_worktree(child_session_id, bout_id, contender_id, row["worktree_path"], row["repo_path"])
        conn.execute(
            "UPDATE contenders SET subagent_session_id = ?, status = 'running', updated_at = ? "
            "WHERE bout_id = ? AND contender_id = ?",
            (child_session_id, _now_iso(), bout_id, contender_id),
        )
        conn.commit()
    record_event(bout_id, "contender.subagent_started", f"Bound child session {child_session_id} ({child_subagent_id or 'n/a'}).", contender_id)
    return True


def on_subagent_started(child_session_id: str, child_subagent_id: str = "", child_goal: str = "") -> bool:
    """subagent_start hook entry.

    Hermes fires this hook *inside* ``service.launch()``. Since ``create_bout`` inserts the contender rows
    before launching, the marker path normally binds directly; otherwise the session is parked under both
    the child's subagent id and the goal marker and ``create_bout`` pops it when ``launch()`` returns.
    """
    if not child_session_id:
        return False
    marker = parse_contender_marker(child_goal)
    if marker and bind_contender_subagent(marker[0], marker[1], child_session_id, child_subagent_id):
        return True
    with _LOCK:
        if child_subagent_id:
            _PENDING_SUBAGENT_SESSIONS[child_subagent_id] = child_session_id
        if marker:
            _PENDING_SUBAGENT_SESSIONS[contender_marker(*marker)] = child_session_id
    return False


def _pop_pending_session(child_subagent_id: str, bout_id: str = "", contender_id: str = "") -> str:
    with _LOCK:
        by_id = _PENDING_SUBAGENT_SESSIONS.pop(child_subagent_id, "") if child_subagent_id else ""
        by_marker = _PENDING_SUBAGENT_SESSIONS.pop(contender_marker(bout_id, contender_id), "") if bout_id else ""
        return by_id or by_marker


def _db_lookup_session(session_id: str) -> Optional[Tuple[str, str, str, str]]:
    """Fallback for bindings lost to a plugin-host restart: (bout_id, contender_id, worktree_path, repo_path).

    Cheap on the hot path: skipped entirely until Joust has ever created its database, and negative
    results are cached so ordinary (non-contender) sessions cost one lookup at most.
    """
    if not session_id or session_id in _UNBOUND_SESSIONS:
        return None
    if not (get_data_dir() / "joust.db").exists():
        return None
    try:
        row = get_db().execute(
            "SELECT c.bout_id, c.contender_id, c.worktree_path, b.repo_path FROM contenders c "
            "JOIN bouts b ON b.bout_id = c.bout_id WHERE c.subagent_session_id = ? "
            "ORDER BY c.updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    except Exception:
        return None
    if row is None or not Path(row["worktree_path"]).is_dir():
        with _LOCK:
            if len(_UNBOUND_SESSIONS) > _UNBOUND_CACHE_MAX:
                _UNBOUND_SESSIONS.clear()
            _UNBOUND_SESSIONS.add(session_id)
        return None
    bind_session_worktree(session_id, row["bout_id"], row["contender_id"], row["worktree_path"], row["repo_path"])
    return row["bout_id"], row["contender_id"], row["worktree_path"], row["repo_path"]


def get_session_binding(session_id: Optional[str], task_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return {bout_id, contender_id, worktree_path, repo_path} for a bound child session, else None."""
    for key in (session_id, task_id):
        if not key:
            continue
        with _LOCK:
            if key in _ACTIVE_SESSION_WORKTREES:
                bout_id, cid = _ACTIVE_SESSION_CONTENDERS.get(key, ("", ""))
                return {
                    "bout_id": bout_id,
                    "contender_id": cid,
                    "worktree_path": _ACTIVE_SESSION_WORKTREES[key],
                    "repo_path": _ACTIVE_SESSION_REPOS.get(key, ""),
                }
        found = _db_lookup_session(key)
        if found:
            return {"bout_id": found[0], "contender_id": found[1], "worktree_path": found[2], "repo_path": found[3]}
    return None


def get_session_worktree(session_id: Optional[str], task_id: Optional[str] = None) -> Optional[str]:
    """Return the bound worktree path for a session_id or task_id, if any."""
    b = get_session_binding(session_id, task_id)
    return b["worktree_path"] if b else None


def get_session_contender(session_id: Optional[str], task_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
    b = get_session_binding(session_id, task_id)
    return (b["bout_id"], b["contender_id"]) if b else None


def record_violation(key: str, message: str) -> None:
    """Remember an escape attempt so the pre_tool_call hook can veto the call (middleware already clamped it)."""
    if not key:
        return
    with _LOCK:
        if len(_PENDING_VIOLATIONS) > 512:
            _PENDING_VIOLATIONS.clear()
        _PENDING_VIOLATIONS[key] = message


def pop_violation(*keys: Optional[str]) -> Optional[str]:
    with _LOCK:
        for k in keys:
            if k and k in _PENDING_VIOLATIONS:
                return _PENDING_VIOLATIONS.pop(k)
    return None


def _inside(p: Path, root: Path) -> bool:
    """Component-aware containment (``/wt/alpha-2`` is NOT inside ``/wt/alpha``)."""
    return p == root or root in p.parents


def _jail_path_inside_worktree(raw_path: str, worktree_root: Path, main_repo_root: Optional[Path] = None) -> str:
    """Resolve a file path inside worktree_root.

    Relative paths resolve against the worktree; absolute paths inside the main repository are mapped
    to the same relative location inside the worktree; anything else raises ValueError (the caller
    clamps and records a violation for the pre_tool_call veto).
    """
    worktree_resolved = worktree_root.resolve()
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        target = (worktree_resolved / p).resolve()
    else:
        resolved = p.resolve()
        if _inside(resolved, worktree_resolved):
            return str(resolved)
        if main_repo_root is not None:
            main_resolved = main_repo_root.resolve()
            if _inside(resolved, main_resolved):
                target = (worktree_resolved / resolved.relative_to(main_resolved)).resolve()
            else:
                target = resolved
        else:
            target = resolved
    if not _inside(target, worktree_resolved):
        raise ValueError(f"Path {raw_path!r} escapes isolated Joust worktree {worktree_resolved}")
    return str(target)


_PATH_KEYS = ("path", "file_path", "filepath", "target_file", "directory", "dir_path", "cwd", "workdir")
_DIR_KEYS = {"directory", "dir_path", "cwd", "workdir"}
_TERMINAL_TOOLS = {"terminal", "bash", "execute_command", "run_command"}
_CWD_DEFAULT_TOOLS = {"search_files", "list_dir", "list_directory", "glob"}
_V4A_HEADER_RE = re.compile(r"^(\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*)(.+?)\s*$", re.M)
_V4A_MOVE_RE = re.compile(r"^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+?)\s*$", re.M)


def rewrite_tool_args_for_worktree(
    tool_name: str,
    args: Dict[str, Any],
    worktree_path: str,
    main_repo_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
    """Rewrite tool arguments so file and terminal operations stay inside the contender worktree.

    Returns (args, reason, violations). ``violations`` lists human-readable escape attempts that were
    clamped; the pre_tool_call hook turns them into a veto so the model learns the boundary.
    """
    if not isinstance(args, dict) or not worktree_path:
        return args, None, []

    wt_root = Path(worktree_path).resolve()
    repo_root = Path(main_repo_path).resolve() if main_repo_path else None
    updated = dict(args)
    rewritten_fields: List[str] = []
    violations: List[str] = []

    def jail(value: str, key: str) -> str:
        try:
            return _jail_path_inside_worktree(value, wt_root, repo_root)
        except ValueError:
            violations.append(f"{key}={value!r}")
            if key in _DIR_KEYS:
                return str(wt_root)
            # keep a relative tail when the input was relative, otherwise fall back to the basename
            tail = Path(value).expanduser()
            if not tail.is_absolute() and ".." not in tail.parts:
                return str(wt_root / tail)
            return str(wt_root / Path(value).name)

    for path_key in _PATH_KEYS:
        val = updated.get(path_key)
        if isinstance(val, str) and val.strip():
            jailed = jail(val, path_key)
            if jailed != val:
                updated[path_key] = jailed
                rewritten_fields.append(path_key)

    if tool_name in _TERMINAL_TOOLS and "workdir" not in updated and "cwd" not in updated:
        updated["workdir"] = str(wt_root)
        rewritten_fields.append("workdir")

    if tool_name in _CWD_DEFAULT_TOOLS:
        cur = updated.get("path")
        if not isinstance(cur, str) or not cur.strip() or cur.strip() == ".":
            updated["path"] = str(wt_root)
            rewritten_fields.append("path")

    if tool_name == "patch" and isinstance(updated.get("patch"), str) and str(updated.get("mode", "patch")) == "patch":
        body = updated["patch"]

        def _hdr(m: "re.Match[str]") -> str:
            return m.group(1) + jail(m.group(2), "patch")

        def _mv(m: "re.Match[str]") -> str:
            return m.group(1) + jail(m.group(2), "patch") + " -> " + jail(m.group(3), "patch")

        new_body = _V4A_MOVE_RE.sub(_mv, _V4A_HEADER_RE.sub(_hdr, body))
        if new_body != body:
            updated["patch"] = new_body
            rewritten_fields.append("patch")

    if not rewritten_fields:
        return args, None, violations
    return updated, f"Joust isolated {tool_name} ({', '.join(dict.fromkeys(rewritten_fields))}) -> {wt_root.name}", violations


def record_event(bout_id: str, event_type: str, detail: str, contender_id: str = "") -> None:
    conn = get_db()
    with _LOCK:
        conn.execute(
            "INSERT INTO bout_events (bout_id, contender_id, event_type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (bout_id, contender_id, event_type, detail, _now_iso()),
        )
        conn.commit()


def create_bout(
    title: str,
    goal: str,
    contenders: List[Dict[str, Any]],
    repo_path: Optional[str] = None,
    test_command: str = "python3 -m unittest",
    ctx: Any = None,
    spawn_subagents: bool = True,
) -> Dict[str, Any]:
    """Create a Joust bout, provision isolated Git worktrees for each contender, and optionally launch subagents."""
    if not contenders or len(contenders) < 2:
        raise ValueError("A Joust bout requires at least 2 contenders.")
    if len(contenders) > 4:
        raise ValueError("A Joust bout supports at most 4 contenders per run.")

    repo_root, base_commit = resolve_git_repo(repo_path)
    bout_id = f"bout-{uuid.uuid4().hex[:8]}"
    worktrees_base = get_data_dir() / "worktrees" / bout_id
    worktrees_base.mkdir(parents=True, exist_ok=True)

    # Capture any uncommitted tracked diff in the main repo so worktrees mirror the exact working state
    dirty_diff_proc = _run_git(["diff", "HEAD"], cwd=repo_root)
    dirty_patch = dirty_diff_proc.stdout if dirty_diff_proc.returncode == 0 else ""

    now = _now_iso()
    conn = get_db()
    created_contenders: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    with _LOCK:
        conn.execute(
            """
            INSERT INTO bouts (
                bout_id, title, goal, repo_path, base_commit, test_command, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (bout_id, title.strip(), goal.strip(), str(repo_root), base_commit, test_command.strip(), now, now),
        )

        for idx, spec in enumerate(contenders, start=1):
            raw_name = str(spec.get("name") or f"contender-{idx}").strip()
            cid = _slugify(raw_name, f"contender-{idx}")
            if cid in seen_ids:
                cid = f"{cid}-{idx}"
            seen_ids.add(cid)

            strategy = str(spec.get("strategy") or "").strip()
            model = str(spec.get("model") or "").strip()
            wt_path = (worktrees_base / cid).resolve()

            add_res = _run_git(["worktree", "add", "--detach", str(wt_path), base_commit], cwd=repo_root)
            if add_res.returncode != 0:
                # Fallback: clean copy of repo files if git worktree is locked
                shutil.copytree(
                    repo_root,
                    wt_path,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules"),
                )
                _run_git(["init"], cwd=wt_path)
                _run_git(["add", "-A"], cwd=wt_path)
                _run_git(
                    ["-c", "user.name=Joust", "-c", "user.email=joust@local", "commit", "-m", "base"],
                    cwd=wt_path,
                )
            elif dirty_patch.strip():
                patch_file = wt_path / ".joust_dirty.patch"
                patch_file.write_text(dirty_patch, encoding="utf-8")
                _run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=wt_path)
                patch_file.unlink(missing_ok=True)

            subagent_session_id = ""
            subagent_handle_dict: Dict[str, Any] = {}
            status = "ready"

            if spawn_subagents and ctx is not None:
                try:
                    from agent.subagent_lifecycle import SubagentLaunchRequest

                    service = getattr(ctx, "subagent_lifecycle", None)
                    if service is not None:
                        sub_goal = (
                            f"[Joust Contender: {raw_name}]\n"
                            f"Isolated Worktree Directory: {wt_path}\n"
                            f"Problem Goal: {goal}\n"
                            f"Assigned Strategy: {strategy}\n"
                            f"Verification Command: {test_command}\n"
                            f"IMPORTANT: Perform all file reads, edits, and test executions strictly inside {wt_path}."
                        )
                        req_kwargs: Dict[str, Any] = {
                            "goal": sub_goal,
                            "context": f"Joust bout {bout_id} contender {cid} in isolated worktree {wt_path}",
                            "role": "leaf",
                            "correlation_id": f"{bout_id}:{cid}",
                            "metadata": {"bout_id": bout_id, "contender_id": cid, "worktree_path": str(wt_path)},
                        }
                        if model:
                            req_kwargs["model"] = model
                        handle = service.launch(SubagentLaunchRequest(**req_kwargs))
                        subagent_handle_dict = handle.to_dict() if hasattr(handle, "to_dict") else {}
                        subagent_session_id = str(
                            subagent_handle_dict.get("session_id")
                            or subagent_handle_dict.get("child_session_id")
                            or getattr(handle, "session_id", "")
                            or ""
                        )
                        if subagent_session_id:
                            bind_session_worktree(subagent_session_id, bout_id, cid, str(wt_path))
                        status = "running"
                except Exception:
                    # Outside an active parent turn (e.g., CLI/Dashboard/direct tool test), worktree is ready
                    status = "ready"

            conn.execute(
                """
                INSERT INTO contenders (
                    contender_id, bout_id, name, strategy, model, worktree_path,
                    subagent_session_id, subagent_handle_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    bout_id,
                    raw_name,
                    strategy,
                    model,
                    str(wt_path),
                    subagent_session_id,
                    json.dumps(subagent_handle_dict),
                    status,
                    now,
                ),
            )
            created_contenders.append(
                {
                    "contender_id": cid,
                    "name": raw_name,
                    "strategy": strategy,
                    "model": model,
                    "worktree_path": str(wt_path),
                    "status": status,
                    "subagent_session_id": subagent_session_id,
                }
            )

        conn.commit()

    record_event(bout_id, "bout.launched", f"Launched bout '{title}' with {len(created_contenders)} contenders.")
    bout_snapshot = get_bout_details(bout_id)
    notify_desktop("bout.updated", {"bout_id": bout_id, "bout": bout_snapshot})
    return bout_snapshot


def _compute_worktree_diff_stats(wt_path: Path) -> Tuple[int, int, int, str]:
    """Return (files_changed, insertions, deletions, unified_diff) for a contender worktree."""
    if not wt_path.exists():
        return 0, 0, 0, ""

    # Stage untracked files with intent-to-add so git diff includes new files created by the contender
    _run_git(["add", "-N", "."], cwd=wt_path)
    diff_proc = _run_git(["diff", "HEAD"], cwd=wt_path)
    diff_text = diff_proc.stdout if diff_proc.returncode == 0 else ""

    files_changed = 0
    insertions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files_changed += 1
        elif line.startswith("+") and not line.startswith("+++"):
            insertions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    return files_changed, insertions, deletions, diff_text


def _run_verification_command(command: str, wt_path: Path, timeout_seconds: int = 60) -> Tuple[int, float, str]:
    """Run the verification/benchmark command inside wt_path and return (exit_code, duration_ms, output)."""
    if not wt_path.exists():
        return 1, 0.0, f"Worktree directory missing: {wt_path}"

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PYTHONPATH": str(wt_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI": "true",
    }

    # Use argv execution when no shell metacharacters are present; otherwise use /bin/sh -c
    has_shell_meta = any(tok in command for tok in ("&&", "||", "|", ";", ">", "<", "$"))
    if has_shell_meta:
        argv = ["/bin/sh", "-c", command]
    else:
        argv = shlex.split(command)

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(wt_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5, int(timeout_seconds)),
            check=False,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, duration_ms, _redact(combined[:8000])
    except subprocess.TimeoutExpired:
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return 124, duration_ms, f"Verification timed out after {timeout_seconds}s"
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return 1, duration_ms, f"Execution error: {exc}"


def evaluate_bout(
    bout_id: str,
    contender_id: Optional[str] = None,
    test_command_override: Optional[str] = None,
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """Execute the verification & benchmark suite across contenders, compute Joust Scores, and rank the winner."""
    conn = get_db()
    bout_row = conn.execute("SELECT * FROM bouts WHERE bout_id = ?", (bout_id,)).fetchone()
    if bout_row is None:
        raise ValueError(f"Unknown bout_id: {bout_id!r}")

    test_cmd = (test_command_override or bout_row["test_command"] or "python3 -m unittest").strip()
    if test_command_override and test_command_override.strip() != bout_row["test_command"]:
        with _LOCK:
            conn.execute("UPDATE bouts SET test_command = ?, updated_at = ? WHERE bout_id = ?", (test_cmd, _now_iso(), bout_id))
            conn.commit()

    contender_rows = conn.execute(
        "SELECT * FROM contenders WHERE bout_id = ? ORDER BY rowid ASC", (bout_id,)
    ).fetchall()
    if not contender_rows:
        raise ValueError(f"Bout {bout_id!r} has no contenders.")

    raw_results: List[Dict[str, Any]] = []
    for row in contender_rows:
        cid = row["contender_id"]
        if contender_id and cid != contender_id and row["name"] != contender_id:
            # Keep existing metrics for non-selected contenders so relative scoring still works
            raw_results.append(
                {
                    "contender_id": cid,
                    "exit_code": int(row["exit_code"]),
                    "tests_passed": bool(row["tests_passed"]),
                    "duration_ms": float(row["duration_ms"]),
                    "files_changed": int(row["files_changed"]),
                    "insertions": int(row["insertions"]),
                    "deletions": int(row["deletions"]),
                    "test_output": str(row["test_output"] or ""),
                    "evaluated_now": False,
                }
            )
            continue

        wt_path = Path(row["worktree_path"])
        files_changed, insertions, deletions, _ = _compute_worktree_diff_stats(wt_path)
        exit_code, duration_ms, output = _run_verification_command(test_cmd, wt_path, timeout_seconds=timeout_seconds)
        passed = exit_code == 0

        # Also check if benchmark output emitted a custom JOUST_BENCHMARK_MS=<float> metric
        bench_match = re.search(r"JOUST_BENCHMARK_MS=([0-9.]+)", output)
        if bench_match:
            try:
                duration_ms = round(float(bench_match.group(1)), 2)
            except ValueError:
                pass

        raw_results.append(
            {
                "contender_id": cid,
                "exit_code": exit_code,
                "tests_passed": passed,
                "duration_ms": duration_ms,
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
                "test_output": output,
                "evaluated_now": True,
            }
        )

    # Compute relative Joust Composite Score (0 - 100):
    # - Correctness Gate: 60 points (60 if tests_passed else 0)
    # - Relative Speed: 25 points (25 * (fastest_passing_ms / contender_ms))
    # - Diff Cleanliness & Minimalism: 15 points (rewards surgical edits; penalizes zero-diff if a change was expected)
    passing_durations = [max(0.5, r["duration_ms"]) for r in raw_results if r["tests_passed"] and r["duration_ms"] > 0]
    fastest_ms = min(passing_durations) if passing_durations else 1.0

    now = _now_iso()
    best_cid = ""
    best_score = -1.0

    with _LOCK:
        for r in raw_results:
            cid = r["contender_id"]
            passed = r["tests_passed"]
            dur = max(0.5, r["duration_ms"])
            loc_delta = r["insertions"] + r["deletions"]

            correctness_pts = 60.0 if passed else 0.0
            if passed:
                speed_pts = round(25.0 * min(1.0, fastest_ms / dur), 2)
            else:
                speed_pts = round(5.0 * min(1.0, fastest_ms / max(dur, fastest_ms)), 2)

            if loc_delta == 0:
                # Unmodified worktree gets partial minimalism credit only
                minimalism_pts = 5.0
            else:
                minimalism_pts = round(15.0 / (1.0 + max(0, loc_delta - 12) / 90.0), 2)

            total_score = round(correctness_pts + speed_pts + minimalism_pts, 2)
            breakdown = {
                "correctness": correctness_pts,
                "speed": speed_pts,
                "minimalism": minimalism_pts,
                "total": total_score,
            }

            if passed and total_score > best_score:
                best_score = total_score
                best_cid = cid

            status = "passed" if passed else "failed"
            conn.execute(
                """
                UPDATE contenders
                SET status = ?, tests_passed = ?, exit_code = ?, duration_ms = ?,
                    files_changed = ?, insertions = ?, deletions = ?,
                    score = ?, score_breakdown_json = ?, test_output = ?, updated_at = ?
                WHERE bout_id = ? AND contender_id = ?
                """,
                (
                    status,
                    1 if passed else 0,
                    r["exit_code"],
                    r["duration_ms"],
                    r["files_changed"],
                    r["insertions"],
                    r["deletions"],
                    total_score,
                    json.dumps(breakdown),
                    r["test_output"],
                    now,
                    bout_id,
                    cid,
                ),
            )

        bout_status = "evaluated" if best_cid else "failed_all"
        conn.execute(
            "UPDATE bouts SET status = ?, winner_id = ?, updated_at = ? WHERE bout_id = ?",
            (bout_status, best_cid, now, bout_id),
        )
        conn.commit()

    record_event(
        bout_id,
        "bout.evaluated",
        f"Evaluated contenders with `{test_cmd}`. Winner: {best_cid or 'none'} (score: {max(0.0, best_score):.1f}).",
        contender_id=best_cid,
    )
    details = get_bout_details(bout_id)
    notify_desktop("bout.updated", {"bout_id": bout_id, "bout": details})
    return details


def steer_contender(
    bout_id: str,
    contender_id: str,
    guidance: str,
    write_files: Optional[Dict[str, str]] = None,
    ctx: Any = None,
) -> Dict[str, Any]:
    """Steer a contender mid-flight (via ctx.subagent_lifecycle.steer and/or direct worktree file updates)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM contenders WHERE bout_id = ? AND (contender_id = ? OR name = ?)",
        (bout_id, contender_id, contender_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Contender {contender_id!r} not found in bout {bout_id!r}.")

    cid = row["contender_id"]
    wt_path = Path(row["worktree_path"]).resolve()
    notes = json.loads(row["steering_notes_json"] or "[]")
    notes.append({"guidance": guidance, "timestamp": _now_iso()})

    written_paths: List[str] = []
    if isinstance(write_files, dict):
        for rel_path, content in write_files.items():
            jailed = Path(_jail_path_inside_worktree(rel_path, wt_path))
            jailed.parent.mkdir(parents=True, exist_ok=True)
            jailed.write_text(str(content), encoding="utf-8")
            written_paths.append(str(jailed.relative_to(wt_path)))

    steered_live = False
    handle_raw = json.loads(row["subagent_handle_json"] or "{}")
    if handle_raw and ctx is not None:
        try:
            service = getattr(ctx, "subagent_lifecycle", None)
            if service is not None:
                steered_live = bool(service.steer(handle_raw, guidance))
        except Exception:
            steered_live = False

    files_changed, insertions, deletions, _ = _compute_worktree_diff_stats(wt_path)
    with _LOCK:
        conn.execute(
            """
            UPDATE contenders
            SET steering_notes_json = ?, files_changed = ?, insertions = ?, deletions = ?, updated_at = ?
            WHERE bout_id = ? AND contender_id = ?
            """,
            (json.dumps(notes), files_changed, insertions, deletions, _now_iso(), bout_id, cid),
        )
        conn.commit()

    record_event(
        bout_id,
        "contender.steered",
        f"Steered '{cid}': {guidance[:120]}" + (f" (updated {len(written_paths)} file(s))" if written_paths else ""),
        contender_id=cid,
    )
    details = get_bout_details(bout_id)
    notify_desktop("bout.updated", {"bout_id": bout_id, "bout": details})
    return {
        "ok": True,
        "bout_id": bout_id,
        "contender_id": cid,
        "steered_live_subagent": steered_live,
        "written_files": written_paths,
        "bout": details,
    }


def get_bout_diffs(bout_id: str, contender_id: Optional[str] = None, max_chars: int = 8000) -> Dict[str, Any]:
    """Return unified diffs and file change lists for each contender in a bout."""
    conn = get_db()
    bout_row = conn.execute("SELECT * FROM bouts WHERE bout_id = ?", (bout_id,)).fetchone()
    if bout_row is None:
        raise ValueError(f"Unknown bout_id: {bout_id!r}")

    rows = conn.execute("SELECT * FROM contenders WHERE bout_id = ? ORDER BY score DESC, rowid ASC", (bout_id,)).fetchall()
    diffs: Dict[str, Any] = {}
    for row in rows:
        cid = row["contender_id"]
        if contender_id and cid != contender_id and row["name"] != contender_id:
            continue
        wt_path = Path(row["worktree_path"])
        files_changed, insertions, deletions, diff_text = _compute_worktree_diff_stats(wt_path)
        truncated = len(diff_text) > max_chars
        diffs[cid] = {
            "contender_id": cid,
            "name": row["name"],
            "status": row["status"],
            "score": float(row["score"]),
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
            "diff": diff_text[:max_chars] + ("\n... [diff truncated]" if truncated else ""),
            "truncated": truncated,
        }
    return {"bout_id": bout_id, "title": bout_row["title"], "diffs": diffs}


def merge_winner(
    bout_id: str,
    contender_id: Optional[str] = None,
    action: str = "apply",
    keep_backup: bool = True,
) -> Dict[str, Any]:
    """Merge the winning contender's patch into the main repo, dry-run, rollback, or cleanup worktrees."""
    conn = get_db()
    bout_row = conn.execute("SELECT * FROM bouts WHERE bout_id = ?", (bout_id,)).fetchone()
    if bout_row is None:
        raise ValueError(f"Unknown bout_id: {bout_id!r}")

    repo_root = Path(bout_row["repo_path"]).resolve()
    action = (action or "apply").strip().lower()

    if action == "cleanup":
        rows = conn.execute("SELECT worktree_path FROM contenders WHERE bout_id = ?", (bout_id,)).fetchall()
        removed: List[str] = []
        for r in rows:
            wt = Path(r["worktree_path"])
            if wt.exists():
                _run_git(["worktree", "remove", "--force", str(wt)], cwd=repo_root)
                if wt.exists():
                    shutil.rmtree(wt, ignore_errors=True)
                removed.append(str(wt))
        _run_git(["worktree", "prune"], cwd=repo_root)
        record_event(bout_id, "bout.cleanup", f"Cleaned up {len(removed)} ephemeral worktrees.")
        details = get_bout_details(bout_id)
        notify_desktop("bout.updated", {"bout_id": bout_id, "bout": details})
        return {"ok": True, "action": "cleanup", "removed_worktrees": removed, "bout": details}

    if action == "rollback":
        backup_path_str = str(bout_row["backup_patch_path"] or "").strip()
        if not backup_path_str or not Path(backup_path_str).is_file():
            raise ValueError(f"No rollback patch found for bout {bout_id!r}.")
        rev = _run_git(["apply", "-R", "--whitespace=nowarn", backup_path_str], cwd=repo_root)
        if rev.returncode != 0:
            raise RuntimeError(f"Rollback failed: {rev.stderr.strip()}")
        with _LOCK:
            conn.execute(
                "UPDATE bouts SET status = 'evaluated', merged_contender_id = '', updated_at = ? WHERE bout_id = ?",
                (_now_iso(), bout_id),
            )
            conn.commit()
        record_event(bout_id, "bout.rollback", "Rolled back merged contender patch from main repository.")
        details = get_bout_details(bout_id)
        notify_desktop("bout.updated", {"bout_id": bout_id, "bout": details})
        return {"ok": True, "action": "rollback", "bout": details}

    target_cid = (contender_id or bout_row["winner_id"] or "").strip()
    if not target_cid:
        raise ValueError("No contender_id specified and no winner has been evaluated yet. Run joust_evaluate first.")

    row = conn.execute(
        "SELECT * FROM contenders WHERE bout_id = ? AND (contender_id = ? OR name = ?)",
        (bout_id, target_cid, target_cid),
    ).fetchone()
    if row is None:
        raise ValueError(f"Contender {target_cid!r} not found in bout {bout_id!r}.")

    cid = row["contender_id"]
    wt_path = Path(row["worktree_path"]).resolve()
    _, _, _, diff_text = _compute_worktree_diff_stats(wt_path)
    if not diff_text.strip():
        raise ValueError(f"Contender {cid!r} has no file changes to merge.")

    backups_dir = get_data_dir() / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    patch_file = backups_dir / f"{bout_id}_{cid}.patch"
    patch_file.write_text(diff_text, encoding="utf-8")

    check_res = _run_git(["apply", "--check", "--whitespace=nowarn", str(patch_file)], cwd=repo_root)
    if action == "dry_run":
        return {
            "ok": check_res.returncode == 0,
            "action": "dry_run",
            "bout_id": bout_id,
            "contender_id": cid,
            "clean_apply": check_res.returncode == 0,
            "message": "Patch applies cleanly." if check_res.returncode == 0 else check_res.stderr.strip(),
        }

    if check_res.returncode != 0:
        raise RuntimeError(f"Patch conflict when applying {cid} to {repo_root}: {check_res.stderr.strip()}")

    apply_res = _run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=repo_root)
    if apply_res.returncode != 0:
        raise RuntimeError(f"git apply failed: {apply_res.stderr.strip()}")

    saved_patch = str(patch_file) if keep_backup else ""
    if not keep_backup:
        patch_file.unlink(missing_ok=True)

    with _LOCK:
        conn.execute(
            """
            UPDATE bouts
            SET status = 'merged', merged_contender_id = ?, backup_patch_path = ?, updated_at = ?
            WHERE bout_id = ?
            """,
            (cid, saved_patch, _now_iso(), bout_id),
        )
        conn.execute(
            "UPDATE contenders SET status = 'merged', updated_at = ? WHERE bout_id = ? AND contender_id = ?",
            (_now_iso(), bout_id, cid),
        )
        conn.commit()

    record_event(
        bout_id,
        "bout.merged",
        f"Merged winning contender '{cid}' into {repo_root} (reversible patch saved).",
        contender_id=cid,
    )
    details = get_bout_details(bout_id)
    notify_desktop("bout.updated", {"bout_id": bout_id, "bout": details})
    return {
        "ok": True,
        "action": "apply",
        "bout_id": bout_id,
        "merged_contender_id": cid,
        "repo_path": str(repo_root),
        "backup_patch_path": saved_patch,
        "bout": details,
    }


def get_bout_details(bout_id: str) -> Dict[str, Any]:
    """Return full serialized bout state, ranked contenders, diffs, and recent audit events."""
    conn = get_db()
    bout_row = conn.execute("SELECT * FROM bouts WHERE bout_id = ?", (bout_id,)).fetchone()
    if bout_row is None:
        raise ValueError(f"Unknown bout_id: {bout_id!r}")

    c_rows = conn.execute(
        "SELECT * FROM contenders WHERE bout_id = ? ORDER BY score DESC, duration_ms ASC, rowid ASC",
        (bout_id,),
    ).fetchall()
    e_rows = conn.execute(
        "SELECT * FROM bout_events WHERE bout_id = ? ORDER BY id DESC LIMIT 30",
        (bout_id,),
    ).fetchall()

    contenders: List[Dict[str, Any]] = []
    for rank, r in enumerate(c_rows, start=1):
        wt_path = Path(r["worktree_path"])
        _, _, _, diff_text = _compute_worktree_diff_stats(wt_path)
        contenders.append(
            {
                "rank": rank,
                "contender_id": r["contender_id"],
                "name": r["name"],
                "strategy": r["strategy"],
                "model": r["model"],
                "worktree_path": r["worktree_path"],
                "status": r["status"],
                "tests_passed": bool(r["tests_passed"]),
                "exit_code": int(r["exit_code"]),
                "duration_ms": float(r["duration_ms"]),
                "files_changed": int(r["files_changed"]),
                "insertions": int(r["insertions"]),
                "deletions": int(r["deletions"]),
                "score": float(r["score"]),
                "score_breakdown": json.loads(r["score_breakdown_json"] or "{}"),
                "test_output": r["test_output"],
                "steering_notes": json.loads(r["steering_notes_json"] or "[]"),
                "tool_calls_count": int(r["tool_calls_count"]),
                "diff_preview": diff_text[:4000],
                "is_winner": (r["contender_id"] == bout_row["winner_id"] and bool(bout_row["winner_id"])),
                "is_merged": (r["contender_id"] == bout_row["merged_contender_id"] and bool(bout_row["merged_contender_id"])),
                "updated_at": r["updated_at"],
            }
        )

    return {
        "bout_id": bout_row["bout_id"],
        "title": bout_row["title"],
        "goal": bout_row["goal"],
        "repo_path": bout_row["repo_path"],
        "base_commit": bout_row["base_commit"],
        "test_command": bout_row["test_command"],
        "status": bout_row["status"],
        "winner_id": bout_row["winner_id"],
        "merged_contender_id": bout_row["merged_contender_id"],
        "backup_patch_path": bout_row["backup_patch_path"],
        "created_at": bout_row["created_at"],
        "updated_at": bout_row["updated_at"],
        "directive": f'::joust-bout{{id="{bout_row["bout_id"]}"}}',
        "contenders": contenders,
        "events": [dict(e) for e in e_rows],
    }


def list_bouts(limit: int = 20) -> Dict[str, Any]:
    """Return summary list of bouts and overall arena statistics."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM bouts ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    bouts = [get_bout_details(r["bout_id"]) for r in rows]

    total_bouts = len(bouts)
    total_contenders = sum(len(b["contenders"]) for b in bouts)
    merged_count = sum(1 for b in bouts if b["status"] == "merged")
    return {
        "total_bouts": total_bouts,
        "total_contenders": total_contenders,
        "merged_bouts": merged_count,
        "bouts": bouts,
    }


def run_demo_bout() -> Dict[str, Any]:
    """Create a self-contained demo Git repo and run a real 3-contender Joust tournament end-to-end."""
    demo_repo = get_data_dir() / "demo_orderbook_repo"
    if demo_repo.exists():
        shutil.rmtree(demo_repo, ignore_errors=True)
    demo_repo.mkdir(parents=True, exist_ok=True)

    # 1. Write baseline slow O(N*M) orderbook matcher with a zero-quantity bug
    (demo_repo / "order_matcher.py").write_text(
        '''"""Baseline order-book matching engine (O(N*M) scan with zero-qty edge bug)."""

from typing import Dict, List, Tuple


def match_orders(bids: List[Dict], asks: List[Dict]) -> List[Tuple[str, str, int, float]]:
    """Match buy bids against sell asks at identical price levels.
    Returns list of (bid_id, ask_id, quantity, price) fills.
    """
    fills: List[Tuple[str, str, int, float]] = []
    used_asks = set()
    for bid in bids:
        # BUG: does not skip zero/negative quantity bids, and scans all asks linearly O(N*M)
        for idx, ask in enumerate(asks):
            if idx in used_asks:
                continue
            if bid["price"] == ask["price"]:
                qty = min(bid["qty"], ask["qty"])
                fills.append((bid["id"], ask["id"], qty, float(ask["price"])))
                used_asks.add(idx)
                break
    return fills
''',
        encoding="utf-8",
    )

    (demo_repo / "test_order_matcher.py").write_text(
        '''"""Verification & latency benchmark suite for order_matcher.py."""

import time
import unittest
from order_matcher import match_orders


class TestOrderMatcher(unittest.TestCase):
    def test_basic_exact_match(self):
        bids = [{"id": "b1", "price": 100.5, "qty": 10}, {"id": "b2", "price": 99.0, "qty": 5}]
        asks = [{"id": "a1", "price": 99.0, "qty": 5}, {"id": "a2", "price": 100.5, "qty": 4}]
        fills = match_orders(bids, asks)
        self.assertEqual(fills, [("b1", "a2", 4, 100.5), ("b2", "a1", 5, 99.0)])

    def test_ignores_zero_or_negative_quantity(self):
        bids = [{"id": "b_zero", "price": 50.0, "qty": 0}, {"id": "b_valid", "price": 50.0, "qty": 8}]
        asks = [{"id": "a_zero", "price": 50.0, "qty": 0}, {"id": "a_valid", "price": 50.0, "qty": 8}]
        fills = match_orders(bids, asks)
        self.assertEqual(fills, [("b_valid", "a_valid", 8, 50.0)])

    def test_throughput_benchmark(self):
        bids = [{"id": f"b{i}", "price": float(i % 400), "qty": (i % 10) + 1} for i in range(2500)]
        asks = [{"id": f"a{i}", "price": float((i * 7) % 400), "qty": (i % 10) + 1} for i in range(2500)]
        t0 = time.perf_counter()
        for _ in range(4):
            fills = match_orders(bids, asks)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertTrue(len(fills) > 1000)
        print(f"\\nJOUST_BENCHMARK_MS={elapsed_ms:.2f}")


if __name__ == "__main__":
    unittest.main()
''',
        encoding="utf-8",
    )

    _run_git(["init"], cwd=demo_repo)
    _run_git(["add", "-A"], cwd=demo_repo)
    _run_git(
        ["-c", "user.name=JoustDemo", "-c", "user.email=joust@local", "commit", "-m", "Initial baseline order_matcher"],
        cwd=demo_repo,
    )

    bout = create_bout(
        title="Orderbook Matcher Optimization & Zero-Qty Bugfix",
        goal=(
            "Fix the zero-quantity order bug in match_orders() and replace the O(N*M) nested loop "
            "with a sub-millisecond algorithm that passes all unit tests and minimizes benchmark latency."
        ),
        repo_path=str(demo_repo),
        test_command="python3 test_order_matcher.py",
        contenders=[
            {
                "name": "hash-bucket-index",
                "strategy": "Group valid asks by price into a collections.deque hash table for O(1) price-level matching.",
                "model": "hermes-4-405b",
            },
            {
                "name": "bisect-sorted-book",
                "strategy": "Sort asks by price and use bisect_left binary search to locate matching price levels in O(N log M).",
                "model": "claude-sonnet-4-5",
            },
            {
                "name": "naive-filter-scan",
                "strategy": "Pre-filter bids with list comprehension but retain nested linear scan over asks.",
                "model": "gpt-4.1-mini",
            },
        ],
        spawn_subagents=False,
    )
    bout_id = bout["bout_id"]

    # Populate each contender's isolated Git worktree with its distinct implementation:
    # Contender 1: O(N + M) hash-bucket deque lookup (Fastest + passes all tests)
    steer_contender(
        bout_id=bout_id,
        contender_id="hash-bucket-index",
        guidance="Implemented O(1) deque price-bucket lookup with strict positive qty guard.",
        write_files={
            "order_matcher.py": '''"""O(N+M) hash-bucket order-book matching engine."""

from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple


def match_orders(bids: List[Dict], asks: List[Dict]) -> List[Tuple[str, str, int, float]]:
    """Match buy bids against sell asks in O(N+M) using price-indexed FIFO deques."""
    ask_buckets: Dict[float, Deque[Dict]] = defaultdict(deque)
    for ask in asks:
        if ask.get("qty", 0) > 0:
            ask_buckets[float(ask["price"])].append(ask)

    fills: List[Tuple[str, str, int, float]] = []
    for bid in bids:
        bid_qty = bid.get("qty", 0)
        if bid_qty <= 0:
            continue
        price = float(bid["price"])
        bucket = ask_buckets.get(price)
        if bucket:
            ask = bucket.popleft()
            fills.append((bid["id"], ask["id"], min(bid_qty, ask["qty"]), price))
    return fills
'''
        },
    )

    # Contender 2: O(N log M) bisect binary search with overhead (Passes tests, slower + larger diff)
    steer_contender(
        bout_id=bout_id,
        contender_id="bisect-sorted-book",
        guidance="Implemented sorted price index with bisect_left lookup and active-slot scanning.",
        write_files={
            "order_matcher.py": '''"""O(N log M) bisect-based order-book matching engine."""

from bisect import bisect_left
from typing import Dict, List, Tuple


def match_orders(bids: List[Dict], asks: List[Dict]) -> List[Tuple[str, str, int, float]]:
    """Sort valid asks by price and binary-search each bid."""
    valid_asks = [
        (float(a["price"]), idx, a)
        for idx, a in enumerate(asks)
        if a.get("qty", 0) > 0
    ]
    valid_asks.sort(key=lambda item: (item[0], item[1]))
    keys = [item[0] for item in valid_asks]
    used = [False] * len(valid_asks)

    fills: List[Tuple[str, str, int, float]] = []
    for bid in bids:
        bid_qty = bid.get("qty", 0)
        if bid_qty <= 0:
            continue
        price = float(bid["price"])
        pos = bisect_left(keys, price)
        while pos < len(valid_asks) and keys[pos] == price:
            if not used[pos]:
                used[pos] = True
                ask = valid_asks[pos][2]
                fills.append((bid["id"], ask["id"], min(bid_qty, ask["qty"]), price))
                break
            pos += 1
    return fills
'''
        },
    )

    # Contender 3: Naive filter scan (Forgets to filter zero-qty asks -> fails unit test + slow)
    steer_contender(
        bout_id=bout_id,
        contender_id="naive-filter-scan",
        guidance="Filtered bids > 0 but kept linear scan over asks.",
        write_files={
            "order_matcher.py": '''"""Linear scan matcher with partial bid filter (misses zero-qty ask bug)."""

from typing import Dict, List, Tuple


def match_orders(bids: List[Dict], asks: List[Dict]) -> List[Tuple[str, str, int, float]]:
    fills: List[Tuple[str, str, int, float]] = []
    used_asks = set()
    for bid in [b for b in bids if b.get("qty", 0) > 0]:
        for idx, ask in enumerate(asks):
            if idx not in used_asks and bid["price"] == ask["price"]:
                fills.append((bid["id"], ask["id"], min(bid["qty"], ask["qty"]), float(ask["price"])))
                used_asks.add(idx)
                break
    return fills
'''
        },
    )

    return evaluate_bout(bout_id)
