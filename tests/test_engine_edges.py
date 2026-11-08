"""Edge-case regression tests for the Joust engine (worktree_engine.py + arena_tools.py).

Every test runs against a fresh temporary ``HERMES_HOME`` and a throwaway Git repository, drives the
engine through the public tool handlers (``joust_launch`` / ``joust_evaluate`` / ``joust_merge`` ...)
or the engine helpers, and only asserts behaviour that is correct today. Scenarios that currently
misbehave are deliberately *not* locked in here; they live in the engine-edges bug report.

Groups: merge/rollback/cleanup lifecycle, the verification judge (timeouts, failures, telemetry,
encoding, ties, bounds), launch validation and dirty-tree carry-over, the middleware path jail,
and SQLite/data-dir handling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine as eng  # noqa: E402

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
_PY = sys.executable
_FIXED_CALC = "def add(a, b):\n    return a + b\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)


def _make_repo(root: Path, name: str = "repo", broken: bool = True) -> Path:
    """Create a one-commit repo whose test suite fails (broken=True) or passes at HEAD."""
    repo = root / name
    repo.mkdir(parents=True)
    body = "a - b" if broken else "a + b"
    (repo / "calc.py").write_text(f"def add(a, b):\n    return {body}\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


class _EngineEdgeCase(unittest.TestCase):
    """Fresh HERMES_HOME per test; module-level session bindings are cleared on teardown."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-edges-")
        self.tmp = Path(self._tmp.name)
        os.environ["HERMES_HOME"] = self._tmp.name
        tools.set_context(None)

    def tearDown(self) -> None:
        tools.set_context(None)
        eng.close_cached_dbs()
        with eng._LOCK:
            eng._ACTIVE_SESSION_WORKTREES.clear()
            eng._ACTIVE_SESSION_CONTENDERS.clear()
            eng._PENDING_SUBAGENT_SESSIONS.clear()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------------------------------
    def launch(self, repo: Path, names: Iterable[str] = ("alpha", "beta"), **extra: Any) -> Dict[str, Any]:
        raw = tools.joust_launch(
            {
                "title": "edge",
                "goal": "edge goal",
                "repo_path": str(repo),
                "test_command": extra.pop("test_command", f"{_PY} -m unittest -q"),
                "contenders": [{"name": n, "strategy": "s"} for n in names],
                "spawn_subagents": False,
                **extra,
            }
        )
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res["bout"]

    @staticmethod
    def worktrees(bout: Dict[str, Any]) -> Dict[str, Path]:
        return {c["contender_id"]: Path(c["worktree_path"]) for c in bout["contenders"]}

    def evaluate(self, bout_id: str, **kw: Any) -> Dict[str, Any]:
        raw = tools.joust_evaluate({"bout_id": bout_id, **kw})
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        return res

    @staticmethod
    def merge(bout_id: str, action: str, contender_id: Optional[str] = None) -> Dict[str, Any]:
        args: Dict[str, Any] = {"bout_id": bout_id, "action": action}
        if contender_id:
            args["contender_id"] = contender_id
        return json.loads(tools.joust_merge(args))

    @staticmethod
    def by_id(res: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {c["contender_id"]: c for c in res["bout"]["contenders"]}


# ====================================================================================================
# joust_merge: apply / dry_run / rollback / cleanup lifecycle
# ====================================================================================================
class TestMergeLifecycle(_EngineEdgeCase):
    def _winner_bout(self):
        repo = _make_repo(self.tmp)
        bout = self.launch(repo)
        wt = self.worktrees(bout)
        (wt["alpha"] / "calc.py").write_text(_FIXED_CALC, encoding="utf-8")
        res = self.evaluate(bout["bout_id"])
        self.assertEqual(res["winner_id"], "alpha")
        return repo, bout["bout_id"], wt

    def test_zero_diff_contender_is_refused_for_dry_run_and_apply(self) -> None:
        _, bid, _ = self._winner_bout()
        for action in ("dry_run", "apply"):
            res = self.merge(bid, action, "beta")
            self.assertFalse(res["ok"], res)
            self.assertIn("no file changes", res["error"])

    def test_conflicting_upstream_change_fails_cleanly_and_leaves_tree_untouched(self) -> None:
        repo, bid, _ = self._winner_bout()
        (repo / "calc.py").write_text("def add(a, b):\n    return a * b  # upstream\n", encoding="utf-8")
        before = (repo / "calc.py").read_text(encoding="utf-8")

        dry = self.merge(bid, "dry_run")
        self.assertFalse(dry["ok"])
        self.assertFalse(dry["clean_apply"])
        self.assertIn("patch does not apply", dry["message"])

        ap = self.merge(bid, "apply")
        self.assertFalse(ap["ok"])
        self.assertIn("Patch conflict", ap["error"])
        self.assertEqual((repo / "calc.py").read_text(encoding="utf-8"), before)
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, " M calc.py\n")
        self.assertEqual(eng.get_bout_details(bid)["status"], "evaluated")

    def test_rollback_before_any_merge_is_a_clear_error(self) -> None:
        _, bid, _ = self._winner_bout()
        res = self.merge(bid, "rollback")
        self.assertFalse(res["ok"])
        self.assertRegex(res["error"], "No rollback patch|no merged contender")

    def test_apply_twice_and_rollback_twice_are_idempotent_and_safe(self) -> None:
        repo, bid, _ = self._winner_bout()
        ap1 = self.merge(bid, "apply")
        self.assertTrue(ap1["ok"], ap1)
        self.assertEqual(ap1["merged_contender_id"], "alpha")
        self.assertTrue(Path(ap1["backup_patch_path"]).is_file())
        self.assertEqual((repo / "calc.py").read_text(encoding="utf-8"), _FIXED_CALC)

        ap2 = self.merge(bid, "apply")  # second apply must not double-apply or corrupt
        self.assertFalse(ap2["ok"])
        self.assertEqual((repo / "calc.py").read_text(encoding="utf-8"), _FIXED_CALC)
        self.assertEqual(eng.get_bout_details(bid)["status"], "merged")
        self.assertTrue(Path(ap1["backup_patch_path"]).is_file())

        rb1 = self.merge(bid, "rollback")
        self.assertTrue(rb1["ok"], rb1)
        self.assertIn("a - b", (repo / "calc.py").read_text(encoding="utf-8"))
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")
        self.assertEqual(eng.get_bout_details(bid)["status"], "evaluated")

        rb2 = self.merge(bid, "rollback")  # nothing left to revert
        self.assertFalse(rb2["ok"])
        self.assertIn("a - b", (repo / "calc.py").read_text(encoding="utf-8"))
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_cleanup_removes_worktrees_and_is_idempotent(self) -> None:
        repo, bid, wt = self._winner_bout()
        cl = self.merge(bid, "cleanup")
        self.assertTrue(cl["ok"])
        self.assertEqual(sorted(cl["removed_worktrees"]), sorted(str(p) for p in wt.values()))
        self.assertFalse(any(p.exists() for p in wt.values()))
        listed = _git(repo, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listed), 1, listed)  # only the main checkout remains registered
        again = self.merge(bid, "cleanup")
        self.assertTrue(again["ok"])
        self.assertEqual(again["removed_worktrees"], [])
        self.assertEqual(json.loads(tools.joust_diff({"bout_id": bid}))["diffs"]["alpha"]["diff"], "")

    def test_new_file_and_deletion_patches_apply_and_roll_back(self) -> None:
        repo = _make_repo(self.tmp, broken=False)
        bout = self.launch(repo, names=("adder", "remover"))
        wt = self.worktrees(bout)
        (wt["adder"] / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(500)), encoding="utf-8")
        (wt["remover"] / "test_calc.py").unlink()
        bid = bout["bout_id"]
        res = self.evaluate(bid)
        stats = self.by_id(res)
        self.assertEqual((stats["adder"]["files_changed"], stats["adder"]["insertions"]), (1, 500))
        self.assertEqual((stats["remover"]["files_changed"], stats["remover"]["deletions"]), (1, 6))

        self.assertTrue(self.merge(bid, "apply", "adder")["ok"])
        self.assertTrue((repo / "big.py").exists())
        self.assertTrue(self.merge(bid, "rollback")["ok"])
        self.assertFalse((repo / "big.py").exists())

        self.assertTrue(self.merge(bid, "apply", "remover")["ok"])
        self.assertFalse((repo / "test_calc.py").exists())
        self.assertTrue(self.merge(bid, "rollback")["ok"])
        self.assertTrue((repo / "test_calc.py").exists())
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_keep_backup_false_skips_patch_and_rollback_reports_it(self) -> None:
        class _Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return False if key == "keep_backup_patches" else default

        repo, bid, _ = self._winner_bout()
        tools.set_context(_Ctx())
        ap = self.merge(bid, "apply")
        self.assertTrue(ap["ok"], ap)
        self.assertEqual(ap["backup_patch_path"], "")
        self.assertEqual((repo / "calc.py").read_text(encoding="utf-8"), _FIXED_CALC)
        rb = self.merge(bid, "rollback")
        self.assertFalse(rb["ok"])
        self.assertIn("No rollback patch", rb["error"])

    def test_merge_without_winner_or_unknown_targets_are_clear_errors(self) -> None:
        repo = _make_repo(self.tmp)
        bid = self.launch(repo)["bout_id"]
        res = self.merge(bid, "apply")
        self.assertFalse(res["ok"])
        self.assertIn("no winner", res["error"])
        res = self.merge(bid, "apply", "nobody")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])
        res = self.merge("bout-ffffffff", "apply")
        self.assertFalse(res["ok"])
        self.assertIn("Unknown bout_id", res["error"])


# ====================================================================================================
# joust_evaluate: judge behaviour under hostile commands and outputs
# ====================================================================================================
class TestEvaluateJudge(_EngineEdgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = _make_repo(self.tmp, broken=False)
        self.bout = self.launch(self.repo)
        self.bid = self.bout["bout_id"]
        self.wt = self.worktrees(self.bout)

    def test_timeout_is_honoured_even_with_background_grandchild(self) -> None:
        t0 = time.perf_counter()
        res = self.evaluate(self.bid, contender_id="alpha", test_command="sh -c 'sleep 12 & sleep 12'", timeout_seconds=5)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 10.0, "timeout must not wait for the orphaned background child")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual(alpha["exit_code"], 124)
        self.assertIn("timed out", alpha["test_output"])
        self.assertFalse(alpha["tests_passed"])
        self.assertEqual(alpha["score_breakdown"]["correctness"], 0.0)

    def test_failing_and_missing_commands_are_recorded_not_raised(self) -> None:
        res = self.evaluate(self.bid, test_command="sh -c 'echo failing; exit 3'")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual((alpha["exit_code"], alpha["status"], alpha["tests_passed"]), (3, "failed", False))
        self.assertEqual(res["winner_id"], "")
        self.assertEqual(res["bout"]["status"], "failed_all")

        res = self.evaluate(self.bid, test_command="definitely_not_a_command_xyz --flag")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual(alpha["exit_code"], 127)  # /bin/sh: command not found
        self.assertFalse(alpha["tests_passed"])
        self.assertIn("definitely_not_a_command_xyz", alpha["test_output"])

    def test_telemetry_overrides_duration_even_on_non_zero_exit(self) -> None:
        res = self.evaluate(self.bid, test_command="sh -c 'echo JOUST_BENCHMARK_MS=12.5; exit 1'")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual(alpha["duration_ms"], 12.5)
        self.assertFalse(alpha["tests_passed"])
        self.assertEqual(alpha["score_breakdown"]["correctness"], 0.0)
        self.assertLessEqual(alpha["score_breakdown"]["speed"], 5.0)

    def test_unparseable_telemetry_falls_back_to_wall_clock(self) -> None:
        res = self.evaluate(self.bid, test_command="sh -c 'echo JOUST_BENCHMARK_MS=1.2.3'")
        alpha = self.by_id(res)["alpha"]
        self.assertGreater(alpha["duration_ms"], 0.0)
        self.assertNotEqual(alpha["duration_ms"], 1.2)
        self.assertTrue(alpha["tests_passed"])

    def test_non_utf8_output_is_decoded_with_replacement(self) -> None:
        res = self.evaluate(self.bid, test_command="sh -c 'printf \"ok\\xff\\xfe\\x80 done\\n\"'")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual(alpha["exit_code"], 0)
        self.assertTrue(alpha["test_output"].startswith("ok"))
        self.assertTrue(alpha["test_output"].endswith("done"))
        self.assertIn("\ufffd", alpha["test_output"])
        json.dumps(res)  # payload stays JSON-serialisable

    def test_huge_output_is_capped_head_and_tail(self) -> None:
        res = self.evaluate(self.bid, test_command="sh -c 'head -c 20000 /dev/zero | tr \"\\0\" x; echo; echo TAIL_MARKER; echo JOUST_BENCHMARK_MS=42'")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual(alpha["exit_code"], 0)
        self.assertLessEqual(len(alpha["test_output"]), 8200)
        self.assertIn("[Joust truncated", alpha["test_output"])
        self.assertIn("TAIL_MARKER", alpha["test_output"])  # the tail (where failures print) survives
        self.assertEqual(alpha["duration_ms"], 42.0)  # telemetry parsed from the full output

    def test_evaluate_by_name_and_unknown_bout(self) -> None:
        bout = self.launch(self.repo, names=("Fast Path", "slow-path"))
        res = self.evaluate(bout["bout_id"], contender_id="Fast Path")
        self.assertEqual(self.by_id(res)["fast-path"]["status"], "passed")
        self.assertTrue(self.by_id(res)["fast-path"]["tests_passed"])
        unknown = json.loads(tools.joust_evaluate({"bout_id": "bout-ffffffff"}))
        self.assertFalse(unknown["ok"])
        self.assertIn("Unknown bout_id", unknown["error"])

    def test_nobody_passes_yields_no_winner_and_blocks_merge(self) -> None:
        res = self.evaluate(self.bid, test_command="false")
        self.assertEqual(res["winner_id"], "")
        self.assertEqual(res["bout"]["status"], "failed_all")
        for c in res["bout"]["contenders"]:
            self.assertFalse(c["is_winner"])
            self.assertEqual(c["score_breakdown"]["correctness"], 0.0)
            self.assertLessEqual(c["score"], 20.0)
        merged = self.merge(self.bid, "apply")
        self.assertFalse(merged["ok"])
        self.assertIn("no winner", merged["error"])

    def test_exact_tie_is_deterministic_by_launch_order(self) -> None:
        for wt in self.wt.values():
            (wt / "bench.sh").write_text("echo JOUST_BENCHMARK_MS=100\n", encoding="utf-8")
        for _ in range(2):
            res = self.evaluate(self.bid, test_command="sh bench.sh")
            ranked = res["bout"]["contenders"]
            self.assertEqual(ranked[0]["score"], ranked[1]["score"])
            self.assertEqual(res["winner_id"], "alpha")
            self.assertEqual(ranked[0]["contender_id"], "alpha")
            self.assertTrue(ranked[0]["is_winner"])
            self.assertFalse(ranked[1]["is_winner"])

    def test_scores_are_bounded_and_winner_is_top_ranked(self) -> None:
        bout = self.launch(self.repo, names=("tiny", "huge", "untouched"))
        wt = self.worktrees(bout)
        (wt["tiny"] / "calc.py").write_text("def add(a, b):\n    return a + b  # c\n", encoding="utf-8")
        (wt["huge"] / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(20000)), encoding="utf-8")
        res = self.evaluate(bout["bout_id"])
        ranked = res["bout"]["contenders"]
        self.assertEqual(res["winner_id"], ranked[0]["contender_id"])
        self.assertTrue(ranked[0]["is_winner"])
        for c in ranked:
            b = c["score_breakdown"]
            self.assertGreaterEqual(b["minimalism"], 0.0)
            self.assertLessEqual(b["minimalism"], 15.0)
            self.assertLessEqual(b["speed"], 25.0)
            self.assertGreaterEqual(b["speed"], 0.0)
            self.assertIn(b["correctness"], (0.0, 60.0))
            self.assertGreaterEqual(c["score"], 0.0)
            self.assertLessEqual(c["score"], 100.0)
            self.assertAlmostEqual(c["score"], round(b["correctness"] + b["speed"] + b["minimalism"], 2), places=2)
        stats = self.by_id(res)
        self.assertEqual(stats["untouched"]["score_breakdown"]["minimalism"], 5.0)  # zero diff: partial credit
        self.assertEqual(stats["tiny"]["score_breakdown"]["minimalism"], 15.0)
        self.assertGreater(stats["huge"]["score_breakdown"]["minimalism"], 0.0)
        self.assertLess(stats["huge"]["score_breakdown"]["minimalism"], 1.0)

    def test_timeout_seconds_zero_uses_default_and_bad_value_is_reported(self) -> None:
        res = json.loads(tools.joust_evaluate({"bout_id": self.bid, "timeout_seconds": 0, "test_command": "true"}))
        self.assertTrue(res["ok"], res)
        bad = json.loads(tools.joust_evaluate({"bout_id": self.bid, "timeout_seconds": "abc", "test_command": "true"}))
        self.assertFalse(bad["ok"])
        self.assertIn("abc", bad["error"])


# ====================================================================================================
# joust_launch: validation, naming, dirty trees, path forms, copytree fallback
# ====================================================================================================
class TestLaunchEdges(_EngineEdgeCase):
    def _launch_raw(self, repo_path: str, names: Iterable[str] = ("a", "b"), contenders: Any = None) -> Dict[str, Any]:
        return json.loads(
            tools.joust_launch(
                {
                    "title": "t",
                    "goal": "g",
                    "repo_path": repo_path,
                    "contenders": contenders if contenders is not None else [{"name": n, "strategy": "s"} for n in names],
                }
            )
        )

    def test_rejects_non_repo_unborn_repo_and_missing_path(self) -> None:
        not_git = self.tmp / "notgit"
        not_git.mkdir()
        res = self._launch_raw(str(not_git))
        self.assertFalse(res["ok"])
        self.assertIn("Not a Git repository", res["error"])

        unborn = self.tmp / "unborn"
        unborn.mkdir()
        _git(unborn, "init", "-q")
        res = self._launch_raw(str(unborn))
        self.assertFalse(res["ok"])
        self.assertIn("no commits", res["error"])

        res = self._launch_raw(str(self.tmp / "does-not-exist"))
        self.assertFalse(res["ok"])
        self.assertIn("does not exist", res["error"])
        self.assertEqual(eng.list_bouts()["total_bouts"], 0)

    def test_contender_count_is_bounded_to_2_through_4(self) -> None:
        repo = _make_repo(self.tmp)
        one = self._launch_raw(str(repo), names=("solo",))
        self.assertFalse(one["ok"])
        self.assertIn("2 to 4", one["error"])
        five = self._launch_raw(str(repo), names=tuple(f"c{i}" for i in range(5)))
        self.assertFalse(five["ok"])
        self.assertIn("at most 4", five["error"])
        bad = self._launch_raw(str(repo), contenders="alpha,beta")
        self.assertFalse(bad["ok"])
        self.assertEqual(eng.list_bouts()["total_bouts"], 0)
        self.assertEqual(len(_git(repo, "worktree", "list").stdout.strip().splitlines()), 1)

    def test_duplicate_and_blank_names_are_rejected_unicode_and_slashes_get_unique_ids(self) -> None:
        repo = _make_repo(self.tmp)
        dup = self._launch_raw(str(repo), names=("alpha", "alpha", "beta"))
        self.assertFalse(dup["ok"])
        self.assertIn("Duplicate contender name", dup["error"])
        blank = self._launch_raw(str(repo), names=("alpha", ""))
        self.assertFalse(blank["ok"])
        self.assertEqual(eng.list_bouts()["total_bouts"], 0)  # failed launches leave no phantom bout
        self.assertEqual(len(_git(repo, "worktree", "list").stdout.strip().splitlines()), 1)

        bout = self.launch(repo, names=("Hash Bucket", "hash-bucket", "日本語", "fast/track"))
        ids = {c["contender_id"]: c["name"] for c in bout["contenders"]}
        self.assertEqual(len(ids), 4)
        self.assertEqual(set(ids.values()), {"Hash Bucket", "hash-bucket", "日本語", "fast/track"})
        self.assertEqual(len({str(p) for p in self.worktrees(bout).values()}), 4)
        for cid in ids:
            self.assertRegex(cid, r"^[a-z0-9][a-z0-9_\-]*$")
    def test_dirty_tree_tracked_edits_carry_over_but_untracked_do_not(self) -> None:
        repo = _make_repo(self.tmp)
        (repo / "tracked2.txt").write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "more")
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # unstaged\n", encoding="utf-8")
        (repo / "tracked2.txt").write_text("v2 staged\n", encoding="utf-8")
        _git(repo, "add", "tracked2.txt")
        (repo / "staged_new.txt").write_text("new staged\n", encoding="utf-8")
        _git(repo, "add", "staged_new.txt")
        (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        bout = self.launch(repo)
        for wt in self.worktrees(bout).values():
            self.assertIn("unstaged", (wt / "calc.py").read_text(encoding="utf-8"))
            self.assertEqual((wt / "tracked2.txt").read_text(encoding="utf-8"), "v2 staged\n")
            self.assertTrue((wt / "staged_new.txt").is_file())
            # Current behaviour (undocumented limitation): untracked files are not mirrored.
            self.assertFalse((wt / "untracked.txt").exists())
            self.assertFalse((wt / ".joust_dirty.patch").exists())
        # main repo working tree is left exactly as it was
        self.assertEqual(
            sorted(_git(repo, "status", "--porcelain").stdout.splitlines()),
            sorted([" M calc.py", "A  staged_new.txt", "M  tracked2.txt", "?? untracked.txt"]),
        )

    def test_tilde_relative_and_subdirectory_repo_paths_resolve_to_repo_root(self) -> None:
        repo = _make_repo(self.tmp)
        (repo / "subdir").mkdir()
        old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
        try:
            os.environ["HOME"] = str(self.tmp)
            res = self._launch_raw("~/repo")
            self.assertTrue(res["ok"], res)
            self.assertEqual(Path(res["bout"]["repo_path"]), repo.resolve())
            os.chdir(self.tmp)
            res = self._launch_raw("repo")
            self.assertTrue(res["ok"], res)
            self.assertEqual(Path(res["bout"]["repo_path"]), repo.resolve())
        finally:
            os.chdir(old_cwd)
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
        res = self._launch_raw(str(repo / "subdir"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(Path(res["bout"]["repo_path"]), repo.resolve())

    def test_copytree_fallback_when_git_worktree_add_fails(self) -> None:
        repo = _make_repo(self.tmp)
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo / "ignored.txt").write_text("local only\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "gitignore")
        original = eng._run_git

        def failing_worktree_add(args, cwd, timeout=30):
            if list(args[:2]) == ["worktree", "add"]:
                return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated worktree add failure")
            return original(args, cwd, timeout)

        eng._run_git = failing_worktree_add
        try:
            bout = self.launch(repo)
        finally:
            eng._run_git = original

        wt = self.worktrees(bout)
        for p in wt.values():
            self.assertTrue((p / ".git").exists())
            self.assertEqual(Path(_git(p, "rev-parse", "--show-toplevel").stdout.strip()), p.resolve())
            self.assertEqual(_git(p, "status", "--porcelain").stdout, "")  # committed baseline, clean tree
            self.assertTrue((p / "ignored.txt").exists())  # copied, but .gitignore keeps it out of diffs
        self.assertEqual(len(_git(repo, "worktree", "list").stdout.strip().splitlines()), 1)

        (wt["alpha"] / "calc.py").write_text(_FIXED_CALC, encoding="utf-8")
        res = self.evaluate(bout["bout_id"])
        self.assertEqual(res["winner_id"], "alpha")
        alpha = self.by_id(res)["alpha"]
        self.assertEqual((alpha["files_changed"], alpha["insertions"], alpha["deletions"]), (1, 1, 1))
        self.assertTrue(self.merge(bout["bout_id"], "apply")["ok"])
        self.assertEqual((repo / "calc.py").read_text(encoding="utf-8"), _FIXED_CALC)
        self.assertFalse((repo / "ignored.txt").read_text(encoding="utf-8") != "local only\n")
        self.assertTrue(self.merge(bout["bout_id"], "rollback")["ok"])
        cleaned = self.merge(bout["bout_id"], "cleanup")
        self.assertTrue(cleaned["ok"])
        self.assertFalse(any(p.exists() for p in wt.values()))


# ====================================================================================================
# Middleware path jail
# ====================================================================================================
class TestPathJail(_EngineEdgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = _make_repo(self.tmp)
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "src" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "pkg")
        self.bout = self.launch(self.repo, names=("alpha", "beta"))
        self.wt = self.worktrees(self.bout)
        self.A, self.B = self.wt["alpha"], self.wt["beta"]
        self.sid = f"edge-sess-{os.getpid()}-{id(self)}"
        eng.bind_session_worktree(self.sid, self.bout["bout_id"], "alpha", str(self.A))

    def mw(self, tool: str, args: Any, sid: Optional[str] = None) -> Dict[str, Any]:
        return joust_pkg._on_tool_request(
            tool_name=tool, args=args, session_id=sid or self.sid, task_id="", tool_call_id="tc", turn_id="", api_request_id=""
        )

    def assert_inside_alpha(self, path: str) -> None:
        self.assertTrue(Path(path).is_relative_to(self.A.resolve()), path)
        self.assertNotEqual(Path(path).resolve(), self.A.resolve().parent)

    def test_symlink_pointing_outside_is_clamped(self) -> None:
        (self.A / "evil_link").symlink_to("/etc")
        out = self.mw("read_file", {"path": "evil_link/hosts"})["args"]["path"]
        self.assert_inside_alpha(out)
        self.assertNotEqual(Path(out).resolve(), Path("/etc/hosts"))
        with self.assertRaises(ValueError):
            eng._jail_path_inside_worktree("evil_link/hosts", self.A)

    def test_tilde_dotdot_and_absolute_system_paths_are_clamped(self) -> None:
        for raw in ("~/x.txt", "../../etc/passwd", "a/../../b", "/etc/passwd", "/", "~"):
            out = self.mw("write_file", {"path": raw, "content": "c"})
            self.assert_inside_alpha(out["args"]["path"])
            self.assertEqual(out["source"], "joust")

    def test_sibling_contender_worktree_is_not_reachable(self) -> None:
        out = self.mw("write_file", {"path": str(self.B / "calc.py"), "content": "pwn"})["args"]["path"]
        self.assert_inside_alpha(out)
        self.assertFalse(Path(out).is_relative_to(self.B.resolve()))
        out = self.mw("terminal", {"command": "ls", "workdir": str(self.B)})["args"]["workdir"]
        self.assert_inside_alpha(out)

    def test_main_repo_paths_map_into_the_worktree(self) -> None:
        out = self.mw("read_file", {"path": str(self.repo / "calc.py")})["args"]["path"]
        self.assertEqual(Path(out), (self.A / "calc.py").resolve())
        # Engine-level API with the main repo root preserves nested structure.
        rewritten, reason, violations = eng.rewrite_tool_args_for_worktree(
            "read_file", {"path": str(self.repo / "src" / "pkg" / "mod.py")}, str(self.A), str(self.repo)
        )
        self.assertEqual(violations, [])
        self.assertEqual(Path(rewritten["path"]), (self.A / "src" / "pkg" / "mod.py").resolve())
        self.assertIn("joust isolated read_file".lower(), (reason or "").lower())

    def test_relative_paths_and_dot_resolve_inside_worktree(self) -> None:
        self.assertEqual(Path(self.mw("read_file", {"path": "."})["args"]["path"]), self.A.resolve())
        self.assertEqual(Path(self.mw("search_files", {"pattern": "x", "path": "."})["args"]["path"]), self.A.resolve())
        self.assertEqual(
            Path(self.mw("patch", {"path": "calc.py", "old_string": "a", "new_string": "b"})["args"]["path"]),
            (self.A / "calc.py").resolve(),
        )
        out = self.mw("write_file", {"path": "src/pkg/new.py", "content": ""})["args"]["path"]
        self.assertEqual(Path(out), (self.A / "src" / "pkg" / "new.py").resolve())

    def test_terminal_workdir_is_pinned_and_cwd_key_rewritten(self) -> None:
        out = self.mw("terminal", {"command": "ls"})["args"]
        self.assertEqual(Path(out["workdir"]), self.A.resolve())
        self.assertEqual(out["command"], "ls")
        out = self.mw("terminal", {"command": "ls", "cwd": "/"})["args"]
        self.assert_inside_alpha(out["cwd"])
        self.assertNotIn("workdir", out)  # explicit cwd respected, no duplicate key injected

    def test_empty_none_non_string_and_non_dict_args_pass_through(self) -> None:
        for args in ({"path": ""}, {"path": None}, {"path": 5}, {"content": "no path key"}):
            # Nothing to rewrite -> None, so Hermes records no plugin rewrite for the call.
            self.assertIsNone(joust_pkg._on_tool_request(tool_name="read_file", args=dict(args), session_id=self.sid))
        self.assertIsNone(joust_pkg._on_tool_request(tool_name="read_file", args=None, session_id=self.sid))
        self.assertIsNone(joust_pkg._on_tool_request(tool_name="read_file", args=["x"], session_id=self.sid))

    def test_unbound_session_is_untouched(self) -> None:
        out = joust_pkg._on_tool_request(tool_name="write_file", args={"path": "../escape.py"}, session_id="nobody-bound")
        self.assertIsNone(out)

    def test_steer_write_files_refuses_escapes_and_writes_nothing_outside(self) -> None:
        res = json.loads(
            tools.joust_steer(
                {
                    "bout_id": self.bout["bout_id"],
                    "contender_id": "alpha",
                    "guidance": "g",
                    "write_files": {"../../escape.txt": "x", "/etc/joust_escape.txt": "y"},
                }
            )
        )
        self.assertFalse(res["ok"])
        self.assertIn("escapes isolated Joust worktree", res["error"])
        self.assertFalse((self.A.parent / "escape.txt").exists())
        self.assertFalse((self.A.parent.parent / "escape.txt").exists())
        self.assertFalse(Path("/etc/joust_escape.txt").exists())
        ok = json.loads(
            tools.joust_steer(
                {"bout_id": self.bout["bout_id"], "contender_id": "alpha", "guidance": "g", "write_files": {"ok/inner.txt": "z"}}
            )
        )
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["written_files"], ["ok/inner.txt"])
        self.assertEqual((self.A / "ok" / "inner.txt").read_text(encoding="utf-8"), "z")

    def test_worktree_path_with_trailing_slash(self) -> None:
        rewritten, _, _ = eng.rewrite_tool_args_for_worktree("read_file", {"path": "calc.py"}, str(self.A) + "/")
        self.assertEqual(Path(rewritten["path"]), (self.A / "calc.py").resolve())


# ====================================================================================================
# SQLite / data-dir handling
# ====================================================================================================
