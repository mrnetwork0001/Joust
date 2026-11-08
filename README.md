# ⚔️ Joust — Parallel Git-Worktree Subagent Arena & Benchmark Judge for Hermes Agent

**Joust** turns Hermes into a **Speculative Parallel Coding Arena**. Instead of letting a single agent mutate your working tree directly—or having parallel subagents overwrite each other's files—Joust provisions **isolated ephemeral Git worktrees** for 2 to 4 competing strategies, jails each contender's file and terminal tool calls inside its own worktree via `tool_request` middleware, runs your verification and benchmark suite across every contender, ranks them by **Correctness (60 pts)**, **Execution Speed (25 pts)**, and **Diff Minimalism (15 pts)** with Crossfire survival as the tiebreak, and merges the winning patch into your main repository with **1-click rollback safety**.

---

## ✨ Why Joust Goes Far Beyond a Prompt or Skill

1. **True Parallel Subagent Filesystem Isolation (`tool_request` Middleware + Git Worktrees)**
   - In core Hermes, `SubagentLaunchRequest` rejects `working_directory` overrides, so parallel coding subagents share the same working directory and collide when editing files.
   - Joust creates detached Git worktrees under `<HERMES_HOME>/plugin-data/joust/worktrees/<bout_id>/<contender_id>` (carrying over any uncommitted local edits) and registers a `tool_request` middleware that transparently rewrites relative file paths and terminal working directories per child `session_id` / `task_id` into that contender's worktree.
   - The child session is bound through the `subagent_start` hook: `joust_launch` embeds a `joust:bout=…;contender=…` marker in each child's goal, the hook (which Hermes fires inside `launch()`) pairs `child_session_id` with that marker, and the binding is also persisted in SQLite so it survives a plugin-host restart.
2. **Deterministic Verification & Benchmark Judge (`joust_evaluate`)**
   - Runs your test or benchmark command inside each isolated worktree with bounded timeouts, captures exit codes, wall-clock execution time (`ms`), and custom `JOUST_BENCHMARK_MS=<float>` telemetry, and computes a composite **Joust Score (0–100)**.
3. **Reversible 1-Click Winner Merge (`joust_merge`)**
   - Pre-flights patches with `git apply --check`, saves a reversible backup patch under `<HERMES_HOME>/plugin-data/joust/backups/` before applying, and supports instant 1-click rollback (`action="rollback"`).
4. **Native Hermes Desktop Arena Studio (`desktop/plugin.js` + `dashboard/plugin_api.py`)**
   - **Inline Chat Transcript Directives (`::joust-bout{id="..."}`)**: Renders a live interactive tournament scoreboard card directly inside the assistant reply with 1-click **Re-Judge**, **Steer**, and **Merge** buttons.
   - **Full Desktop Workspace Page (`/joust`) & Docked Right Pane**: Side-by-side contender leaderboard, syntax-colored unified Git diff viewer, benchmark console output, and a **1-Click Live 3-Way Demo Tournament** button.

---

## 🎯 What Makes a Joust Different From a Race

- **Crossfire (`joust_crossfire`).** Each contender's new or changed test files are fired at every rival worktree (copied under a `__xfire_<contender>` suffix, run, removed). A contender *survives* a rival when it passes that rival's tests, and survival rate is the first tiebreak on the leaderboard: the winner has to beat its rivals' tests, not just its own. In the demo, `hash-bucket-index` ships a partial-fill test that `bisect-sorted-book` fails, and `bisect-sorted-book` ships a zero-quantity test that `naive-filter-scan` fails.
- **Flake Shield (`joust_evaluate runs=N`).** The verification command runs N times (1–7, config `default_runs`); every run must pass, the speed score uses the **median**, and a mixed pass/fail record marks the contender `flaky`, which can never win. The demo judges each contender three times.
- **Nothing is lost (`joust_report`, `keep_losers`).** Before a merge or cleanup every contender, winner included, is snapshotted as a `joust/<bout>/<contender>` branch in your repository (built with `commit-tree`, so worktree HEADs never move), and a markdown bout report (scoreboard, Flake Shield runs, Crossfire matrix, diffs, outputs, timeline) is written under `plugin-data/joust/reports/` or to a path inside the repo.
- **Model vs model.** Give each contender its own `model` in `joust_launch`; the child subagent runs on it, and the card, page and report show which model produced which attempt, so a bout can compare models as well as approaches.

```json
{"title": "Fix the cart discount", "goal": "…", "test_command": "pytest -q", "contenders": [
  {"name": "hermes-405b", "strategy": "minimal surgical fix", "model": "nousresearch/hermes-4-405b"},
  {"name": "sonnet", "strategy": "refactor the discount pipeline", "model": "anthropic/claude-sonnet-4-5"}]}
```

## 🛠️ Provided Capabilities

| Surface | Name | Purpose |
| :--- | :--- | :--- |
| **Tool** | `joust_launch` | Provision 2–4 isolated Git worktrees for competing strategies and optionally spawn parallel subagents via `ctx.subagent_lifecycle`. |
| **Tool** | `joust_evaluate` | Execute verification/benchmark commands across contender worktrees, compute Joust Scores (0–100), and rank the winner. |
| **Tool** | `joust_diff` | Inspect unified Git diffs and LOC stats (`files_changed`, `insertions`, `deletions`) per contender. |
| **Tool** | `joust_steer` | Send mid-flight steering guidance to a running subagent contender (`ctx.subagent_lifecycle.steer`) or update files in its worktree. |
| **Tool** | `joust_merge` | Apply (`apply`), pre-flight (`dry_run`), revert (`rollback`), or prune (`cleanup`) contender worktrees. |
| **Tool** | `joust_crossfire` | Fire every contender's new tests at every rival worktree; survival rate becomes the first leaderboard tiebreak. |
| **Tool** | `joust_report` | Keep every contender as a `joust/<bout>/<contender>` branch and write a markdown bout report. |
| **Tool** | `joust_status` | Inspect bouts, leaderboards, or run a self-contained 3-contender demo tournament (`run_demo=True`). |
| **Middleware** | `tool_request` | Jails contender subagent file/terminal paths strictly inside their assigned Git worktree. |
| **Hooks** | `pre_tool_call`, `subagent_start`, `post_tool_call`, `subagent_stop` | Vetoes a contender's tool call that tried to escape its worktree and asks for human approval before `joust_merge` touches your tree (`approve` directive); binds each contender subagent's session to its worktree; counts tool activity; auto-judges a worktree (off the hook thread) when its subagent completes. |
| **Slash / CLI** | `/joust`, `hermes joust` | Manage bouts, run `/joust demo`, judge, `crossfire`, `report`, and merge/rollback from chat or CLI. |
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

## ✅ Verified Against Hermes 0.23.0

- `hermes plugins validate` passes with no warnings (manifest, capability probe, security scan, no core override, desktop surface); `hermes plugins doctor joust` reports 8 tools and 4 hooks; `hermes plugins enable joust` + `hermes joust demo` run end to end from an installed copy.
- Only public plugin surfaces are used: `ctx.register_*`, `ctx.subagent_lifecycle`, `ctx.dispatch_tool("terminal", …)`, `hermes_cli.plugin_events.broadcast_plugin_event`, and the Desktop plugin SDK. No private core imports, no `sys.path` or `sys.modules` tricks.
- `python3 -m unittest discover -s tests` runs 90+ tests on the plain interpreter and 30 more when Hermes is importable: the demo tournament with merge and rollback; the live-subagent path driven with the exact kwargs Hermes passes (`subagent_start` binding inside `launch()`, per-session path jailing incl. V4A `patch` bodies and `search_files`, escape veto via `pre_tool_call`, DB-backed binding recovery after a plugin-host restart, `joust_steer` reaching a live handle, auto-judge on `subagent_stop`); the engine edge cases (merge conflicts, dirty-tree launches, timeouts, telemetry, truncation, concurrency, cleanup); Crossfire, Flake Shield, kept branches and reports; and the real Hermes chain (`apply_tool_request_middleware`, `invoke_hook`, tool dispatch, slash dispatch) loaded through the real plugin manager, in both `plugins.isolation: in_process` and `host` modes.
- `tests/desktop_smoke/` renders every Desktop contribution with React (server render plus a happy-dom mount that clicks the buttons and fires live events) against a fixture recorded from the real dashboard API: 55 checks pass.
- Works on Linux and macOS (POSIX shell, `git` ≥ 2.20). `hermes joust …` is wired only when plugins run in-process (the Hermes default); under `plugins.isolation: host` use `/joust …`, the `joust_status` tool or the dashboard.

## 🔒 Disclosure (Catalog Rule 13)

- **Repository access and git writes.** Joust reads the Git repository given by `repo_path` (or the session's working directory) and runs local `git` in it and in its worktrees: `rev-parse`, `diff HEAD --binary`, `worktree add --detach` (this registers `<repo>/.git/worktrees/<contender>` entries that persist until `joust_merge action="cleanup"`), `worktree remove --force`, `worktree prune`, `add -N` plus `diff HEAD` on every status, listing, diff and dashboard refresh (new files are intent-added inside the *worktree* index only), `apply` (your uncommitted tracked edits into each worktree at launch; the winning patch into your working tree on `joust_merge`), `apply -R` (rollback), and `init`/`add -A`/`commit` for the built-in demo repository and for the fallback used when `git worktree add` is refused. That fallback copies the repository directory, including untracked files, into the plugin data folder (ignoring `.git`, `__pycache__`, `.venv`, `node_modules`). Only tracked edits are mirrored into worktrees; untracked files are not.
- **Command execution.** The bout's test or benchmark command (chosen by you or the model) runs in every contender worktree **through the Hermes `terminal` tool via `ctx.dispatch_tool`**, so Hermes's dangerous-command guards, approval prompts and the configured terminal backend apply; if the terminal tool refuses or is unavailable the contender is recorded as failed and nothing else is tried. The same path is used when a contender subagent finishes (`auto_evaluate_on_stop`, default on). Only the self-contained demo (`/joust demo`, `hermes joust demo`, the Desktop “Run Demo” button) and explicit CLI/dashboard calls made outside an agent context fall back to a bounded `/bin/sh -c` subprocess, and the demo runs plugin-authored commands in a plugin-owned temporary repository only. Timeouts are clamped to 5–600 s per contender; contenders are judged sequentially.
- **Approvals.** `joust_merge apply`/`rollback` is escalated to Hermes's human-approval gate through a `pre_tool_call` `approve` directive; the Desktop Re-Judge buttons hand the request to the agent instead of executing from an HTTP click; the Desktop Merge button is an explicit user action on a reviewed diff with a saved rollback patch.
- **Branches.** With `keep_loser_branches` (default on) `joust_merge apply`/`cleanup` and `joust_report` create `joust/<bout>/<contender>` branches in your repository from each worktree's state (including untracked files, junk excluded); delete them with `git branch -D`. `joust_crossfire` temporarily copies test files into rival worktrees under a `__xfire_` suffix and removes them afterwards; `joust_report write_path` writes a markdown file inside the repository only when you ask for it.
- **Files written.** Worktrees under `<HERMES_HOME>/plugin-data/joust/worktrees/<bout>/<contender>`, rollback patches under `…/backups/`, temporary pre-flight patches under `…/tmp/`, the demo repository under `…/demo_orderbook_repo/`, and bout metadata (goals, repository and worktree paths, test commands, captured output, diff stats, events) in `…/joust.db` (SQLite, WAL). `joust_steer(write_files=…)` writes only inside the contender's worktree.
- **Subagents and cost.** `joust_launch` starts one Hermes subagent per contender through `ctx.subagent_lifecycle` (default on); each uses tokens on the model it runs on. A `tool_request` middleware rewrites file paths, V4A patch headers, `search_files` roots and terminal `workdir` for those child sessions into their worktree; shell command *text* is not rewritten (best-effort scoping, not a sandbox). A `pre_tool_call` hook vetoes calls whose paths point outside the worktree.
- **Desktop events.** Bout snapshots (title, goal, repository and worktree paths, scores, up to 4000 characters of diff per contender, captured test output) are broadcast to connected Hermes Desktop windows as `plugin.joust.bout.updated`; the Desktop half also polls the dashboard API every 4 s while visible. Captured output is scrubbed of token-like strings with a stdlib regex before it is stored or shown.
- **Network, credentials, telemetry, dependencies.** No network calls, no credentials or `requires_env`, no telemetry or usage reporting, no third-party Python dependencies (stdlib plus Hermes public plugin APIs). Linux and macOS.
