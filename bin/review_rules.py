#!/usr/bin/env python3
"""Developer-opinion learning loop persistence (wave-8 B5).

ReviewRuleProposal -> human-gated promotion -> versioned ReviewRule
(docs/reviews/bugsweep-product-model.md §4.3). Proposals are MUTABLE rows
mined from the review-comment corpus; they carry attribution, OBSERVED
B/N enforcement class, ≥2 verbatim evidence quotes ("the rule is only as
strong as these"), exceptions, conflicts and dissent.

Lifecycle (grounded in the aug16/aug17 corpus; sticky terminals):

  draft -> evaluated -> versioned            (promotion collapses
  draft/evaluated -> rejected  (terminal)     approved+versioned into the
  versioned            (terminal)             single atomic promote() step)

* draft      — mined; quotes + counts attached; nothing downstream changes.
* evaluated  — back-tested (retro-apply / fixture-must-trip). Evaluation is
               EVIDENCE, not approval: it can never promote by itself.
* versioned  — a human principal promoted it. Promotion is the ONLY writer
               of review_rule and it (1) authorizes the principal via
               bin.approvals.authorize (fail-closed RBAC), (2) writes an
               approval_event audit row, (3) FREEZES rule_text/evidence/
               attribution/dissent into an immutable versioned review_rule
               row (UNIQUE(name, version); append-only triggers). Dissent
               is carried, never erased. Old versions remain — rollback is
               a repoint, not a delete.

Raw comments / frequency counts NEVER auto-promote: there is no code path
from create_proposal or record_evaluation to a review_rule row.

CLI is a debug aid mirroring the module functions:
  review_rules.py init
  review_rules.py create-proposal --from-json FILE
  review_rules.py list [--status S] [--category C]
  review_rules.py promote --id N --principal P --name NAME [--supersedes S]
  review_rules.py history --name NAME
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin.campaigns import _connect, _db_path, _row_to_dict  # shared conventions

# ── Taxonomy (§4.3) ───────────────────────────────────────────────────────

STATUSES = ("draft", "evaluated", "approved", "versioned", "rejected")
TERMINAL_STATUSES = ("versioned", "rejected")
BLOCKING_CLASSES = ("B", "N", "mixed")

_JSON_FIELDS = (
    "sub_forms",
    "top_enforcers",
    "evidence",
    "exceptions",
    "conflicts_with",
    "dissent",
    "mechanization",
    "application_targets",
)


def init() -> None:
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()


# ── Proposal CRUD (mutable while draft/evaluated) ─────────────────────────


def create_proposal(
    *,
    category: str,
    rule_text: str,
    evidence: list[dict],
    sub_forms: Optional[list] = None,
    approx_frequency: int = 0,
    window: str = "",
    blocking_class: str = "N",
    written_status: str = "unwritten",
    doc_pointer: str = "",
    top_enforcers: Optional[list] = None,
    exceptions: Optional[list] = None,
    conflicts_with: Optional[list] = None,
    dissent: Optional[list] = None,
    mechanization: Optional[dict] = None,
    application_targets: Optional[list] = None,
) -> int:
    """Create a draft proposal. REQUIRES ≥2 evidence quotes — the corpus
    rule: a proposal is only as strong as its evidence. Frequency counts
    without quotes are not a proposal."""
    if not category:
        raise ValueError("category is required")
    if not rule_text:
        raise ValueError("rule_text is required")
    if blocking_class not in BLOCKING_CLASSES:
        raise ValueError(
            f"blocking_class must be one of {BLOCKING_CLASSES}, got {blocking_class!r}"
        )
    if not isinstance(evidence, list) or len(evidence) < 2:
        raise ValueError(
            "a proposal requires >=2 evidence quotes — "
            "the rule is only as strong as its evidence"
        )
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO review_rule_proposal(category, rule_text, sub_forms, "
            "approx_frequency, window, blocking_class, written_status, "
            "doc_pointer, top_enforcers, evidence, exceptions, conflicts_with, "
            "dissent, mechanization, application_targets) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                category,
                rule_text,
                json.dumps(sub_forms or []),
                int(approx_frequency),
                window,
                blocking_class,
                written_status,
                doc_pointer,
                json.dumps(top_enforcers or []),
                json.dumps(evidence),
                json.dumps(exceptions or []),
                json.dumps(conflicts_with or []),
                json.dumps(dissent or []),
                json.dumps(mechanization or {}),
                json.dumps(application_targets or []),
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_proposal(proposal_id: int) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_rule_proposal WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"no review_rule_proposal with id {proposal_id}")
        return _row_to_dict(row, _JSON_FIELDS)


def list_proposals(
    *, status: Optional[str] = None, category: Optional[str] = None
) -> list[dict]:
    q = "SELECT * FROM review_rule_proposal WHERE 1=1"
    args: tuple = ()
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        q += " AND status=?"
        args += (status,)
    if category is not None:
        q += " AND category=?"
        args += (category,)
    q += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
        return [_row_to_dict(r, _JSON_FIELDS) for r in rows]


def record_evaluation(proposal_id: int, *, evaluated_by: str, result: str) -> None:
    """draft -> evaluated. Evaluation is EVIDENCE, not approval: it attaches
    a back-test reference + result but cannot promote. Only valid from
    draft (re-evaluating a settled or already-evaluated proposal is an
    error, not a silent overwrite)."""
    if not evaluated_by:
        raise ValueError("evaluated_by is required (judge run / back-test reference)")
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM review_rule_proposal WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"no review_rule_proposal with id {proposal_id}")
        if row["status"] != "draft":
            raise ValueError(
                f"record_evaluation requires status 'draft', got {row['status']!r}"
            )
        conn.execute(
            "UPDATE review_rule_proposal SET status='evaluated', evaluated_by=?, "
            "evaluation_result=?, updated_at=datetime('now') WHERE id=?",
            (evaluated_by, result, proposal_id),
        )
        conn.commit()


def reject(proposal_id: int, *, reason: str) -> None:
    """Sticky terminal rejection. Rejecting an already-terminal proposal is
    a silent no-op (workflow_runs / finding precedent)."""
    if not reason:
        raise ValueError("a rejection requires a reason")
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM review_rule_proposal WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"no review_rule_proposal with id {proposal_id}")
        if row["status"] in TERMINAL_STATUSES:
            return  # sticky terminal state
        conn.execute(
            "UPDATE review_rule_proposal SET status='rejected', "
            "evaluation_result=?, updated_at=datetime('now') WHERE id=?",
            (reason, proposal_id),
        )
        conn.commit()


# ── Promotion (the ONLY writer of review_rule) ────────────────────────────


def promote(
    proposal_id: int,
    *,
    principal: str,
    name: str,
    version: Optional[int] = None,
    supersedes: Optional[str] = None,
) -> dict:
    """Promote an EVALUATED proposal into an immutable versioned rule.

    Human-gated, fail-closed: bin.approvals.authorize(principal,
    'rule:<name>') must grant (ApprovalDenied propagates — no policy means
    NO promotions, not everyone-promotes). An approval_event audit row is
    written (workflow_id 'rule:<name>:v<version>'), then rule_text /
    evidence / attribution (top_enforcers) / dissent are FROZEN into the
    review_rule row and the proposal is marked 'versioned' (terminal).

    Corpus lifecycle: draft proposals must be evaluated FIRST (back-tested)
    — promote from draft raises. Terminal proposals cannot be promoted.
    Dissent is carried verbatim, never erased. version defaults to
    latest(name)+1; old versions remain (rollback = repoint).
    """
    from bin import approvals

    if not name:
        raise ValueError("name is required")
    prop = get_proposal(proposal_id)
    if prop["status"] != "evaluated":
        raise ValueError(
            "promote requires status 'evaluated' (evaluation is evidence, "
            f"and it must exist before approval), got {prop['status']!r}"
        )

    # AUTHORIZATION first — fail closed before any write.
    policy_id = approvals.authorize(principal, f"rule:{name}")

    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM review_rule WHERE name=?", (name,)
            ).fetchone()
            version = (row["v"] or 0) + 1
        version = int(version)

    # AUDIT before the rule row (approvals precedent: intent is recorded
    # even if the freeze then fails — operators reconcile from audit).
    workflow_id = f"rule:{name}:v{version}"
    event_id = approvals.record_event(
        workflow_id=workflow_id,
        decision=approvals.APPROVE,
        approver=principal,
        reason=f"promote review_rule_proposal {proposal_id} -> {name} v{version}",
        policy_id=policy_id,
    )

    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO review_rule(name, version, category, rule_text, "
                "evidence, attribution, dissent, proposal_id, approved_by, "
                "approval_event_id, supersedes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    version,
                    prop["category"],
                    prop["rule_text"],
                    json.dumps(prop["evidence"]),
                    json.dumps(prop["top_enforcers"]),
                    json.dumps(prop["dissent"]),
                    proposal_id,
                    principal,
                    event_id,
                    supersedes,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"review_rule {name!r} v{version} already exists")
        conn.execute(
            "UPDATE review_rule_proposal SET status='versioned', "
            "updated_at=datetime('now') WHERE id=?",
            (proposal_id,),
        )
        conn.commit()
        rule_id = cur.lastrowid
    return {
        "rule_id": rule_id,
        "name": name,
        "version": version,
        "approval_event_id": event_id,
        "policy_id": policy_id,
    }


# ── Rule reads ────────────────────────────────────────────────────────────


def get_rule(name: str, version: int) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_rule WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            raise LookupError(f"no review_rule {name!r} v{version}")
        return _row_to_dict(row, ("evidence", "attribution", "dissent"))


def active_rules() -> list[dict]:
    """Latest version per name (old versions remain; rollback = repoint)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT r.* FROM review_rule r JOIN ("
            "  SELECT name, MAX(version) AS v FROM review_rule GROUP BY name"
            ") latest ON r.name = latest.name AND r.version = latest.v "
            "ORDER BY r.name"
        ).fetchall()
        return [_row_to_dict(r, ("evidence", "attribution", "dissent")) for r in rows]


def rule_history(name: str) -> list[dict]:
    """All versions of a rule, oldest first (the supersedes chain)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM review_rule WHERE name=? ORDER BY version", (name,)
        ).fetchall()
        if not rows:
            raise LookupError(f"no review_rule named {name!r}")
        return [_row_to_dict(r, ("evidence", "attribution", "dissent")) for r in rows]


# ── CLI (debug aid; the real entry points are the importable functions) ──


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="review_rules.py", description=__doc__.splitlines()[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="apply the shared schema (idempotent)")

    sp = sub.add_parser("create-proposal")
    sp.add_argument(
        "--from-json", required=True,
        help="path to a JSON file with the proposal fields (§4.3 shape)",
    )
    sp = sub.add_parser("list")
    sp.add_argument("--status", choices=STATUSES)
    sp.add_argument("--category")
    sp = sub.add_parser("promote")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--principal", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", type=int)
    sp.add_argument("--supersedes")
    sp = sub.add_parser("history")
    sp.add_argument("--name", required=True)

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "init":
        init()
        print(f"schema applied at {_db_path()}")
    elif cmd == "create-proposal":
        fields = json.loads(Path(args.from_json).read_text())
        _print({"id": create_proposal(**fields)})
    elif cmd == "list":
        _print(list_proposals(status=args.status, category=args.category))
    elif cmd == "promote":
        _print(
            promote(
                args.id,
                principal=args.principal,
                name=args.name,
                version=args.version,
                supersedes=args.supersedes,
            )
        )
    elif cmd == "history":
        _print(rule_history(args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
