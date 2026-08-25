"""Pytest config: prefer temporalio's time-skipping test environment; fall
back to a live `temporal server start-dev` on localhost:7233.

The time-skipping env is a real Temporal test server (a GraalVM-native Java
binary the SDK downloads) whose clock jumps whenever all workflows are blocked
on timers — a 24h approval timeout resolves in milliseconds.

SANDBOX PITFALL (macOS, this repo's agent sandbox): that binary aborts at
startup with `CSunMiscSignal.create() failed. errno: 1 Operation not
permitted` (GraalVM signal/semaphore init blocked by the sandbox). When that
happens we fall back to wrapping a real dev server via
WorkflowEnvironment.from_client() — timers then run in real time, so tests
read `env.supports_time_skipping` and shrink timer durations accordingly.
Either way the tests execute against a REAL Temporal server.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

DEV_SERVER_TARGET = "localhost:7233"


@pytest_asyncio.fixture(scope="session")
async def env():
    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as ts_err:
        try:
            client = await Client.connect(DEV_SERVER_TARGET)
        except Exception:
            pytest.fail(
                "Neither the time-skipping test server could start "
                f"({ts_err}) nor is a dev server running at {DEV_SERVER_TARGET}. "
                "Run: nix run nixpkgs#temporal-cli -- server start-dev"
            )
        env = WorkflowEnvironment.from_client(client)
    yield env
    await env.shutdown()
