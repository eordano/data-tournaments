# Sweeps: the configurable multi-round review machine

*2026-08-21. Synthesized from a 7-corpus research sweep (foundation Slack
digests, meeting-recording findings, lorebook archive, foundry stories,
claude notes, this repo) — 91 findings. This doc is the design of record
for the sweeps layer: `bin/sweep_spec.py`, the round machinery in
`bin/campaigns.py`, `bin/lenses.py`, the `foundry_stories` adapter, and
the `/campaigns` UI slice.*

## Why

Three workflows converged on the same machinery:

1. **Bugsweeps.** The August 2026 hand-run campaigns (16 fix branches
   Aug 3–4, 9 Sentry PRs Aug 6; 30 candidates → 24 CONFIRMED+VALIDATED /
   5 NO_GO) proved the pipeline, and Stoyan's process work made it a
   standing operation (automated sweep from Aug 17, human final sign-off).
   The driving arithmetic: ~40 important issues reported per week against
   fewer than 40 merges — "we are under water." The binding constraint is
   **review/QA capacity, not diagnosis** ("we have only 2 people and they
   need to review bunch of PRs").
2. **Multi-round review pain.** Agent reviews were not converging: 14–15
   rounds on one PR in a day, findings arriving one per round ("every
   time you see it you find just 1 P1. I can't waste more time";
   "Jarvis is going in circles"). The known fix — batched findings,
   explicit resolution, a round cap — converges in 2 rounds. The August
   bugsweeps encoded exactly that: batched per-lens findings, one repair
   cycle, terminal verdicts.
3. **Hot-or-slop.** The swipe/wheel quality-judging loop ("hot" =
   the artifact maintains its internal rules; "slop" = it breaks basic
   physical/narrative coherence) whose judging primitive already
   generalized into the judgement fabric (docs/judgement-fabric.md). What
   was missing was the corpus/campaign side, not the judging side.

The August process lived in the operator's head. A **sweep** makes it
data: a campaign with a declared, versioned **SweepSpec**.

## SweepSpec

`bin/sweep_spec.py`. Frozen pydantic model; canonical JSON + SHA-256
digest stored on the campaign row (`spec_json` / `spec_digest`) at
creation. A running sweep can never drift from the spec it launched with
(EvalTemplate pinning precedent).

```jsonc
{
  "kind": "bugsweep | perfsweep | featuresweep | slopsweep",
  "corpus":  [ { "adapter": "sentry_csv | slack_csv | github_autoclosed | foundry_stories", "config": {} } ],
  "intake":  { "max_candidates": 30, "rationale_required": true },
  "panel": {
    "lenses":  [ { "name": "root-cause", "prompt_ref": "lens:root-cause", "burden": "refute" } ],
    "human":   { "rubric": "<eval template>", "judgement_kind": "pair|single", "required": true },
    "quorum":  "all_lenses | all_lenses_and_human"
  },
  "rounds": {
    "max": 3,                     // hard cap — the anti-11-rounds rule
    "batching": "required",       // every lens reports before a round closes
    "convergence": "no_new_confirmed_findings | all_findings_settled",
    "repair_max_cycles_per_finding": 1   // 0 or 1; the schema's ceiling
  },
  "validation": { "mode": "red_green | perf_budget | rubric_only",
                  "perf_budgets": [ { "metric": "allocs_per_1000_msgs", "budget": 0, "direction": "max" } ] },
  "publish":    { "gate": "human | none", "granularity": "branch-per-finding | pr-per-finding | report-only" }
}
```

Working examples: `configs/sweeps/*.json` (validated by the test suite).

### Kinds

| kind | corpus | validation | first customer |
|---|---|---|---|
| `bugsweep` | sentry/slack/autoclosed signals | `red_green` (two-patch RED/GREEN) | the Aug-17 standing sweep |
| `perfsweep` | perf issues / profiler exports | `perf_budget` (quantitative RED generalized) | perf/stability pod |
| `featuresweep` | **foundry stories** | `rubric_only` | story spec-honesty review |
| `slopsweep` | any generated-artifact set | `rubric_only` | hot-or-slop |

`release` campaigns remain spec-less.

## Rounds

`sweep_round` table + `open_round` / `close_round` / `list_rounds` /
`sweep_metrics` in `bin/campaigns.py`. The rules, each of which kills a
recorded failure mode:

* **One open round at a time; `rounds.max` is a hard cap.** Hitting the
  cap is a loud error telling the operator to make a terminal decision —
  not round 12.
* **Spec'd sweeps refuse lens verdicts outside a round** — findings land
  as per-round batches, never a drip.
* **`batching: required`**: a round cannot close until every configured
  lens has reported. A half-reported round is the drip-review failure
  mode with extra steps.
* **Convergence is computed, not vibes**: `no_new_confirmed_findings`
  (round produced no new non-repair CONFIRM) or `all_findings_settled`
  (every finding terminal). The outcome and per-lens batch stats freeze
  onto the round row at close.
* **Guards hold under concurrent writers.** Round open/close and verdict
  writes run in `BEGIN IMMEDIATE` transactions, an INSERT trigger refuses
  verdicts into a non-open round (a stamped outcome can never miss a
  late verdict), and lens names are validated against the spec panel —
  hardened after a double-adversarial verification pass (2026-08-22)
  confirmed check-then-act races in the original python-only guards.
* **Repair depth stays capped at one per REFUTE** (a unique index on
  `repair_of` is the SQL backstop); the spec field is a binary toggle —
  `0` forbids repairs entirely (the shipped slopsweep example uses it —
  you don't argue with taste). Total repairs over a finding's life are
  bounded by `rounds.max` × panel size, not by this field.

`sweep_metrics` is the trust rollup for the people who have to believe
the machine: rounds-to-converge, per-lens REFUTE and repair rates, NO_GO
breakdown, terminal-state counts.

## Lenses

`bin/lenses.py`: lens prompts are versioned prompt-registry entries
(`lens:<name>`), referenced from specs by `prompt_ref` — panels are data.
Shipped set: the bugsweep trio (`root-cause`, `lifecycle-regression`) plus
`perf-budget`, the featuresweep pair (`spec-honesty`, `fake-success` —
the creator-hub audit methodology), and `slop` (the hot-or-slop
definition). The review-rule learning loop (B5) revises lens prompts as
new versions, never in place.

Lens generalization is per-landscape: the unity-explorer trio was mined
from that corpus; onboarding a new landscape should mine its own
(ReviewRuleProposal pipeline).

## Foundry stories as corpus

`bin/landscape/adapters/foundry_stories.py`: walks
`<root>/<surface>/<slug>/story.md`, parses the YAML frontmatter
(hypothesis / metric / decision / experiment), and freezes one
TIER2_INTERNAL EvidenceRef per story (`story:<surface>/<slug>#<id>`,
revision = content hash, mtime never leaks into digests).
`spec.stories.tsx` is generated FROM story.md and is never read.

`campaign_intake.ingest_from_spec(name)` maps the spec's corpus entries
onto intake signals and applies `intake.max_candidates` as the candidate
budget (`truncated: true` in the result when it bites — no silent caps).

## The visual designer (/designer)

A ComfyUI-style node canvas for composing sweeps
(`ui/lib/tournament_ui_web/live/designer_live.ex` +
`ui/lib/tournament_ui/sweep_graph.ex`). The pipeline renders as typed
nodes — corpus sources, intake gate, lens panel, human panel, rounds,
validation, publish gate — with wires DERIVED from node types: a sweep
cannot be mis-wired, only mis-configured. Clicking a node edits it in the
sidebar; dragging rearranges the canvas; `+ lens` / `+ corpus` /
`± human panel` change multiplicity; picking a kind loads its
`configs/sweeps/` template.

The graph compiles live into SweepSpec JSON. **Validate** shells to
`bin/campaigns.py validate-spec` (pydantic stays the single authority —
the designer never re-implements validation) and shows the spec digest;
**Create from graph** runs the same `create-campaign --spec-file` path
the /campaigns form uses, so the digest shown in the designer is the
digest frozen on the campaign. Round-tripping every example config
through `from_spec |> to_spec` is test-pinned
(`ui/test/tournament_ui/sweep_graph_test.exs`).

## Agents, loops, and enacting past workflow runs

The sweep machinery is deliberately agent-agnostic: agents ENACT sweeps
through the same surface humans use — `bin/campaigns.py` (or the UI's
CLI seam): collect signals, `add-lens-verdict` per lens per finding,
`open-round`/`close-round`, `dispose-finding`. The loop discipline
(batching, cap, convergence, one-repair) constrains whoever is driving,
model or human; nothing about a lens agent's runtime is assumed.

`bin/enact_workflow.py` closes the historical loop: a Claude Code
multi-agent workflow run (the find → adversarially-verify shape) is
structurally a sweep — finder agents produce candidate findings,
verifiers with the burden of refutation are lenses, a verification pass
is a round. The enactor parses a run's on-disk record (`journal.jsonl`
results + `agent-*.jsonl` prompts, classified by shape rather than by
trusting labels), freezes finder results as TIER2 evidence, mints the
verified findings, replays verifier votes as lens verdicts
(CONFIRMED→CONFIRM, REFUTED→REFUTE, PLAUSIBLE skipped), and closes the
round with computed convergence. First real enactment:
`adversarial-verify-2026-08-22` — the double-adversarial verification of
this very layer, replayed as a campaign (10 findings, 20 verdicts,
`not_converged`, 9 later `confirmed_validated`, the split verdict
disposed `ship_anyway` as a docs fix).

## The Runner node — enactment as spec data

The designer's **Runner** node makes the agent loop a first-class part of
the spec: `runner: {driver: opencode | claude-workflow, model, parallel}`
(optional — absent means manual, humans/ad-hoc agents drive the CLI). The
runner is hands, never judgment: whichever driver runs, the round guards
stay enforced server-side by `bin/campaigns.py`.

For `driver: opencode`, **Export opencode pack** (designer sidebar, or
`campaigns.py runner-pack --spec-file S --name N`) generates the
three-markdown-file pattern into `.opencode/`: a `/sweep-<name>` command,
a `sweep-orch-<name>` primary agent whose protocol is "the CLI's refusal
messages are instructions, not obstacles" (batching refusal → dispatch
the missing lens work; cap refusal → present to the human, NEVER
dispose-finding itself), and one `sweep-lens-*` subagent per panel lens
with the registry prompt baked in at generation time plus a strict
`VERDICT: CONFIRM|REFUTE` I/O contract, edit-denied. This is the
community `/goal` pattern with its weak point removed — the model never
decides for itself that it has converged (`bin/runner_pack.py`).

## Decisions of record (interview, 2026-08-22)

* **Cap without convergence = human tie-break, first-class.** A sweep
  that exhausts `rounds.max` not-converged cannot close until every
  still-open finding carries a disposition (`ship_anyway` / `needs_fix` /
  `no_go`+reason) with a mandatory rationale — append-only
  `finding_disposition` rows, `dispose_finding()` /
  `campaigns.py dispose-finding`, surfaced in the campaign UI when the
  tie-break is due. `close_campaign` enforces it (and refuses while a
  round is open).
* **QA handoff: branches by default, PRs opt-in.** `branch-per-finding`
  stays the default `publish.granularity`; a sweep opts into
  `pr-per-finding` + `need qa validation` labels only when the QA queue
  has capacity. Sweeps must not flood the 2-person QA bottleneck by
  default.
* **Sweep judging precedes QA validation — two gates.** Gate 1 is the
  sweep's human verdict over the finding+patch dossier (with the
  validation-ledger evidence attached); gate 2 is QA validating the fix
  in a real build, per the standing process — two separate things, kept
  separate. Report-only sweeps (featuresweep/slopsweep) ship nothing, so
  gate 2 does not exist for them: they end at the sweep verdict.
* **Foundry stories: both modes from day one.** Per-story spec-honesty
  review runs as the sweep; `campaigns.py export-corpus` writes the same
  frozen story evidence to files so the /brackets prepared-artifacts flow
  can pair-rank the portfolio without re-collecting anything.
* **Hot-or-slop stays internal until a distribution surface exists.**
  Slopsweeps run on the judgement fabric now; the creator-facing swipe
  middleware (and Scott's aggregate-only / consent rails, which bind the
  moment judgment data becomes a product) starts only when a generator
  produces volume and there is somewhere real to swipe. JSONL export
  remains the interface either way.
* **Generic lenses are canonical.** New landscapes start on the shipped
  lens set; the B5 mining pass runs only when a sweep's metrics show a
  lens underperforming (refute-rate noise, missed classes) — not as an
  onboarding gate.

## Still open

* **Multi-user identity** — DT_OPERATOR single-operator until there's a
  second real judge.
