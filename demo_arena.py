#!/usr/bin/env python3
"""Interactive terminal demo for Joust (Parallel Git-Worktree Subagent Arena & Benchmark Judge)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
# Load the plugin package by path so this works from a clone named Joust or an install named joust.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("joust_plugin_demo", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["joust_plugin_demo"] = _pkg
_spec.loader.exec_module(_pkg)
tools = _pkg.tools


def main() -> None:
    print("=" * 78)
    print("⚔️  JOUST — PARALLEL GIT-WORKTREE SUBAGENT ARENA & BENCHMARK JUDGE")
    print("=" * 78)
    print("\n[1/3] Provisioning demo repository & 3 isolated Git worktrees...")
    res = json.loads(tools.joust_status({"run_demo": True}))
    bout = res["bout"]
    print(f"      Bout ID:      {bout['bout_id']}")
    print(f"      Title:        {bout['title']}")
    print(f"      Base Commit:  {bout['base_commit'][:12]}")
    print(f"      Verify Cmd:   {bout['test_command']}")

    print("\n[2/3] Contender Leaderboard (Scored on Correctness 60pt | Speed 25pt | Minimalism 15pt):")
    print("-" * 78)
    print(f"{'Rank':<6} {'Contender':<22} {'Status':<10} {'Score':<10} {'Latency':<12} {'Diff (+/-)'}")
    print("-" * 78)
    for c in bout["contenders"]:
        status = "🏆 WINNER" if c["is_winner"] else ("✅ PASS" if c["tests_passed"] else "❌ FAIL")
        diff_str = f"+{c['insertions']} / -{c['deletions']} LOC"
        print(
            f"#{c['rank']:<5} {c['contender_id']:<22} {status:<10} "
            f"{c['score']:>5.1f}/100  {c['duration_ms']:>7.2f} ms   {diff_str}"
        )
    print("-" * 78)

    print(f"\n[3/3] Merging winning worktree ({bout['winner_id']}) into main repository with rollback backup...")
    merge_res = json.loads(tools.joust_merge({"bout_id": bout["bout_id"], "action": "apply"}))
    print(f"      ✓ Cleanly merged: {merge_res['merged_contender_id']}")
    print(f"      ✓ Backup patch:   {merge_res['backup_patch_path']}")
    print(f"      ✓ Desktop Card:   {bout['directive']}")
    print("=" * 78)


if __name__ == "__main__":
    main()
