"""Worker: hosts UnityReleaseWorkflow + activities (incl. projection writers).

Run against a dev server:
    nix run nixpkgs#temporal-cli -- server start-dev      # terminal 1
    <venv-with-temporalio>/bin/python -m bin.release_workflow.worker  # terminal 2

Env:
    TEMPORAL_TARGET          (default localhost:7233)
    DATA_TOURNAMENTS_HOME    projection sqlite location (bin.workflow_runs)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin.release_workflow import activities  # noqa: E402
from bin.release_workflow.workflow import UnityReleaseWorkflow  # noqa: E402

TASK_QUEUE = os.environ.get("RELEASE_TASK_QUEUE", "unity-release")
DEFAULT_TARGET = "localhost:7233"

ALL_ACTIVITIES = [
    activities.record_started,
    activities.record_stage,
    activities.set_run_status,
    activities.assemble_context,
    activities.generate_workorders,
    activities.judging_gate,
    activities.sandbox_preflight,
    activities.build,
    activities.canary,
    activities.check_canary_health,
    activities.promote,
    activities.rollback,
]

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    target = os.environ.get("TEMPORAL_TARGET", DEFAULT_TARGET)
    client = await Client.connect(target)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    )
    print(f"worker started on task queue {TASK_QUEUE!r} (server {target})")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
