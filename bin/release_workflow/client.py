"""Thin client surface for the release workflow — what Phoenix/CLI call.

Library API (async):
    start_release(repo, commit, project="", ...) -> workflow_id
    get_status(workflow_id) -> {"current_stage": ..., "stage_results": [...]}
    send_approval(workflow_id, approved, approver, reason="") -> None

CLI:
    python3 -m bin.release_workflow.client start  <repo> <commit> [--project P]
        [--requested-by WHO] [--approval-timeout S] [--monitor-window S]
    python3 -m bin.release_workflow.client status  <workflow_id>
    python3 -m bin.release_workflow.client approve <workflow_id> --approver WHO [--reason R]
    python3 -m bin.release_workflow.client reject  <workflow_id> --approver WHO [--reason R]

Env: TEMPORAL_TARGET (default localhost:7233).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from temporalio.client import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin.release_workflow.models import (  # noqa: E402
    ApprovalDecision,
    ReleaseRequest,
    workflow_id_for,
)
from bin.release_workflow.worker import TASK_QUEUE  # noqa: E402
from bin.release_workflow.workflow import UnityReleaseWorkflow  # noqa: E402

DEFAULT_TARGET = "localhost:7233"


async def _client(target: Optional[str] = None) -> Client:
    return await Client.connect(
        target or os.environ.get("TEMPORAL_TARGET", DEFAULT_TARGET)
    )


async def start_release(
    repo: str,
    commit: str,
    project: str = "",
    *,
    domain: str = "",
    requested_by: str = "client",
    approval_timeout_seconds: float = 24 * 3600,
    monitor_window_seconds: float = 30 * 60,
    task_queue: str = TASK_QUEUE,
    client: Optional[Client] = None,
) -> str:
    """Start a release; returns the workflow_id (release:<repo>:<commit>).

    Starting the same (repo, commit) while one is already running raises
    temporalio.client.WorkflowAlreadyStartedError — desired idempotence.

    ``domain`` names a generation domain: when set, the workflow's
    generate_workorders activity runs the REAL pipeline instead of the
    labeled stub (wave-9 L3).
    """
    client = client or await _client()
    wf_id = workflow_id_for(repo, commit)
    await client.start_workflow(
        UnityReleaseWorkflow.run,
        ReleaseRequest(
            repo=repo,
            commit=commit,
            project=project,
            domain=domain,
            requested_by=requested_by,
            approval_timeout_seconds=approval_timeout_seconds,
            monitor_window_seconds=monitor_window_seconds,
        ),
        id=wf_id,
        task_queue=task_queue,
    )
    return wf_id


async def get_status(
    workflow_id: str, *, client: Optional[Client] = None
) -> dict[str, Any]:
    """Query the LIVE workflow: current_stage + stage_results.

    (The workflow_run projection is the read model for listing/history;
    this queries the running execution directly.)
    """
    client = client or await _client()
    handle = client.get_workflow_handle_for(UnityReleaseWorkflow.run, workflow_id)
    current = await handle.query(UnityReleaseWorkflow.current_stage)
    stages = await handle.query(UnityReleaseWorkflow.stage_results)
    return {
        "workflow_id": workflow_id,
        "current_stage": current,
        "stage_results": [dataclasses.asdict(s) for s in stages],
    }


async def send_approval(
    workflow_id: str,
    approved: bool,
    approver: str,
    reason: str = "",
    *,
    client: Optional[Client] = None,
) -> None:
    """Send the human-approval Signal (first decision wins in the workflow)."""
    client = client or await _client()
    handle = client.get_workflow_handle_for(UnityReleaseWorkflow.run, workflow_id)
    await handle.signal(
        UnityReleaseWorkflow.submit_approval,
        ApprovalDecision(approved=approved, approver=approver, reason=reason),
    )


# ── CLI ──────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m bin.release_workflow.client",
        description="Start / inspect / approve unity release workflows.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="start a release workflow")
    sp.add_argument("repo")
    sp.add_argument("commit")
    sp.add_argument("--project", default="", help="catalog project for real assembly")
    sp.add_argument(
        "--domain",
        default="",
        help="generation domain: when set, generate_workorders runs the real "
        "pipeline instead of the labeled stub",
    )
    sp.add_argument("--requested-by", default="cli")
    sp.add_argument("--approval-timeout", type=float, default=24 * 3600)
    sp.add_argument("--monitor-window", type=float, default=30 * 60)

    st = sub.add_parser("status", help="query current_stage + stage_results")
    st.add_argument("workflow_id")

    for name, approved in (("approve", True), ("reject", False)):
        ap = sub.add_parser(name, help=f"{name} a pending release")
        ap.add_argument("workflow_id")
        ap.add_argument("--approver", required=True)
        ap.add_argument("--reason", default="")
        ap.set_defaults(approved=approved)

    return p


async def _amain(args: argparse.Namespace) -> None:
    if args.cmd == "start":
        wf_id = await start_release(
            args.repo,
            args.commit,
            args.project,
            domain=args.domain,
            requested_by=args.requested_by,
            approval_timeout_seconds=args.approval_timeout,
            monitor_window_seconds=args.monitor_window,
        )
        print(f"started {wf_id}")
    elif args.cmd == "status":
        print(json.dumps(await get_status(args.workflow_id), indent=2))
    else:  # approve / reject
        await send_approval(
            args.workflow_id, args.approved, args.approver, args.reason
        )
        print(f"{args.cmd}ed {args.workflow_id} as {args.approver}")


def main() -> None:
    asyncio.run(_amain(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
