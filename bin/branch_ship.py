#!/usr/bin/env python3
"""Fail-closed ship gateway for fix branches (wave-10 V3).

Closes the SHIP BYPASS hole: the generic release client starts a workflow
for ANY commit passed positionally — nothing there enforces "only the
approved branch at its exact tested SHA ships". This gateway is the ONLY
sanctioned path from a fix branch to the release workflow, and it NEVER
accepts a caller-supplied commit: repo and commit DERIVE from the branch
record (repo = basename of the registered repo_path, commit = the current
head_sha).

ship_branch(fix_branch_id, requested_by=...) refuses — ShipRefused with a
machine-readable ``.reason`` code — unless EVERY gate holds:

* head-current      refresh_head() is called FIRST; a moved tip means the
                    record is stale → refuse (code 'stale').
* status-approved   fix_branch.status must be exactly 'approved'
                    (codes: 'failed', 'stale', 'rejected',
                    'already-shipped', 'not-approved').
* approval-current  the LATEST approving review's tested_sha must equal
                    the current head_sha (code 'approval-not-current').
* validation-passed a PASSED validation bound to the current head must
                    exist (code 'no-passing-validation').

On acceptance it shells out to the release client
(``python3 -m bin.release_workflow.client start <repo> <sha> ...``,
overridable via ``client_cmd`` or $BRANCH_SHIP_CLIENT_CMD) — never imports
temporalio — and, once the client accepted the start, sets the branch to
'shipping' and appends one immutable fix_branch_ship row recording the
started workflow_id (wave-11 W2: 'shipped' means release-COMPLETED).

sync_completion(fix_branch_id) projects the recorded workflow's
workflow_run outcome back onto the branch: 'done' -> 'shipped',
'rolled-back' -> 'rolled-back', still 'running' -> no change, no
workflow_run row -> honest error dict, no change.

Re-ship semantics: a rolled-back branch may NOT ship again on the old
approval — it needs a FRESH validation and a FRESH approving review at
the current head, both NEWER than the last ship record (codes:
'ship-in-progress' while a ship is in flight, 'rolled-back' until fresh
evidence exists).

refusal_matrix(fix_branch_id) returns every gate with its current
pass/fail — the evidence package for "why did/didn't this ship".

CLI:
  branch_ship.py ship  --id N --requested-by WHO [--project P --domain D]
                       [--repo-name ORG/REPO]
  branch_ship.py sync  --id N        # project workflow_run -> fix_branch
  branch_ship.py check --id N        # prints the refusal matrix JSON
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import fix_branches  # noqa: E402

DEFAULT_CLIENT_CMD = "python3 -m bin.release_workflow.client"
CLIENT_CMD_ENV = "BRANCH_SHIP_CLIENT_CMD"


class ShipRefused(Exception):
    """The gateway refused to ship. ``.reason`` is a machine-readable code:
    'stale' | 'failed' | 'rejected' | 'already-shipped' | 'not-approved' |
    'ship-in-progress' | 'rolled-back' | 'approval-not-current' |
    'no-passing-validation'."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"[{reason}] {detail}")


class ShipClientError(Exception):
    """The release client exited nonzero — the ship did NOT happen."""


# ── Gate evaluation (shared by ship_branch and refusal_matrix) ────────────


def _latest_approval(branch: dict) -> Optional[dict]:
    approvals = [r for r in branch["reviews"] if r["decision"] == "approve"]
    return approvals[-1] if approvals else None


def _evaluate(fix_branch_id: int) -> dict:
    """Refresh the head FIRST (a moved tip must be seen, not assumed),
    then evaluate every ship gate against the refreshed record."""
    branch = fix_branches.refresh_head(fix_branch_id)
    status = branch["status"]
    head_sha = branch["head_sha"]

    approval = _latest_approval(branch)
    cv = branch["current_validation"]
    last_ship = fix_branches.latest_ship(fix_branch_id)

    # Freshness vs the last ship record (re-ship semantics, wave-11 W2):
    # after a rollback, the OLD approval/validation that already shipped
    # may never ship again — both must be NEWER rows than the ones the
    # last ship consumed.
    approval_fresh = approval is not None and (
        last_ship is None
        or (last_ship["approval_review_id"] is not None
            and approval["id"] > last_ship["approval_review_id"])
    )
    validation_fresh = cv is not None and (
        last_ship is None
        or (last_ship["validation_id"] is not None
            and cv["id"] > last_ship["validation_id"])
    )

    gates = {
        "head-current": status != "stale",
        "status-approved": status == "approved",
        "approval-current": (
            approval is not None and approval["tested_sha"] == head_sha
        ),
        "validation-passed": cv is not None and cv["passed"] == 1,
        "no-ship-in-progress": status != "shipping",
        "approval-fresh": approval_fresh and validation_fresh,
    }

    reason: Optional[str] = None
    if status == "stale":
        reason = "stale"
    elif status in ("failed", "rejected"):
        reason = status
    elif status == "shipped":
        reason = "already-shipped"
    elif status == "shipping":
        reason = "ship-in-progress"
    elif status == "rolled-back":
        reason = "rolled-back"
    elif status != "approved":
        reason = "not-approved"
    elif not gates["approval-current"]:
        reason = "approval-not-current"
    elif not gates["validation-passed"]:
        reason = "no-passing-validation"
    elif not gates["approval-fresh"]:
        # Approved-looking record, but the approval/validation predates the
        # last ship (rolled-back release) — old evidence never re-ships.
        reason = "rolled-back"

    return {
        "fix_branch_id": fix_branch_id,
        "status": status,
        "head_sha": head_sha,
        "repo_path": branch["repo_path"],
        "branch_name": branch["branch_name"],
        "gates": gates,
        "ship_allowed": reason is None,
        "refusal_reason": reason,
        "approved_sha": approval["tested_sha"] if approval else None,
        "approval_review_id": approval["id"] if approval else None,
        "validation_id": cv["id"] if cv else None,
        "last_ship": last_ship,
    }


def refusal_matrix(fix_branch_id: int) -> dict:
    """Every ship gate with its current pass/fail (evidence package)."""
    return _evaluate(fix_branch_id)


# ── Canonical repo identity ───────────────────────────────────────────────


def _repo_name_from_origin_url(url: str) -> Optional[str]:
    """'<org>/<repo>' parsed from a git remote URL, else None.

    Handles https://host/org/repo(.git), ssh://git@host/org/repo(.git),
    and scp-like git@host:org/repo(.git).
    """
    url = url.strip()
    if not url:
        return None
    # scp-like: git@host:org/repo(.git) — no scheme, single ':' separator.
    if "://" not in url and ":" in url:
        url = url.split(":", 1)[1]
    else:
        # strip scheme://host
        if "://" in url:
            rest = url.split("://", 1)[1]
            url = rest.split("/", 1)[1] if "/" in rest else ""
    url = url.strip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return None


def derive_repo_name(repo_path: str) -> str:
    """Canonical repo identity: '<org>/<repo>' from the 'origin' remote URL
    when one exists, else the clone directory's basename."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        env={**os.environ,
             "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if proc.returncode == 0:
        parsed = _repo_name_from_origin_url(proc.stdout)
        if parsed:
            return parsed
    return Path(repo_path).name


# ── The gateway ───────────────────────────────────────────────────────────


def ship_branch(
    fix_branch_id: int,
    *,
    requested_by: str,
    project: str = "",
    domain: str = "",
    repo_name: Optional[str] = None,
    client_cmd: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> dict:
    """Ship an approved fix branch through the release client.

    Fail-closed: raises ShipRefused (machine-readable ``.reason``) unless
    every gate passes. The commit is NEVER caller-supplied — repo name and
    SHA derive from the branch record (``repo_name`` may override the
    derived identity; default is '<org>/<repo>' from the clone's 'origin'
    remote, falling back to the directory basename). Returns
    {'workflow_id', 'head_sha', 'repo', 'ship_id', 'argv', 'output'} and
    sets the branch to 'shipping' — 'shipped' is reserved for a COMPLETED
    release, projected by sync_completion.
    """
    ev = _evaluate(fix_branch_id)
    if not ev["ship_allowed"]:
        raise ShipRefused(
            ev["refusal_reason"],
            f"fix_branch {fix_branch_id} ({ev['branch_name']}) status="
            f"{ev['status']} head={ev['head_sha'][:12]} gates="
            f"{ev['gates']}",
        )

    # Derived — the ONLY source of repo/commit is the branch record.
    repo = repo_name or derive_repo_name(ev["repo_path"])
    head_sha = ev["head_sha"]

    cmd = client_cmd or os.environ.get(CLIENT_CMD_ENV) or DEFAULT_CLIENT_CMD
    argv = shlex.split(cmd) + [
        "start",
        repo,
        head_sha,
        "--project", project,
        "--domain", domain,
        "--requested-by", requested_by,
    ] + list(extra_args or [])

    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    if proc.returncode != 0:
        raise ShipClientError(
            f"release client exited {proc.returncode} for fix_branch "
            f"{fix_branch_id} ({repo}@{head_sha[:12]}): "
            f"{output.strip()}"
        )

    # The client prints 'started <workflow_id>'.
    workflow_id = None
    for line in output.splitlines():
        if line.startswith("started "):
            workflow_id = line[len("started "):].strip()
    if workflow_id is None:
        workflow_id = f"release:{repo}:{head_sha}"

    # 'shipping' + an immutable ship row recording the started workflow —
    # the same approval cannot ship twice, and 'shipped' now means the
    # release COMPLETED (sync_completion projects the outcome).
    ship_id = fix_branches.mark_shipping(
        fix_branch_id,
        workflow_id=workflow_id,
        tested_sha=head_sha,
        requested_by=requested_by,
        approval_review_id=ev["approval_review_id"],
        validation_id=ev["validation_id"],
    )

    return {
        "workflow_id": workflow_id,
        "head_sha": head_sha,
        "repo": repo,
        "ship_id": ship_id,
        "argv": argv,
        "output": output,
    }


# ── Completion projection (wave-11 W2) ────────────────────────────────────


def sync_completion(fix_branch_id: int) -> dict:
    """Project the recorded release workflow's outcome onto the branch.

    Reads the workflow_run row(s) for the workflow_id recorded by the last
    ship from the SAME judgements.db:

    * 'done'        -> fix_branch 'shipped' (release completed)
    * 'rolled-back' -> fix_branch 'rolled-back'
    * still 'running' (or any non-terminal) -> no change
    * no workflow_run row -> honest {'error': ...} dict, no change

    Returns {'fix_branch_id', 'workflow_id', 'workflow_status',
    'fix_branch_status', 'changed'} or an error dict.
    """
    from bin import workflow_runs

    branch = fix_branches.get_branch(fix_branch_id)
    last_ship = fix_branches.latest_ship(fix_branch_id)
    if last_ship is None:
        return {
            "error": f"fix_branch {fix_branch_id} has no ship record — "
                     "nothing to sync",
            "fix_branch_id": fix_branch_id,
            "changed": False,
        }
    workflow_id = last_ship["workflow_id"]
    runs = workflow_runs.get_by_workflow_id(workflow_id)
    if not runs:
        return {
            "error": f"no workflow_run row for workflow_id {workflow_id!r} "
                     f"(fix_branch {fix_branch_id}) — the release "
                     "projection has not landed; nothing changed",
            "fix_branch_id": fix_branch_id,
            "workflow_id": workflow_id,
            "changed": False,
        }
    run_status = runs[0]["status"]  # newest run for this workflow id

    outcome = {"done": "shipped", "rolled-back": "rolled-back"}.get(run_status)
    changed = False
    if outcome is not None and branch["status"] == "shipping":
        fix_branches.set_ship_outcome(fix_branch_id, outcome)
        changed = True

    return {
        "fix_branch_id": fix_branch_id,
        "workflow_id": workflow_id,
        "workflow_status": run_status,
        "fix_branch_status": fix_branches.get_branch(fix_branch_id)["status"],
        "changed": changed,
    }


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="branch_ship.py",
        description="Fail-closed ship gateway for approved fix branches.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ship", help="ship an approved branch (fail-closed)")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--requested-by", required=True)
    sp.add_argument("--project", default="")
    sp.add_argument("--domain", default="")
    sp.add_argument(
        "--repo-name", default=None,
        help="canonical repo identity (default: '<org>/<repo>' from the "
             "clone's 'origin' remote, else the directory basename)",
    )

    sp = sub.add_parser(
        "sync", help="project the release workflow outcome onto the branch"
    )
    sp.add_argument("--id", type=int, required=True)

    sp = sub.add_parser("check", help="print the refusal matrix JSON")
    sp.add_argument("--id", type=int, required=True)

    args = p.parse_args(argv)

    def _print(obj: Any) -> None:
        print(json.dumps(obj, indent=2, default=str))

    if args.cmd == "check":
        _print(refusal_matrix(args.id))
        return 0
    if args.cmd == "sync":
        res = sync_completion(args.id)
        _print(res)
        return 1 if "error" in res else 0
    try:
        _print(
            ship_branch(
                args.id,
                requested_by=args.requested_by,
                project=args.project,
                domain=args.domain,
                repo_name=args.repo_name,
            )
        )
        return 0
    except ShipRefused as exc:
        _print({"refused": exc.reason, "detail": exc.detail})
        return 1
    except ShipClientError as exc:
        _print({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
