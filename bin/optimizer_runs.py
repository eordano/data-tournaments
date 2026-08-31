"""Persistent registry for async optimizer runs.

The user clicks "Optimize", may close the page, and come back later. We need
durable state for the optimizer job so the UI can show:
  - status (running, done, error, canceled)
  - log (live tail or final)
  - result (e.g. candidate prompt version, score metric)

The runner writes to this table; the LiveView polls/reads from it.

Schema lives in the central fabric DB so Python and Elixir share it.

Table: optimizer_run
  id           PK
  domain       TEXT  (the domain being optimized)
  target       TEXT  ("judge" | "generator")
  rubric       TEXT  (e.g. "pair-wheel-v2")
  prompt_name  TEXT  (langfuse prompt name)
  status       TEXT  ("running" | "done" | "error" | "canceled")
  exit_code    INTEGER
  log          TEXT  (accumulated stdout/stderr lines, newline-joined)
  result       TEXT  (JSON: {"candidate_version": N, "metric": 0.8, ...})
  started_at   TEXT
  finished_at  TEXT
"""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS optimizer_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT    NOT NULL,
    target       TEXT    NOT NULL,
    rubric       TEXT,
    prompt_name  TEXT,
    status       TEXT    NOT NULL DEFAULT 'running',
    exit_code    INTEGER,
    log          TEXT    NOT NULL DEFAULT '',
    result       TEXT,
    started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_optimizer_run_domain ON optimizer_run(domain);
CREATE INDEX IF NOT EXISTS idx_optimizer_run_status ON optimizer_run(status);
"""

def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def init() -> None:
    """Create the optimizer_run table if missing. Idempotent."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)

def start(*, domain: str, target: str, rubric: str | None = None,
          prompt_name: str | None = None) -> int:
    """Create a new running optimizer-run row, return its id."""
    init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO optimizer_run(domain, target, rubric, prompt_name) "
            "VALUES (?, ?, ?, ?)",
            (domain, target, rubric, prompt_name),
        )
        return cur.lastrowid

def append_log(run_id: int, line: str) -> None:
    """Append one line to the run's log buffer."""
    with _connect() as conn:
        conn.execute(
            "UPDATE optimizer_run SET log = log || ? WHERE id = ?",
            (line + "\n", run_id),
        )

def finish(run_id: int, *, status: str, exit_code: int | None = None,
           result: dict | None = None) -> None:
    """Mark the run finished. status ∈ {done, error, canceled}."""
    if exit_code is None:
        exit_code = 0 if status == "done" else 1
    with _connect() as conn:
        conn.execute(
            "UPDATE optimizer_run "
            "SET status = ?, exit_code = ?, result = ?, "
            "    finished_at = datetime('now') "
            "WHERE id = ?",
            (status, exit_code, json.dumps(result) if result is not None else None, run_id),
        )

def get(run_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM optimizer_run WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_dict(row)

def list_for_domain(domain: str, *, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM optimizer_run WHERE domain = ? "
            "ORDER BY id DESC LIMIT ?",
            (domain, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

def latest(*, domain: str, target: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM optimizer_run WHERE domain = ? AND target = ? "
            "ORDER BY id DESC LIMIT 1",
            (domain, target),
        ).fetchone()
        return _row_to_dict(row)

def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except (TypeError, ValueError):
            pass
    return d
