---
name: release-unity-explorer
description: Drive a unity-explorer release candidate through the gated Temporal release workflow — assemble, generate, judge, approve, sandbox-verify, build, canary, monitor, promote or rollback.
version: 0.1.0
---

# Release unity-explorer

Orchestrate one release of github.com:decentraland/unity-explorer from RC
ref to promoted (or rolled-back) release via the durable release workflow.
This skill COORDINATES; it never performs side effects itself — the
Temporal workflow's activities do, behind approval gates.

## Required evidence
- Project `unity-explorer` in the catalog with sources: git repo,
  GitHub releases/tags, Unity Cloud Build.
- RC ref resolvable to a commit; that commit's CI status visible.
- Release policy for the target environment (who may approve, canary
  budget, monitor window duration).

## Capabilities (allowlist)
- `assemble_pack`, `start_release_workflow`, `inspect_run`, `fetch_artifact`
- Resource reads: all `landscape://*`
- NOT `signal_approval` — approvals are human-only, via Phoenix.

## Procedure
1. Assemble packs: creator pack (release-notes/work-order generation) and
   executor pack (verification) for the RC commit.
2. `start_release_workflow(project_id, rc_ref)` →
   `workflow_id = release:unity-explorer:<commit>`. Idempotent: if a
   workflow for this commit exists, attach to it instead of starting new.
3. Monitor via `inspect_run`. Stage sequence (enforced by the workflow,
   not by you): assemble → generate workorders → judging gate (tournament
   reaches quorum) → HUMAN APPROVAL (Signal, durable timeout → auto-
   reject) → sandbox preflight → build (Unity Cloud) → canary deploy →
   monitor window (durable timer) → promote. Reject/timeout/failed
   monitor → rollback stage runs and the workflow terminates as
   rolled_back.
4. At each human gate: surface a decision brief — what changed (cited
   EvidenceRefs), judging outcomes, sandbox verification report, canary
   metrics — to the approver in Phoenix. Never nag beyond the policy's
   escalation schedule; the durable timer handles absence.
5. On terminal state: produce the release manifest — commit, packs,
   WorkOrders executed, verdicts, approvals (who/when), artifacts,
   monitor summary — as the audit record.

## Approval boundaries
- Every deploy/promote/rollback stage is workflow-gated; this skill can
  only observe and assemble briefs. It cannot approve, cannot skip a
  stage, cannot signal.

## Outputs
- Workflow id, stage-by-stage status reports, final release manifest.

## Failure handling
- Stage failure → report the failing activity's error and artifacts;
  the workflow's retry policy owns retries, not you.
- Monitor-window regression → the workflow auto-rolls back; your job is
  the honest post-mortem brief citing the canary evidence.
- Never restart a failed release by deleting history — start a new
  workflow for a new RC commit instead.
