# Operationalize & serve (wave-14): dt-stack supervisor + bound campaign

Status: CONTRACT. User asked to "spawn a new full workflow to
operationalize this and serve it" (sentence truncated; scope chosen:
supervised stack + fresh pipeline-bound campaign, per clarify default).

## Honesty constraints (non-negotiable)

- The pipeline registry (bin/pipelines.py) is a SPEC + fail-closed
  validator, NOT an executor. Operationalizing means: the stack runs,
  the domain is BOUND to branch-fix-review v1, and each stage is
  orchestrated by the existing per-stage tools (generate_cards,
  branch_author, fix_branches validate, branch_ship, release worker).
  Nothing may claim "pipeline engine executed this" — stage provenance
  stays with the stage tools.
- No secrets in any generated env file — var NAMES only; user fills
  values. .env stays user-owned at repo root.
- ~/NixOS is outside the allowed workspace: we do NOT read or write it.
  dt-stack is self-contained (launchd/systemd wiring is the user's
  call; the supervisor gives them the exact commands).
- Missing ship credentials => DRY-RUN promote, visibly labeled.

## 1. bin/dt_stack.py — the supervisor

Commands (argparse, campaigns.py conventions):
  up      start Temporal dev server (if not SERVING), release worker,
          UI (mix phx.server) — each with health check + pidfile under
          $DT_STACK_HOME/run/; idempotent (running components skipped).
  status  one line per component: name, pid, port, health (real check:
          temporal operator cluster health / HTTP 200 / queue poller).
  down    stop components in reverse order (worker, UI, temporal),
          pidfile-based, SIGTERM then SIGKILL after grace.
  logs    tail -n the per-component log files under run/logs/.

Config via env file $DT_STACK_HOME/stack.env (generated on first `up`
with documented defaults, values editable): DATA_TOURNAMENTS_HOME,
UI_PORT (default 4113), TEMPORAL_PORT (7233), RELEASE_TASK_QUEUE,
PROMPT_BACKEND, DT_OPERATOR, PYTHON (spike venv), NIX_HOME_UI.
Port handling: if UI_PORT is held by a live process -> refuse with the
pid; if held by a dead/stuck socket (the :4111 kernel-stuck case) ->
auto-increment and SAY SO.

## 2. Operational data home

$DT_STACK_HOME/home (persistent, NOT /tmp): catalog init, judgement
init (seeds v0 + wheel templates), pipelines init + seed-defaults,
one operational domain bound PERMANENTLY to branch-fix-review v1.
Binding asserted by DB row in evidence.

## 3. Serve + verify

dt-stack up -> health-check /, /judge, /environment (pipelines tab
shows the binding), /branch-fixes, /runs. Evidence: run.json with
component pids/ports/health + binding row + honest status vocabulary.

## Out of scope (stated, not silently dropped)

- A pipeline EXECUTOR (stage auto-advance) — future wave; would need
  run/stage event tables + fail-closed gating per stage.
- launchd/systemd units — user wires their NixOS config themselves;
  `dt_stack.py up` is the single entrypoint those units would call.
- Real shipping credentials (DRY-RUN stands).
