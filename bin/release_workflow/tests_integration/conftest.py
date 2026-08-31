"""Pytest config for the release-workflow integration tests.

Runs OUTSIDE the main dev shell — temporalio is not installed there. Execute
with the spike venv:

    spikes/temporal-unity-release/.venv/bin/python -m pytest \
        bin/release_workflow/tests_integration/ -q

Prefer temporalio's time-skipping test environment; fall back to a live
`temporal server start-dev` on localhost:7233.

SANDBOX PITFALL (macOS, this repo's agent sandbox): the time-skipping test
server (GraalVM-native Java binary) aborts at startup with
`CSunMiscSignal.create() failed. errno: 1 Operation not permitted`. When
that happens we wrap a real dev server via WorkflowEnvironment.from_client()
— timers then run in real time, so tests read `env.supports_time_skipping`
and shrink timer durations accordingly. Either way the tests execute against
a REAL Temporal server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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

@pytest.fixture
def data_home(tmp_path, monkeypatch) -> Path:
    """Per-test DATA_TOURNAMENTS_HOME so projection assertions are isolated.

    Activities run in-process (the test hosts the Worker), and
    bin.workflow_runs reads the env var at call time — so a plain monkeypatch
    is enough to redirect every projection write to this tmp dir.
    """
    home = tmp_path / "data-tournaments"
    home.mkdir()
    monkeypatch.setenv("DATA_TOURNAMENTS_HOME", str(home))
    return home
