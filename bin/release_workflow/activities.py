"""Activities for the unity-explorer release workflow.

ALL side effects live here (Temporal rule: workflows are deterministic,
activities do I/O). Two kinds of activity in this module:

1. Projection activities (record_started / record_stage / set_run_status):
   write the workflow_run projection via bin.workflow_runs. Per ADR 0001 §2
   the projection is written ONLY by Python Temporal Activities — never by
   workflow bodies (determinism) and never by Elixir. The write discipline
   mirrors bin/workflow_runs.py's contract: start() is idempotent per
   (workflow_id, run_id) so activity retries never mint duplicate rows;
   record_stage() is append-only; set_status() is sticky-terminal.

2. Pipeline activities: assemble_context is REAL (calls
   bin.assemble_pack.assemble() when the catalog knows the project, with an
   explicit canned-data fallback otherwise). build/canary/
   check_canary_health/promote/rollback call the shipping layer
   (bin.release_workflow.shipping — wave 8 B6) when the relevant env vars
   are configured, and otherwise return their explicit stub values with
   notes; docstrings state the real integration, secrets, and retry policy
   (enforced caller-side in workflow.py).
"""

from __future__ import annotations

import os

from temporalio import activity

# NOTE: no sys.path munging and no module-level filesystem calls here — the
# workflow sandbox may (re)import this module while validating workflow.py,
# and restricted calls (e.g. pathlib.Path.resolve) abort worker startup.
# If `bin.release_workflow.activities` is importable at all, the repo root
# is already on sys.path.
from bin import workflow_runs
from bin.release_workflow.models import (
    BuildInfo,
    CanaryReport,
    JudgeVerdict,
    ReleaseContext,
    ReleaseRequest,
    RunStatusUpdate,
    StageRecord,
    WorkOrderBatch,
)

# ── projection activities ────────────────────────────────────────────────


@activity.defn
async def record_started(req: ReleaseRequest) -> int:
    """Stage 0: register this Temporal execution in the workflow_run
    projection and return the projection row id.

    Uses activity.info() for the REAL temporal workflow_id/run_id — the
    workflow body must not observe them (determinism). workflow_runs.start()
    is idempotent on (workflow_id, run_id): a retried record_started returns
    the existing row id instead of duplicating.

    RETRY POLICY: max 5 attempts, 1s initial, 2.0 backoff — local sqlite
    write, safe to retry (idempotent by contract).
    """
    info = activity.info()
    run_id = workflow_runs.start(
        temporal_workflow_id=info.workflow_id,
        temporal_run_id=info.workflow_run_id,
        detail={
            "repo": req.repo,
            "commit": req.commit,
            "project": req.project,
            "requested_by": req.requested_by,
        },
    )
    activity.logger.info(
        "record_started workflow_id=%s run row id=%d", info.workflow_id, run_id
    )
    return run_id


@activity.defn
async def record_stage(rec: StageRecord) -> None:
    """Append one entry to the projection's append-only stage history.

    History is never rewritten; a retried append at worst duplicates one
    entry with identical content (harmless for the audit UI, and Temporal
    only retries on failure — the common case appends exactly once).

    RETRY POLICY: max 5 attempts, 1s initial, 2.0 backoff.
    """
    workflow_runs.record_stage(
        rec.run_id, stage=rec.stage, status=rec.status, detail=rec.detail or None
    )


@activity.defn
async def set_run_status(upd: RunStatusUpdate) -> None:
    """Set the projection's coarse status (running / awaiting-approval /
    done / failed / rolled-back). Terminal statuses are sticky in
    bin.workflow_runs — a late retry cannot flip a finished run back.

    RETRY POLICY: max 5 attempts, 1s initial, 2.0 backoff.
    """
    workflow_runs.set_status(upd.run_id, upd.status, detail=upd.detail or None)


# ── pipeline activities ──────────────────────────────────────────────────


@activity.defn
async def assemble_context(req: ReleaseRequest) -> ReleaseContext:
    """Gather everything an LM planner needs about this release.

    REAL PATH: when ``req.project`` names a project the catalog knows, run
    bin.assemble_pack.assemble() — collect evidence from the project's
    active sources, persist an immutable LandscapeSnapshot + role packs, and
    surface the snapshot digest for citation.

    FALLBACK: project missing/empty or assembly fails (e.g. no evidence) —
    return the spike's canned data with an explicit note; never silently.

    SECRETS/CAPABILITIES: read access to the catalog DB and the project's
    git roots; (future) read-only GitHub token, monitoring API read key.

    RETRY POLICY: max 5 attempts, 1s initial, 2.0 backoff — reads are safe
    to retry; assemble() itself is idempotent (content-addressed inserts).
    """
    activity.logger.info(
        "assemble_context repo=%s commit=%s project=%r",
        req.repo, req.commit, req.project,
    )
    if req.project:
        from bin import catalog
        from bin.assemble_pack import AssembleError, assemble

        try:
            catalog.get_project(req.project)
        except LookupError:
            note = (
                f"project {req.project!r} not found in catalog — "
                "fell back to canned spike data"
            )
        else:
            try:
                result = assemble(
                    req.project,
                    objective=f"release {req.repo}@{req.commit}",
                )
                return ReleaseContext(
                    repo=req.repo,
                    commit=req.commit,
                    changelog=[
                        f"snapshot {result.snapshot_digest} assembled from "
                        f"sources: {', '.join(result.collected_sources)}"
                    ],
                    open_incidents=0,
                    snapshot_digest=result.snapshot_digest,
                    note=f"assembled from catalog project {req.project!r}",
                    domain=req.domain,
                )
            except AssembleError as err:
                note = (
                    f"assembly failed for project {req.project!r} ({err}) — "
                    "fell back to canned spike data"
                )
    else:
        note = "no project supplied — canned spike data"

    return ReleaseContext(
        repo=req.repo,
        commit=req.commit,
        changelog=[f"stub: commit {req.commit[:12]} touches Explorer/Assets"],
        open_incidents=0,
        snapshot_digest="",
        note=note,
        domain=req.domain,
    )


@activity.defn
async def generate_workorders(ctx: ReleaseContext) -> WorkOrderBatch:
    """Run the REAL work-order generation pipeline when a domain is set.

    Delegates to bin.release_workflow.generation_bridge (temporalio-free,
    tested in the root suite): generate_cards.run(domain,
    artifact="work-order") — the full pipeline with provenance stamping,
    cited_evidence, failure taxonomy, and systemic circuit breaker. When no
    domain is configured or the worker env lacks the generation stack, the
    batch carries an explicit note — never fake success.

    SECRETS/CAPABILITIES: LLM provider API key, Langfuse keys, fabric DB
    write access (pending_judgement pairs + evidence refs).

    RETRY POLICY: max 3 attempts, 2s initial, 2.0 backoff. The bridge
    encodes generation failures in its RESULT (not exceptions), so retries
    only fire on infrastructure faults, not on bad generations.
    """
    activity.logger.info("generate_workorders for %s@%s", ctx.repo, ctx.commit)
    if not ctx.domain:
        return WorkOrderBatch(
            work_order_ids=["wo-build-001", "wo-smoke-002", "wo-deploy-003"],
            summary="stub: no generation domain configured on the request",
        )
    from bin.release_workflow.generation_bridge import run_generation

    out = run_generation(ctx.domain)
    return WorkOrderBatch(
        work_order_ids=list(out["work_order_ids"]),
        summary=out["summary"],
        generated=out["generated"],
        errors=out["errors"],
        aborted_reason=out["aborted_reason"],
        unavailable=out["unavailable"],
    )


@activity.defn
async def judging_gate(batch: WorkOrderBatch) -> JudgeVerdict:
    """Batch-level judging gate with HONEST semantics (was: auto-pass 0.92).

    Delegates to generation_bridge.gate_verdict: systemic aborts,
    generation-unavailable, and empty batches FAIL the gate (workflow rolls
    back without bothering a human); healthy batches pass with a score
    derived from the batch's success ratio. The per-pair LLM tournament
    verdict happens in the judge UI / drain_llm_queue — deliberately not
    faked here.

    RETRY POLICY: max 3 attempts, 2s initial, 2.0 backoff.
    """
    activity.logger.info("judging_gate on %d work orders", len(batch.work_order_ids))
    from bin.release_workflow.generation_bridge import gate_verdict

    passed, score, rationale = gate_verdict(
        work_order_ids=list(batch.work_order_ids),
        aborted_reason=batch.aborted_reason,
        unavailable=batch.unavailable,
        errors=batch.errors,
        generated=batch.generated,
    )
    return JudgeVerdict(passed=passed, score=score, rationale=rationale)


@activity.defn
async def sandbox_preflight(batch: WorkOrderBatch) -> str:
    """Dry-run the WorkOrders in an isolated sandbox. STUB (wave 6).

    REAL IMPLEMENTATION: execute WorkOrders in a throwaway sandbox (nix
    shell / container) against a scratch clone — compile checks, unit smoke,
    asset validation. No production credentials by design.

    RETRY POLICY: max 3 attempts, 5s initial, 2.0 backoff.
    """
    activity.logger.info("sandbox_preflight: %s", batch.summary)
    return "preflight-ok"


@activity.defn
async def build(req: ReleaseRequest) -> BuildInfo:
    """Produce the Unity client/server artifacts for <commit>.

    REAL PATH (wave 8): when UNITY_CLOUD_BUILD_API_KEY + SHIP_UCB_ORG +
    SHIP_UCB_PROJECT + SHIP_UCB_TARGET are set, trigger a Unity Cloud
    Build via bin.release_workflow.shipping.UCBTracker and poll once for
    status/artifact. Otherwise return the explicit stub values below.
    Long-poll heartbeating (~30-60 min builds) remains future work — the
    workflow's monitor window covers the gap for now.

    SECRETS/CAPABILITIES: CI dispatch token, Unity license, artifact store
    write credentials.

    RETRY POLICY: max 3 attempts, 5s initial, 2.0 backoff.
    """
    activity.logger.info("build %s@%s", req.repo, req.commit)
    org = os.environ.get("SHIP_UCB_ORG", "")
    project = os.environ.get("SHIP_UCB_PROJECT", "")
    target = os.environ.get("SHIP_UCB_TARGET", "")
    if org and project and target and os.environ.get("UNITY_CLOUD_BUILD_API_KEY"):
        from bin.release_workflow.shipping import UCBTracker

        tracker = UCBTracker(org=org, project=project)
        triggered = tracker.trigger_build(target, req.commit)
        polled = tracker.poll_build(target, triggered["build_number"])
        return BuildInfo(
            artifact_url=polled.get("artifact_url", ""),
            build_id=f"ucb:{target}#{triggered['build_number']}",
        )
    return BuildInfo(
        artifact_url=f"https://artifacts.example/{req.commit}.tar.zst",
        build_id=f"dry-run-{req.commit[:8]}",
    )


@activity.defn
async def canary(info: BuildInfo) -> CanaryReport:
    """Deploy the build to the canary environment.

    REAL PATH (wave 8, partial): when SHIP_CANARY_URL is set, probe it
    via bin.release_workflow.shipping.CanaryMonitor and report honest
    health. Rolling the artifact out / registering with monitoring still
    needs deploy credentials — not implemented, per the shipping module's
    no-fake-execution rule. Otherwise return the explicit stub below.

    SECRETS/CAPABILITIES: canary deploy credentials, monitoring write key.

    RETRY POLICY: max 2 attempts, 5s initial — deploys are not hammered.
    """
    activity.logger.info("canary deploy of %s", info.build_id)
    canary_url = os.environ.get("SHIP_CANARY_URL", "")
    if canary_url:
        from bin.release_workflow.shipping import CanaryMonitor

        result = CanaryMonitor().check(canary_url)
        return CanaryReport(canary_url=canary_url, healthy=result["healthy"])
    return CanaryReport(
        canary_url="dry-run://canary-not-deployed", healthy=True
    )


@activity.defn
async def check_canary_health(report: CanaryReport) -> bool:
    """Point-in-time canary health check after the monitor window.

    REAL PATH (wave 8, partial): when SHIP_CANARY_URL is set, re-probe it
    now via shipping.CanaryMonitor (point-in-time, honest). SLO queries
    over the window (error/crash/latency from monitoring) remain future
    work. Otherwise echo the deploy-time report (stub). (The waiting
    itself is a durable workflow timer.)

    RETRY POLICY: max 5 attempts, 1s initial — pure read.
    """
    activity.logger.info("check_canary_health %s", report.canary_url)
    canary_url = os.environ.get("SHIP_CANARY_URL", "")
    if canary_url:
        from bin.release_workflow.shipping import CanaryMonitor

        return bool(CanaryMonitor().check(canary_url)["healthy"])
    return report.healthy


@activity.defn
async def promote(info: BuildInfo) -> str:
    """Promote the canary build to production.

    REAL PATH (wave 8, partial): when GITHUB_TOKEN + SHIP_GITHUB_REPO +
    SHIP_PR_BRANCH are set, open/update the release PR idempotently via
    shipping.GitHubShipper (base = SHIP_PR_BASE, default 'main').
    Flipping the production pointer / tagging remains future work. NOTE:
    PR creation and promote are SEPARATE approvable actions
    (shipping.ACTION_SCOPES); the approvals layer gates them upstream.
    Otherwise return the explicit stub below. Idempotent per build_id.

    SECRETS/CAPABILITIES: production deploy credentials, GitHub tag/release
    token, webhook URLs.

    RETRY POLICY: max 3 attempts, 5s initial.
    """
    activity.logger.info("promote %s to production", info.build_id)
    repo = os.environ.get("SHIP_GITHUB_REPO", "")
    branch = os.environ.get("SHIP_PR_BRANCH", "")
    if repo and branch and os.environ.get("GITHUB_TOKEN"):
        from bin.release_workflow.shipping import GitHubShipper

        pr = GitHubShipper(repo=repo).create_or_update_pr(
            branch,
            os.environ.get("SHIP_PR_BASE", "main"),
            f"release: {info.build_id}",
            f"Automated release PR for build {info.build_id}.",
        )
        return f"promoted:{info.build_id} pr#{pr['number']} ({pr['action']})"
    return (
        f"promoted:{info.build_id} "
        "[DRY-RUN — no shipping credentials; nothing was deployed]"
    )


@activity.defn
async def rollback(reason: str) -> str:
    """Compensation: tear down canary / revert any partial rollout.

    REAL PATH (wave 8, contract only): when SHIP_CANARY_URL is set the
    typed rollback plan from shipping.CanaryMonitor.rollback_plan is
    surfaced in the result — a documented NO-OP contract (executed=False),
    never fake-executed. Actually destroying the canary deployment,
    reverting the production pointer, marking WorkOrders aborted, and
    notifying humans requires deploy credentials this worker does not
    hold. Otherwise return the explicit stub below.

    RETRY POLICY: max 5 attempts, 2s initial — compensation must eventually
    succeed; also invoked from failure paths.
    """
    activity.logger.info("rollback: %s", reason)
    if os.environ.get("SHIP_CANARY_URL"):
        from bin.release_workflow.shipping import CanaryMonitor

        plan = CanaryMonitor().rollback_plan({"reason": reason})
        steps = "; ".join(plan["requires"])
        return f"rolled-back ({reason}) [plan not executed: {steps}]"
    return f"rolled-back ({reason})"
