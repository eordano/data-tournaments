"""Typed payloads shared between workflow, activities, worker, and client.

SERIALIZATION CHOICE: plain dataclasses with temporalio's default data
converter (JSON with full type restoration). The spike's docstring describes
the pydantic switch (temporalio.contrib.pydantic.pydantic_data_converter);
we defer it until PydanticAI WorkOrders actually cross the workflow boundary
— nothing here needs validation beyond types, and dataclasses keep this
module importable in the main dev shell where neither temporalio nor a
matching pydantic pin is guaranteed.

NO temporalio imports in this module — the root test suite imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def workflow_id_for(repo: str, commit: str) -> str:
    """workflow_id convention: release:<repo>:<commit> — duplicate starts of
    the same release are rejected by the Temporal server (idempotence) and
    the id itself is provenance."""
    return f"release:{repo}:{commit}"


@dataclass
class ReleaseRequest:
    """Input to UnityReleaseWorkflow. workflow_id = release:<repo>:<commit>.

    ``project``: catalog project name for real evidence assembly via
    bin.assemble_pack.assemble(); when empty/unknown the assemble_context
    activity falls back to canned spike data (noted in the stage detail).
    """

    repo: str
    commit: str
    project: str = ""
    # Judging/generation domain (bin/domains.py). When set, the
    # generate_workorders activity runs the REAL generation pipeline for
    # this domain (worker env must carry the generation deps — dspy etc.);
    # when empty, activities fall back to canned data with explicit notes.
    domain: str = ""
    requested_by: str = "unknown"
    # Durable-timer knobs (seconds) so tests / demos can shrink them.
    # Production defaults: 24h approval window, 30min canary monitor window.
    approval_timeout_seconds: float = 24 * 3600
    monitor_window_seconds: float = 30 * 60


@dataclass
class StageResult:
    """One completed stage, accumulated on the workflow for audit/query."""

    stage: str
    status: str  # "ok" | "failed" | "skipped"
    detail: str = ""


@dataclass
class StageRecord:
    """Input to the record_stage projection activity."""

    run_id: int
    stage: str
    status: str
    detail: Optional[dict[str, Any]] = None


@dataclass
class RunStatusUpdate:
    """Input to the set_run_status projection activity."""

    run_id: int
    status: str  # one of bin.workflow_runs.STATUSES
    detail: Optional[dict[str, Any]] = None


@dataclass
class ReleaseContext:
    """Output of assemble_context."""

    repo: str
    commit: str
    changelog: list[str] = field(default_factory=list)
    open_incidents: int = 0
    # Provenance of the assembly: real snapshot digest when the catalog had
    # the project, empty + explanatory note on the canned-data fallback.
    snapshot_digest: str = ""
    note: str = ""
    # Propagated from ReleaseRequest.domain so downstream activities
    # (generate_workorders) know which generation domain to run.
    domain: str = ""


@dataclass
class WorkOrderBatch:
    """Output of generate_workorders.

    Carries the honest batch telemetry the judging gate needs: systemic
    aborts and generation-unavailable are FAILURE signals, never hidden
    behind a fake id list (gate semantics in generation_bridge.gate_verdict).
    """

    work_order_ids: list[str] = field(default_factory=list)
    summary: str = ""
    generated: int = 0
    errors: int = 0
    aborted_reason: str = ""
    unavailable: str = ""


@dataclass
class JudgeVerdict:
    """Output of judging_gate."""

    passed: bool
    score: float
    rationale: str = ""


@dataclass
class ApprovalDecision:
    """Payload of the human-approval Signal (sent from Phoenix/CLI)."""

    approved: bool
    approver: str
    reason: str = ""


@dataclass
class BuildInfo:
    """Output of build."""

    artifact_url: str
    build_id: str


@dataclass
class CanaryReport:
    """Output of canary deploy."""

    canary_url: str
    healthy: bool


@dataclass
class ReleaseResult:
    """Terminal workflow result."""

    status: str  # "promoted" | "rolled_back"
    reason: str
    repo: str
    commit: str
    stages: list[StageResult] = field(default_factory=list)
    approval: Optional[ApprovalDecision] = None
