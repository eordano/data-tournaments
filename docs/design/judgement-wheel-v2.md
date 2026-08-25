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

PairJudgement renders an 8-position compass. Position IS meaning:

         NW                N                NE
   a-slightly-better  tie-both-important  b-slightly-better

    W                 [ A   vs   B ]            E
   a-strongly-better    (center legend)   b-strongly-better

         SW                S                SE
   a-lean-both-invalid  neither-good   b-lean-both-invalid

Axes: vertical = joint quality (up: both matter, down: both bad);
horizontal = preference direction (left A, right B); diagonals mix them.
- N  tie-both-important — a tie worth flagging: both deserve to survive
- NE/NW slight preference, both acceptable
- E/W  strong preference
- SE/SW slight preference for B/A even though the pair is weak/invalid —
  distinguishes "both weak, but B is closer" from a flat tie
- S  neither-good — reject both

Declared in the template so UI stays data-driven:

    output_definition.wheel: {"n": "tie-both-important", "ne": ..., ...}
    (positions: n ne e se s sw w nw; wheel verdicts must be a subset of
    verdict_enum)

skip / incoherent are OPERATIONAL actions, not directions: they stay in
verdict_enum but off the wheel (separate controls). Keyboard: numpad
geometry 7/8/9 = nw/n/ne, 4/6 = w/e, 1/2/3 = sw/s/se.

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
       {"key": "idea-compare",  "subject": "idea",      "judgement": "pair",   "rubric": "pair-wheel-v1"},
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

- card-prioritizer-v0 is historical; never reinterpret stored verdicts.
  In particular tie-both-weak is NOT silently mapped to neither-good —
  they are distinct stored semantics; old ratings keep their own labels.
- New seeds: pair-wheel-v1, single-idea-v1, single-execution-v1.
- One normalization helper maps legacy output_definitions to the v2
  shape (kind=pair, subjects=[execution], no wheel) — single code path.
- Old ratings render unchanged in the UI (glyph fallback).

Interpretation notes (flagged for cheap correction):
- "up is tie on an important note" is read as "a tie between two artifacts
  that BOTH matter" (tie-both-important). If it meant "tie requires a
  written note", that is one config line: per-verdict rationale_required.
- "45 down is slight preference for invalid a or b" is read as "the pair
  is weak/invalid overall, but I lean A (SW) or B (SE)" — a-lean-both-invalid /
  b-lean-both-invalid. Alternative reading: "prefer B because A is
  invalid" (validity asymmetry). Both are one enum rename away; the UI
  labels spell out the current reading so a correction is obvious.
  DECISION 2026-08-18: user was asked, no answer in the window — shipped
  with the "both weak, lean A/B" reading (S covers flat rejection);
  flip = rename two enum values + two labels in pair-wheel-v2.
