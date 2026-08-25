"""End-to-end release-workflow tests against a REAL Temporal server, with
workflow_run projection assertions (DATA_TOURNAMENTS_HOME → tmp dir).

Covers:
    * happy path approve → promoted, projection status "done",
      stage_history contains every stage
    * explicit rejection → rolled_back, sticky "rolled-back" projection status
    * approval timeout → rolled_back
    * projection idempotency: record_started retried does not duplicate rows
    * real assemble path: catalog project seeded → snapshot digest in
      the projection stage detail
"""

from __future__ import annotations

import dataclasses
import subprocess
import uuid
from pathlib import Path

from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from bin import workflow_runs
from bin.release_workflow import activities as acts
from bin.release_workflow.models import (
    ApprovalDecision,
    ReleaseRequest,
    workflow_id_for,
)
from bin.release_workflow.worker import ALL_ACTIVITIES
from bin.release_workflow.workflow import UnityReleaseWorkflow

REPO = "decentraland/unity-explorer"


def _req(commit: str, env, project: str = "") -> ReleaseRequest:
    """24h approval / 30m monitor timers when time-skipping is available;
    seconds-scale real timers when falling back to a live dev server."""
    ts = env.supports_time_skipping
    return ReleaseRequest(
        repo=REPO,
        commit=commit,
        project=project,
        requested_by="pytest",
        approval_timeout_seconds=24 * 3600 if ts else 3.0,
        monitor_window_seconds=30 * 60 if ts else 1.0,
    )


def _commit() -> str:
    # Unique commit per test so workflow_ids don't collide across runs.
    return uuid.uuid4().hex


def _projection(wf_id: str) -> dict:
    rows = workflow_runs.get_by_workflow_id(wf_id)
    assert len(rows) == 1, f"expected exactly one projection row, got {len(rows)}"
    return rows[0]


# ── tests ────────────────────────────────────────────────────────────────


async def test_happy_path_approved_promotes_and_projects(env, data_home):
    commit = _commit()
    task_queue = f"tq-{commit}"
    wf_id = workflow_id_for(REPO, commit)
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=wf_id,
            task_queue=task_queue,
        )
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=True, approver="alice", reason="lgtm"),
        )
        result = await handle.result()

    # Workflow result — same contract as the spike.
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

    # Projection row (written only by activities, in DATA_TOURNAMENTS_HOME).
    run = _projection(wf_id)
    assert run["status"] == "done"
    assert run["finished_at"] is not None
    assert run["detail"]["repo"] == REPO
    assert run["detail"]["commit"] == commit
    assert run["detail"]["result"] == "promoted"
    history_stages = [e["stage"] for e in run["stage_history"]]
    assert history_stages == stage_names  # every stage transition recorded
    assert all(e["status"] == "ok" for e in run["stage_history"])
    # Canned fallback was used (no project supplied) — and said so.
    assemble_entry = run["stage_history"][0]
    assert "canned" in assemble_entry["detail"]["note"]


async def test_rejection_rolls_back_with_sticky_projection(env, data_home):
    commit = _commit()
    task_queue = f"tq-{commit}"
    wf_id = workflow_id_for(REPO, commit)
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=wf_id,
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

    run = _projection(wf_id)
    assert run["status"] == "rolled-back"
    assert run["detail"]["reason"].startswith("rejected by bob")
    history_stages = [e["stage"] for e in run["stage_history"]]
    assert history_stages[-1] == "rollback"
    assert ("approval", "failed") in [
        (e["stage"], e["status"]) for e in run["stage_history"]
    ]

    # Terminal status is STICKY: a late/retried projection update cannot
    # flip a finished run back to running.
    workflow_runs.set_status(run["id"], "running")
    assert workflow_runs.get(run["id"])["status"] == "rolled-back"


async def test_approval_timeout_rolls_back(env, data_home):
    """No signal ever arrives: the durable approval timer fires."""
    commit = _commit()
    task_queue = f"tq-{commit}"
    wf_id = workflow_id_for(REPO, commit)
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env),
            id=wf_id,
            task_queue=task_queue,
        )
        result = await handle.result()  # no signal sent

    assert result.status == "rolled_back"
    assert "timed out" in result.reason
    # Nothing after the approval gate ran.
    assert "build" not in [s.stage for s in result.stages]

    run = _projection(wf_id)
    assert run["status"] == "rolled-back"
    assert "timed out" in run["detail"]["reason"]


async def test_record_started_retry_is_idempotent(data_home):
    """A retried record_started activity must not mint duplicate projection
    rows: workflow_runs.start() is idempotent per (workflow_id, run_id)."""
    aenv = ActivityEnvironment()
    aenv.info = dataclasses.replace(
        aenv.info,
        workflow_id="release:acme/idem:cafebabe",
        workflow_run_id="run-idem-1",
    )
    req = ReleaseRequest(repo="acme/idem", commit="cafebabe")

    id_first = await aenv.run(acts.record_started, req)
    id_retry = await aenv.run(acts.record_started, req)  # simulated retry

    assert id_first == id_retry
    rows = workflow_runs.get_by_workflow_id("release:acme/idem:cafebabe")
    assert len(rows) == 1
    assert rows[0]["temporal_run_id"] == "run-idem-1"

    # A NEW temporal run (retry/continue-as-new of the whole workflow) DOES
    # mint a new row — one projection row per execution.
    aenv.info = dataclasses.replace(aenv.info, workflow_run_id="run-idem-2")
    id_new_run = await aenv.run(acts.record_started, req)
    assert id_new_run != id_first
    assert len(workflow_runs.get_by_workflow_id("release:acme/idem:cafebabe")) == 2


# ── real assemble path (catalog project seeded into tmp data home) ──────


def _git_env(home: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def _make_repo(root: Path) -> str:
    """Init a repo with one committed file; returns the HEAD sha."""

    def git(*args):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env=_git_env(root),
        )

    root.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main")
    (root / "README.md").write_text("release evidence line 1\nline 2\n")
    git("add", "README.md")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(root),
    ).stdout.strip()


async def test_assemble_context_uses_real_catalog_project(env, data_home, tmp_path):
    """When the catalog has the project, assemble_context runs the REAL
    bin.assemble_pack.assemble() and the projection carries the snapshot
    digest; no canned data involved."""
    from bin import catalog

    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    catalog.init()
    catalog.create_project(name="unity-proj", description="release test project")
    catalog.create_source(
        project="unity-proj",
        name="main-repo",
        kind="git",
        locator=str(repo_root),
        trust_tier=1,
        config={"root": str(repo_root), "paths": ["README.md"]},
    )

    commit = _commit()
    task_queue = f"tq-{commit}"
    wf_id = workflow_id_for(REPO, commit)
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnityReleaseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        handle = await env.client.start_workflow(
            UnityReleaseWorkflow.run,
            _req(commit, env, project="unity-proj"),
            id=wf_id,
            task_queue=task_queue,
        )
        await handle.signal(
            UnityReleaseWorkflow.submit_approval,
            ApprovalDecision(approved=True, approver="alice", reason="lgtm"),
        )
        result = await handle.result()

    assert result.status == "promoted"
    run = _projection(wf_id)
    assert run["status"] == "done"
    entry = run["stage_history"][0]
    assert entry["stage"] == "assemble_context"
    assert entry["detail"]["note"] == "assembled from catalog project 'unity-proj'"
    digest = entry["detail"]["snapshot_digest"]
    assert digest  # non-empty — a real, citable snapshot was persisted
    assert catalog.get_landscape_snapshot(digest) is not None
