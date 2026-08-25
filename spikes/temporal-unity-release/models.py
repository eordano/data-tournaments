"""Typed payloads shared between workflow, activities, worker, and starter.

Plain dataclasses: temporalio's default data converter serializes them as JSON
with full type restoration on the workflow/activity side. When we integrate
PydanticAI/pydantic models for WorkOrders, switch the client/worker to
`temporalio.contrib.pydantic.pydantic_data_converter` and these become
pydantic.BaseModel subclasses with no other changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ReleaseRequest:
    """Input to UnityReleaseWorkflow. workflow_id = release:<repo>:<commit>."""

    repo: str
    commit: str
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
class ReleaseContext:
    """Output of assemble_context."""

    repo: str
    commit: str
    changelog: list[str] = field(default_factory=list)
    open_incidents: int = 0


@dataclass
class WorkOrderBatch:
    """Output of generate_workorders."""

    work_order_ids: list[str] = field(default_factory=list)
    summary: str = ""


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
