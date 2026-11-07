---
name: joust-arena
description: Launch parallel Git-worktree coding tournaments, benchmark competing implementations, and merge the winning branch with Joust.
---

# Joust Arena Skill (`joust:joust-arena`)

Use **Joust** whenever a coding, refactoring, debugging, or optimization task benefits from racing 2 to 4 distinct architectural approaches in parallel without risking edits to the user's main working tree.

## Workflow

1. **Launch a Bout (`joust_launch`)**
   - Define 2–4 contenders with distinct strategies (e.g., `hash-bucket-index` vs `bisect-sorted-book`).
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
   - Use `joust_merge` (`action="apply"`) to apply the winning contender cleanly to the main repository (saving a reversible rollback patch automatically), or `action="rollback"` to undo a merge.
