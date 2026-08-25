#!/usr/bin/env python3
"""Branch-fix spine: SHA-bound persistence for per-branch fixes (wave-9 B1).

Every fix lives on ONE branch; every validation/review row binds to ONE
exact head SHA. Validation of an aggregate/merged tree is impossible by
construction:

* register_branch resolves base (merge-base with the default branch, or an
  explicit base ref) and head via git, computes patch_digest = sha256 of
  ``git diff base..head``, and REJECTS merge commits in the base..head
  range (``git rev-list --merges`` must be empty).
* refresh_head re-reads the branch tip; a changed head marks the branch
  'stale' (unless terminal shipped/rejected) — prior validation/review
  rows reference the OLD SHA and no longer apply.
* record_validation refuses writes whose tested_sha no longer matches the
  registered head (staleness guard at write time).
* current_validation returns only rows whose tested_sha matches the
  CURRENT head — a head change strands old rows by construction.
* record_review: approve REQUIRES a current passed validation and goes
  through bin.approvals.authorize (fail-closed RBAC) + an approval_event
  audit row (review_rules.promote precedent). ApprovalDenied propagates.

Row families (schema: bin/judgement_schema.sql, applied by ``init()``):
* fix_branch — mutable registration row (status accretes).
* fix_branch_validation, fix_branch_review — append-only histories
  (BEFORE UPDATE/DELETE triggers RAISE(ABORT); approval_event precedent).

CLI is a debug aid mirroring the module functions:
  fix_branches.py register --repo R --branch B [--base REF] [...]
  fix_branches.py list [--status S]
  fix_branches.py get --id N
  fix_branches.py refresh --id N
  fix_branches.py validate --id N --red-cmd C --green-cmd C [--guard-cmd C]
  fix_branches.py review --id N --reviewer P --decision approve [...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

STATUSES = (
    "registered",
    "validating",
    "validated",
    "failed",
    "stale",
    "approved",
    "rejected",
    "shipping",
    "shipped",
    "rolled-back",
)
TERMINAL_STATUSES = ("shipped", "rejected")
# Statuses a validation write must never clobber: terminal ones, plus
# 'shipping' — a release is in flight; its projection (sync_completion)
# owns the next transition, not a late validation row.
_STATUS_LOCKED = TERMINAL_STATUSES + ("shipping",)
DECISIONS = ("approve", "reject", "needs-changes")


# ── Paths / connection (bin/campaigns.py conventions) ─────────────────────


def _data_home() -> Path:
    return Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))


def _db_path() -> Path:
    return _data_home() / "judgements.db"


class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block also CLOSES on exit
    (see bin/catalog.py for the fd-exhaustion rationale)."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)  # commit / rollback
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> None:
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()


# ── Git plumbing ──────────────────────────────────────────────────────────


def _git_env() -> dict:
    """Hermetic git: no user/system config can change behaviour."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _git(repo_path: str, *args: str) -> str:
    """Run a git command in ``repo_path``; return stripped stdout.

    Raises ValueError with the git stderr on failure — callers surface
    actionable messages ('unknown branch', 'not a git repo', ...).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _default_branch(repo_path: str) -> str:
    """The repo's default branch: origin/HEAD if set, else init.defaultBranch
    fallback probing of common names, else the current HEAD's branch."""
    try:
        ref = _git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.rsplit("/", 1)[-1]
    except ValueError:
        pass
    for name in ("main", "master"):
        try:
            _git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
            return name
        except ValueError:
            continue
    # Last resort: whatever HEAD points at.
    return _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def _resolve_sha(repo_path: str, ref: str) -> str:
    return _git(repo_path, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _assert_no_merges(repo_path: str, base_sha: str, head_sha: str) -> None:
    merges = _git(repo_path, "rev-list", "--merges", f"{base_sha}..{head_sha}")
    if merges:
        raise ValueError(
            f"merge commits in {base_sha[:12]}..{head_sha[:12]} — branch fixes "
            f"must be linear (rebase, don't merge): {merges.splitlines()}"
        )


def _diffs_dir() -> Path:
    return _data_home() / "branch-diffs"


def _patch_digest(repo_path: str, base_sha: str, head_sha: str) -> str:
    """sha256 of ``git diff base..head`` (the exact patch content).

    Also stores the diff text content-addressed at
    $DATA_TOURNAMENTS_HOME/branch-diffs/<digest>.patch (mkdir parents;
    overwrite-safe because the filename IS the content hash) so the UI /
    reviewers can render the patch without touching the repo (wave-10 V2).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "diff", f"{base_sha}..{head_sha}"],
        capture_output=True,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git diff failed in {repo_path}: {proc.stderr.decode(errors='replace')}"
        )
    digest = hashlib.sha256(proc.stdout).hexdigest()
    diffs = _diffs_dir()
    diffs.mkdir(parents=True, exist_ok=True)
    (diffs / f"{digest}.patch").write_bytes(proc.stdout)
    return digest


def _changed_files(repo_path: str, base_sha: str, head_sha: str) -> list[dict]:
    """``git diff --name-status base..head`` as [{'status', 'path'}, ...].
    Best-effort: an unreadable repo yields [] (the DB record still stands)."""
    try:
        out = _git(repo_path, "diff", "--name-status", f"{base_sha}..{head_sha}")
    except ValueError:
        return []
    files: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        # rename/copy lines are 'R100\told\tnew' — the LAST field is the
        # path as it exists at head.
        files.append({"status": parts[0], "path": parts[-1]})
    return files


# ── Registration / refresh ────────────────────────────────────────────────


UNRESOLVED_REF_STAMP = "unresolved-ref"


def _workorder_ref_resolves(conn: sqlite3.Connection, ref: str) -> bool:
    """True when ``ref`` resolves inside THIS judgements.db: an int-like
    pending_judgement id, an existing finding slug, or a domain name."""
    ref = str(ref).strip()
    if not ref:
        return False
    try:
        pending_id = int(ref)
    except ValueError:
        pending_id = None
    if pending_id is not None:
        row = conn.execute(
            "SELECT 1 FROM pending_judgement WHERE id=?", (pending_id,)
        ).fetchone()
        if row is not None:
            return True
    if conn.execute("SELECT 1 FROM finding WHERE slug=?", (ref,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM domain WHERE name=?", (ref,)).fetchone():
        return True
    return False


def _resolve_lineage(
    conn: sqlite3.Connection,
    *,
    workorder_ref: Optional[str],
    finding: Optional[int],
    allow_unresolved: bool,
) -> Optional[str]:
    """Fail-closed lineage resolution (wave-11 W2).

    * ``finding`` (when provided) must exist in the finding table — always,
      the escape hatch does not cover it.
    * ``workorder_ref`` (when provided) must resolve to a pending_judgement
      id (int-like), a finding slug, or a domain name in the SAME DB;
      otherwise ValueError.
    * ``allow_unresolved=True`` (exploratory escape hatch): an unresolved
      ref is accepted but stamped '<UNRESOLVED_REF_STAMP>:<ref>' so lineage
      is honestly marked, never silently passed.

    Returns the workorder_ref value to STORE (possibly stamped).
    """
    if finding is not None:
        row = conn.execute(
            "SELECT 1 FROM finding WHERE id=?", (finding,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"finding {finding} does not exist in the finding table"
            )
    if workorder_ref is None:
        return None
    if _workorder_ref_resolves(conn, workorder_ref):
        return workorder_ref
    if allow_unresolved:
        return f"{UNRESOLVED_REF_STAMP}:{workorder_ref}"
    raise ValueError(
        f"workorder_ref does not resolve: {workorder_ref!r} is not a "
        "pending_judgement id, finding slug, or domain name in this DB "
        "(pass allow_unresolved=True / --allow-unresolved for exploratory "
        "use — the ref will be stamped 'unresolved-ref')"
    )


def register_branch(
    repo_path: str,
    branch_name: str,
    *,
    base: Optional[str] = None,
    finding: Optional[int] = None,
    workorder_ref: Optional[str] = None,
    allow_unresolved: bool = False,
) -> int:
    """Register a fix branch: resolve base + head SHAs via git, compute the
    patch digest, reject merge commits in base..head. Returns the row id.

    ``base`` is an explicit base ref; default is the merge-base of the
    branch with the repo's default branch.

    Lineage is fail-closed (wave-11 W2): a provided ``workorder_ref`` must
    resolve to a pending_judgement id, finding slug, or domain name in this
    DB (ValueError otherwise); a provided ``finding`` must exist.
    ``allow_unresolved=True`` accepts a dangling ref but stamps it
    'unresolved-ref:<ref>'.
    """
    repo_path = str(Path(repo_path).resolve())
    head_sha = _resolve_sha(repo_path, branch_name)
    base_ref = base if base is not None else _default_branch(repo_path)
    base_tip = _resolve_sha(repo_path, base_ref)
    base_sha = _git(repo_path, "merge-base", base_tip, head_sha)
    _assert_no_merges(repo_path, base_sha, head_sha)
    digest = _patch_digest(repo_path, base_sha, head_sha)
    with _connect() as conn:
        stored_ref = _resolve_lineage(
            conn,
            workorder_ref=workorder_ref,
            finding=finding,
            allow_unresolved=allow_unresolved,
        )
        try:
            cur = conn.execute(
                "INSERT INTO fix_branch(finding_id, workorder_ref, repo_path, "
                "branch_name, base_sha, head_sha, patch_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (finding, stored_ref, repo_path, branch_name,
                 base_sha, head_sha, digest),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"branch {branch_name!r} in {repo_path!r} is already registered"
            )
        conn.commit()
        return cur.lastrowid


def _get_row(conn: sqlite3.Connection, fix_branch_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM fix_branch WHERE id=?", (fix_branch_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no fix_branch with id {fix_branch_id}")
    return row


def refresh_head(fix_branch_id: int) -> dict:
    """Re-read the branch tip. If it moved (amend/force-push/new commits):
    re-resolve base + patch digest, re-check the no-merge rule, and mark
    the branch 'stale' (unless terminal shipped/rejected). Prior
    validation/review rows reference the old SHA and no longer apply."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
    new_head = _resolve_sha(row["repo_path"], row["branch_name"])
    if new_head == row["head_sha"]:
        return get_branch(fix_branch_id)
    # Re-anchor the base: prefer the merge-base with the default branch
    # (handles rebases onto a newer default); fall back to the old base
    # (handles amends in repos without a resolvable default).
    try:
        default_tip = _resolve_sha(row["repo_path"], _default_branch(row["repo_path"]))
        base_sha = _git(row["repo_path"], "merge-base", default_tip, new_head)
    except ValueError:
        base_sha = _git(row["repo_path"], "merge-base", row["base_sha"], new_head)
    _assert_no_merges(row["repo_path"], base_sha, new_head)
    digest = _patch_digest(row["repo_path"], base_sha, new_head)
    with _connect() as conn:
        sets = ["head_sha=?", "base_sha=?", "patch_digest=?",
                "updated_at=datetime('now')"]
        args: list[Any] = [new_head, base_sha, digest]
        if row["status"] not in _STATUS_LOCKED:
            sets.append("status='stale'")
        args.append(fix_branch_id)
        conn.execute(f"UPDATE fix_branch SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    return get_branch(fix_branch_id)


# ── Validation rows (append-only; SHA-bound) ──────────────────────────────


def record_validation(
    fix_branch_id: int,
    tested_sha: str,
    *,
    passed: bool,
    red_cmd: Optional[str] = None,
    red_intended: Optional[int] = None,
    red_observed: Optional[int] = None,
    green_cmd: Optional[str] = None,
    green_total: Optional[int] = None,
    green_passed: Optional[int] = None,
    guard_total: Optional[int] = None,
    guard_passed: Optional[int] = None,
    log_digest: Optional[str] = None,
) -> int:
    """Append one validation run bound to ``tested_sha``. REFUSED when
    tested_sha no longer matches the registered head (staleness guard at
    write time — a validation of a moved branch never lands). Also updates
    fix_branch.status to validated/failed."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        if tested_sha != row["head_sha"]:
            raise ValueError(
                f"tested_sha {tested_sha[:12]} does not match current head "
                f"{row['head_sha'][:12]} for fix_branch {fix_branch_id} — "
                "the branch moved; refresh and re-validate"
            )
        cur = conn.execute(
            "INSERT INTO fix_branch_validation(fix_branch_id, tested_sha, "
            "red_cmd, red_intended, red_observed, green_cmd, green_total, "
            "green_passed, guard_total, guard_passed, passed, log_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fix_branch_id,
                tested_sha,
                red_cmd,
                red_intended,
                red_observed,
                green_cmd,
                green_total,
                green_passed,
                guard_total,
                guard_passed,
                1 if passed else 0,
                log_digest,
            ),
        )
        if row["status"] not in _STATUS_LOCKED:
            conn.execute(
                "UPDATE fix_branch SET status=?, updated_at=datetime('now') "
                "WHERE id=?",
                ("validated" if passed else "failed", fix_branch_id),
            )
        conn.commit()
        return cur.lastrowid


def current_validation(fix_branch_id: int) -> Optional[dict]:
    """Latest validation row bound to the CURRENT head_sha, else None.
    Rows referencing an old SHA are stranded by construction."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        v = conn.execute(
            "SELECT * FROM fix_branch_validation "
            "WHERE fix_branch_id=? AND tested_sha=? ORDER BY id DESC LIMIT 1",
            (fix_branch_id, row["head_sha"]),
        ).fetchone()
        return dict(v) if v is not None else None


# ── Review rows (append-only; approve is RBAC-gated) ──────────────────────


def record_review(
    fix_branch_id: int,
    *,
    reviewer: str,
    decision: str,
    rationale: str = "",
) -> dict:
    """Append one review decision bound to the branch's current head.

    * ``approve`` REQUIRES a current (head-matching) validation row with
      passed=1 (ValueError otherwise), then goes through
      bin.approvals.authorize(reviewer, 'branchfix:<branch>') — fail-closed
      RBAC, ApprovalDenied propagates — and writes an approval_event audit
      row (workflow_id 'branchfix:<branch>:<head12>') BEFORE the review row.
      Sets status='approved' and stores approval_event_id.
    * ``reject`` is allowed anytime; sets status='rejected' (terminal).
    * ``needs-changes`` is allowed anytime; status is left as-is.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
    head_sha = row["head_sha"]
    branch_name = row["branch_name"]
    approval_event_id: Optional[int] = None

    if decision == "approve":
        cv = current_validation(fix_branch_id)
        if cv is None or cv["passed"] != 1 or cv["tested_sha"] != head_sha:
            raise ValueError(
                f"approve requires a passed validation of the CURRENT head "
                f"{head_sha[:12]} for fix_branch {fix_branch_id}; none exists "
                "(validate the branch, or refresh if it moved)"
            )
        from bin import approvals

        # AUTHORIZATION first — fail closed before any write
        # (review_rules.promote precedent). ApprovalDenied propagates.
        policy_id = approvals.authorize(reviewer, f"branchfix:{branch_name}")
        # AUDIT before the review row: intent is recorded even if the
        # review insert then fails — operators reconcile from audit.
        approval_event_id = approvals.record_event(
            workflow_id=f"branchfix:{branch_name}:{head_sha[:12]}",
            decision=approvals.APPROVE,
            approver=reviewer,
            reason=rationale or f"approve fix_branch {fix_branch_id}",
            policy_id=policy_id,
        )

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fix_branch_review(fix_branch_id, tested_sha, "
            "reviewer, decision, rationale, approval_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fix_branch_id, head_sha, reviewer, decision, rationale,
             approval_event_id),
        )
        if decision == "approve":
            conn.execute(
                "UPDATE fix_branch SET status='approved', "
                "updated_at=datetime('now') WHERE id=?",
                (fix_branch_id,),
            )
        elif decision == "reject":
            conn.execute(
                "UPDATE fix_branch SET status='rejected', "
                "updated_at=datetime('now') WHERE id=?",
                (fix_branch_id,),
            )
        # needs-changes: status left as-is (typically 'validated').
        conn.commit()
        review_id = cur.lastrowid
    return {
        "review_id": review_id,
        "decision": decision,
        "tested_sha": head_sha,
        "approval_event_id": approval_event_id,
    }


# ── Queries ───────────────────────────────────────────────────────────────


def mark_shipped(fix_branch_id: int) -> None:
    """Flip an APPROVED branch to terminal 'shipped' (called by the ship
    gateway AFTER the release client accepted the start). Guarded: only an
    'approved' row can become 'shipped'."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        if row["status"] != "approved":
            raise ValueError(
                f"only an approved branch can be marked shipped; fix_branch "
                f"{fix_branch_id} is {row['status']!r}"
            )
        conn.execute(
            "UPDATE fix_branch SET status='shipped', "
            "updated_at=datetime('now') WHERE id=?",
            (fix_branch_id,),
        )
        conn.commit()


def mark_shipping(
    fix_branch_id: int,
    *,
    workflow_id: str,
    tested_sha: str,
    requested_by: str,
    approval_review_id: Optional[int] = None,
    validation_id: Optional[int] = None,
) -> int:
    """Record an accepted ship START (wave-11 W2): set status='shipping'
    (NOT terminal 'shipped' — that now means release-COMPLETED) and append
    one immutable fix_branch_ship row binding the started workflow_id to
    the exact tested SHA. Guarded: only an 'approved' row can start
    shipping. Returns the fix_branch_ship row id."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        if row["status"] != "approved":
            raise ValueError(
                f"only an approved branch can start shipping; fix_branch "
                f"{fix_branch_id} is {row['status']!r}"
            )
        cur = conn.execute(
            "INSERT INTO fix_branch_ship(fix_branch_id, workflow_id, "
            "tested_sha, requested_by, approval_review_id, validation_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fix_branch_id, workflow_id, tested_sha, requested_by,
             approval_review_id, validation_id),
        )
        conn.execute(
            "UPDATE fix_branch SET status='shipping', "
            "updated_at=datetime('now') WHERE id=?",
            (fix_branch_id,),
        )
        conn.commit()
        return cur.lastrowid


def latest_ship(fix_branch_id: int) -> Optional[dict]:
    """The most recent fix_branch_ship row for a branch, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM fix_branch_ship WHERE fix_branch_id=? "
            "ORDER BY id DESC LIMIT 1",
            (fix_branch_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def set_ship_outcome(fix_branch_id: int, outcome: str) -> None:
    """Project a release outcome onto a SHIPPING branch: 'shipped' (release
    completed) or 'rolled-back'. Only a 'shipping' row may transition —
    the projection writer is branch_ship.sync_completion."""
    if outcome not in ("shipped", "rolled-back"):
        raise ValueError(
            f"ship outcome must be 'shipped' or 'rolled-back', got {outcome!r}"
        )
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        if row["status"] != "shipping":
            raise ValueError(
                f"only a shipping branch can record a ship outcome; "
                f"fix_branch {fix_branch_id} is {row['status']!r}"
            )
        conn.execute(
            "UPDATE fix_branch SET status=?, updated_at=datetime('now') "
            "WHERE id=?",
            (outcome, fix_branch_id),
        )
        conn.commit()


def list_branches(
    *, finding: Optional[int] = None, status: Optional[str] = None
) -> list[dict]:
    q = "SELECT * FROM fix_branch WHERE 1=1"
    args: tuple = ()
    if finding is not None:
        q += " AND finding_id=?"
        args += (finding,)
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        q += " AND status=?"
        args += (status,)
    q += " ORDER BY repo_path, branch_name"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_branch(fix_branch_id: int) -> dict:
    """Fetch a fix_branch with its validation runs and reviews attached
    (the full dossier view). Adds ``current_validation`` — the latest run
    bound to the CURRENT head, or None."""
    with _connect() as conn:
        row = _get_row(conn, fix_branch_id)
        d = dict(row)
        d["validations"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM fix_branch_validation WHERE fix_branch_id=? "
                "ORDER BY id",
                (fix_branch_id,),
            ).fetchall()
        ]
        d["reviews"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM fix_branch_review WHERE fix_branch_id=? "
                "ORDER BY id",
                (fix_branch_id,),
            ).fetchall()
        ]
    cv = [
        v for v in d["validations"] if v["tested_sha"] == d["head_sha"]
    ]
    d["current_validation"] = cv[-1] if cv else None
    # Content-addressed diff (wave-10 V2): text when captured, None when
    # missing (the UI renders 'diff not captured' for None).
    diff_path = _diffs_dir() / f"{d['patch_digest']}.patch"
    try:
        d["diff"] = diff_path.read_text(errors="replace")
    except OSError:
        d["diff"] = None
    d["changed_files"] = _changed_files(
        d["repo_path"], d["base_sha"], d["head_sha"]
    )
    return d


# ── CLI (debug aid; the real entry points are the importable functions) ──


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="fix_branches.py", description=__doc__.splitlines()[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("register")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--branch", required=True)
    sp.add_argument("--base")
    sp.add_argument("--finding", type=int)
    sp.add_argument("--workorder-ref")
    sp.add_argument(
        "--allow-unresolved", action="store_true",
        help="accept a workorder_ref that does not resolve in this DB; "
             "it is stamped 'unresolved-ref:<ref>' (exploratory use)",
    )

    sp = sub.add_parser("list")
    sp.add_argument("--finding", type=int)
    sp.add_argument("--status")

    sp = sub.add_parser("get")
    sp.add_argument("--id", type=int, required=True)

    sp = sub.add_parser("refresh")
    sp.add_argument("--id", type=int, required=True)

    sp = sub.add_parser("validate")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--red-cmd", required=True)
    sp.add_argument("--green-cmd", required=True)
    sp.add_argument("--guard-cmd")
    sp.add_argument("--scratch-dir")
    sp.add_argument(
        "--expected",
        help="pin exact leg counts, e.g. 'red=1/1,green=30/30,guard=5/5' — "
        "any parsed-counter drift fails the leg with COUNTER-MISMATCH",
    )

    sp = sub.add_parser("review")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--reviewer", required=True)
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--rationale", default="")

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "register":
        init()
        _print(
            {
                "id": register_branch(
                    args.repo,
                    args.branch,
                    base=args.base,
                    finding=args.finding,
                    workorder_ref=args.workorder_ref,
                    allow_unresolved=args.allow_unresolved,
                )
            }
        )
    elif cmd == "list":
        _print(list_branches(finding=args.finding, status=args.status))
    elif cmd == "get":
        _print(get_branch(args.id))
    elif cmd == "refresh":
        _print(refresh_head(args.id))
    elif cmd == "validate":
        from bin import branch_validator

        expected = None
        if getattr(args, "expected", None):
            expected = {}
            for part in args.expected.split(","):
                leg, _, counts = part.strip().partition("=")
                got, _, want = counts.partition("/")
                if not (leg and got and want):
                    p.error(
                        "--expected wants 'red=1/1,green=30/30,guard=5/5' "
                        f"style entries; could not parse {part!r}"
                    )
                expected[leg.strip().lower()] = (int(got), int(want))
        _print(
            branch_validator.validate(
                args.id,
                red_cmd=args.red_cmd,
                green_cmd=args.green_cmd,
                guard_cmd=args.guard_cmd,
                scratch_dir=args.scratch_dir,
                expected=expected,
            )
        )
    elif cmd == "review":
        _print(
            record_review(
                args.id,
                reviewer=args.reviewer,
                decision=args.decision,
                rationale=args.rationale,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
