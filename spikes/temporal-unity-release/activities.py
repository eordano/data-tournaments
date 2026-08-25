"""Stub activities for the unity-explorer release workflow.

ALL side effects live here (Temporal rule: workflows are deterministic,
activities do I/O). Every stub prints what it would do and returns canned,
typed data. Docstrings state what the REAL implementation will do and which
secrets/capabilities it needs, plus the retry policy the workflow attaches.

Retry-policy annotations below are enforced in workflow.py (RetryPolicy is a
caller-side concern in Temporal — the workflow decides per-invocation).
"""

from __future__ import annotations

from temporalio import activity

from models import (
    BuildInfo,
    CanaryReport,
    JudgeVerdict,
    ReleaseContext,
    ReleaseRequest,
    WorkOrderBatch,
)


@activity.defn
async def assemble_context(req: ReleaseRequest) -> ReleaseContext:
    """Gather everything an LM planner needs about this release.

    REAL IMPLEMENTATION: clone/fetch unity-explorer at <commit>, diff against
    the last released tag, pull linked issues/PRs, current incident status
    from monitoring, and prior release outcomes from the WorkOrder store.

    SECRETS/CAPABILITIES: read-only GitHub token (decentraland/unity-explorer),
    monitoring API read key, read access to the WorkOrder Postgres.

    RETRY POLICY: max 5 attempts, 1s initial, 2.0 backoff — network reads are
    safe to retry aggressively.
    """
    activity.logger.info("assemble_context repo=%s commit=%s", req.repo, req.commit)
    return ReleaseContext(
        repo=req.repo,
        commit=req.commit,
        changelog=[f"stub: commit {req.commit[:12]} touches Explorer/Assets"],
        open_incidents=0,
    )


@activity.defn
async def generate_workorders(ctx: ReleaseContext) -> WorkOrderBatch:
    """Run the DSPy/pydantic WorkOrder generation pipeline.

    REAL IMPLEMENTATION: invoke the existing data-tournaments generation
    pipeline (or a PydanticAI agent with TemporalDurability, in which case the
    LM calls themselves become nested activities) to produce typed WorkOrders
    for build/test/deploy steps; persist them to the WorkOrder store.

    SECRETS/CAPABILITIES: LLM provider API key (via env / secret manager),
    Langfuse keys for tracing, write access to WorkOrder Postgres.

    RETRY POLICY: max 3 attempts, 2s initial, 2.0 backoff — LM calls are
    expensive; retries mainly cover transient provider 5xx.
    """
    activity.logger.info("generate_workorders for %s@%s", ctx.repo, ctx.commit)
    return WorkOrderBatch(
        work_order_ids=["wo-build-001", "wo-smoke-002", "wo-deploy-003"],
        summary="stub: 3 work orders generated",
    )


@activity.defn
async def judging_gate(batch: WorkOrderBatch) -> JudgeVerdict:
    """Score the WorkOrder batch with the LLM judge tournament.

    REAL IMPLEMENTATION: run the existing judge tournament over the generated
    WorkOrders; emit score + rationale; below-threshold batches fail the gate
    (workflow rolls back without bothering a human).

    SECRETS/CAPABILITIES: judge LLM API key (config .judge.api_key_env),
    Langfuse keys.

    RETRY POLICY: max 3 attempts, 2s initial, 2.0 backoff.
    """
    activity.logger.info("judging_gate on %d work orders", len(batch.work_order_ids))
    return JudgeVerdict(passed=True, score=0.92, rationale="stub: judged fine")


@activity.defn
async def sandbox_preflight(batch: WorkOrderBatch) -> str:
    """Dry-run the WorkOrders in an isolated sandbox.

    REAL IMPLEMENTATION: execute WorkOrders in a throwaway sandbox (nix shell /
    container) against a scratch clone — compile checks, unit smoke, asset
    validation. No production credentials present by design.

    SECRETS/CAPABILITIES: none beyond repo read access; sandbox runner
    capability (local or CI executor).

    RETRY POLICY: max 3 attempts, 5s initial, 2.0 backoff.
    """
    activity.logger.info("sandbox_preflight: %s", batch.summary)
    return "preflight-ok"


@activity.defn
async def build(req: ReleaseRequest) -> BuildInfo:
    """Produce the Unity client/server artifacts for <commit>.

    REAL IMPLEMENTATION: trigger the unity-explorer build (CI dispatch or
    self-hosted Unity builder), wait for completion (heartbeating — real
    builds take ~30-60 min, so start_to_close must be generous and the
    activity must heartbeat), upload artifacts.

    SECRETS/CAPABILITIES: CI dispatch token, Unity license, artifact store
    write credentials.

    RETRY POLICY: max 3 attempts, 10s initial, 2.0 backoff. Build must be
    idempotent per (repo, commit) — re-runs return the cached artifact.
    """
    activity.logger.info("build %s@%s", req.repo, req.commit)
    return BuildInfo(
        artifact_url=f"https://artifacts.example/{req.commit}.tar.zst",
        build_id=f"build-{req.commit[:8]}",
    )


@activity.defn
async def canary(info: BuildInfo) -> CanaryReport:
    """Deploy the build to the canary environment.

    REAL IMPLEMENTATION: roll the artifact to a canary realm/slice, run
    synthetic checks, register the deployment with monitoring.

    SECRETS/CAPABILITIES: deploy credentials for the canary environment,
    monitoring API write key.

    RETRY POLICY: max 2 attempts, 5s initial — deploys should not be
    hammered; a second failure escalates to workflow failure.
    """
    activity.logger.info("canary deploy of %s", info.build_id)
    return CanaryReport(canary_url="https://canary.example/unity", healthy=True)


@activity.defn
async def check_canary_health(report: CanaryReport) -> bool:
    """Point-in-time canary health check, called after the monitor window.

    REAL IMPLEMENTATION: query monitoring for error rate / crash rate /
    latency SLOs over the monitor window; return pass/fail. (The waiting
    itself is a durable workflow timer, NOT activity sleep.)

    SECRETS/CAPABILITIES: monitoring API read key.

    RETRY POLICY: max 5 attempts, 1s initial — pure read.
    """
    activity.logger.info("check_canary_health %s", report.canary_url)
    return report.healthy


@activity.defn
async def promote(info: BuildInfo) -> str:
    """Promote the canary build to production.

    REAL IMPLEMENTATION: flip the production pointer / full rollout, tag the
    release in GitHub, announce (Slack/Discord webhook), record provenance.

    SECRETS/CAPABILITIES: production deploy credentials, GitHub token with
    tag/release write, webhook URLs.

    RETRY POLICY: max 3 attempts, 5s initial. Must be idempotent per build_id.
    """
    activity.logger.info("promote %s to production", info.build_id)
    return f"promoted:{info.build_id}"


@activity.defn
async def rollback(reason: str) -> str:
    """Compensation: tear down canary / revert any partial rollout.

    REAL IMPLEMENTATION: destroy the canary deployment if present, revert
    production pointer if promotion partially applied, mark WorkOrders as
    aborted, notify humans with the reason (rejected / timeout / failure).

    SECRETS/CAPABILITIES: deploy credentials, WorkOrder store write, webhooks.

    RETRY POLICY: max 5 attempts, 2s initial — compensation must eventually
    succeed; also invoked from failure paths.
    """
    activity.logger.info("rollback: %s", reason)
    return f"rolled-back ({reason})"
