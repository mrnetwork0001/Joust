# ⚔️ Joust — Parallel Git-Worktree Subagent Arena & Benchmark Judge for Hermes Agent

**Joust** turns Hermes into a **Speculative Parallel Coding Arena**. Instead of letting a single agent mutate your working tree directly—or having parallel subagents overwrite each other's files—Joust provisions **isolated ephemeral Git worktrees** for 2 to 4 competing strategies, jails each contender's file and terminal tool calls inside its own worktree via `tool_request` middleware, runs your verification and benchmark suite across every contender, ranks them by **Correctness (60 pts)**, **Execution Speed (25 pts)**, and **Diff Minimalism (15 pts)**, and merges the winning patch into your main repository with **1-click rollback safety**.

---

## ✨ Why Joust Goes Far Beyond a Prompt or Skill

1. **True Parallel Subagent Filesystem Isolation (`tool_request` Middleware + Git Worktrees)**
   - In core Hermes, `SubagentLaunchRequest` rejects `working_directory` overrides, so parallel coding subagents share the same working directory and collide when editing files.
   - Joust creates detached Git worktrees under `<HERMES_HOME>/plugin-data/joust/worktrees/<bout_id>/<contender_id>` (carrying over any uncommitted local edits) and registers a `tool_request` middleware that transparently rewrites relative file paths and terminal working directories per child `session_id` / `task_id` into that contender's worktree.
2. **Deterministic Verification & Benchmark Judge (`joust_evaluate`)**
   - Runs your test or benchmark command inside each isolated worktree with bounded timeouts, captures exit codes, wall-clock execution time (`ms`), and custom `JOUST_BENCHMARK_MS=<float>` telemetry, and computes a composite **Joust Score (0–100)**.
3. **Reversible 1-Click Winner Merge (`joust_merge`)**
   - Pre-flights patches with `git apply --check`, saves a reversible backup patch under `<HERMES_HOME>/plugin-data/joust/backups/` before applying, and supports instant 1-click rollback (`action="rollback"`).
4. **Native Hermes Desktop Arena Studio (`desktop/plugin.js` + `dashboard/plugin_api.py`)**
   - **Inline Chat Transcript Directives (`::joust-bout{id="..."}`)**: Renders a live interactive tournament scoreboard card directly inside the assistant reply with 1-click **Re-Judge**, **Steer**, and **Merge** buttons.
   - **Full Desktop Workspace Page (`/joust`) & Docked Right Pane**: Side-by-side contender leaderboard, syntax-colored unified Git diff viewer, benchmark console output, and a **1-Click Live 3-Way Demo Tournament** button.

---

## 🛠️ Provided Capabilities

| Surface | Name | Purpose |
| :--- | :--- | :--- |
| **Tool** | `joust_launch` | Provision 2–4 isolated Git worktrees for competing strategies and optionally spawn parallel subagents via `ctx.subagent_lifecycle`. |
| **Tool** | `joust_evaluate` | Execute verification/benchmark commands across contender worktrees, compute Joust Scores (0–100), and rank the winner. |
| **Tool** | `joust_diff` | Inspect unified Git diffs and LOC stats (`files_changed`, `insertions`, `deletions`) per contender. |
| **Tool** | `joust_steer` | Send mid-flight steering guidance to a running subagent contender (`ctx.subagent_lifecycle.steer`) or update files in its worktree. |
| **Tool** | `joust_merge` | Apply (`apply`), pre-flight (`dry_run`), revert (`rollback`), or prune (`cleanup`) contender worktrees. |
| **Tool** | `joust_status` | Inspect bouts, leaderboards, or run a self-contained 3-contender demo tournament (`run_demo=True`). |
| **Middleware** | `tool_request` | Jails contender subagent file/terminal paths strictly inside their assigned Git worktree. |
| **Hooks** | `post_tool_call`, `subagent_stop` | Tracks tool activity per contender and auto-evaluates worktrees when a subagent completes. |
| **Slash / CLI** | `/joust`, `hermes joust` | Manage bouts, run `/joust demo`, judge contenders, and merge/rollback from chat or CLI. |
| **Desktop SDK** | `desktop/plugin.js` | Full `/joust` Arena page, docked right pane, status-bar chip, palette command, and `::joust-bout` inline directive. |

---

## 🚀 Quick Start & 60-Second Demo

### Install & Validate
```bash
hermes plugins validate /path/to/Joust --install-deps
hermes plugins enable joust
```

### Run the Built-In 3-Contender Demo Tournament
In any Hermes chat session:
```text
/joust demo
```
Or ask Hermes:
> *"Run a Joust demo tournament (`joust_status` with `run_demo=true`) and show me the arena card."*

Or open **Joust Arena** from the Hermes Desktop sidebar (`/joust`) and click **⚡ Run Live 3-Way Demo Tournament**.

---

## 🔒 Disclosure (Catalog Rule 13)

- **Local Filesystem & Git Operations**: Joust reads the target Git repository specified by `repo_path` (or the current working directory), creates ephemeral Git worktrees under `<HERMES_HOME>/plugin-data/joust/worktrees/`, stores bout metadata in `<HERMES_HOME>/plugin-data/joust/joust.db` (SQLite WAL), and writes reversible backup patches under `<HERMES_HOME>/plugin-data/joust/backups/` before applying a winning contender patch with `git apply`.
- **Subprocess Execution**: `joust_launch`, `joust_diff`, and `joust_merge` invoke local `git` commands (`rev-parse`, `worktree`, `diff`, `apply`) with `GIT_TERMINAL_PROMPT=0`. `joust_evaluate` runs the user- or model-specified `test_command` inside each contender's isolated worktree with a bounded timeout (default 60s) and redacts sensitive output via `agent.redact.redact_sensitive_text`.
- **Network & Credentials**: Joust makes **no network calls**, requires **no API keys or credentials**, has **no telemetry**, and ships **zero third-party Python dependencies** (100% Python stdlib + Hermes public plugin APIs).
