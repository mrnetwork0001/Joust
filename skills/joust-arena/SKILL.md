---
name: joust-arena
description: Launch parallel Git-worktree coding tournaments, benchmark competing implementations, and merge the winning branch with Joust.
---

# Joust Arena Skill (`joust:joust-arena`)

Use **Joust** whenever a coding, refactoring, debugging, or optimization task benefits from racing 2 to 4 distinct architectural approaches in parallel without risking edits to the user's main working tree.

## Workflow

1. **Launch a Bout (`joust_launch`)**
   - Define 2–4 contenders with distinct strategies (e.g., `hash-bucket-index` vs `bisect-sorted-book`); give each a `model` to race models against each other.
   - Provide a deterministic `test_command` (e.g., `pytest -q` or `python3 -m unittest`).
   - Joust provisions an isolated Git worktree for each contender under `plugin-data/joust/worktrees/<bout_id>/<contender_id>` and scopes subagent file/terminal operations to that worktree via `tool_request` middleware.

2. **Implement or Steer Contenders (`joust_steer`)**
   - When subagents run automatically, monitor or steer them with `joust_steer`.
   - You can also write competing file implementations directly into each contender's worktree via `joust_steer(..., write_files={...})`.

3. **Run the Benchmark Judge (`joust_evaluate`)**
   - Executes `test_command` inside every contender's isolated worktree.
   - Scores each contender (0–100) across **Correctness (60 pts)**, **Relative Speed (25 pts)**, and **Diff Minimalism (15 pts)**.

4. **Render the Inline Desktop Arena Card**
   - Always place the bout directive on its own paragraph in your response so Hermes Desktop renders the interactive scoreboard card:

```text
::joust-bout{id="bout-xxxxxxxx"}
```

5. **Inspect Diffs & Merge Winner (`joust_diff` / `joust_merge`)**
   - Use `joust_diff` to compare unified diffs across contenders.
   - Use `joust_merge` (`action="apply"`) to apply the winning contender cleanly to the main repository (saving a reversible rollback patch automatically), or `action="rollback"` to undo a merge. Both are escalated to the user's approval gate; `action="dry_run"` pre-flights without touching anything.

6. **Crossfire & Flake Shield (`joust_crossfire`, `joust_evaluate runs=3`)**
   - Ask contenders to add tests for the behaviour they fixed, then run `joust_crossfire`: each contender's new tests are fired at every rival worktree and survival becomes the first tiebreak.
   - Use `runs=3` (or more, up to 7) when timing matters or the suite might be flaky: every run must pass, speed uses the median, and flaky contenders cannot win.

7. **Keep everything (`joust_report`)**
   - `joust_report` snapshots every contender as a `joust/<bout>/<contender>` branch and writes a markdown report (optionally `write_path` inside the repo). Merges and cleanups keep the branches too (`keep_losers`).

## Good to know

- Judging runs the bout's test command through the Hermes `terminal` tool, so approvals and guards apply; timeouts are clamped to 5–600 s per contender and contenders are judged one after another.
- Only tracked, uncommitted edits in the main repository are mirrored into the worktrees; untracked files are not.
- Each contender's own test files can print `JOUST_BENCHMARK_MS=<float>` (last occurrence wins) to replace wall-clock time in the speed score.
- Contenders that have not been judged yet keep their `ready`/`running` status; the bout is `partial` until every contender has run.
- `hermes joust …` (CLI) is available when plugins run in-process (the Hermes default). Under `plugins.isolation: host`, use `/joust …`, the `joust_status` tool (`run_demo=true` for the demo) or the dashboard.
