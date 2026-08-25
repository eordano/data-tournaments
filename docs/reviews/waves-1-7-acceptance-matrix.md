# Waves 1–7 Acceptance Matrix (Track A1, wave-8 charter)

Audit date: 2026-08-17 · HEAD: `b79b048` · Auditor: automated code
audit — every claim below verified by reading code/tests at this commit,
not by trusting plan checkboxes.

> Worktree note: while this audit ran, an uncommitted in-flight change to
> `bin/generate_cards.py` appeared (a parallel Track-B1 slice adding
> pipeline-side `cited_evidence` stamping to `_work_order_payload`). All
> statements in this matrix describe **committed HEAD `b79b048`**, where
> the stamping does not exist. Re-check §4 item 3 once B1 lands.

**Live test baseline** (`pytest tests/ -p no:warnings`, dev shell):

    431 collected · 423 passed · 8 skipped · 0 failed

Skip breakdown (all verified in-source):

| Skip | Where | Kind |
|---|---|---|
| 2 | `tests/test_e2e_live.py:28,40` | live-gated (`RUN_LIVE_TESTS=1`) |
| 2 | `tests/test_landscape_adapters.py:367,378` | live-gated (`RUN_LIVE_TESTS=1`) |
| 2 | `tests/test_ops.py:111,124` | seed-conditional (no CAS bodies in seed — CAS round-trip paths untested in default run) |
| 2 | `tests/test_release_workflow.py:73,83` | `importorskip("temporalio")` — **temporalio is not in the dev env**, so the default baseline exercises zero Temporal code |

Status vocabulary is the charter's, used exactly:
`IMPLEMENTED+TESTED` / `FIXTURE-E2E` / `REAL-E2E` / `GATED` / `STUB`.

---

## 1. Surface inventory (what actually exists at HEAD)

### bin/ (Python control plane)

| Module | What it is | Tests |
|---|---|---|
| `bin/catalog.py` | project/component/source/skill/environment/policy CRUD + CAS (hybrid, 64KiB inline threshold, ADR 0002) | `test_catalog.py` (35) |
| `bin/landscape/` | typed contracts: `evidence.py`, `snapshot.py`, `pack.py`, `workflow_spec.py`, `canonical.py` | 13+14+11+19 = 57 (matches plan claim) |
| `bin/landscape/adapters/` | `git_local.py`, `github_api.py`, `unity_cloud.py`, `build_snapshot.py` — **no sentry, slack, telemetry, review-comment, or bugsweep-corpus adapter exists** | `test_landscape_adapters.py` (29, 2 live-gated), `test_landscape_unity_cloud.py` (8) |
| `bin/assemble_pack.py` | evidence collection → immutable snapshot → role packs CLI/API | `test_assemble_pack.py` (14) |
| `bin/landscape_mcp.py` | stdio JSON-RPC MCP server v1 (Resources/Tools, trust-tier + secret rules) | `test_landscape_mcp.py` (23) |
| `bin/workorder.py` | WorkOrder/WorkOrderDraft pydantic models; system-stamped provenance; `derive_links` from git state only | `test_workorder.py` (11) |
| `bin/generators/` | `card_gen.py`, `workorder_gen.py` (DSPy draft extraction, failure taxonomy), `builder.py` | `test_workorder_gen.py` (4), `test_card_gen*.py` (15), `test_generate_cards.py` (24) |
| `bin/release_workflow/` | Temporal workflow + worker + client + activities + `tests_integration/` (run from spike venv, NOT in `tests/`) | `test_release_workflow.py` (7, of which 2 skip w/o temporalio) |
| `bin/workflow_runs.py` | workflow_run projection: idempotent start, append-only stages, sticky-terminal status | `test_workflow_runs.py` (10) |
| `bin/approvals.py` | human approval gateway: fail-closed RBAC allowlist, audit-before-delivery, immutable audit rows | `test_approvals.py` (19) |
| `bin/ops.py` | backup/restore (sha manifest), cas-verify, gc --dry-run | `test_ops.py` (10, 2 seed-skips) |
| `bin/sandbox/` | `profile.py`, `backend.py`, `e2b_backend.py`, `report.py` — E2B is env-gated (`E2B_API_KEY`) | `test_sandbox.py` (15), `test_sandbox_e2b.py` (12, all mock-injection; none hit E2B) |
| `bin/judgement_schema.sql` | 21 tables incl. project/source/policy/evidence_ref/landscape_snapshot/context_pack/workflow_spec/workflow_run/approval_event — **zero campaign, finding, or dossier tables** (verified by grep) |  |

### ui/ (Phoenix control plane) — `router.ex` routes

`/`, `/start`, `/brackets`, `/new`, `/judge`, `/results`, `/judgements`,
`/prompts`, `/domains`, `/domains/new`, `/domains/:name/edit`,
`/catalog`, `/catalog/:project`, `/runs`, `/runs/:workflow_id`,
`/inspect` (+ 2 download/export controllers).

LiveViews: 12 (`catalog_live`, `runs_live`, `judge_live`, `domains*`,
`bracket`, `judgements`, `prompts`, `inspect`, `start`, `new_tournament`).
Read adapters in `ui/lib/tournament_ui/`: `catalog.ex`, `workflow_runs.ex`,
`approvals.ex`, `judgement.ex`, etc. Elixir test files: 9 context + 15
LiveView/component/controller suites (incl. `judge_live_citations_test.exs`,
`catalog_live_test.exs`, `runs_live_test.exs`).

Notable by absence: **no create/edit `handle_event` in `catalog_live.ex`**
(grep finds zero `handle_event` — pure render). `runs_live.ex` has exactly
one event, `"decide"` (approve/reject).

### Temporal activities — real vs stub (`bin/release_workflow/activities.py`)

| Activity | Verdict | Evidence |
|---|---|---|
| `record_started` / `record_stage` / `set_run_status` | **REAL** | write projection via `bin.workflow_runs`; idempotent/sticky contracts (activities.py:46–99) |
| `assemble_context` | **REAL (with canned fallback)** | calls `bin.assemble_pack.assemble()` when project known; explicit-note fallback otherwise (activities.py:105–170) |
| `generate_workorders` | **STUB** — returns 3 hardcoded IDs, "STUB (wave 6)" docstring (activities.py:173) |
| `judging_gate` | **STUB** — always `passed=True, score=0.92` (activities.py:193) |
| `sandbox_preflight` | **STUB** — returns "preflight-ok" (activities.py:210) |
| `build` | **STUB** — fake `https://artifacts.example/...` URL (activities.py:224) |
| `canary` | **STUB** — fake canary URL, always healthy (activities.py:244) |
| `check_canary_health` | **STUB** — echoes input (activities.py:259) |
| `promote` | **STUB** — returns string (activities.py:272) |
| `rollback` | **STUB** — returns string (activities.py:288) |

**4 of 12 activities are real; all are bookkeeping/context. Every activity
that touches the world (generate, judge, sandbox, build, ship) is a stub.**

---

## 2. Acceptance matrix — the 11 charter journey steps

Legend: gap column = what is missing to reach REAL-E2E specifically.

| # | Journey step | Status | Evidence | Gap to REAL-E2E |
|---|---|---|---|---|
| 1 | Create Landscape from UI (code, GitHub, Sentry, Slack, UCB, docs, skills) | **STUB** (UI) / IMPLEMENTED+TESTED (CLI only) | `bin/catalog.py:create_project/create_source` + `test_catalog.py` (35); `catalog_live.ex` has zero `handle_event` — render-only (ea333fc) | UI: create/edit forms + source wiring; adapters: sentry/slack/docs; credential storage UX |
| 2 | Verify sources: health, last sync, trust tier, credential status | **STUB** | `catalog_live.ex` shows sources + snapshots read-only; no health-check code anywhere in `bin/` or `ui/` | health-check probes per adapter, last-sync tracking columns, credential-status surface |
| 3 | Start Campaign (bugsweep \| release) w/ objective + window | **STUB** | grep `campaign\|finding\|dossier` in `bin/judgement_schema.sql` → **0 hits**; no campaign module exists | entire campaign layer: schema, template, UI (charter B4) |
| 4 | Collect signals: per-source counts + failures | **FIXTURE-E2E** (git/GitHub/UCB only) | `bin/assemble_pack.py:assemble` (SkippedSource reporting) + `test_assemble_pack.py` (14), `test_landscape_adapters.py` (27 fixture); live fetch **GATED** (`RUN_LIVE_TESTS`, tokens) | adapters for Sentry/Slack/telemetry/review-comments/bugsweep-corpus (B2); live credentials; per-source counts UI |
| 5 | Triage findings: deduped dossiers w/ cross-source links | **STUB** | no finding/dossier schema, no dedup code anywhere | everything (B4) |
| 6 | Generate cited WorkOrders (pipeline-stamped `cited_evidence`) | **IMPLEMENTED+TESTED** (drafts) / **STUB** (citation stamping) | `bin/generators/workorder_gen.py` + `test_workorder_gen.py` (4); `bin/workorder.py:finalize_work_order` stamps provenance/links — but grep `cited_evidence` in `bin/` → **0 hits** (only consumer is `judge_live.ex:399,663`) | stamp snapshot/evidence digests into generated WorkOrders (B1); wire `generate_workorders` activity to the real pipeline |
| 7 | Judge: Human Review Bar + developer rules, pairwise, rationale | **IMPLEMENTED+TESTED** (pairwise UI + LM judge) / **STUB** (developer rules) | `judge_live.ex` (workorder rendering, cited-evidence section, `judge_live_citations_test.exs`), `bin/judges/match_judge.py` + `test_match_judge.py` (8); no ReviewRule/Skill influence on judging | developer-rule layer (B5); `judging_gate` activity is a stub — tournament never called from workflow |
| 8 | Approve: authenticated principal, fail-closed, immutable audit | **REAL-E2E** (single-operator) | `bin/approvals.py` (fail-closed, audit-before-delivery) + `test_approvals.py` (19 incl. malformed-policy hardening 0f29c47, 8a47a8c); `/runs` buttons (300d48f); drilled on real Temporal (runbook §Approvals, audit events #3/#5) | multi-user auth (today: `DT_OPERATOR` env var = principal — self-asserted identity) |
| 9 | Execute: pinned sandbox; inspect patch + test evidence | **GATED** (never executed) | E2B backend env-gated (`e2b_backend.py:48` raises w/o `E2B_API_KEY`; `test_sandbox_e2b.py` uses injected fakes — **zero live E2B runs ever**); microvm.nix guest + nftables egress (a6f0f7e) needs Linux/KVM; `sandbox_preflight` activity STUB | E2B_API_KEY + one recorded live run, or Linux/KVM host; patch/test-evidence capture + UI |
| 10 | Ship: PR, CI/UCB tracking, canary, monitor, promote/rollback | **STUB** (activities) / **REAL-E2E** (orchestration shell only) | workflow skeleton ran all 9 stages on real Temporal dev server incl. approval-timeout + rollback paths (runbook drill, 48033aa, `tests_integration/`); but build/canary/check_canary_health/promote/rollback all STUB; **no PR-creation activity exists at all** | PR create/update + CI/UCB polling activities (B6); UCB + deploy credentials; monitoring adapter |
| 11 | Learn: outcomes + review comments → ReviewRuleProposal → human promotion → versioned rules | **STUB** (rules) / IMPLEMENTED+TESTED (pair-verdict feedback only) | `bin/feedback.py` projects pair verdicts to card labels (`test_feedback.py` 11, `test_feedback_aggregation.py` 5) — feeds prompt optimization, not rules; no ReviewRuleProposal type anywhere; `skill` table exists in schema but nothing writes versioned rules | entire learning loop (B5): comment ingestion, proposal synthesis, promotion UX, rule versioning/rollback |

**Journey scorecard: 1 step REAL-E2E (approve), 0 fully-real shipping steps,
4 steps entirely STUB (campaign, triage, learn-rules, source-health).**

---

## 3. Acceptance matrix — wave 1–7 requirements (old plan)

| Wave/req | Claimed | Audited status | Evidence | Gap to REAL-E2E |
|---|---|---|---|---|
| 0a Storage ADRs | ✅ | **IMPLEMENTED** (docs) | `docs/adr/0001-catalog-storage.md`, `0002-content-addressed-artifacts.md` (4b66550) | n/a (docs) |
| 0b Typed context contracts | ✅ 57 tests | **IMPLEMENTED+TESTED** | `bin/landscape/{evidence,snapshot,pack,workflow_spec}.py`; 13+14+11+19 = 57 tests exactly (2bbea3c) | none for the contracts themselves |
| 0c SafeMarkdown sanitization | ✅ 21 tests | **IMPLEMENTED+TESTED** | `ui/.../safe_markdown_test.exs`, both call sites (02db10f) | none |
| 2a-1 Temporal spike | ✅ 4/4 vs real server | **REAL-E2E** (spike) | `spikes/temporal-unity-release/` (b9585be); approve/timeout/reject/retry all vs live dev server | superseded by production package |
| 2a-2 Agent Skills package | ✅ | **IMPLEMENTED (inert)** | `skills/` 5 SKILL.md folders (cb947bf); `skill`/`project_skill` tables exist — **no code loads or executes skills** | runtime that reads skills into agent context |
| 2a-3 Landscape MCP contract + server | ✅ | **IMPLEMENTED+TESTED** | `docs/specs/landscape-mcp-v1.md`; `bin/landscape_mcp.py` + `test_landscape_mcp.py` (23) incl. spec-rule enforcement (5f19cf5) | no real MCP client (agent) exercises it yet |
| P1 Catalog persistence | ✅ | **IMPLEMENTED+TESTED** | `bin/catalog.py` + 35 tests; CAS + immutability triggers (5a1d96f) | none |
| P1 Catalog UI | ✅ | **IMPLEMENTED+TESTED (read-only)** | `catalog_live.ex` + `catalog_live_test.exs` (ea333fc); zero write events | create/edit UI = charter B3 |
| P2 Source adapters (git/GitHub/UCB) | ✅ | **FIXTURE-E2E**; live **GATED** | `bin/landscape/adapters/` + 27 fixture tests; 2 live tests skipped (`RUN_LIVE_TESTS`, tokens) (dc0399c, a265f53) | run the live tests with real tokens; add missing signal adapters |
| P2 assemble_pack pipeline | ✅ | **IMPLEMENTED+TESTED** / **FIXTURE-E2E** | `bin/assemble_pack.py` + 14 tests; also invoked by real `assemble_context` activity (8ade671) | live sources |
| P2 Judge view cited evidence | ✅ ("stamping rides next batch") | **half-STUB** | render+resolve: `judge_live.ex:399–678` + `judge_live_citations_test.exs`; producer side: **grep `cited_evidence` across `bin/` = 0 hits** — nothing ever stamped it; the "next generation batch" never happened | B1: stamp digests in `finalize_work_order`/generation pipeline |
| P3 Temporal production pkg + projection | ✅ | **REAL-E2E (dry-run, out-of-band)** | d84253d, 48033aa, 8a47a8c; `bin/release_workflow/tests_integration/` from spike venv; **in-repo baseline skips both Temporal tests (no temporalio in dev env)** | temporalio in a first-class env; CI that runs integration suite |
| P3 Runs UI + approval Signals | ✅ | **IMPLEMENTED+TESTED** + **REAL-E2E** (drilled) | `runs_live.ex` `"decide"` event → `bin/approvals.py`; `runs_live_test.exs`; drill audit events #3/#5 (300d48f, ac73bcc) | multi-user principal auth |
| P4 Sandbox scaffold + E2B | ✅ (gated) | **GATED — never executed** | `e2b_backend.py` raises w/o key; all 12 e2b tests use injected fakes (809fddd, 604c907) | `E2B_API_KEY` + one live run with captured report |
| P4 microvm.nix guest | ✅ (gated) | **GATED — unverified** | `infra/microvm/{flake.nix,egress.nft}` (a6f0f7e); never booted (needs Linux/KVM) | Linux/KVM host; boot + egress-deny verification |
| P5 Release pilot promote/rollback | ✅ dry-run | **REAL-E2E (orchestration only)** | runbook drill 2026-08-17: 9 stages, audited approval, sticky rolled_back; **but 8/12 activities stubbed — "dry-run" = real Temporal driving fake work** | B6 shipping activities + credentials |
| W7 ops: backup/restore/cas-verify/gc | ✅ drilled | **IMPLEMENTED+TESTED** + drilled | `bin/ops.py` + `test_ops.py` (10); restore drill sha-verified (62bb3e2; runbook §Backup) | note: 2 CAS-path tests skip in default seed |
| W7 approvals hardening | ✅ | **IMPLEMENTED+TESTED** | 19 adversarial tests (malformed rules, substring grants, charclass scopes) (ac73bcc, 0f29c47) | none at current threat model |

---

## 4. Known-gap verification (each independently confirmed in code)

1. **Catalog UI read-only** — CONFIRMED. `catalog_live.ex` contains no
   `handle_event`, no forms; only `format_date` helpers and render. All
   writes go through `bin/catalog.py` CLI.
2. **No Sentry/Slack/telemetry/review-comment adapters** — CONFIRMED.
   `bin/landscape/adapters/` = `git_local`, `github_api`, `unity_cloud`,
   `build_snapshot` only; repo-wide grep for sentry/slack/telemetry in
   `bin/landscape/` returns nothing.
3. **cited_evidence rendered but never stamped** — CONFIRMED. Only
   occurrences in the entire repo are consumers in
   `ui/.../judge_live.ex` (assign/resolve/render). `bin/generators/`,
   `bin/generate_cards.py`, `bin/workorder.py` never emit the key. The
   old plan's "rides the next generation batch" never landed.
4. **build/canary/promote stubs** — CONFIRMED. Explicit "STUB (wave 6)"
   docstrings; fake `artifacts.example` / `canary.example` URLs. Also
   stubs (beyond the charter's list): `generate_workorders`,
   `judging_gate`, `sandbox_preflight`, `check_canary_health`,
   `rollback`. **No PR-creation activity exists.**
5. **E2B never executed** — CONFIRMED. Backend raises without
   `E2B_API_KEY`; every test injects a fake SDK; no run report artifact
   exists anywhere in the repo.
6. **No campaign/finding/dossier schema** — CONFIRMED. grep of
   `bin/judgement_schema.sql` (21 tables) and all of `bin/`: zero hits.
7. (Additional, found during audit) **Temporal test coverage is
   environment-dependent**: the shipped baseline silently skips both
   `test_release_workflow.py` workflow tests because temporalio isn't in
   the dev shell — the only Temporal verification lives in a spike venv
   invoked manually (runbook §Known sharp edges).
8. (Additional) **Skills package is inert**: 5 SKILL.md folders + schema
   tables, but no loader/executor references them.

---

## 5. Ranked distance-to-charter-journey — top 10 missing pieces

1. **Campaign/finding/dossier layer** (kills journey steps 3+5) — add
   `campaign`, `finding`, `dossier` tables to `judgement_schema.sql` plus a
   bugsweep campaign template on the existing workflow engine (charter B4).
2. **Signal adapters: sentry, slack, review-comments, telemetry, bugsweep-corpus**
   (step 4) — clone the `git_local`/`unity_cloud` adapter pattern; stable
   URIs (`campaign://`, `review-rule://`), secret-redacted excerpts (B2).
3. **cited_evidence pipeline stamping** (step 6; cheapest big win) — stamp
   snapshot/evidence digests in `bin.workorder.finalize_work_order` from the
   assembling snapshot; the UI consumer already works and is tested (B1).
4. **Real `generate_workorders` activity** — replace the stub with a call
   into `bin/generators/workorder_gen.py` + persistence; the generator and
   its failure taxonomy already exist and are tested.
5. **Real `judging_gate` activity** — wire the existing tournament judge
   (`bin/judges/match_judge.py`) so below-threshold batches actually fail
   the gate instead of auto-passing at 0.92.
6. **PR-creation + CI/UCB tracking activities** (step 10) — new activities
   using the existing github_api adapter's auth pattern; fixture contract
   tests mandatory, live GATED (B6).
7. **Landscape create/edit UI + source health checks** (steps 1–2) — add
   forms/`handle_event` to `catalog_live.ex` backed by `bin/catalog.py`
   semantics, plus per-adapter health probes with last-sync columns (B3).
8. **One real sandbox execution** (step 9) — obtain `E2B_API_KEY` and run a
   single WorkOrder end-to-end, capturing patch + test evidence via
   `bin/sandbox/report.py`; converts the whole execution plane from GATED
   to REAL-E2E in an afternoon.
9. **Learning loop: ReviewRuleProposal → human promotion → versioned rule**
   (step 11) — new proposal type fed by review comments + feedback.py
   outcomes; promotion UI reusing the approvals gateway pattern; raw
   comments never mutate prompts directly (B5).
10. **Real build/canary/promote against Unity Cloud Build** (step 10) —
    implement the documented integration points in `activities.py` behind
    UCB credentials; keep fixture contract tests, live = GATED until keys
    arrive (B6).

---

*Method note: baseline from a single `pytest tests/ -p no:warnings` run at
HEAD; per-file counts via `pytest --co -q`; all status calls backed by the
cited file:symbol/test/commit. Nothing in this audit modified code.*
