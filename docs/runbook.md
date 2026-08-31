# Release Platform Runbook

Operator guide for the unity-explorer release platform (waves 0–7,
2026-08-17). Verified procedures only — every command here was executed
during the wave-6/7 drills.

## State layout

    $DATA_TOURNAMENTS_HOME/          one unit of state — back up together
      judgements.db                  fabric DB (tournaments, catalog,
                                     workflow_run projection, approval audit)
      cas/sha256/xx/<hex>            content-addressed bodies (0444)
    Temporal server                  SOURCE OF TRUTH for execution state;
                                     workflow_run is a rebuildable projection

## Start the stack (dev)

    # 1. Temporal dev server (survives via its own state dir)
    nix run nixpkgs#temporal-cli -- server start-dev --headless --log-level error
    nix run nixpkgs#temporal-cli -- operator cluster health   # -> SERVING

    # 2. Release worker (spike venv carries temporalio; dev shell doesn't)
    DATA_TOURNAMENTS_HOME=... PYTHONPATH=$REPO \
      spikes/temporal-unity-release/.venv/bin/python -m bin.release_workflow.worker
    # optional: RELEASE_TASK_QUEUE=<queue> to isolate parallel workers

    # 3. Phoenix (ui/): DT_OPERATOR=<principal> enables approval buttons on /runs

## Run a release

    python -m bin.release_workflow.client start <repo> <commit> \
      --project <catalog-project> --requested-by <who> \
      [--approval-timeout S] [--monitor-window S]
    # workflow_id = release:<repo>:<commit> — idempotent per commit
    python -m bin.release_workflow.client status release:<repo>:<commit>

Stages: assemble_context -> generate_workorders -> judging_gate ->
HUMAN APPROVAL (durable timeout -> auto-reject) -> sandbox_preflight ->
build -> canary -> monitor_window -> promote. Reject/timeout/failure ->
rollback -> rolled_back.

## Approvals (human-only, audited)

Path: Phoenix /runs buttons or `python -c` via bin.approvals. NEVER signal
Temporal directly — bin/approvals.py is the sanctioned gateway:
fail-closed policy check (policy kind='approval', rule
{"approvers": [...], "scope": "release:*"}) -> append-only approval_event
row -> Signal. Audit is written BEFORE delivery: a failed send leaves
recorded intent (reconcile by re-submitting; duplicate Signals are safe —
first decision wins in the workflow).

Bootstrap a policy:

    python bin/catalog.py create-policy --name release-approvals \
      --kind approval --rule '{"approvers":["changeme"],"scope":"release:*"}'

## Backup / restore (drilled 2026-08-17)

    python bin/ops.py backup  --dest /backups          # online-safe, sha manifest
    python bin/ops.py restore --archive F --dest DIR   # verifies sha; --force to overwrite
    python bin/ops.py cas-verify                       # rows<->CAS consistency
    python bin/ops.py gc --dry-run                     # report only, never deletes

Restore drill result: projects, workflow_run rows, and all 5 audit events
read back intact; db sha256 verified. Temporal history is NOT in the
archive — the projection rebuilds from Temporal, not vice versa.

## Known sharp edges

- Dev shell python has NO temporalio: worker/client/integration tests run
  from spikes/temporal-unity-release/.venv (pinned requirements.txt).
- Time-skipping test server aborts under the macOS sandbox (GraalVM
  signals); conftest falls back to the live dev server automatically.
- A workflow whose approval window expired completes as rolled_back;
  Signals to it fail with 'workflow execution already completed' — the
  audit row still records the late intent (observed in drill; by design).
- macOS kill limits: orphaned listeners can't always be killed from the
  agent sandbox; pick a fresh port/queue instead of fighting.
- Approval UI principal = DT_OPERATOR env var (single-operator local
  deployment). Multi-user auth is out of scope until there are users.

## Credential-gated (config exists, needs keys/hardware)

- E2B sandbox runs: E2B_API_KEY + e2b package (bin/sandbox/e2b_backend.py)
- microvm runner: Linux host with KVM (infra/microvm/)
- Live GitHub/Unity Cloud fetches: tokens via named env vars, RUN_LIVE_TESTS
- Real Unity builds: Unity Cloud Build credentials (wave-6 stubs document
  the integration points in bin/release_workflow/activities.py)
