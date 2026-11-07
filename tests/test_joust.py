"""End-to-end unit and integration tests for the Joust Hermes plugin."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT.parent))

import Joust as joust_pkg  # noqa: E402
from Joust import tools, worktree_engine  # noqa: E402


class TestJoustPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="joust-test-")
        os.environ["HERMES_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        worktree_engine.close_cached_dbs()
        self._tmp.cleanup()

    def test_demo_bout_end_to_end_and_merge_rollback(self) -> None:
        raw = tools.joust_status({"run_demo": True})
        res = json.loads(raw)
        self.assertTrue(res["ok"], msg=raw)
        bout = res["bout"]
        bout_id = bout["bout_id"]
        self.assertEqual(bout["winner_id"], "hash-bucket-index")
        self.assertEqual(len(bout["contenders"]), 3)

        # Winner must rank #1 and pass all tests
        winner = bout["contenders"][0]
        self.assertEqual(winner["contender_id"], "hash-bucket-index")
        self.assertTrue(winner["tests_passed"])
        self.assertGreater(winner["score"], 85.0)

        # Contender #3 (naive-filter-scan) must fail the zero-qty ask test
        loser = [c for c in bout["contenders"] if c["contender_id"] == "naive-filter-scan"][0]
        self.assertFalse(loser["tests_passed"])

        # Inspect diffs
        diff_res = json.loads(tools.joust_diff({"bout_id": bout_id}))
        self.assertTrue(diff_res["ok"])
        self.assertIn("hash-bucket-index", diff_res["diffs"])
        self.assertIn("ask_buckets", diff_res["diffs"]["hash-bucket-index"]["diff"])

        # Dry-run merge
        dry = json.loads(tools.joust_merge({"bout_id": bout_id, "action": "dry_run"}))
        self.assertTrue(dry["ok"])
        self.assertTrue(dry["clean_apply"])

        # Apply merge to demo repository
        merged = json.loads(tools.joust_merge({"bout_id": bout_id, "action": "apply"}))
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["merged_contender_id"], "hash-bucket-index")
        repo_file = Path(merged["repo_path"]) / "order_matcher.py"
        self.assertIn("ask_buckets", repo_file.read_text(encoding="utf-8"))

        # Rollback merge and confirm original baseline is restored
        rolled = json.loads(tools.joust_merge({"bout_id": bout_id, "action": "rollback"}))
        self.assertTrue(rolled["ok"])
        self.assertNotIn("ask_buckets", repo_file.read_text(encoding="utf-8"))

        # Cleanup worktrees
        cleaned = json.loads(tools.joust_merge({"bout_id": bout_id, "action": "cleanup"}))
        self.assertTrue(cleaned["ok"])

    def test_middleware_worktree_path_isolation(self) -> None:
        wt_dir = Path(self._tmp.name) / "wt_alpha"
        wt_dir.mkdir(parents=True, exist_ok=True)
        worktree_engine.bind_session_worktree("sess-123", "bout-test", "alpha", str(wt_dir))

        out = joust_pkg._on_tool_request(
            tool_name="write_file",
            args={"path": "src/main.py", "content": "print(1)"},
            session_id="sess-123",
        )
        self.assertEqual(Path(out["args"]["path"]), (wt_dir / "src" / "main.py").resolve())
        self.assertEqual(out["source"], "joust")

        term_out = joust_pkg._on_tool_request(
            tool_name="terminal",
            args={"command": "pytest -q"},
            session_id="sess-123",
        )
        self.assertEqual(Path(term_out["args"]["workdir"]), wt_dir.resolve())


if __name__ == "__main__":
    unittest.main()
