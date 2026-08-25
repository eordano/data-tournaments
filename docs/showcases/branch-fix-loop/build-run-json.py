#!/usr/bin/env python3
"""Assemble the machine-readable run.json for the branch-fix-loop showcase.

Reads live state (git SHAs, DB rows, artifacts) — never fabricates. Any
missing piece is recorded as {"status": "absent"} rather than invented.

Usage: build-run-json.py <data-home> <fixture-repo> <out-path>
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

HOME, REPO, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SHOW = os.path.join(ROOT, "docs/showcases/branch-fix-loop")
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=ENV).stdout.strip()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


run = {
    "showcase": "branch-fix-loop",
    "written_at": datetime.now(timezone.utc).isoformat(),
    "repo_head": git(ROOT, "rev-parse", "HEAD"),
    "data_home": HOME,
    "fixture_repo": {
        "path": REPO,
        "base_sha": git(REPO, "rev-parse", "main"),
        "branch_a": {"name": "fix/retry-deadline-reset", "sha": git(REPO, "rev-parse", "fix/retry-deadline-reset")},
        "branch_b": {"name": "fix/retry-token-clone", "sha": git(REPO, "rev-parse", "fix/retry-token-clone")},
        "merge_commits_base_to_a": git(REPO, "rev-list", "--merges", "main..fix/retry-deadline-reset") or "none",
        "merge_commits_base_to_b": git(REPO, "rev-list", "--merges", "main..fix/retry-token-clone") or "none",
    },
    "db": {},
    "artifacts": {},
    "screenshots": {},
}

db_path = os.path.join(HOME, "judgements.db")
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for table, key in (
        ("fix_branch", "branches"),
        ("fix_branch_validation", "validations"),
        ("fix_branch_review", "reviews"),
        ("approval_event", "approval_events"),
        ("workflow_run", "workflow_runs"),
    ):
        try:
            run["db"][key] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        except sqlite3.OperationalError as e:
            run["db"][key] = {"status": "absent", "error": str(e)}
else:
    run["db"] = {"status": "absent", "path": db_path}

for sub in ("artifacts", "shots"):
    d = os.path.join(SHOW, sub)
    if os.path.isdir(d):
        target = run["artifacts"] if sub == "artifacts" else run["screenshots"]
        for name in sorted(os.listdir(d)):
            target[name] = {"sha256": sha256_file(os.path.join(d, name))}

with open(OUT, "w") as f:
    json.dump(run, f, indent=2, default=str)
print("run.json ->", OUT)
