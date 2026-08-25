"""UnityReleaseWorkflow — durable release pipeline for unity-explorer.

Promoted from spikes/temporal-unity-release/workflow.py with projection
recording added (ADR 0001 §4 step 6).

Stages:
    record_started (projection stage 0)
    -> assemble_context -> generate_workorders -> judging_gate
    -> WAIT for human approval Signal (durable timer timeout, default 24h)
    -> sandbox_preflight -> build -> canary
    -> monitor_window (durable timer) -> promote
Reject / timeout / gate-failure path -> rollback -> terminal "rolled_back".

Determinism rules honored: no I/O, no datetime.now(), no random in the
workflow body — only Temporal primitives (execute_activity, wait_condition,
timers). ALL projection writes go through activities (record_started /
record_stage / set_run_status in activities.py); this module never imports
bin.workflow_runs.

workflow_id convention: release:<repo>:<commit> (models.workflow_id_for) —
duplicate starts of the same release are rejected by the server
(idempotence) and the id itself is provenance.

Terminal projection statuses set here via the set_run_status activity:
    promoted   -> "done"
    rolled back-> "rolled-back"
    failure    -> "failed" (after compensation, before re-raising)
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activities and models pass through the workflow sandbox untouched
# (imported for type refs / registration names only).
with workflow.unsafe.imports_passed_through():
    from bin.release_workflow import activities
    from bin.release_workflow.models import (
        ApprovalDecision,
        BuildInfo,
        CanaryReport,
        JudgeVerdict,
        ReleaseContext,
        ReleaseRequest,
        ReleaseResult,
        RunStatusUpdate,
        StageRecord,
        StageResult,
        WorkOrderBatch,
        workflow_id_for,  # re-exported for callers  # noqa: F401
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
# Projection writes: local sqlite, idempotent/append-only — retry hard.
PROJECTION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=5,
)

# Short start_to_close for stubs; real build/canary need long timeouts +
# heartbeats (see activities.py docstrings).
STUB_TIMEOUT = timedelta(seconds=30)
PROJECTION_TIMEOUT = timedelta(seconds=10)


@workflow.defn
class UnityReleaseWorkflow:
    def __init__(self) -> None:
        self._approval: Optional[ApprovalDecision] = None
        self._stages: list[StageResult] = []
        self._current_stage: str = "created"
        self._projection_run_id: Optional[int] = None

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
            # 0. record_started — projection stage 0. The activity reads the
            # real workflow_id/run_id from activity.info(); idempotent per
            # execution so retries never duplicate rows.
            self._current_stage = "record_started"
            self._projection_run_id = await workflow.execute_activity(
                activities.record_started,
                req,
                start_to_close_timeout=PROJECTION_TIMEOUT,
                retry_policy=PROJECTION_RETRY,
            )

            # 1. assemble_context
            self._current_stage = "assemble_context"
            ctx: ReleaseContext = await workflow.execute_activity(
                activities.assemble_context,
                req,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=READ_RETRY,
            )
            await self._record(
                "assemble_context",
                "ok",
                ctx.note or f"{len(ctx.changelog)} changelog entries",
                projection_detail={
                    "snapshot_digest": ctx.snapshot_digest,
                    "note": ctx.note,
                },
            )

            # 2. generate_workorders
            self._current_stage = "generate_workorders"
            batch: WorkOrderBatch = await workflow.execute_activity(
                activities.generate_workorders,
                ctx,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=LM_RETRY,
            )
            await self._record("generate_workorders", "ok", batch.summary)

            # 3. judging_gate
            self._current_stage = "judging_gate"
            verdict: JudgeVerdict = await workflow.execute_activity(
                activities.judging_gate,
                batch,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=LM_RETRY,
            )
            if not verdict.passed:
                await self._record("judging_gate", "failed", verdict.rationale)
                return await self._roll_back(
                    req, f"judging gate failed: {verdict.rationale}"
                )
            await self._record("judging_gate", "ok", f"score={verdict.score}")

            # 4. human approval — Signal + durable timer timeout.
            self._current_stage = "awaiting_approval"
            await self._set_projection_status("awaiting-approval")
            try:
                await workflow.wait_condition(
                    lambda: self._approval is not None,
                    timeout=timedelta(seconds=req.approval_timeout_seconds),
                )
            except asyncio.TimeoutError:
                await self._record("approval", "failed", "timed out")
                return await self._roll_back(req, "approval timed out — auto-rejected")

            assert self._approval is not None
            if not self._approval.approved:
                await self._record(
                    "approval", "failed",
                    f"rejected by {self._approval.approver}: {self._approval.reason}",
                )
                return await self._roll_back(
                    req,
                    f"rejected by {self._approval.approver}: {self._approval.reason}",
                )
            await self._set_projection_status("running")
            await self._record(
                "approval", "ok", f"approved by {self._approval.approver}"
            )

            # 5. sandbox_preflight
            self._current_stage = "sandbox_preflight"
            preflight = await workflow.execute_activity(
                activities.sandbox_preflight,
                batch,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            await self._record("sandbox_preflight", "ok", preflight)

            # 6. build
            self._current_stage = "build"
            info: BuildInfo = await workflow.execute_activity(
                activities.build,
                req,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            await self._record("build", "ok", info.build_id)

            # 7. canary
            self._current_stage = "canary"
            report: CanaryReport = await workflow.execute_activity(
                activities.canary,
                info,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=DEPLOY_RETRY,
            )
            await self._record("canary", "ok", report.canary_url)

            # 8. monitor_window — a pure durable timer (NOT activity sleep),
            # then a point-in-time health read.
            self._current_stage = "monitor_window"
            await asyncio.sleep(req.monitor_window_seconds)  # durable timer
            healthy: bool = await workflow.execute_activity(
                activities.check_canary_health,
                report,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=READ_RETRY,
            )
            if not healthy:
                await self._record("monitor_window", "failed", "canary unhealthy")
                return await self._roll_back(
                    req, "canary unhealthy after monitor window"
                )
            await self._record("monitor_window", "ok", "canary healthy")

            # 9. promote
            self._current_stage = "promote"
            promoted = await workflow.execute_activity(
                activities.promote,
                info,
                start_to_close_timeout=STUB_TIMEOUT,
                retry_policy=BUILDISH_RETRY,
            )
            await self._record("promote", "ok", promoted)

            self._current_stage = "promoted"
            await self._set_projection_status(
                "done", detail={"result": "promoted", "build_id": info.build_id}
            )
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
            # compensation + mark the projection failed, then re-raise so
            # the workflow FAILS visibly — a failed release must not
            # masquerade as a clean rolled_back.
            self._current_stage = "failed"
            try:
                await workflow.execute_activity(
                    activities.rollback,
                    "workflow failure — compensating",
                    start_to_close_timeout=STUB_TIMEOUT,
                    retry_policy=ROLLBACK_RETRY,
                )
                await self._set_projection_status(
                    "failed", detail={"result": "failed"}
                )
            finally:
                pass
            raise

    # ------------------------------------------------------------ helpers
    async def _record(
        self,
        stage: str,
        status: str,
        detail: str = "",
        *,
        projection_detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Accumulate the stage on workflow state AND append it to the
        projection's stage history (via activity — determinism)."""
        self._stages.append(StageResult(stage=stage, status=status, detail=detail))
        if self._projection_run_id is None:
            return  # record_started itself failed terminally; nothing to write to
        merged = dict(projection_detail or {})
        if detail:
            merged.setdefault("summary", detail)
        await workflow.execute_activity(
            activities.record_stage,
            StageRecord(
                run_id=self._projection_run_id,
                stage=stage,
                status=status,
                detail=merged or None,
            ),
            start_to_close_timeout=PROJECTION_TIMEOUT,
            retry_policy=PROJECTION_RETRY,
        )

    async def _set_projection_status(
        self, status: str, *, detail: Optional[dict[str, Any]] = None
    ) -> None:
        if self._projection_run_id is None:
            return
        await workflow.execute_activity(
            activities.set_run_status,
            RunStatusUpdate(
                run_id=self._projection_run_id, status=status, detail=detail
            ),
            start_to_close_timeout=PROJECTION_TIMEOUT,
            retry_policy=PROJECTION_RETRY,
        )

    async def _roll_back(self, req: ReleaseRequest, reason: str) -> ReleaseResult:
        self._current_stage = "rolling_back"
        detail = await workflow.execute_activity(
            activities.rollback,
            reason,
            start_to_close_timeout=STUB_TIMEOUT,
            retry_policy=ROLLBACK_RETRY,
        )
        await self._record("rollback", "ok", detail)
        self._current_stage = "rolled_back"
        await self._set_projection_status(
            "rolled-back", detail={"result": "rolled_back", "reason": reason}
        )
        return ReleaseResult(
            status="rolled_back",
            reason=reason,
            repo=req.repo,
            commit=req.commit,
            stages=self._stages,
            approval=self._approval,
        )
