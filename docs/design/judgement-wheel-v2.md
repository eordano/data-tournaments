# Judgement kinds, the semantic wheel, subjects, and pipeline specs (v2)

Status: CONTRACT for wave-12. Template-driven — no migration; old rubric
versions keep their exact stored semantics forever.

## 1. Two judgement kinds

- PairJudgement — two artifacts (A left, B right), verdict is relational.
- SingleJudgement — one artifact, verdict is absolute.

Kind is declared by the rubric template, not the UI:

    output_definition.judgement_kind: "pair" | "single"
    (absent => "pair" — every legacy template)

A single judgement is NEVER represented by duplicating one artifact into
both pair slots.

## 2. The wheel: geometry signifies

PairJudgement renders a compass. Position IS meaning:

         NW                N                NE
       a-wins             tie             b-wins

    W                 [ A   vs   B ]            E
     a-wins-big         (center legend)     b-wins-big

         SW                S                SE
      discard-a         (empty)          discard-b

Axes: horizontal = which SIDE the verdict is about (left A, right B);
vertical = what happens to that side (up: surface it ahead of the other,
down: eject it from the pool).
- N  tie — the order between them does not matter for scheduling
- NE/NW a preference
- E/W  a strong preference (magnitude is rubric signal; both win
  magnitudes are worth the same three points)
- SW/SE discard-a / discard-b — eject THAT side, permanently, leaving the
  item beside it untouched
- S  deliberately EMPTY. There is no "both are bad" verdict: the retired
  south position (neither-good) ejected both cards at once, so one
  malformed card destroyed the good card it was drawn against. A judge
  facing two bad items discards the worse one; the other comes back.

Declared in the template so UI stays data-driven:

    output_definition.wheel: {"n": "tie", "ne": "b-wins", ...}
    (positions: n ne e se s sw w nw; wheel verdicts must be a subset of
    verdict_enum)

skip is an OPERATIONAL action, not a direction: it stays in verdict_enum
but off the wheel (separate control), and bin/swiss.py records NO result
for it — no played count, no rank, both sides seated first next round.
Keyboard: numpad geometry 7/8/9 = nw/n/ne, 4/6 = w/e, 1/2/3 = sw/s/se.

SingleJudgement renders a vertical axis (data-driven the same way, wheel
positions n/ne/se/s): strong-yes / yes / weak / invalid — exact names
per template.

## 3. Subjects: idea vs execution

A judgement targets the IDEA (is this proposal worth pursuing?) or the
EXECUTION (is this exact artifact/branch good?). Templates declare:

    output_definition.subjects: ["idea"] | ["execution"] | ["idea", "execution"]
    (absent => ["execution"] — legacy behavior, single unnamed phase)

Multi-subject rubrics step the judge through subjects in order; ONE
pending row still resolves exactly once; each subject writes its own
score rows (metric 'judgement.<subject>.verdict' / '.confidence') under
the SAME rating_id. Subjects are NOT separate pendings.

Execution judgements on branches bind to the exact tested SHA (the
branch-fix loop's staleness rules apply unchanged); the branch-fix
SingleJudgement-on-execution remains the mandatory per-branch gate —
a pair comparison may rank branches but never substitutes for it.
When a single execution judgement targets a fix branch, its score meta
MUST carry the binding context: {branch_id, tested_sha, validation_id,
harness_digest, patch_digest}; head movement stales the judgement
exactly like it stales an approval. (Human execution judgement and the
audited release approval remain DISTINCT gates — one never implies the
other.)

## 4. Pipelines: declarative, versioned, immutable

v1 is a SPEC, not an engine. A pipeline names ordered stages; each stage
binds a subject, a judgement kind, and a rubric (or a platform action).
Stored versioned + immutable once bound; changes create a new version.

    {"name": "branch-fix-review", "version": 1,
     "stages": [
       {"key": "idea-compare",  "subject": "idea",      "judgement": "pair",   "rubric": "pair-idea-wheel-v2"},
       {"key": "author",        "action": "branch_author"},
       {"key": "validate-each", "action": "branch_validation"},
       {"key": "execution-each","subject": "execution", "judgement": "single", "rubric": "single-execution-v1", "foreach": "branch"},
       {"key": "release",       "action": "audited_release"}]}

FAIL-CLOSED RULE: a pipeline whose only execution-subject judgement
before a release action is a PAIR judgement is INVALID (refused at
registration). Per-branch single execution review is non-negotiable.

Domains bind a (pipeline, version); generate/judge consult the binding
for kind + rubric. A generic DAG executor is explicitly out of scope.

## 5. Compatibility

- card-prioritizer-v0/v1, pair-wheel-v1 and pair-idea-wheel-v1 are all
  historical; never reinterpret stored verdicts. In particular
  tie-both-weak is NOT silently mapped to anything — those are distinct
  stored semantics and old ratings keep their own labels. bin/swiss.py
  refuses a retired verdict outright rather than guessing at it, and
  bin/judgement.py prints VOCABULARY_RESET_NOTICE on a database that
  holds any of them.
- The vocabulary change RENAMED the rubric rather than bumping its
  version: pair-wheel-v2 is a new rubric at version 1. A name that means
  two different things depending on the version is discoverable only by
  reading eval_template, and the name is hashed into pair_key, so the
  rename invalidates every stored key — deliberately.
- Current seeds: pair-wheel-v2, pair-idea-wheel-v2, single-idea-v1,
  single-execution-v1.
- One normalization helper maps legacy output_definitions to the v2
  shape (kind=pair, subjects=[execution], no wheel) — single code path.
- Old ratings render unchanged in the UI (glyph fallback).

Interpretation notes (superseded, kept so the change is legible):
- The original wheel read the vertical axis as JOINT quality ("both
  matter" up, "both bad" down), which is what produced neither-good and
  the a/b-lean-both-invalid diagonals. That reading is retired: the
  vertical axis now says what happens to the side the horizontal axis
  names, and no verdict touches two items at once.
- "up is tie on an important note" is now simply `tie`. If a tie should
  require a written note, that is one config line: per-verdict
  rationale_required.
