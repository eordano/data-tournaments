"""End-to-end workflow tests against Temporal's time-skipping test server.

These execute the REAL workflow code through a REAL Temporal server (the SDK's
Java-based test server with a skippable clock): event history, signals, durable
timers, and retry policies are all exercised for real — only wall-clock waits
are skipped.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

import activities as acts
from models import ApprovalDecision, ReleaseRequest
from worker import ALL_ACTIVITIES
from workflow import UnityReleaseWorkflow, workflow_id_for

REPO = "decentraland/unity-explorer"


def _req(commit: str, env) -> ReleaseRequest:
    """24h approval / 30m monitor timers when time-skipping is available;
    seconds-scale real timers when falling back to a live dev server."""
    ts = env.supports_time_skipping
    return ReleaseRequest(
        repo=REPO,
        commit=commit,
        requested_by="pytest",
        approval_timeout_seconds=24 * 3600 if ts else 3.0,
        monitor_window_seconds=30 * 60 if ts else 1.0,
    )


def _commit() -> str:
    # Unique commit per test so workflow_ids don't collide across runs.
    return uuid.uuid4().hex


async def test_happy_path_approved_promotes(env):
    commit = _commit()
    task_queue = f"tq-{commit}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=workflow_id_for(REPO, commit),
            task_queue=task_queue,
        )
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=True, approver="alice", reason="lgtm"),
        )
        result = await handle.result()

    assert result.status == "promoted"
    stage_names = [s.stage for s in result.stages]
    assert stage_names == [
        "assemble_context",
        "generate_workorders",
        "judging_gate",
        "approval",
        "sandbox_preflight",
        "build",
        "canary",
        "monitor_window",
        "promote",
    ]
    assert all(s.status == "ok" for s in result.stages)
    assert result.approval is not None and result.approval.approver == "alice"


async def test_approval_timeout_rolls_back(env):
    """No signal ever arrives: the 24h durable timer fires (time-skipped)."""
    commit = _commit()
    task_queue = f"tq-{commit}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=workflow_id_for(REPO, commit),
            task_queue=task_queue,
        )
        result = await handle.result()  # no signal sent

    assert result.status == "rolled_back"
    assert "timed out" in result.reason
    assert result.stages[-1].stage == "rollback"
    # Nothing after the approval gate ran.
    assert "build" not in [s.stage for s in result.stages]


async def test_explicit_rejection_rolls_back(env):
    commit = _commit()
    task_queue = f"tq-{commit}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=workflow_id_for(REPO, commit),
            task_queue=task_queue,
        )
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=False, approver="bob", reason="bad vibes"),
        )
        result = await handle.result()

    assert result.status == "rolled_back"
    assert "rejected by bob" in result.reason
    assert result.stages[-1].stage == "rollback"


async def test_activity_failure_retries_then_fails_workflow(env):
    """generate_workorders fails on every attempt: the LM_RETRY policy
    (max_attempts=3) retries it, then the workflow surfaces a clean failure
    (after running the rollback compensation activity)."""
    attempts = 0

    @activity.defn(name="generate_workorders")
    async def flaky_generate_workorders(ctx) -> None:
        nonlocal attempts
        attempts += 1
        raise ApplicationError(f"stub LM outage (attempt {attempts})")

    commit = _commit()
    task_queue = f"tq-{commit}"
    # Same activity set, but generate_workorders replaced by the failing stub
    # (Temporal dispatches activities by name).
    overridden = [
        a for a in ALL_ACTIVITIES if a is not acts.generate_workorders
    ] + [flaky_generate_workorders]

    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=overridden,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=workflow_id_for(REPO, commit),
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError) as excinfo:
            await handle.result()

    # Retried exactly per policy, then failed cleanly.
    assert attempts == 3
    cause = excinfo.value.cause
    assert isinstance(cause, ActivityError)
    assert isinstance(cause.cause, ApplicationError)
    assert "stub LM outage" in str(cause.cause)
