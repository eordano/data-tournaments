"""UnityReleaseWorkflow — durable release pipeline for unity-explorer.

Stages:
    assemble_context -> generate_workorders -> judging_gate
    -> WAIT for human approval Signal (durable timer timeout, default 24h)
    -> sandbox_preflight -> build -> canary
    -> monitor_window (durable timer) -> promote
Reject / timeout / gate-failure path -> rollback -> terminal "rolled_back".

Determinism rules honored: no I/O, no datetime.now(), no random in the
workflow body — only Temporal primitives (execute_activity, wait_condition,
workflow.now/timers). All side effects live in activities.py.

workflow_id convention: release:<repo>:<commit>  (see starter.py) — gives
idempotence (duplicate starts of the same release are rejected by the server)
and provenance for free.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activities and models pass through the workflow sandbox untouched
# (they are imported for type refs / registration names only).
with workflow.unsafe.imports_passed_through():
    import activities
    from models import (
        ApprovalDecision,
        BuildInfo,
        CanaryReport,
        JudgeVerdict,
        ReleaseContext,
        ReleaseRequest,
        ReleaseResult,
        StageResult,
        WorkOrderBatch,
    )

# --- retry policies (caller-side in Temporal; annotated in activities.py) ---
READ_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=5,
)
LM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)
BUILDISH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)
DEPLOY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_attempts=2,
)
ROLLBACK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=5,
)

# Short start_to_close for stubs; real build/canary need long timeouts +
# heartbeats (see activities.py docstrings).
STUB_TIMEOUT = timedelta(seconds=30)


def workflow_id_for(repo: str, commit: str) -> str:
    """workflow_id convention: release:<repo>:<commit>."""
    return f"release:{repo}:{commit}"


@workflow.defn
class UnityReleaseWorkflow:
    def __init__(self) -> None:
        self._approval: Optional[ApprovalDecision] = None
        self._stages: list[StageResult] = []
        self._current_stage: str = "created"

    # ------------------------------------------------------------- signals
    @workflow.signal
    def submit_approval(self, decision: ApprovalDecision) -> None:
        """Human approval Signal — sent by Phoenix LiveView button / CLI.

        First decision wins; later duplicates are ignored (signals must be
        tolerant of redelivery/dedup concerns on the sender side).
        """
        if self._approval is None:
            self._approval = decision

    # ------------------------------------------------------------- queries
    @workflow.query
    def current_stage(self) -> str:
        return self._current_stage

    @workflow.query
    def stage_results(self) -> list[StageResult]:
        return self._stages

    # --------------------------------------------------------------- run
    @workflow.run
    async def run(self, req: ReleaseRequest) -> ReleaseResult:
        try:
            # 1. assemble_context
            self._current_stage = "assemble_context"
            ctx: ReleaseContext = await workflow.execute_activity(
                activities.assemble_context,
                req,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=READ_RETRY,
            )
            self._record("assemble_context", "ok", f"{len(ctx.changelog)} changelog entries")

            # 2. generate_workorders
            self._current_stage = "generate_workorders"
            batch: WorkOrderBatch = await workflow.execute_activity(
                activities.generate_workorders,
                ctx,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=LM_RETRY,
            )
            self._record("generate_workorders", "ok", batch.summary)

            # 3. judging_gate
            self._current_stage = "judging_gate"
            verdict: JudgeVerdict = await workflow.execute_activity(
                activities.judging_gate,
                batch,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=LM_RETRY,
            )
            if not verdict.passed:
                self._record("judging_gate", "failed", verdict.rationale)
                return await self._roll_back(req, f"judging gate failed: {verdict.rationale}")
            self._record("judging_gate", "ok", f"score={verdict.score}")

            # 4. human approval — Signal + durable timer timeout.
            # wait_condition with timeout is the canonical HITL pattern:
            # the timer is durable (survives worker restarts, costs nothing
            # while waiting) and raises asyncio.TimeoutError on expiry.
            self._current_stage = "awaiting_approval"
            try:
                await workflow.wait_condition(
                    lambda: self._approval is not None,
                    timeout=timedelta(seconds=req.approval_timeout_seconds),
                )
            except asyncio.TimeoutError:
                self._record("approval", "failed", "timed out")
                return await self._roll_back(req, "approval timed out — auto-rejected")

            assert self._approval is not None
            if not self._approval.approved:
                self._record(
                    "approval", "failed",
                    f"rejected by {self._approval.approver}: {self._approval.reason}",
                )
                return await self._roll_back(
                    req, f"rejected by {self._approval.approver}: {self._approval.reason}"
                )
            self._record("approval", "ok", f"approved by {self._approval.approver}")

            # 5. sandbox_preflight
            self._current_stage = "sandbox_preflight"
            preflight = await workflow.execute_activity(
                activities.sandbox_preflight,
                batch,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            self._record("sandbox_preflight", "ok", preflight)

            # 6. build
            self._current_stage = "build"
            info: BuildInfo = await workflow.execute_activity(
                activities.build,
                req,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            self._record("build", "ok", info.build_id)

            # 7. canary
            self._current_stage = "canary"
            report: CanaryReport = await workflow.execute_activity(
                activities.canary,
                info,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=DEPLOY_RETRY,
            )
            self._record("canary", "ok", report.canary_url)

            # 8. monitor_window — a pure durable timer (NOT activity sleep),
            # then a point-in-time health read.
            self._current_stage = "monitor_window"
            await asyncio.sleep(req.monitor_window_seconds)  # durable in workflow ctx
            healthy: bool = await workflow.execute_activity(
                activities.check_canary_health,
                report,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=READ_RETRY,
            )
            if not healthy:
                self._record("monitor_window", "failed", "canary unhealthy")
                return await self._roll_back(req, "canary unhealthy after monitor window")
            self._record("monitor_window", "ok", "canary healthy")

            # 9. promote
            self._current_stage = "promote"
            promoted = await workflow.execute_activity(
                activities.promote,
                info,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            self._record("promote", "ok", promoted)

            self._current_stage = "promoted"
            return ReleaseResult(
                status="promoted",
                reason="all stages passed",
                repo=req.repo,
                commit=req.commit,
                stages=self._stages,
                approval=self._approval,
            )

        except Exception:
            # Activity retries exhausted (or unexpected failure): attempt
            # compensation, then re-raise so the workflow FAILS visibly —
            # a failed release must not masquerade as a clean rolled_back.
            self._current_stage = "failed"
            try:
                await workflow.execute_activity(
                    activities.rollback,
                    "workflow failure — compensating",
                    start_to_close_timeout=STUB_TIMEOUT,
                    retry_policy=ROLLBACK_RETRY,
                )
            finally:
                pass
            raise

    # ------------------------------------------------------------ helpers
    def _record(self, stage: str, status: str, detail: str = "") -> None:
        self._stages.append(StageResult(stage=stage, status=status, detail=detail))

    async def _roll_back(self, req: ReleaseRequest, reason: str) -> ReleaseResult:
        self._current_stage = "rolling_back"
        detail = await workflow.execute_activity(
            activities.rollback,
            reason,
            start_to_close_timeout=STUB_TIMEOUT,
            retry_policy=ROLLBACK_RETRY,
        )
        self._record("rollback", "ok", detail)
        self._current_stage = "rolled_back"
        return ReleaseResult(
            status="rolled_back",
            reason=reason,
            repo=req.repo,
            commit=req.commit,
            stages=self._stages,
            approval=self._approval,
        )
