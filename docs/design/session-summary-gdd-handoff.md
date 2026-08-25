# Session summary & handoff: from branch-fix loop to GDD-driven development

Status: HANDOFF (2026-08-18). Written for future sessions to implement
the next phase. Repo HEAD at writing: 7b028ba. Gates at HEAD: pytest
750 passed / 9 skipped (clean env), mix precommit 350 tests / 0
failures. ~145 commits, ALL LOCAL/UNPUSHED (user has not said push).

## 1. Executive summary

We built and REALLY tested an end-to-end bugfix-automation platform on
an external repo (github.com/eordano/catalyrst): live-agent-authored
fix branches, per-branch isolated validation with a tamper-proof
harness, human review in the browser, append-only judgement revisions,
audited approvals, Temporal release workflow, DRY-RUN promote.

The user's NEXT hypothesis is NOT yet tested: GDD-driven development —
"idea to generated scene in under 60 minutes" with feedback captured at
every stage for future automated AI analysis. Most reusable primitives
exist; the game-specific pipeline, artifact lineage, scene generation,
and a timed pilot do not.

## 2. What was built this session (waves 10–14)

- Wave 10-11 (branch-fix loop, REAL): authoring bridge
  (bin/branch_author.py, Fixture+Command backends), trusted harness
  (protected paths incl. transitive/manifest discovery, HARNESS-TAMPERED
  pre-execution refusal, expected-count pinning), fail-closed ship
  gateway (bin/branch_ship.py: SHA derived from approved record,
  4-gate refusal matrix), completion projection (shipped =
  release-COMPLETED; rolled-back needs fresh approval), strict lineage,
  conftest env guard. Live claude agent (`claude-achtung-achtung` — the
  seatbelt wrappers can't nest) authored a real fix from the judged
  WorkOrder; external oracle (@dcl/hashing fixtures) validated the fix
  premise 3/3.
- Wave 12 (typed judgements): eval_template output_definition v2 —
  judgement_kind pair|single, subjects idea|execution, 8-position
  semantic WHEEL where geometry signifies (N tie-both-important, W/E
  strong A/B, NW/NE slight, SW/SE lean-though-both-weak, S neither;
  skip/incoherent off-wheel; numpad geometry). Seeds: pair-wheel-v1,
  pair-idea-wheel-v1, single-idea-v1, single-execution-v1.
  Subject-aware ratings (judgement.<subject>.verdict/.confidence under
  ONE rating_id). Single enqueue (one pending per artifact). Pipeline
  spec v1 (bin/pipelines.py): versioned immutable registry, FAIL-CLOSED
  rule — audited_release requires a preceding SINGLE EXECUTION
  judgement stage (pair comparison never substitutes); stage/rubric
  kind+subject compatibility enforced.
- Wave 13 (operator UX): append-only judgement REVISION (revise =
  new rating + judgement_revision link row; old score rows immutable;
  effective = chain tip; stale/concurrent refusals). /environment IA
  (sources|prompts|rubrics|pipelines|policies tabs; catalog+prompts
  redirect). /campaigns/:name exploration hub. GitHub-style diff view
  (pure-Elixir parser). /runs per-stage timeline with full logs.
  /judge full-width (aside's domain picker relocated to queue bar).
  Brackets demoted from nav.
- Wave 14 (operationalize): bin/dt_stack.py up|status|down|logs —
  supervises Temporal + release worker + Phoenix UI with pidfiles,
  logs, real health checks, honest port handling. Operational data
  home .dt-stack/home; domain catalyrst-ops PERMANENTLY bound to
  branch-fix-review v1 (digest 9139f4d5…). Serving :4113.

## 3. Realness ledger (honest status vocabulary)

REAL: external-repo defect; live-agent authoring; cargo test harness
runs; oracle parity; browser judgements/approvals (DB-asserted);
Temporal workflow execution; append-only revision through the real UI.
REAL BUT LOCAL/DRY-RUN: release promote (no ship credentials — label
"[DRY-RUN — no shipping credentials; nothing was deployed]"); Temporal
dev server (local SQLite).
FIXTURE-BACKED: candidates B/C in the walkthroughs (deliberate trap +
tamper branches); sentry CSV intake signals (invented, adapter-real).
NOT YET TESTED: everything in §5 below — the GDD hypothesis itself,
pipeline auto-advance, scene generation, the 60-minute clock.

## 4. Current serving state

    bin/dt_stack.py up | status | down | logs <component>
    UI http://localhost:4113 · Temporal :7233 (SERVING) · worker on
    queue dt-stack-release · state in .dt-stack/ (git-excluded)
    catalyrst-ops bound to branch-fix-review v1
    Walkthrough data homes: /tmp/dt-wave11 (:4112 server), /tmp/dt-catalyrst-e2e

## 5. The user's GDD hypothesis (verbatim intent, structured)

Source: user message 2026-08-18 (speech-transcribed; wording like
"central and games process" / "charging things" is transcription noise —
read as "scene generation process" / "judging things"; CONFIRM with
user before encoding names).

The workflow to test: generate MULTIPLE items/cards → review them →
implement → review the implementation → test the implementation →
review again — and at EVERY step capture feedback that can be analyzed
later, structured so future automated AI searches can mine it.

THE KEY INSIGHT — the judgement model is THREE independent axes, and we
have only built two:
  - judgement_kind: single | pair (BUILT, wave 12)
  - judgement_subject: idea | execution (BUILT; must WIDEN for GDD to
    something like idea | implementation | test_result | material |
    asset | scene)
  - judgement_purpose: intrinsic_quality | prioritization (NOT BUILT —
    the user's new axis)
Single-vs-pair does NOT by itself express quality-vs-priority:
  - INTRINSIC QUALITY: judge the thing in itself ("what is the quality
    of this suggestion individually?").
  - PRIORITIZATION: judge relative importance ("this is more important
    than this").
A pair judgement can serve either purpose; so can a single. Proposed:
add `judgement_purpose` to output_definition v3 (template-declared,
same normalization pattern; old templates default per rubric
semantics). Capture PER-ITEM DIVERGENCE between the two purposes
(high quality + low priority, and vice versa) — that divergence is the
analytical payload for future AI mining.

The "slob generator" (user's own working name for the AI-generated
suggestion stream — do NOT invent an expansion): generator emits cheap
AI suggestions → a prioritization filter ranks them → each surviving
item gets intrinsic evaluation → implementation → independent gating of
what the AI generated. Then iterate: elicit what the user wants, create
alternatives, push them, test back. Applies beyond code: GDD, materials,
assets, generation order.

TARGET METRIC: a person goes from idea to generated scene in UNDER 60
MINUTES, with complete feedback lineage and no unaudited gate.

## 6. Mapping the hypothesis onto what exists

| GDD stage                      | Existing primitive (real)             | Gap |
|--------------------------------|---------------------------------------|-----|
| Generate N idea cards          | generate_cards pair/single enqueue    | generator = "slob" source adapter |
| Judge idea intrinsically       | single-idea-v1 axis                   | purpose field |
| Rank ideas (priority)          | pair-idea-wheel-v1 wheel              | purpose field |
| Implement per accepted idea    | branch_author CommandBackend (live)   | scene/GDD artifact types beyond git branches |
| Review implementation          | single-execution-v1 + /branch-fixes   | — |
| Test implementation            | trusted harness RED/GREEN/GUARD       | playable-scene runtime validation adapter |
| Review tested implementation   | same single gate + revision           | — |
| Feedback for AI mining         | score rows, revisions, authoring/validation/ship provenance | unified queryable projection across stages |
| 60-minute clock                | timestamps exist per table            | first-class per-stage timing rows + run entity |
| Pipeline stitching             | branch-fix-review SPEC (fail-closed)  | gdd-scene pipeline spec; NO executor exists (spec only — do not claim otherwise) |

## 7. First real pilot (design, not yet run)

One narrowly scoped scene brief → 4–6 generated idea cards → intrinsic
singles + priority pairs (both recorded, DIVERGENCE between the two
axes captured per item — that divergence is the analytical payload) →
top 2 implemented independently (same-base, isolated) → per-candidate
automated validation → single execution review each → optional pair
compare of survivors → runnable scene → final user feedback. Clock
from intent submission to runnable scene; success = <60 min AND
complete event lineage AND no unaudited gate.

## 8. Missing implementation (ordered backlog)

1. `judgement_purpose` axis (v3 outdef, normalization, UI badge).
2. gdd-scene-v1 pipeline SPEC + domain bind (registry pattern exists).
3. Artifact/version lineage beyond git branches (GDD docs, materials,
   assets, scenes — content-addressed like evidence_ref).
4. Per-stage timing rows + run entity (answer "<60min" with data).
5. Unified feedback projection for AI search — a read-side view over
   the existing append-only tables (never replace them); MUST include
   rejected alternatives and counterfactuals, not just winners.
6. Scene generation + runtime validation adapters (the "playable" gate).
7. Timed pilot with a real participant.
8. Real ship credentials (DRY-RUN stands until then).
9. Push decision on the ~145-commit local stack (user's call).

## 9. Commit ledger (this session's chain)

0c1a513 L2/L3/L6 → 7e378fd E4 evidence → 5b3d501 authoring bridge →
12d8a40 harness trust + gateway → b0310b4 ship tests → 643fdfc UI
diff/ship → 9c1dcc6 catalyrst showcase → a475511 completion+lineage+
guard → 24c471e transitive protection → 668ea3a scorecard+delivery →
78109a7 wave-11 live-agent run → a9431a0 typed judgements → b96c64b
pipeline spec → 29321ca wheel UI → e19bd1b revision → e7beeaa
environment+hub → 9d98c19 diff/timeline/judge → 59e53a0 acceptance
evidence → 7b028ba dt-stack.

## 10. Invariants that must not regress

Append-only everywhere (revisions, approvals, bindings, ship rows).
User-devs decide at the END of every loop in the UI. Every branch
validated ALONE at its exact head SHA. Staleness invalidates approvals.
Pair judgements never substitute for the single execution gate.
Honest status vocabulary (REAL/DRY-RUN/FIXTURE/CREDENTIAL-GATED);
simulated success must never look real. No secrets in any surface or
generated file — names only. corpus/ and report-*/ never touched.
