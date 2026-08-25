"""Worker: hosts UnityReleaseWorkflow + stub activities.

Run against a dev server:
    nix run nixpkgs#temporal-cli -- server start-dev   # terminal 1
    .venv/bin/python worker.py                          # terminal 2
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

import activities
from workflow import UnityReleaseWorkflow

TASK_QUEUE = "unity-release"

ALL_ACTIVITIES = [
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
    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    )
    print(f"worker started on task queue {TASK_QUEUE!r}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
