# Spike: Temporal release workflow for unity-explorer

Proves the workflow shape selected in
`docs/research/durable-workflow-orchestration-2026.md` (self-hosted Temporal +
Python workers, later PydanticAI `TemporalDurability`) **before** integrating
into `bin/` + Phoenix.

```
assemble_context -> generate_workorders -> judging_gate
  -> human approval Signal  (durable timer timeout, default 24h -> auto-reject)
  -> sandbox_preflight -> build -> canary
  -> monitor_window (durable timer, default 30m) -> promote
reject / timeout / gate-fail  -> rollback -> terminal "rolled_back"
activity retries exhausted    -> rollback compensation -> workflow FAILS visibly
```

- `workflow_id = release:<repo>:<commit>` — duplicate starts rejected by the
  server (`WorkflowAlreadyStartedError`) = release idempotence for free.
- All stage results accumulate in typed dataclasses (`models.py`) and are
  returned in the terminal `ReleaseResult`; `current_stage` / `stage_results`
  Queries expose live progress (this is what a LiveView poller would read).
- Workflow body is deterministic: only Temporal primitives
  (`execute_activity`, `wait_condition(timeout=...)`, workflow-context
  `asyncio.sleep` = durable timer). All side effects are stub activities
  (`activities.py`) whose docstrings say what the real impl does and which
  secrets it needs.

## Files

| file | what |
|---|---|
| `models.py` | typed payloads (dataclasses; pydantic-ready — see note below) |
| `workflow.py` | `UnityReleaseWorkflow` + retry policies + `workflow_id_for()` |
| `activities.py` | 9 stub activities with real-impl/secrets/retry docstrings |
| `worker.py` | worker for task queue `unity-release` |
| `starter.py` | starts a release, sends approve/reject signal, prints result |
| `tests/test_workflow.py` | 4 end-to-end tests (see below) |
| `conftest.py` | time-skipping env with live-dev-server fallback |

## How to run

```sh
# 1. venv (NOTE: uv is banned in this repo — plain venv from the nix dev shell python)
HOME=$(mktemp -d) nix develop --command python -m venv spikes/temporal-unity-release/.venv
spikes/temporal-unity-release/.venv/bin/pip install temporalio pytest pytest-asyncio

# 2. dev server (temporal-cli 1.8.2 / Server 1.31.2 from nixpkgs)
nix run nixpkgs#temporal-cli -- server start-dev        # UI at http://localhost:8233

# 3. tests
cd spikes/temporal-unity-release && .venv/bin/python -m pytest -v

# 4. live demo
.venv/bin/python worker.py                              # terminal 2
.venv/bin/python starter.py abc123def456 --approve      # terminal 3 (or --reject / --no-signal)
```

## What was ACTUALLY executed in this spike (real output)

Environment: macOS arm64, temporalio SDK 1.31.0, temporal-cli 1.8.2
(Server 1.31.2) via `nix shell nixpkgs#temporal-cli`, dev server
`temporal server start-dev --headless`.

**pytest — 4/4 end-to-end tests against the live dev server:**

```
tests/test_workflow.py::test_happy_path_approved_promotes PASSED         [ 25%]
tests/test_workflow.py::test_approval_timeout_rolls_back PASSED          [ 50%]
tests/test_workflow.py::test_explicit_rejection_rolls_back PASSED        [ 75%]
tests/test_workflow.py::test_activity_failure_retries_then_fails_workflow PASSED [100%]

============================== 4 passed in 15.36s ==============================
```

**worker.py + starter.py live run:**

```
started workflow release:decentraland/unity-explorer:abc123def456 (run_id=01a00db1-...)
sent approval signal

terminal status: promoted — all stages passed
  [    ok] assemble_context: 1 changelog entries
  [    ok] generate_workorders: stub: 3 work orders generated
  [    ok] judging_gate: score=0.92
  [    ok] approval: approved by starter-cli
  [    ok] sandbox_preflight: preflight-ok
  [    ok] build: build-abc123de
  [    ok] canary: https://canary.example/unity
  [    ok] monitor_window: canary healthy
  [    ok] promote: promoted:build-abc123de

terminal status: rolled_back — rejected by starter-cli: demo reject
  ...
  [failed] approval: rejected by starter-cli: demo reject
  [    ok] rollback: rolled-back (rejected by starter-cli: demo reject)
```

**Idempotence + query check:**

```
duplicate start correctly rejected: WorkflowAlreadyStartedError
query current_stage: assemble_context
```

`temporal workflow list` confirms server-side history:
`Completed release:decentraland/unity-explorer:abc123def456`,
`Completed ...:999rejectme` (rolled_back result), and one intentional
`Failed` from the retry-exhaustion test.

### Sandbox caveat: time-skipping test server

`WorkflowEnvironment.start_time_skipping()` downloads a GraalVM-native Java
test server; **in this macOS agent sandbox it aborts at startup**
(`CSunMiscSignal.create() failed. errno: 1 Operation not permitted` — signal
handler/semaphore init blocked). `conftest.py` therefore falls back to
`WorkflowEnvironment.from_client()` against the live dev server, and tests
shrink timers (24h→3s approval, 30m→1s monitor) when
`env.supports_time_skipping` is false. On an unsandboxed machine or CI the
same tests run the true 24h/30m timers via time-skipping — no code changes.

## temporalio API friction worth knowing (for the real integration)

1. **Retry policies are caller-side.** `RetryPolicy` is passed per
   `execute_activity` call in the workflow, not on `@activity.defn`. We
   annotate the intent in activity docstrings and enforce in `workflow.py`.
2. **Workflow sandbox imports.** Anything imported by the workflow module that
   isn't deterministic-safe must go inside
   `with workflow.unsafe.imports_passed_through():` — including your own
   `activities`/`models` modules (avoids re-import validation cost and pydantic
   sandbox issues later).
3. **`wait_condition(timeout=...)` raises `asyncio.TimeoutError`** — the
   approval gate is a try/except, not a return value.
4. **Test-server dispatch is by activity *name*** — the retry test overrides
   `generate_workorders` with `@activity.defn(name="generate_workorders")` on a
   failing stub; no monkeypatching needed.
5. **Failure surfacing:** client gets `WorkflowFailureError` whose `.cause` is
   `ActivityError` whose `.cause` is the original `ApplicationError`. Retry
   count observed exactly matched `maximum_attempts=3`.
6. **Dataclasses vs pydantic:** default converter handles dataclasses with
   full type restore. For pydantic models switch client+worker to
   `temporalio.contrib.pydantic.pydantic_data_converter` — do this from day
   one in the real integration since WorkOrders are already pydantic.
7. **Real build/canary activities need heartbeats** + long
   `start_to_close_timeout`; stubs here use 30s.

## Integration into bin/ + Phoenix (per the research doc)

- **Workers stay in Python** (only place the SDK is needed). A `bin/` entry
  point wraps `worker.py`'s registration list; WorkOrder generation/judging
  activities call the existing DSPy pipeline, later via
  `pydantic-ai[temporal]` `TemporalDurability` so LM/tool calls become nested
  activities automatically.
- **Phoenix needs only the thin client surface**: start workflow, send
  `submit_approval` Signal (the LiveView approve/reject buttons), query
  `current_stage`/`stage_results`. Two options from the research doc:
  1. **Elixir → gRPC frontend directly** (grpc-elixir + generated protos for
     `StartWorkflowExecution` / `SignalWorkflowExecution` /
     `QueryWorkflow`). No SDK needed for these three calls; payload encoding
     must match the Python data converter (JSON payloads — fine).
  2. **Python sidecar** (FastAPI) exposing `/releases`, `/releases/:id/approve`
     etc. Simpler, one more process; likely the pragmatic first step since a
     Python worker process exists anyway (same deployable can serve both).
- **Nix/prod**: `temporal` server package exists in nixpkgs; prod = server +
  Postgres systemd units per the self-hosted guide. Dev stays
  `temporal server start-dev`.

## Open questions for the ADR-dependent WorkflowRun persistence

1. **Source of truth vs projection.** Temporal event history IS the durable
   audit record. Does the ADR's `WorkflowRun` table become a *projection*
   (workers upsert stage transitions into Postgres for LiveView reads), or
   does Phoenix query Temporal directly (Query calls / visibility API) and we
   persist nothing? Projection favors LiveView ergonomics (PubSub on row
   change) but introduces dual-write drift; direct query keeps one source of
   truth but couples page loads to Temporal availability.
2. **ID mapping.** `release:<repo>:<commit>` is the workflow_id; runs re-using
   an id get new run_ids. Does `WorkflowRun` key on (workflow_id, run_id) and
   how do WorkOrder rows FK to it?
3. **Retention.** Dev-server history is ephemeral; prod retention is
   namespace-configurable (default 30d). If `WorkflowRun` rows must outlive
   history retention, the projection answer to Q1 is forced for long-term
   provenance.
4. **Signal audit.** Approval decisions land in workflow history, but should
   the approver/reason also be written to the app DB for reporting without
   history scans?
5. **Child workflows.** The research doc's end-state models each WorkOrder
   execution as a child workflow (`workflow_id = work_order_id`). Does
   `WorkflowRun` model the parent only, or parent + children rows?
