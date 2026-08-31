"""workflow_run: queryable projection of Temporal release-workflow state.

ADR 0001 §4 step 6. Temporal is the SOURCE OF TRUTH for execution state;
this table exists so the LiveView audit UI can list/inspect runs with plain
SQL joins. Written ONLY by Python Temporal Activities (never by workflow
bodies — those must stay deterministic — and never by Elixir), mirroring the
optimizer_run precedent (bin/optimizer_runs.py).

If this projection is ever lost or corrupted it can be rebuilt from
Temporal's own history via the frontend API; nothing here is authoritative.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

TERMINAL_STATUSES = ("done", "failed", "canceled", "rolled-back")
STATUSES = ("running", "awaiting-approval") + TERMINAL_STATUSES

def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def init() -> None:
    """Ensure the schema (incl. workflow_run) exists. Idempotent."""
    from bin import catalog

    catalog.init()

def start(
    *,
    temporal_workflow_id: str,
    temporal_run_id: str,
    spec_digest: Optional[str] = None,
    environment_id: Optional[int] = None,
    detail: Optional[dict[str, Any]] = None,
) -> int:
    """Record a new run. Idempotent on (workflow_id, run_id): re-recording
    the same Temporal execution returns the existing row id — Activities
    retry, and a retried record-start must not mint duplicate rows."""
    init()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM workflow_run "
            "WHERE temporal_workflow_id=? AND temporal_run_id=?",
            (temporal_workflow_id, temporal_run_id),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO workflow_run(spec_digest, temporal_workflow_id, "
            "temporal_run_id, environment_id, detail) VALUES (?, ?, ?, ?, ?)",
            (
                spec_digest,
                temporal_workflow_id,
                temporal_run_id,
                environment_id,
                json.dumps(detail or {}),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

def record_stage(
    run_id: int, *, stage: str, status: str, detail: Optional[dict[str, Any]] = None
) -> None:
    """Append one entry to the run's append-only stage history.

    History is never rewritten: each call appends {stage, status, at}. The
    run's coarse status is NOT changed here — use set_status for that.
    """
    entry = {"stage": stage, "status": status, "at": _now()}
    if detail:
        entry["detail"] = detail
    with _connect() as conn:
        row = conn.execute(
            "SELECT stage_history FROM workflow_run WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"workflow_run id {run_id} not found")
        history = json.loads(row["stage_history"])
        history.append(entry)
        conn.execute(
            "UPDATE workflow_run SET stage_history=? WHERE id=?",
            (json.dumps(history), run_id),
        )
        conn.commit()

def set_status(
    run_id: int, status: str, *, detail: Optional[dict[str, Any]] = None
) -> None:
    """Set the coarse run status; stamps finished_at on terminal statuses.

    Terminal statuses are sticky: a projection update arriving late (retried
    activity) cannot flip done/failed/rolled-back back to running — mirrors
    the judgement.py done-flip guard.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, detail FROM workflow_run WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"workflow_run id {run_id} not found")
        if row["status"] in TERMINAL_STATUSES:
            return
        merged = json.loads(row["detail"])
        if detail:
            merged.update(detail)
        if status in TERMINAL_STATUSES:
            conn.execute(
                "UPDATE workflow_run SET status=?, detail=?, finished_at=? "
                "WHERE id=?",
                (status, json.dumps(merged), _now(), run_id),
            )
        else:
            conn.execute(
                "UPDATE workflow_run SET status=?, detail=? WHERE id=?",
                (status, json.dumps(merged), run_id),
            )
        conn.commit()

def get(run_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_run WHERE id=?", (run_id,)
        ).fetchone()
    return _to_dict(row) if row else None

def get_by_workflow_id(temporal_workflow_id: str) -> list[dict[str, Any]]:
    """All runs for a workflow id (retries/continue-as-new mint new run ids),
    newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_run WHERE temporal_workflow_id=? "
            "ORDER BY id DESC",
            (temporal_workflow_id,),
        ).fetchall()
    return [_to_dict(r) for r in rows]

def list_runs(
    *, status: Optional[str] = None, limit: int = 50
) -> list[dict[str, Any]]:
    q = "SELECT * FROM workflow_run"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY id DESC LIMIT ?"
    args += (int(limit),)
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_to_dict(r) for r in rows]

def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["detail"] = json.loads(d["detail"])
    d["stage_history"] = json.loads(d["stage_history"])
    return d
