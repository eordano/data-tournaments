# judgement-fabric

A design spec for a generic, multi-rater judgment system that slots into
Langfuse's existing evaluator pipeline. Replaces the original "slopeorhot"
proposal with a more general framing: one tool, many rubrics, many rater
types, one storage path.

Status: draft. Resolved through 5 design questions in conversation; this
doc records the decisions and their reasoning so we don't relitigate.

## Background

Langfuse already has an LLM-as-judge pipeline:

- `EvalTemplate` defines a versioned, Zod-validated rubric prompt.
- `JobConfiguration` binds a template to traces matching a filter, at a
  given sampling rate, with delay, time-scope, and per-config blocking.
- `JobExecution` is one run of one config against one trace.
- `Score` is the immutable output: a row per scored field per execution.

The original slopeorhot proposal added human raters as a parallel pipeline
with a wheel UI. After working through it we realized two things:

1. The "human-vs-LLM judge" framing was too narrow. Agent-as-judge,
   programmatic-judge (CI rules), and rater-vs-rater all fall out of the
   same primitive once you stop privileging humans.
2. The way Langfuse already represents LLM judges via tool-style
   structured output IS the universal interface. We don't need a separate
   path for humans — we need to re-frame "human submits via UI" as "human
   makes a tool call."

## Core abstraction: one tool, many invokers

```
express_judgement(
  rationale: string,        // optional by default; reasoning before answer
  confidence: enum<L,M,H>,  // optional; defaults to "mid"; never AVG()'d
  verdict: enum<rubric>     // required; rubric-parameterized
)
```

The tool signature is fixed. The `verdict` enum's values are bound late
from the active `EvalTemplate`. Every rater type — LLM judge, human via
the wheel UI, agent-as-judge, CI rule — produces an invocation of this
tool with the same shape. Storage is identical.

### Property order matters

The order is **rationale → confidence → verdict**, and that's load-bearing.
Structured-output backends (OpenAI strict mode, Anthropic tool use, local
constrained-decoding via outlines/guidance/llama.cpp) generate properties
in declared order. With this ordering, an LLM tool-caller emits its
reasoning tokens *before* committing to a verdict, conditioning the
verdict on the rationale rather than confabulating the rationale after.

This is the well-documented "reason first, answer second" effect in CoT
and judge literature, worth ~5–15 points on judge-agreement benchmarks
depending on rubric.

We rely on:
- `additionalProperties: false`
- `strict: true` (OpenAI) / equivalent on other providers
- For local models: grammar-constrained decoding

We additionally **post-validate** that the raw output emitted properties
in the declared order before parsing. If a provider regresses, we catch
it instead of silently degrading.

We deliberately *did not* use a structurally-nested schema like
`{rationale: ["text", {confidence: ["mid", {verdict: "..."}]}]}` to
enforce ordering grammatically, even though it would lock ordering more
tightly. Reasons: off-distribution shape can hurt rationale generation
quality on smaller models, downstream parsers become fragile, and the
nesting conflates "generation order" with "data model parent-child."
Re-evaluable per-rubric later if we measure quality issues.

## Rater types

Polymorphic, expressed as `Score.metadata.rater = {type, ...}`:

| `rater.type` | Identifier fields                          | Authentication path                            |
|--------------|--------------------------------------------|------------------------------------------------|
| `llm`        | `model`, `provider`                        | API key on the JobConfiguration                |
| `human`      | `userId`                                   | Standard Langfuse user session                 |
| `agent`      | `traceId` (the judging agent's own trace)  | Same as parent agent's auth                    |
| `programmatic` | `source` (e.g. `"ci"`, `"webhook:foo"`)  | Service token                                  |

`rater.type` is the column the disagreement view uses to compute
agreement/disagreement. It is NOT "human vs LLM" specifically — it is
"any pair (or N-tuple) of rater types you want to compare."

This unlocks **N-way disagreement**: three different LLM judges, one
human, and a CI rule all judge the same trace under the same rubric →
five Score rows on one ratingId-cluster, and the comparison view shows
where they cluster and where they diverge. Richer training signal than
human-vs-LLM alone.

## Rubrics

A rubric is an `EvalTemplate` row. It declares:

- **name** — `code-style-tournament`, `chat-outcome`, `signal-classifier`,
  `customer-complaint-handling`, etc. Generic across domains; not
  code-specific.
- **verdict enum** — the rubric's possible verdicts. May be 2 (`a-wins` /
  `b-wins`), 4 (`good`/`bad`/`unclear`/`abstain`), or 8 (slopeorhot's
  wheel slices), or N. Always reserves `skip` / `cant-judge` as a slice
  so raters who can't evaluate don't have to fake a verdict.
- **confidence options** — defaults to `["low", "mid", "high"]` but a
  rubric can override (e.g. binary confident/unsure) or disable.
- **rationale required?** — per-rubric flag. Default loose (see Q5 below).
- **prompt / instructions** — the rubric blurb shown to whoever is
  judging. Same text drives the LLM system prompt AND the human UI's
  instructions panel — single source of truth.

Langfuse-managed default rubrics live with `projectId: null` and
`partner: 'judgement-fabric'`. Projects clone them and customize the
verdict enum or instructions. Same clone-then-customize pattern Langfuse
prompt templates already use.

## Storage shape

Two `Score` rows per judgment, joined by a shared UUID:

| Score row name             | dataType    | value                              | notes |
|----------------------------|-------------|------------------------------------|-------|
| `judgement.verdict`        | CATEGORICAL | one of the rubric's enum members   | primary signal |
| `judgement.confidence`     | CATEGORICAL | `low` / `mid` / `high`             | charted as distribution, never AVG |

Verdict row's `metadata`:
```json
{
  "ratingId": "<uuid>",
  "rater": {"type": "human", "userId": "..."},
  "rubricVersion": 4,
  "rationale": "free-text, optional"
}
```

Confidence row's `metadata`:
```json
{
  "ratingId": "<same uuid>",
  "rater": {"type": "human", "userId": "..."}
}
```

`rater` is denormalized onto both rows so per-rater queries don't have to
join through `ratingId`.

### Why two rows, not one or four

Considered three shapes:

- **One row** (verdict as `value`, everything else in metadata): cheapest
  to store, but `confidence` becomes a JSON-buried field that can't be
  charted with Langfuse's built-in numeric/categorical-distribution
  visualizations, and the disagreement view needs JSON-extract in
  filter clauses.
- **Four rows** (one each for verdict, confidence, rationale, priority):
  every field first-class, but 4× row count and rationale isn't actually
  aggregatable (it's free text). Over-engineered.
- **Two rows** (the chosen shape): the two fields people will chart
  constantly are first-class; the two that are read-on-drill-down
  (rationale, and originally priority) live in metadata. Asymmetric but
  honest about how the data is actually used.

`priority` was dropped from the original design entirely — it conflates
a property of the trace's owner's queue with a property of the rating.

## Submission semantics

**Default: loose.**

- `verdict` required. Submit button enables on verdict-click.
- `confidence` defaults to `mid` and is always populated in the stored
  row. Whether the rater explicitly chose mid vs. left it at default is
  not tracked in v1. (`confidenceWasExplicit: true` on metadata is a
  trivial v1.1 add if we measure raters skewing.)
- `rationale` optional. Encouraged in UI copy but skippable.

**Per-rubric override.** The EvalTemplate's `outputDefinition` (Zod
schema) can tighten this. Common pattern: human-rater configs use the
loose default, LLM-rater configs of the same rubric require rationale.
Same rubric, two JobConfigurations, different schemas. This expresses
"humans rate fast in bulk; LLMs are forced to think" without needing
two separate rubrics.

**Why loose by default.** Forced rationale on human raters produces
"ok"/"yes"/"sure" 500 times — pollutes the dataset more than skipping
the field would. LLMs don't have this problem (they always produce
something) so requiring rationale of LLMs is free.

## Auto-block

Per-rater-type-per-rubric, manual resume only.

If a JobConfiguration's pending queue exceeds threshold (raters out, or
LLM judge has bad API key, or CI webhook is failing), THAT specific
rater-type's path on THAT rubric blocks. Other rater types on the same
rubric keep working.

Resume is a manual click in the admin UI. Same mechanism Langfuse
already uses for LLM-judge-bad-API-key recovery — no new infrastructure.

**Why manual.** Auto-recovery on queue drain creates oscillation:
queue grows → block → drain → unblock → grow. Manual click forces the
team to acknowledge they're back online. Slower recovery but the
alternative is hidden flapping.

## Versioning

Lock per `JobConfiguration`, not per `EvalTemplate`.

- Editing an EvalTemplate writes a draft new version. Active
  JobConfigurations stay on whatever version they were pinned to.
- An admin explicitly bumps a JobConfiguration's pinned version to roll
  out a rubric change.
- New traces entering the queue use the pinned version atomically at
  the moment of switch. In-flight pending judgments stay on the
  pre-switch version (no mid-rating UI rug-pull).
- Old `Score` rows stay tagged with the version they were rated under.
  Queries that need to unify versions `CASE WHEN` at read time.

Same discipline Langfuse `Prompt` already uses (the `production` label
moves between versions; old traces stay on their original version).

**No retroactive verdict migration in v1.** "Rename slice X to Y across
old Score rows" is a v1.1+ feature. v1 just keeps history honest.

## Training pipeline

**v1: data-shape only.** No training, no serving, no A/B.

v1 commits to one thing: an export endpoint shaped as training-ready
JSONL.

```
GET /api/public/v2/scores/export
    ?rubric=<name>
    &rater_type=<llm|human|agent|programmatic>
    &since=<timestamp>
    &dataset_run_id=<optional>
```

Returns one JSON object per line, joining Score + Trace + EvalTemplate
into the shape a fine-tuning harness wants:

```json
{
  "ratingId": "<uuid>",
  "rubric": "code-style-tournament",
  "rubricVersion": 4,
  "instructions": "<the rubric's prompt blurb>",
  "trace": {
    "input": "...",
    "output": "..."
  },
  "judgement": {
    "verdict": "a-wins",
    "confidence": "high",
    "rationale": "..."
  },
  "rater": {"type": "human", "userId": "..."},
  "createdAt": "..."
}
```

The schema reserves nullable fields for future training metadata
(`trainedFrom`, `trainingRunId`, `validationSetHash`, etc.) so v1.1 can
add training pipeline without migration.

**Why not ship full pipeline in v1.** Two reasons:

1. You don't know your distribution until you have one. Designing a
   training pipeline before having a few thousand judgments would mean
   designing for hypothetical data. Ship the rater fabric, collect data
   for a quarter, *then* train because you actually know the shape.
2. The rater app + comparison view alone justify the project. Training
   is downstream value. Including it in v1 risks delivering nothing
   well; deferring it lets v1 ship.

**Why not "stub it" with an Export-Training-Data UI button.** A button
that doesn't go anywhere is worse than no button. The export endpoint
is documented; motivated users find it. Real training pipeline is a
v1.1 design problem.

## Eval-of-eval loop guard

Inherited from Langfuse's existing `langfuse-*` environment exclusion in
`createEvalJobs`. Judgment fabric needs no new code here — when a
human-rating UI internally calls an LLM (e.g. to auto-classify free-text
rationales), those internal traces are tagged `langfuse-judgement`
environment and the existing guard prevents them from recursively
becoming things to rate.

## Comparison view (the load-bearing feature)

The reason this whole design exists. Given multiple rater types scoring
the same trace under the same rubric, surface where they agree, where
they diverge, and which divergences matter.

Concrete queries the view supports:

1. **Agreement matrix per rubric.** "On the `code-style-tournament`
   rubric, the human team and gpt-4o agree 73% of the time. Mostly
   diverge on Slope-and vs. Hot-but. Confidence-weighted agreement
   is 81%."
2. **Divergent traces, ranked.** "Show me traces where any pair of
   rater types disagree, ranked by confidence-product (both raters
   highly confident → most interesting)."
3. **Per-prompt-version effect.** "Did `production-system-prompt` v12
   produce more disagreement than v11?" — joins judgment data with
   Langfuse's existing prompt versioning.
4. **Per-rater calibration.** "Rater Alice scores `low` confidence on
   78% of cases, Bob scores `low` on 12%. Either Alice is too cautious
   or Bob is overconfident."

These all reduce to SQL on Score + Trace + (some join through
ratingId for confidence-pairing). The comparison view is the SQL,
visualized; nothing fancier.

## What this design unlocks beyond a vanilla rater app

- **N-way rater comparison** falls out of polymorphic `rater.type`.
- **Cross-rubric judge calibration** is one query: "show me which
  rubrics have the lowest LLM-vs-human agreement; those are the rubrics
  where the LLM judge is least reliable."
- **Prompt-eval feedback loop** — Langfuse already tracks which prompt
  version produced which trace. Cross-reference judgment scores by
  prompt version → "v12 produced 40% more `bad` verdicts than v11" →
  pin v11 back as production.
- **Rubric A/B** — two JobConfigurations on the same trace pool with
  different rubric versions; compare verdict distributions.

## Open questions deferred to post-v1

- Retroactive verdict migration tooling (when a slice is renamed, can
  we project old Score rows onto new labels?). v1.1+.
- `confidenceWasExplicit` metadata bit (currently we don't distinguish
  default mid from explicitly-chosen mid). v1.1+ if data justifies.
- Per-rater calibration adjustments (e.g. should we reweight Alice's
  ratings if she's systematically more cautious?). Statistical
  problem, not a v1 storage problem.
- Training pipeline (full shape: harness, serving, A/B, validation).
  v1.1 or later, after data accumulates.
- Structurally-nested schema for ordering enforcement (the
  `{rationale: ["...", {confidence: [...]}]}` lock). Available as a
  per-rubric `outputShape: "flat" | "nested-ordered"` flag if a
  specific rubric proves it needs the stricter form.

## Implementation surface in Langfuse

For reference; not committing to any of these as v1 deliverables yet.

| Area                     | What changes                                                                          |
|--------------------------|---------------------------------------------------------------------------------------|
| `prisma/schema.prisma`   | Add `JobConfiguration.evaluatorType: enum {LLM, HUMAN, AGENT, PROGRAMMATIC}`. Add `JobConfiguration.templateVersion: int` (replaces "track latest" default). |
| `packages/shared/types`  | Zod schema for the universal `express_judgement` tool signature, parameterized by EvalTemplate. |
| `worker/src/features/evaluation/evalService.ts` | `createEvalJobs` routes to LLMJudgeQueue / HumanPendingQueue / AgentJudgeQueue / ProgrammaticQueue based on `evaluatorType`. Loop guard already free. |
| `worker/src/queues/`     | New `HumanPendingQueue`, `AgentJudgeQueue`, `ProgrammaticQueue`. All write Score rows via existing `buildEvalScoreWritePayloads`. |
| `web/src/features/evals/server/router.ts` | tRPC endpoints for the human-rating UI: list pending, submit judgment, get past judgments. |
| `web/src/features/evals/components/`     | Wheel UI component, comparison view component. |
| `web/src/features/prompts/server/handlers/` | Export endpoint for JSONL training data. |

## What v1 does NOT include

- Full training pipeline (covered above).
- Rubric editor UI (admins edit EvalTemplate JSON directly via existing
  Langfuse template admin; pretty editor is a polish concern).
- Real-time pair-rater workflow (two raters double-blind on the same
  trace, agreement computed live). N-way comparison data shape
  supports it; UX work is post-v1.
- Auto-classification of free-text rationales (LLM that reads
  rationale and tags it with semantic categories). Real but separate
  feature.
- Rater fairness adjustments / per-rater calibration.

## Decision log

The five questions resolved in conversation, with chosen options:

1. **Score grouping** → 2 Score rows (verdict + confidence) joined by
   `metadata.ratingId`. Originally proposed 4 rows; cut after dropping
   `priority` and re-conceiving `intensity` as `confidence`.
2. **Auto-block resume** → manual, per-rater-type-per-rubric. Same
   mechanism as LLM-judge-bad-API-key recovery.
3. **Training pipeline scope** → data-shape only for v1. Documented
   JSONL export endpoint; no training harness.
4. **Rubric versioning** → lock per JobConfiguration, not per template.
   Editing template writes draft; admins explicitly pin to roll out.
   No retroactive migration.
5. **Submission semantics** → loose (verdict required, confidence
   defaults mid, rationale optional). Per-rubric override available
   for stricter LLM-judge variants.

Plus three reframings during conversation:

- The original "slopeorhot" wheel-UI-first proposal generalized to
  "one tool, many rater types, many rubrics."
- `intensity: 0..1` (drag distance from wheel center) was rejected as
  semantically muddy and replaced with `confidence: low|mid|high`.
- `priority` was dropped entirely (queue-owner concern, not a
  property of the rating).
