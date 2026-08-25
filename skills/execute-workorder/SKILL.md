---
name: execute-workorder
description: Execute an approved WorkOrder inside a pinned sandbox using an executor-role ContextPack. Use only after judging and explicit human approval.
version: 0.1.0
---

# Execute work order

Carry out one WorkOrder's implementation plan inside a sandbox pinned to
the work order's base commit. Execution is the highest-privilege skill:
everything here is deny-by-default.

## Required evidence
- WorkOrder id with intact provenance (base commit, repo snapshot).
- Executor-role pack digest — by construction contains NO tier-3
  (external/untrusted) evidence. If the pack role is not `executor`, stop.
- Sandbox profile named by policy for this project/environment.
- A recorded human approval for this execution (workflow Signal or
  explicit policy grant). No approval, no execution.

## Capabilities (allowlist)
- `launch_sandbox`, `inspect_run`, `fetch_artifact`
- Resource reads: `landscape://packs/{digest}`, `landscape://workorders/{id}`

## Procedure
1. Verify approval exists and covers THIS work order id. Verify pack role
   is executor and pack digest matches the one approved.
2. `launch_sandbox(pack_digest, profile, workorder_id)` — sandbox is
   pinned to (flake.lock, base commit); egress deny-by-default; secrets
   arrive per-step via the proxy, never as plaintext in the workspace.
3. Inside the sandbox, follow the WorkOrder plan step by step. Deviations
   are allowed only when a step is impossible as written — record every
   deviation with a reason in the run report.
4. Run the WorkOrder's acceptance criteria as literal checks. Each
   criterion gets pass/fail + evidence (command output, file diff).
5. Export artifacts (diffs, test output, build logs) via `fetch_artifact`
   handles; produce a run report citing them.

## Approval boundaries
- Entry itself is approval-gated (see Required evidence).
- Any step that would touch a real environment (deploy, tag push,
  store publish) is NOT this skill — that belongs to the release
  workflow's gated stages. If the plan asks for it, stop and report.

## Outputs
- Run report: per-criterion pass/fail with evidence, deviations, artifact
  handles, and a recommendation (ready-to-merge / needs-rework / blocked).

## Failure handling
- Sandbox launch failure → report profile + reason; never retry onto a
  weaker profile or outside the sandbox.
- Acceptance criteria failing is a VALID outcome — report honestly with
  evidence; never massage a fail into a pass.
- Budget/timeout exhaustion → stop, export partial artifacts, report.
