"""Crossfire, Flake Shield, kept-loser branches, bout reports, and model-vs-model metadata."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
from _plugin_loader import joust_pkg, tools, worktree_engine as eng  # noqa: E402

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=False)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    for cmd in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base")):
        _git(repo, *cmd)
    return repo


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-feat-")
        os.environ["HERMES_HOME"] = self._tmp.name
        self.repo = _make_repo(Path(self._tmp.name))
        tools.set_context(None)

    def tearDown(self) -> None:
        eng.close_cached_dbs()
        self._tmp.cleanup()

    def launch(self, names=("alpha", "beta"), **extra):
        contenders = [{"name": n, "strategy": f"{n} way", **extra.get(n, {})} for n in names]
        res = json.loads(tools.joust_launch({
            "title": "t", "goal": "g", "repo_path": str(self.repo), "spawn_subagents": False,
            "test_command": f"{sys.executable} -m unittest -q", "contenders": contenders,
        }))
        self.assertTrue(res["ok"], res)
        return res["bout"]

    def wt(self, bout, cid) -> Path:
        return Path(next(c["worktree_path"] for c in bout["contenders"] if c["contender_id"] == cid))


class TestCrossfire(_Base):
    def test_rivals_must_survive_each_others_tests(self) -> None:
        bout = self.launch()
        bid = bout["bout_id"]
        # alpha guards division by zero and ships a test for it; beta only passes the baseline suite
        (self.wt(bout, "alpha") / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return 0 if b == 0 else a / b\n", encoding="utf-8")
        (self.wt(bout, "alpha") / "test_div_guard.py").write_text(
            "import unittest\nfrom calc import div\n\nclass T(unittest.TestCase):\n    def test_div_zero(self):\n        self.assertEqual(div(1, 0), 0)\n",
            encoding="utf-8")
        (self.wt(bout, "beta") / "calc.py").write_text(
            "def add(a, b):\n    return b + a\n\n\ndef div(a, b):\n    return a / b\n", encoding="utf-8")
        (self.wt(bout, "beta") / "tests").mkdir()
        (self.wt(bout, "beta") / "tests" / "test_commutative.py").write_text(
            "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_comm(self):\n        self.assertEqual(add(1, 2), add(2, 1))\n",
            encoding="utf-8")

        judged = json.loads(tools.joust_evaluate({"bout_id": bid}))
        self.assertTrue(judged["ok"], judged)
        self.assertTrue(all(c["tests_passed"] for c in judged["bout"]["contenders"]))  # both pass their own suites

        res = json.loads(tools.joust_crossfire({"bout_id": bid}))
        self.assertTrue(res["ok"], res)
        by = {c["contender_id"]: c for c in res["bout"]["contenders"]}
        self.assertEqual((by["alpha"]["crossfire_survived"], by["alpha"]["crossfire_total"]), (1, 1))  # survives beta's volley
        self.assertEqual((by["beta"]["crossfire_survived"], by["beta"]["crossfire_total"]), (0, 1))  # dies to alpha's div guard test
        self.assertEqual(by["alpha"]["crossfire"]["test_files"], ["test_div_guard.py"])
        self.assertEqual(by["beta"]["crossfire"]["test_files"], ["tests/test_commutative.py"])
        self.assertFalse(by["beta"]["crossfire"]["defenses"]["alpha"]["passed"])
        self.assertEqual(res["winner_id"], "alpha")
        self.assertEqual(res["bout"]["contenders"][0]["contender_id"], "alpha")  # survival is the first tiebreak
        self.assertEqual(res["crossfire"]["volleys"], 2)
        # volleys leave no trace in the rival worktrees
        self.assertEqual([p.name for p in self.wt(bout, "beta").glob("*xfire*")], [])
        self.assertEqual([p.name for p in self.wt(bout, "alpha").rglob("*xfire*")], [])
        status = _git(self.wt(bout, "beta"), "status", "--porcelain").stdout
        self.assertNotIn("xfire", status)
        self.assertIn("calc.py", status)
        # and the merge still applies only the contender's own edits
        merged = json.loads(tools.joust_merge({"bout_id": bid, "action": "dry_run"}))
        self.assertTrue(merged["clean_apply"], merged)

    def test_crossfire_without_test_volleys_is_a_no_op(self) -> None:
        bout = self.launch()
        res = json.loads(tools.joust_crossfire({"bout_id": bout["bout_id"]}))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["crossfire"]["volleys"], 0)
        self.assertTrue(all(c["crossfire_total"] == 0 for c in res["bout"]["contenders"]))

    def test_crossfire_name_keeps_discovery_patterns(self) -> None:
        self.assertEqual(eng._crossfire_name("test_calc.py", "hash-bucket"), "test_calc__xfire_hash_bucket.py")
        self.assertEqual(eng._crossfire_name("src/calc.test.js", "Beta"), "src/calc__xfire_beta.test.js")
        self.assertEqual(eng._crossfire_name("tests/test_x", "a"), "tests/test_x__xfire_a")


class TestFlakeShield(_Base):
    def test_flaky_contender_cannot_win_and_runs_are_recorded(self) -> None:
        bout = self.launch()
        bid = bout["bout_id"]
        # beta's "tests" alternate pass/fail via a marker file; alpha is stable
        (self.wt(bout, "beta") / "flip.sh").write_text("if [ -f .flip ]; then rm .flip; exit 1; else touch .flip; exit 0; fi\n", encoding="utf-8")
        cmd = f"if [ -f flip.sh ]; then sh flip.sh; else {sys.executable} -m unittest -q; fi"
        res = json.loads(tools.joust_evaluate({"bout_id": bid, "runs": 3, "test_command": cmd}))
        self.assertTrue(res["ok"], res)
        by = {c["contender_id"]: c for c in res["bout"]["contenders"]}
        self.assertEqual(len(by["alpha"]["runs"]), 3)
        self.assertTrue(by["alpha"]["tests_passed"])
        self.assertFalse(by["alpha"]["flaky"])
        self.assertTrue(by["beta"]["flaky"])
        self.assertEqual(by["beta"]["status"], "flaky")
        self.assertFalse(by["beta"]["tests_passed"])
        self.assertIn("[Flake Shield]", by["beta"]["test_output"])
        self.assertEqual(res["winner_id"], "alpha")
        self.assertEqual(res["bout"]["contenders"][0]["contender_id"], "alpha")

    def test_median_duration_and_run_cap(self) -> None:
        bout = self.launch()
        cmd = "sh -c 'echo JOUST_BENCHMARK_MS=$(cat .n 2>/dev/null || echo 10); echo $(( $(cat .n 2>/dev/null || echo 10) + 100 )) > .n'"
        res = json.loads(tools.joust_evaluate({"bout_id": bout["bout_id"], "contender_id": "alpha", "runs": 99, "test_command": cmd}))
        self.assertTrue(res["ok"], res)
        alpha = {c["contender_id"]: c for c in res["bout"]["contenders"]}["alpha"]
        self.assertEqual(len(alpha["runs"]), eng.MAX_RUNS)  # 99 is clamped
        self.assertEqual(alpha["duration_ms"], 310.0)  # median of 10,110,...,610


class TestBranchesAndReport(_Base):
    def test_merge_keeps_every_contender_as_a_branch_and_report_renders(self) -> None:
        bout = self.launch(alpha={"model": "nousresearch/hermes-4-405b"}, beta={"model": "anthropic/claude-sonnet-4-5"})
        bid = bout["bout_id"]
        (self.wt(bout, "alpha") / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n# alpha\n", encoding="utf-8")
        (self.wt(bout, "beta") / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n# beta\n", encoding="utf-8")
        (self.wt(bout, "beta") / "notes.txt").write_text("untracked note\n", encoding="utf-8")
        json.loads(tools.joust_evaluate({"bout_id": bid}))
        merged = json.loads(tools.joust_merge({"bout_id": bid, "action": "apply", "contender_id": "alpha"}))
        self.assertTrue(merged["ok"], merged)
        self.assertEqual(set(merged["kept_branches"]), {"alpha", "beta"})
        branches = _git(self.repo, "branch", "--list", "joust/*").stdout
        self.assertIn(f"joust/{bid}/alpha", branches)
        self.assertIn(f"joust/{bid}/beta", branches)
        # the loser's branch carries its full state, including the untracked file; worktree HEAD did not move
        self.assertIn("notes.txt", _git(self.repo, "ls-tree", "--name-only", f"joust/{bid}/beta").stdout)
        self.assertEqual(_git(self.wt(bout, "beta"), "rev-parse", "HEAD").stdout.strip(), bout["base_commit"])
        self.assertIn("# alpha", (self.repo / "calc.py").read_text(encoding="utf-8"))

        rep = json.loads(tools.joust_report({"bout_id": bid, "write_path": "docs/joust-report.md", "include_markdown": True}))
        self.assertTrue(rep["ok"], rep)
        self.assertTrue((self.repo / "docs" / "joust-report.md").is_file())
        md = rep["markdown"]
        for needle in ("# ⚔️ Joust bout report", "| Rank |", "nousresearch/hermes-4-405b", "anthropic/claude-sonnet-4-5", f"joust/{bid}/beta", "## Timeline", "bout.merged"):
            self.assertIn(needle, md)
        by = {c["contender_id"]: c for c in eng.get_bout_details(bid)["contenders"]}
        self.assertEqual(by["beta"]["branch_ref"], f"joust/{bid}/beta")

