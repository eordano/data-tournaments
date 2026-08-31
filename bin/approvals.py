"""Approval gateway: the ONLY sanctioned path for approval/rejection Signals.

Wave 7 (RBAC + audit). Enforces, in order:

1. IDENTITY — the caller supplies an authenticated principal (Phoenix
   session identity or CLI --approver). Blank/unknown principals are
   rejected. This module cannot verify authentication itself; the CALLER
   (Phoenix controller, ops shell) is the authenticator — this module
   enforces authorization + audit.
2. AUTHORIZATION — the principal must appear in the approver allowlist of
   an ACTIVE policy row (kind='approval'). No active approval policy means
   NO approvals are possible (fail closed), not everyone-is-an-approver.
3. AUDIT — an approval_event row is written BEFORE the Signal is sent
   (append-only; UPDATE and DELETE are blocked by triggers). If the Signal
   send then fails, the audit row records intent and the error is raised —
   operators reconcile from the audit trail, which is exactly its job.
4. DELIVERY — the Temporal Signal is sent via an injected sender so this
   module stays importable without temporalio (root test suite) and Phoenix
   / CLI / tests share one enforcement path.

Agents NEVER call this module: the MCP server's signal_approval tool
hard-errors (spec T3); the tournament UI routes through Phoenix which
authenticates a human session.

Policy rule shape (policy.kind='approval', policy.rule JSON):
    {"approvers": ["changeme", "..."], "scope": "release:*"}
``scope`` is a glob matched against the temporal_workflow_id.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

APPROVE = "approved"
REJECT = "rejected"

class ApprovalDenied(Exception):
    """Principal missing, unknown, or not allowlisted for this workflow."""

def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def _active_approval_policies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT id, rule FROM policy WHERE kind='approval' AND status='active'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

def _scope_matches(workflow_id: str, scope: Any) -> bool:
    """Strict scope matching: only ``*`` and ``?`` glob syntax.

    A missing scope defaults to ``"*"`` (caller passes the default); a
    PRESENT but malformed scope must deny, never widen: non-string scopes
    and scopes using fnmatch features beyond * / ? (e.g. ``[abc]`` char
    classes) are rejected. fnmatch is deliberately not used directly — its
    char-class support is wider than the documented policy contract.
    """
    if not isinstance(scope, str) or not scope:
        return False
    if "[" in scope or "]" in scope:
        return False
    pattern = "^" + re.escape(scope).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(pattern, workflow_id) is not None

def _valid_approvers(approvers: Any) -> list[str]:
    """Return the approver allowlist iff well-formed, else [] (deny).

    Must be a list of non-empty strings. A bare string is rejected —
    ``principal in "changeme"`` is SUBSTRING matching and would grant
    'chan' against approver 'changeme' (hole found by the Elixir mirror
    implementation, verified live).
    """
    if not isinstance(approvers, list):
        return []
    if not all(isinstance(a, str) and a.strip() for a in approvers):
        return []
    return approvers

def authorize(principal: str, workflow_id: str) -> int:
    """Return the matching policy id, or raise ApprovalDenied.

    Fail-closed semantics: no principal, no active approval policy, or no
    policy whose scope matches this workflow and whose approvers include
    the principal -> denied. Malformed policy rows (non-dict rule, bare
    string approvers, non-string or unsupported-syntax scope) never grant
    and never crash the approval path.
    """
    principal = (principal or "").strip()
    if not principal:
        raise ApprovalDenied("no authenticated principal supplied")
    conn = _connect()
    try:
        policies = _active_approval_policies(conn)
    finally:
        conn.close()
    if not policies:
        raise ApprovalDenied(
            "no active approval policy exists — approvals fail closed; "
            "create one: bin/catalog.py create-policy --kind approval"
        )
    for row in policies:
        try:
            rule = json.loads(row["rule"])
        except (TypeError, ValueError):
            continue
        if not isinstance(rule, dict):
            continue
        scope = rule.get("scope", "*")
        approvers = _valid_approvers(rule.get("approvers", []))
        if _scope_matches(workflow_id, scope) and principal in approvers:
            return int(row["id"])
    raise ApprovalDenied(
        f"principal {principal!r} is not an allowlisted approver for "
        f"{workflow_id!r}"
    )

def record_event(
    *,
    workflow_id: str,
    decision: str,
    approver: str,
    reason: str,
    policy_id: Optional[int],
) -> int:
    """Append the audit row (immutable, delete-blocked). Returns row id."""
    if decision not in (APPROVE, REJECT):
        raise ValueError(f"decision must be {APPROVE!r} or {REJECT!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO approval_event(temporal_workflow_id, decision, "
            "approver, reason, policy_id) VALUES (?, ?, ?, ?, ?)",
            (workflow_id, decision, approver, reason, policy_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def list_events(workflow_id: str) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM approval_event WHERE temporal_workflow_id=? "
            "ORDER BY id",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def submit_decision(
    *,
    workflow_id: str,
    approved: bool,
    principal: str,
    reason: str = "",
    signal_sender: Optional[Callable[[str, bool, str, str], None]] = None,
) -> dict[str, Any]:
    """Authorize -> audit -> deliver. The one sanctioned entry point.

    ``signal_sender(workflow_id, approved, approver, reason)`` performs the
    actual Temporal Signal (bin.release_workflow.client.send_approval in
    production; a fake in tests). Audit is written BEFORE delivery — a
    failed send leaves the recorded intent for operator reconciliation.
    """
    policy_id = authorize(principal, workflow_id)
    decision = APPROVE if approved else REJECT
    event_id = record_event(
        workflow_id=workflow_id,
        decision=decision,
        approver=principal,
        reason=reason,
        policy_id=policy_id,
    )
    if signal_sender is None:
        from bin.release_workflow.client import send_approval

        signal_sender = send_approval
    outcome = signal_sender(workflow_id, approved, principal, reason)
    if inspect.iscoroutine(outcome):
        asyncio.run(outcome)
    return {"event_id": event_id, "decision": decision, "policy_id": policy_id}
