"""LLM Tool Schemas for Joust (Parallel Git-Worktree Subagent Arena & Benchmark Judge)."""

from __future__ import annotations

JOUST_LAUNCH = {
    "name": "joust_launch",
    "description": (
        "Launch a new Joust bout in a Git repository. Creates isolated ephemeral Git worktrees "
        "for 2 to 4 parallel contenders (approaches/strategies) so each subagent or implementation "
        "can modify code in complete filesystem isolation without clobbering the main working tree. "
        "Optionally spawns parallel Hermes child subagents via ctx.subagent_lifecycle when called "
        "inside an active agent turn, and returns an inline ::joust-bout{id=\"...\"} directive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short human-readable title for the bout (e.g., 'Optimize JSON parser hot loop').",
            },
            "goal": {
                "type": "string",
                "description": "Detailed engineering specification or problem statement all contenders must solve.",
            },
            "repo_path": {
                "type": "string",
                "description": "Path to the target Git repository (defaults to the current session working directory).",
            },
            "test_command": {
                "type": "string",
                "description": (
                    "Verification or benchmark command run inside each contender's worktree to score correctness "
                    "and speed (e.g., 'pytest -q', 'python3 -m unittest', 'npm test')."
                ),
            },
            "contenders": {
                "type": "array",
                "description": "2 to 4 contender specifications representing distinct implementation strategies.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short slug/label for this contender (e.g., 'lru-cache', 'bisect-index').",
                        },
                        "strategy": {
                            "type": "string",
                            "description": "Specific architectural approach or instructions for this contender.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model override for this contender when spawning a subagent.",
                        },
                    },
                    "required": ["name", "strategy"],
                },
            },
            "spawn_subagents": {
                "type": "boolean",
                "description": (
                    "If true and an active parent session exists, automatically launches a parallel child subagent "
                    "for each contender via ctx.subagent_lifecycle (default: true)."
                ),
            },
        },
        "required": ["title", "goal", "contenders"],
    },
}

JOUST_EVALUATE = {
    "name": "joust_evaluate",
    "description": (
        "Run the verification and benchmark judge across all contenders (or a single contender) in a Joust bout. "
        "Executes the bout's test_command inside each isolated Git worktree, measures wall-clock execution time (ms), "
        "computes diff size (files changed, insertions, deletions), calculates a composite Joust Score (0-100), "
        "and ranks the winning contender."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bout_id": {
                "type": "string",
                "description": "The Joust bout ID (e.g., 'bout-a1b2c3d4').",
            },
            "contender_id": {
                "type": "string",
                "description": "Optional specific contender ID/name to evaluate; omit to evaluate all contenders.",
            },
            "test_command": {
                "type": "string",
                "description": "Optional override for the verification/benchmark command.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout in seconds per contender benchmark run (default: 60).",
            },
        },
        "required": ["bout_id"],
    },
}

JOUST_DIFF = {
    "name": "joust_diff",
    "description": (
        "Inspect unified Git diffs and file-level change summaries for each contender worktree in a Joust bout "
        "compared against the bout's base commit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bout_id": {
                "type": "string",
                "description": "The Joust bout ID.",
            },
            "contender_id": {
                "type": "string",
                "description": "Optional specific contender ID/name to diff; omit to return diffs for all contenders.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters of unified diff to return per contender (default: 8000).",
            },
        },
        "required": ["bout_id"],
    },
}

JOUST_STEER = {
    "name": "joust_steer",
    "description": (
        "Steer a contender in an active Joust bout. If the contender has a running child subagent, forwards "
        "the steering text to ctx.subagent_lifecycle.steer(handle, guidance); also logs the steering note "
        "to the contender's audit trail and optionally applies a file patch inside that contender's worktree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bout_id": {
                "type": "string",
                "description": "The Joust bout ID.",
            },
            "contender_id": {
                "type": "string",
                "description": "The contender ID or name to steer.",
            },
            "guidance": {
                "type": "string",
                "description": "Steering instruction or refinement guidance for the contender.",
            },
            "write_files": {
                "type": "object",
                "description": (
                    "Optional mapping of relative file paths to file contents to write directly inside "
                    "this contender's isolated worktree before re-evaluating."
                ),
            },
        },
        "required": ["bout_id", "contender_id", "guidance"],
    },
}

JOUST_MERGE = {
    "name": "joust_merge",
    "description": (
        "Apply the winning (or selected) contender's worktree changes cleanly into the main repository. "
        "Automatically saves a reversible backup patch in plugin-data/joust/backups before applying, "
        "supports dry-run conflict checks, and can also rollback a previous merge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bout_id": {
                "type": "string",
                "description": "The Joust bout ID.",
            },
            "contender_id": {
                "type": "string",
                "description": "Contender ID/name to merge (defaults to the highest-ranked passing winner).",
            },
            "action": {
                "type": "string",
                "enum": ["apply", "dry_run", "rollback", "cleanup"],
                "description": (
                    "'apply' merges the contender's diff into the base repo; 'dry_run' checks if the patch "
                    "applies cleanly without modifying files; 'rollback' reverses a previously applied merge; "
                    "'cleanup' removes ephemeral worktrees for the bout."
                ),
            },
        },
        "required": ["bout_id"],
    },
}

JOUST_STATUS = {
    "name": "joust_status",
    "description": (
        "Get the status, leaderboard, contender metrics, or run a self-contained live demo tournament "
        "for Joust."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bout_id": {
                "type": "string",
                "description": "Optional specific bout ID to inspect; omit to list recent bouts and aggregate stats.",
            },
            "run_demo": {
                "type": "boolean",
                "description": (
                    "If true, creates a self-contained demo Git repo in plugin-data/joust/demo, launches a "
                    "3-contender optimization tournament, writes the 3 competing implementations into their "
                    "isolated worktrees, benchmarks all 3, and returns the scored bout."
                ),
            },
        },
    },
}

ALL_SCHEMAS = [
    JOUST_LAUNCH,
    JOUST_EVALUATE,
    JOUST_DIFF,
    JOUST_STEER,
    JOUST_MERGE,
    JOUST_STATUS,
]
