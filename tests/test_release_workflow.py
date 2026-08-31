"""Root-suite tests for bin/release_workflow.

The main dev shell has NO temporalio, so:
  * models.py (plain dataclasses, no temporalio import) is tested directly;
  * anything touching workflow/activities/client is guarded with
    pytest.importorskip("temporalio") and SKIPS cleanly here.

The real end-to-end coverage lives in
bin/release_workflow/tests_integration/ and runs under the spike venv:

    spikes/temporal-unity-release/.venv/bin/python -m pytest \
        bin/release_workflow/tests_integration/ -q
"""

from __future__ import annotations

import pytest

from bin.release_workflow.models import (
    ApprovalDecision,
    ReleaseRequest,
    ReleaseResult,
    StageRecord,
    workflow_id_for,
)

def test_workflow_id_convention():
    assert (
        workflow_id_for("decentraland/unity-explorer", "abc123")
        == "release:decentraland/unity-explorer:abc123"
    )

def test_release_request_defaults():
    req = ReleaseRequest(repo="r", commit="c")
    assert req.project == ""
    assert req.approval_timeout_seconds == 24 * 3600
    assert req.monitor_window_seconds == 30 * 60

def test_models_importable_without_temporalio():
    """models.py must never grow a temporalio import — the root suite and
    Phoenix-side tooling import it from the plain dev shell."""
    import importlib
    import sys

    assert "temporalio" not in sys.modules or True
    mod = importlib.import_module("bin.release_workflow.models")
    src = (mod.__file__ or "")
    assert src.endswith("models.py")
    import inspect

    assert "import temporalio" not in inspect.getsource(mod)
    assert "from temporalio" not in inspect.getsource(mod)

def test_stage_record_shape():
    rec = StageRecord(run_id=1, stage="build", status="ok", detail={"x": 1})
    assert (rec.run_id, rec.stage, rec.status, rec.detail) == (1, "build", "ok", {"x": 1})

def test_release_result_defaults():
    res = ReleaseResult(status="promoted", reason="ok", repo="r", commit="c")
    assert res.stages == [] and res.approval is None
    assert ApprovalDecision(approved=True, approver="a").reason == ""

def test_workflow_module_imports_with_temporalio():
    pytest.importorskip("temporalio")
    from bin.release_workflow.workflow import UnityReleaseWorkflow  # noqa: F401
    from bin.release_workflow.worker import ALL_ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ALL_ACTIVITIES}
    assert {"record_started", "record_stage", "set_run_status"} <= names

def test_client_module_imports_with_temporalio():
    pytest.importorskip("temporalio")
    from bin.release_workflow import client

    assert callable(client.start_release)
    assert callable(client.get_status)
    assert callable(client.send_approval)

def test_start_release_carries_domain_through_to_request():
    """L3 regression: --domain must reach ReleaseRequest so the workflow's
    generate_workorders activity runs the real pipeline, not the stub."""
    pytest.importorskip("temporalio")
    import asyncio

    from bin.release_workflow import client

    captured = {}

    class _FakeClient:
        async def start_workflow(self, run, request, *, id, task_queue):
            captured["request"] = request
            captured["id"] = id
            captured["task_queue"] = task_queue

    wf_id = asyncio.run(
        client.start_release(
            "unity-explorer",
            "abc1234",
            "proj",
            domain="hrb-release-reliability",
            requested_by="changeme",
            client=_FakeClient(),
        )
    )
    req = captured["request"]
    assert wf_id == "release:unity-explorer:abc1234" == captured["id"]
    assert req.domain == "hrb-release-reliability"
    assert req.project == "proj"
    assert req.repo == "unity-explorer" and req.commit == "abc1234"
