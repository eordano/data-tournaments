"""Starter: kick off a release, optionally send the approval signal, await result.

Usage (dev server + worker already running):
    .venv/bin/python starter.py <commit-sha> [--approve|--reject|--no-signal]

Demonstrates the workflow_id convention release:<repo>:<commit> — starting the
same (repo, commit) twice while running fails with WorkflowAlreadyStartedError,
which is exactly the idempotence we want for releases.
"""

from __future__ import annotations

import asyncio
import sys

from temporalio.client import Client

from models import ApprovalDecision, ReleaseRequest
from workflow import UnityReleaseWorkflow, workflow_id_for
from worker import TASK_QUEUE

REPO = "decentraland/unity-explorer"


async def main() -> None:
    commit = sys.argv[1] if len(sys.argv) > 1 else "deadbeefcafe0123"
    mode = sys.argv[2] if len(sys.argv) > 2 else "--approve"

    client = await Client.connect("localhost:7233")

    req = ReleaseRequest(
        repo=REPO,
        commit=commit,
        requested_by="starter.py",
        # Short timers for the live demo; production uses the 24h/30m defaults.
        approval_timeout_seconds=60,
        monitor_window_seconds=2,
    )
    wf_id = workflow_id_for(REPO, commit)
    handle = await client.start_workflow(
        UnityReleaseWorkflow.run,
        req,
        id=wf_id,
        task_queue=TASK_QUEUE,
    )
    print(f"started workflow {wf_id} (run_id={handle.result_run_id})")

    if mode == "--approve":
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=True, approver="starter-cli", reason="demo"),
        )
        print("sent approval signal")
    elif mode == "--reject":
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=False, approver="starter-cli", reason="demo reject"),
        )
        print("sent rejection signal")
    else:
        print("no signal sent — workflow will time out after "
              f"{req.approval_timeout_seconds}s and roll back")

    result = await handle.result()
    print(f"\nterminal status: {result.status} — {result.reason}")
    for s in result.stages:
        print(f"  [{s.status:>6}] {s.stage}: {s.detail}")


if __name__ == "__main__":
    asyncio.run(main())
