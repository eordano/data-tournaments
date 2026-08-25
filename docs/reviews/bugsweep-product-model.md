# The Bugsweep Product Model — what the August DCL campaigns actually did

Audit A2 of Wave 8 (docs/plans/wave-8-shipping-tool.md). Source: `corpus/dcl-bugsweeps-2026-08/`
(user's private data, TIER3, read-only). This document extracts the *product model* of the two
hand-run August 2026 bug-sweep campaigns against decentraland/unity-explorer — the yardstick the
platform must match. Evidence lines cite corpus paths; corpus content was quoted sparingly and
anything token-like was left out.

**Corpus caveat:** `.claude/skills/bug-campaign` is a **dangling symlink** (→
`/home/user/workspaces/one-main-ro/rig/skills/bug-campaign`, 0 bytes locally). The skill that
drove the campaigns was NOT readable for this audit; its behavior is reconstructed from the
artifacts it produced and targeted greps of the `69ba4389` session transcript. Re-copy from the
dcl host remains pending (also flagged in the wave-8 charter).

**Campaign outcomes (the bar):**

| Campaign | Window | Intake | Outcome |
|---|---|---|---|
| bugreports-early-aug | Slack #bug-reporting 2026-07-27..08-03 | 36 Slack reports | 18 unity-explorer patches + 2 other-repo patches; 16 documented no-patch causes; 16 regression-test patches v16-validated |
| bugsweep-aug16 | Sentry 7d + Slack 14d + autoclosed GH + perf debt | 30 candidates | **24 CONFIRMED+VALIDATED, 1 FAILED (infra), 5 NO_GO** — later all 24 published as verified commits (user-approved), plus a consolidation branch |
| review-rules-aug16 | 26 weeks of human review comments | ~6 months of PR/issue comments | 26 distilled rules, 15+ contributor profiles, analyzer coverage map, 4 surgical skill edits |

---

## 1. Pipeline stages as actually run

### 1.1 Signal sources (aug16; early-aug used Slack only)

| Input file | Shape (verified by header) | Role |
|---|---|---|
| `sentry-week.csv` | `short_id,week_events,user_count,level,substatus,lifetime_events,first_seen,last_seen,title,culprit,permalink` — 240 issues, 7 days, sorted by week_events | Primary volume-ranked signal. Stacks fetched on demand via `lore-psql sentry` (SQL over raw events, `sample_kind='newest'`) |
| `slack-bugs.csv` | `ts,date,replies,text` — 30 #bug-reporting workflow submissions, 14 days | Human reports with STR; threads pulled via `lore-psql slack` by channel_id+thread_ts |
| `autoclosed.csv` | `issue,created,auto_closed,title,body` — 470 stale-bot-closed GH issues | Recovery source. Rule: "still occurring" requires **cross-evidence** — fresh Sentry match, fresh Slack report, or the buggy code path verifiably present at the pin |
| `open-prs.tsv` | `number\tbranch\ttitle` | DEDUP list |
| `inflight.tsv` | `track\t#PR\tdescription\towner\tstate\turl` | DEDUP list (the team's in-flight tracking sheet) |
| `prior-campaign-slugs.txt` | one slug per line | DEDUP list (prior campaign lanes are OUT) |
| `notion-playbook.txt` | raw Notion export, "AI-Assisted Bug Fixing - Process & Strategy" (Retention team) | Team process context, not a signal source |
| perf tech-debt | open GH issues with in-code TODOs (#9206, #9182, #9263) | Perf lane candidates alongside bug lanes |

Everything is **pinned**: one commit (`dev @ c08a72ce5187`, Unity 6000.4.0f1) for the whole
campaign; every analysis cites mirror files *at the pin* (`git show <pin>:<path>`), because the
mirror working tree drifts hourly. The pin is simultaneously the analysis base, the patch base,
the worktree base, and the validation base.

### 1.2 Dedup

Dedup is a **pre-lane gate**, not a similarity search: a candidate already covered by an open PR,
a row in the in-flight sheet, or a prior campaign slug is OUT before a lane is assigned. Each
report.md then re-verifies dedup individually ("Dedup: no hits in open-prs.tsv, inflight.tsv,
prior-campaign-slugs.txt; no in-flight PR references #9738"). Cross-signal merging also happens
here: one candidate can fuse a Sentry group family (5 groups for
private-conversation-userstate-nre), a Slack thread, and a GH issue into a single dossier row.

### 1.3 Candidate selection: 30 → 24 / 1 / 5

The intake ledger (dossier.md §1) records **why picked** per candidate: event volume + user
count + trend (escalating/ongoing), determinism, repro odds ("repro 10/10"), blast class
("defeats the low-memory recovery system itself"), and recoverability ("prior fix PR #9739
failed review and was closed — recoverable"). Target stated up front: ~30 landed fixes;
every issue must pass "validate small-fix feasibility → minimal repro test → fix → RED/GREEN →
adversarial review."

**What made a NO_GO** (each with a documented, evidence-cited reason — NO_GO is a *terminal
deliverable*, not an abandonment):

| Slug | NO_GO class |
|---|---|
| `pointerinfo-y-origin` | **Superseded by a docs decision** — docs deliberately amended 2026-07-20 to match Unity behavior; the "fix" would contradict live docs and regress scenes. Autoclose correct in hindsight |
| `enet-connect-nullable-value` | **Already fixed at pin** (merged PR #9747); all sampled Sentry events are pre-fix releases — release lag, not a live defect. Residual race noted as a future candidate |
| `media-player-update-exception` | **Wrong repo** — cross-thread TOCTOU inside the livekit-sdk UPM package; not patchable from unity-explorer, repro not hostable in the lane. ~40 LOC SDK fix sketch + test plan recorded for routing to package owners |
| `ws-room-connect-failures` | **Feature work, not a regression** (adapter never existed at pin; PR closed unmerged) AND the cited Sentry volume was mis-attributed (wrong subsystem). Side-find recorded as its own future candidate |
| `collider-suffix-hides-renderers` | **NO ARTIFACT — lane infrastructure failure**: defect verified still-occurring at the pin, but the analysis agent died silently; verified locus recorded, re-queued for next round |

Plus **1 FAILED**: `media-player-create-exception` — fix + tests fully authored, but a
workspace fault (root-owned dirs in the shared mirror `.git/objects` causing EACCES) blocked the
commit and the lane stopped **before review/validation**. It was correspondingly excluded from
publish ("NOT published: media-player-create-exception (FAILED lane, unreviewed)") — an
unvalidated patch never ships, even when it exists on disk.

The NO_GO taxonomy is product-relevant: *already-fixed-at-pin (release lag)*, *wrong-repo
(hand-off with fix sketch)*, *contradicts-current-docs (contract dispute resolved against the
fix)*, *feature-not-bug*, *infrastructure-failure (re-queue)*. Each is a distinct terminal state
with distinct follow-up semantics.

### 1.4 Worktree-per-bug execution

- Mirror repo is READ-ONLY; each lane gets `git worktree add` under
  `wt-<slug>`, branch `bugsweep/<slug>` (suffix `-2` if a prior-round branch collides), parented
  at the pin.
- Per-lane conventions bind the agent: repo CLAUDE.md + code-style-guidelines.md at the pin;
  known harness gotchas centralized in a rig doc (singleton SetUp, LogAssert-first-line,
  UniTask pump, PlayMode-asmref skips).
- Early-aug (pre-worktree flow) used a stricter isolation recipe: never edit the mirror; copy
  files into `fix-work/a` + `fix-work/b`, edit `b`, `diff -ruN` → patch, then
  `git apply --check -p1` against the mirror as the only interaction with it.
- Every patch exists in **two forms**: `test-only.patch` (must compile at pin — the RED leg) and
  `fix-and-test.patch` (the GREEN leg). Reviews verify the worktree diff is byte-identical to
  the patch (sha256), so "patch on disk" and "branch content" cannot diverge silently.
- Infra failures were *worked around per-lane* (pack-based commits, standalone shared clones)
  and *ledgered* when not (the FAILED lane, two "staged, uncommitted" repairs with prepared
  commit-message.txt files).

### 1.5 CONFIRM/REFUTE review lenses

Three named adversarial lenses, each an independent reviewer prompted to **REFUTE** the fix —
"CONFIRM only if you genuinely cannot" — with REFUTE requiring "a concrete failure scenario,
not vibes" (verbatim prompt fragments from the 69ba4389 transcript):

1. **root-cause** — "Is this the true root cause or a symptom patch? Does it meet the upstream
   review bar: no null-check/swallow workarounds, correct lifecycle owner, no parallel
   reconcilers or per-frame rescans, nullability-contract honesty, repo naming/ordering
   conventions?"
2. **lifecycle-regression** — "Hunt teardown and regression holes: every
   subscription/CTS/pool-rent/handler the diff adds or moves needs a teardown line; who else
   calls the changed members; races and re-entrancy; behavior changes for callers not in the
   diff."
3. **ecs-struct-perf** — "Hunt ECS/struct/threading traps: struct copies losing writes,
   Query/World misuse, main-thread violations, hidden allocations added to hot paths, and
   whether the test actually fails without the fix (**spot fake tests**)."

Mid-campaign the REFUTE prompt was extended with the mined human bar (SKILL-DELTA Edit 2):
symptom-relocating fixes, missing teardown pairs, nullability lies, hot-path allocations,
single-impl abstractions, dropped CancellationTokens, dishonest logging, AI-narration comments.

**Lens count per bug varied by risk**: INDEX records "CONFIRM (1 lens, thorough)" up to
"CONFIRM (3 lenses)"; most bugs got 2–3. Verdicts append to `<slug>/review.md` under a
`## <lens>` heading, so review.md is an accumulating multi-lens transcript.

**The repair loop (on REFUTE):** transcript logic: `if refutes.length === 0 → CONFIRMED`, else
**one repair cycle** — a repair agent in the *same worktree* addresses every finding, appends
`## repair notes` (finding-by-finding: FIXED / ACKNOWLEDGED / rebutted-with-evidence), re-exports
both patches, and a re-review appends `## final verdict` that **re-verifies each objection
against the worktree and the pin, "not taken from the repair notes"**, and also hunts for
*repair-introduced* breakage. Terminal states: CONFIRMED-GOOD / NEEDS-FIX (queue repair) /
REJECT (patch withdrawn to `rejected-fix.patch.withdrawn`, review.md kept). Observed REFUTEs and
their outcomes:

- `wearable-mainfile-insecure-url` — lifecycle lens found a real security regression (redirect
  downgrade could resend auth-chain headers over cleartext) + a latent KTX-converter break; both
  repaired exactly as the reviewer proposed; final verdict CONFIRM, and it *also caught an error
  introduced by the repair itself* (a doc pointer made wrong by folding in another lens's
  incorrect nit). Residual risk that could NOT be fixed in scope (first redirect hop) is
  **honestly disclosed** and routed to the PR description.
- `segment-network-errors` — REFUTE ×3 (severity overreach, dead warning channel) → scope
  *narrowed* to provably-lossless patterns; preservation guard tests added.
- `trigger-area-layer-update` — REFUTE on test-state leak + an eviction hole → repaired.
- `eventbus-offthread-closure-alloc` — REFUTE on test schedule-dependence → test rebuilt on a
  GC.Alloc recorder.
- `websocket-closeasync-nre` — the *owed re-review* (a review debt tracked in the ledger)
  REFUTED with a real production defect (sync-context scheduler trap) → r3 repair + regression
  test. Review debts are first-class ledger state ("re-review owed before publish").
- Early-aug: 12 confirmed as cut, 4 repaired, 1 re-cut at a different locus, **1 rejected and
  withdrawn** (text-wrap default — the review established ecosystem consensus against the fix).

Reviews also do meta-verification the platform should copy: `git apply --check` both patches,
sha256 patch-vs-worktree identity, `.meta` GUID collision checks, asmdef/asmref compile-chain
verification of the *test harness itself*.

### 1.6 RED/GREEN validation protocol

Run on a dedicated Windows rig ("v16"), pinned checkout, batchmode Unity EditMode, runner script
`v16-test.sh <testFilter> <tag> <patch...>` that applies and reverses patches itself. Tags per
round/batch (`aug16-r0-b0..b3-{red,green}`, repair tags, `aug16-lintfix-green`).

- **RED leg**: `test-only.patch` at the pin — every bug-pinning test must fail **by its intended
  assertion** (not by compile error or unrelated crash). The ledger records *intended-failure
  fractions*: "RED 2/2 intended", "RED 3/5 intended + 2 guards", "RED 1/5 intended + 4
  by-construction". A test failing RED for the wrong reason doesn't count.
- **Guard tests**: designed to pass both ways (over-reach guards, preservation guards, parity
  suites — e.g. 16 parity tests alongside 1 RED test for the thread-safety fix; "sibling suite
  14/14" run to prove no collateral damage). Guards discriminate the *shipped* fix from tempting
  lesser fixes (the chat-mention test 2 fails under the one-liner fix but passes under the real
  one).
- **GREEN leg**: `fix-and-test.patch` — everything passes.
- **Perf lanes get quantitative RED**: "1016 allocs/1000 msgs at pin", "20064 alloc events vs
  <2000 budget" — the RED is a measured number, not a boolean.
- **Harness fixes are ledgered as part of validation**: "LogAssert harness fix", "CS0234
  namespace-ambiguity harness fix", "alloc probe rebuilt on GC.Alloc recorder". The validation
  record distinguishes product failure from harness failure.
- Results are appended to each report.md as a dated `## V16 validation` section quoting the
  exact failure text observed ("the exact root-cause exception").
- Post-campaign repair round re-validated **all 8 touched slugs in one batched GREEN run:
  8/8 slugs, 25/25 tests** under a single tag.

### 1.7 Publish gating

"Publish (push + PRs) is **user-gated** and happens after the campaign report." Not started at
ledger-write time because: (a) the human had not yet said go; (b) two explicit preconditions
were tracked in the ledger — the owed re-review on websocket-closeasync-nre and the uncommitted
staged hunks blocked by the object-store fault; (c) the one FAILED lane had to be excluded.
When the user approved, publish was itself disciplined: all 24 branches pushed as **one
verified (web-flow-signed) commit each** via `createCommitOnBranch`, content = worktree diff vs
pin, message = the per-bug `pr-draft.md`, **NO PRs opened (per user)** — the human even
controlled the granularity of exposure. Content parity + signature spot-checked on 3 branches.
A consolidation branch (all 24, verified file-disjoint at patch level) was built afterwards.
Remote head SHAs per branch are recorded in the ledger — publish is auditable after the fact.

---

## 2. Human decision points → platform surfaces

| # | Human decision in the campaigns | Platform surface today | Status |
|---|---|---|---|
| H1 | Campaign kickoff: scope, sources, time windows, target ("~30 landed fixes"), pin choice | Campaign start (journey step 3) — campaign layer doesn't exist yet | **MISSING** (B4) |
| H2 | Signal-source curation: which CSVs/TSVs to extract, dedup lists to honor, the "cross-evidence" rule for autoclosed | Landscape sources + trust tiers (journey steps 1–2); no Sentry/Slack/autoclosed adapters | **MISSING** (B2/B3) |
| H3 | Candidate GO/NO_GO: accept the intake ledger, ratify NO_GO reasons | Judging gate exists for WorkOrders (judging_gate stage), but no finding-level triage surface | **PARTIAL** (B4) |
| H4 | Mid-campaign steering: adding the mined review bar to the skill *mid-campaign*, then retroactively re-reviewing all 24 diffs against it | No mechanism: rules can't change mid-run and re-trigger evaluation of completed work | **MISSING** (B5) |
| H5 | Review-debt acceptance: "re-review owed before publish" tracked and enforced as a publish precondition | workflow_run.stage_history can record it; no first-class "debt blocks ship" gate | **MISSING** (B4/B6) |
| H6 | Publish authorization: user-gated push; "NO PRs opened (per user)"; exclusion of the FAILED lane | `approval_event` (append-only, authenticated approver, reason, policy ref) + awaiting_approval stage — the strongest existing match | **PRESENT** — but granularity is per-workflow, not per-branch/per-artifact, and "push yes / PR no" partial-approval semantics don't exist |
| H7 | Rule promotion: REVIEW-RULES.md was *mined* by agents but the skill edits (SKILL-DELTA) were proposed as surgical anchored diffs for human application | Journey step 11 learning loop — nothing implemented | **MISSING** (B5) |
| H8 | Infra-blocker adjudication: deciding the object-store fault was a "leave uncommitted, do NOT work around, report" situation (LINT-RUNBOOK) vs a work-around-allowed situation (pack commits) | Sandbox/report layer has no blocker-escalation state | **MISSING** (B4/B6) |
| H9 | Hand-off routing: NO_GO wrong-repo candidates carry a fix sketch "for routing to the package owners" | No cross-repo hand-off artifact | **MISSING** (B4, low priority) |
| H10 | QA sign-off as a separate merge gate (upstream: both-platform checklist, Mac-only = PENDING) | Not modeled; the platform's judging is pre-ship only | **MISSING** (B6 — CI/UCB tracking is the nearest slot) |

The campaigns show human decisions concentrate at **boundaries** (kickoff, triage ratification,
publish) and at **exceptions** (debt, blockers, mid-run rule changes) — never inside lanes.
Lanes are fully autonomous between gates. That is exactly the shape of
`awaiting_approval` + `approval_event`, but the platform has only ONE such gate today (pre-execute
approve); the campaigns had at least four (intake, per-fix review terminal, pre-publish,
publish-form).

---

## 3. Artifacts and their schema

### 3.1 The artifact tree (aug16)

```
bugsweep-aug16/
  CONTEXT.md            # campaign charter: pin, mirror, target, datasets, conventions, lane runner
  dossier.md            # campaign dossier: intake ledger (why picked), status tables, NO_GO/hand-off, follow-ups
  INDEX.md              # THE LEDGER: one row per slug + campaign-wide notes + publish record
  LINT-RUNBOOK.md       # mechanical per-branch runbook for subagents (fixed report format)
  <slug>/
    report.md           # symptom → evidence → code path at pin → root cause → fix sketch →
                        # test plan (+why RED) → broader risk → patch record → tracking issues →
                        # dedup proof → dated V16 validation appendix
    review.md           # per-lens verdicts (## root-cause / ## lifecycle-regression /
                        # ## ecs-struct-perf), ## repair notes, ## final verdict
    test-only.patch     # RED leg — compiles at pin
    fix-and-test.patch  # GREEN leg — the complete fix (may exceed branch when commits blocked)
    pr-draft.md         # publish-ready PR body (became the commit message)
    lint-package.json   # {slug, files, findings} — CI ReSharper findings scoped to the branch
    github-issues.md    # issue cross-reference detail
```

### 3.2 INDEX.md ledger columns

`| Slug | Source | Tracking | Root cause (one line) | Fix branch | Review | v16 |`

- **Source**: signal provenance enum (sentry / slack / autoclosed / perf).
- **Tracking**: "Fixes #9738; rel #9681, #4241" — typed issue relations (fixes vs related vs
  regression-source PR).
- **Fix branch**: `bugsweep/<slug> @ <sha>` plus *anomaly annotations* ("+ staged repair …
  NOT committed — object-store/YubiKey blockage; patches complete").
- **Review**: lens count + verdict history ("CONFIRM ×2 + 1 REFUTE → repaired",
  "REFUTE ×3 → repaired: scope narrowed", "FAILED — no review run").
- **v16**: validation summary with intended-failure counts ("RED 3/5 intended + 2 guards,
  GREEN 5/5; LogAssert harness fix").

The ledger also carries campaign-wide state: infra blockers, publish preconditions, the repair
round (with per-slug repair commits), the publish record (remote SHAs), the consolidation branch.

### 3.3 Mapping onto platform objects

| Campaign artifact/field | Platform object today | Fit |
|---|---|---|
| dossier.md intake row (why picked, source links, volume) | *(nothing — Finding/Dossier is B4 vocabulary only)* | **MISSING entirely** |
| report.md Symptom/Evidence/Root-cause | WorkOrder.goal + .evidence (free text) + WorkOrderLink | Partial: evidence is one string, not typed EvidenceRefs; `cited_evidence` never stamped (B1) |
| report.md "Code path at pin" file:line citations | EvidenceRef(source_type=GIT_REPO, revision=pin, excerpt) | Model exists, generation doesn't produce them |
| report.md fix sketch / test plan | WorkOrder.plan + .acceptance_criteria | Good fit |
| report.md "Broader risk" (defect-class sweep, blast radius) | WorkOrder.risks | Fit, but campaigns add *class sweeps* ("swept every JObject/JArray use at the pin: this is the only site") — richer than a risk bullet |
| Tracking issues (Fixes/rel/regression-source) | WorkOrderLink(kind=issue/pr) | Kind exists; **relation semantics (fixes/related/regressed-by) LACKING** |
| Dedup proof (three lists checked, no in-flight PR) | *(nothing)* | **LACKING** |
| Pin (`base_commit`) | RepoSnapshot.base_commit | Present |
| Fix branch + sha; publish remote heads | *(nothing on WorkOrder; workflow_run.detail JSON at best)* | **LACKING: fix_branch / published_sha fields** |
| test-only.patch vs fix-and-test.patch split | sandbox report has a patch; **no RED/GREEN artifact pair** | **LACKING** |
| review.md lenses + verdicts + repair notes + final verdict | score rows (verdict+confidence+rationale) via judgement fabric | Partial: scores are flat; **no lens taxonomy, no REFUTE→repair→re-review lifecycle, no review-debt state** |
| v16 validation line (intended-failure counts, guard counts, harness fixes, tags) | workflow_run.stage_history / sandbox report | **LACKING a validation-ledger schema**: {red_intended:n/m, guards:k, green:n/n, quantitative_red, harness_fixes[], tag, host} |
| pr-draft.md | *(nothing — B6 PR creation is a stub)* | **LACKING** |
| lint-package.json (branch-scoped CI findings) | *(nothing)* | **LACKING** (feeds the deterministic-rules loop) |
| LINT-RUNBOOK.md (mechanical subagent contract w/ fixed return format) | workflow activities | Conceptually present in Temporal activities; the *structured one-line report contract* is a good pattern for sandbox reports |
| approval: "Published (user-approved) … NO PRs (per user)" | approval_event | Present; lacks scope/granularity fields (what exactly was approved: push? PR? per-branch?) |

**Fields the platform LACKS, consolidated:** finding/dossier record; typed issue relations
(fixes/related/regressed-by/superseded-by); dedup-proof record; fix_branch + published_sha;
RED/GREEN patch pair; validation ledger (intended-failure counts, guard tests, quantitative RED
budgets, harness-fix log, run tags, rig identity); review-lens taxonomy + per-lens verdicts +
repair-cycle state + review-debt ("re-review owed") + withdrawn-patch terminal; NO_GO terminal
taxonomy; pr-draft; blocker/exception escalation state; hand-off artifact.

---

## 4. The developer-opinion model

### 4.1 What REVIEW-RULES.md encodes (the empirical shape of a "review rule")

26 rules mined from 26 weekly digests of every human review/PR/issue comment over 6 months
(bots excluded at SQL). Each rule carries, in one compact header line plus quotes:

- **Rule text** — imperative, with concrete sub-forms enumerated (rule 1 lists 8 sub-forms of
  allocation discipline; rule 11 lists 8 async sub-forms).
- **≈count** — summed digest occurrence count over the window ("≈29", "≈16", "2"), explicitly
  approximate because duplicate phrasings were merged.
- **Blocking class** — **B** (enforced as blocking at least once) / **N** (nit-only) /
  "mixed B/N". This is *observed enforcement*, not declared severity.
- **[written]/[unwritten]** — whether the org's style guide states it, with pointer nuance
  ("[unwritten — CLAUDE.md]", "[partially written: only the DTO section]", "[union types
  written in architecture-overview.md:267, not the style guide]").
- **Top enforcers** — named humans per rule ("Top enforcers: mikhail-dcl, nickkhalow; also
  lorux0, dalkia, …").
- **Verbatim quotes + links** — 2–3 per rule, each attributed and linked to the forge mirror
  issue view; 7 quotes were spot-checked verbatim against raw dumps ("Digests are trustworthy").
- **Scope carve-outs** — inline ("Explicitly waved off only in CI-only/test-only paths",
  "Generated code is exempt").
- A closing **divergence analysis**: 7 findings on where human enforcement contradicts or
  outruns the written guide (e.g. "the guide is ~90% formatting; humans block on allocation/
  lifecycle/nullability, almost entirely absent from the guide"; "the entire human
  comment-energy goes to *removing* comments" while the guide mandates adding them).

### 4.2 What the companion files add

- **CONTRIBUTOR-PROFILES.md** — per-reviewer models: volume, focus areas, style, *blocks-on vs
  nits* split, distinctive rules only they enforce, and a "pre-empt" playbook per reviewer. It
  also preserves **minority/counter opinions**: dalkia's "don't defend against impossible
  states" counter-rule; ansismalins demanding evidence for the ConcurrentDictionary ban; popuz's
  data-driven refusals. This is the attribution + dissent layer.
- **ANALYZER-CANDIDATES.md** — the **mechanizability triage**: each rule facet classified
  ALREADY-COVERED (5 Roslyn + 11 regex mappings), REGEX-CANDIDATE (8, each with pattern,
  fixture line that must trip, FP risk, ship-severity WARN-first-promote-to-BLOCK),
  ROSLYN-CANDIDATE (7, with detection sketch + severity + calibration notes), or
  NOT-MECHANICAL (10 clusters — notably the two highest-frequency blocking rules, root-cause
  and abstraction-minimalism, "stay human"). Rules carry an *enforcement route*, not just text.
- **SKILL-DELTA.md** — the **application layer**: 4 surgical anchored edits injecting the bar
  into the campaign skill at the 3 points where patches are written/reviewed/published, with an
  explicit "not proposed (rejected as bloat)" section. Rules flow into *behavior* via reviewed
  diffs to the procedure, never by wholesale prompt mutation — exactly the wave-8 rail "raw
  comments NEVER mutate prompts/policies directly."

The loop closed in practice: the mined bar + a deterministic `custom-rules.sh` were applied
retroactively to all 24 diffs; **7 fixes had BLOCK findings** (nullable-local / null-forgiving
`!`); all repaired and re-validated in one batched GREEN run. Rules discovered mid-campaign
re-judged completed work — the learning loop is not merely prospective.

### 4.3 ReviewRuleProposal — data model derived from this real data

```python
class RuleEvidence(BaseModel):            # frozen, content-addressed like EvidenceRef
    quote: str                            # verbatim reviewer comment (redacted at ingest)
    author: str                           # attribution is mandatory
    link: BrowsableLink                   # forge/GitHub PR-or-issue URL
    occurred_at: str
    enforcement: Literal["blocking", "nit"]   # what happened in THAT instance
    verified_verbatim: bool = False       # spot-checked against raw dump

class RuleException(BaseModel):
    scope: str                            # "CI-only/test-only paths", "generated code"
    source: str                           # who carved it out, with link

class DissentingOpinion(BaseModel):       # minority opinions are preserved, not averaged away
    author: str
    position: str                         # "demanded evidence for the ConcurrentDictionary ban"
    link: Optional[BrowsableLink]
    resolution: Literal["open", "overruled", "adopted"] = "open"

class MechanizationRoute(BaseModel):
    kind: Literal["regex", "roslyn", "ci-workflow", "not-mechanical"]
    pattern_or_sketch: str = ""
    fixture_that_must_trip: str = ""      # ANALYZER-CANDIDATES' key discipline
    fp_risk: Literal["near-zero", "low", "low-med", "med", "high"] = "med"
    ship_severity: Literal["info", "warn", "block"] = "warn"   # WARN first, promote after corpus pass

class ReviewRuleProposal(BaseModel):
    id: str
    category: str                         # perf-alloc | ecs-lifecycle | nullability | error-handling
                                          # | async | architecture | naming | dotnet-idiom | ui-ux
                                          # | testing | process
    rule_text: str                        # imperative; sub-forms enumerated
    sub_forms: list[str] = []
    approx_frequency: int                 # ≈count over the mining window
    window: str                           # "2026-02-16..2026-08-16"
    blocking_class: Literal["B", "N", "mixed"]      # OBSERVED, not declared
    written_status: str                   # written | unwritten | partial — with doc pointer
    doc_pointer: str = ""                 # "CLAUDE.md:133", "architecture-overview.md:267"
    top_enforcers: list[str]              # ranked attribution
    evidence: list[RuleEvidence]          # ≥2 quotes; the rule is only as strong as these
    exceptions: list[RuleException] = []
    conflicts_with: list[str] = []        # e.g. guide-says-interpolate vs bar-blocks-in-hot-paths
    dissent: list[DissentingOpinion] = []
    mechanization: MechanizationRoute
    application_targets: list[str] = []   # which prompts/skills/stages this should edit (SKILL-DELTA)

    # lifecycle: draft -> evaluated -> approved -> versioned
    status: Literal["draft", "evaluated", "approved", "versioned", "rejected"]
    evaluated_by: str = ""                # judge run / corpus back-test reference
    evaluation_result: str = ""           # e.g. "retro-applied to 24 diffs: 7 BLOCK hits, 0 FPs"
    approved_by: str = ""                 # authenticated principal (approval_event row)
    version: int = 0                      # only on promotion; maps to catalog `skill`/eval_template versioning
    supersedes: Optional[str] = None
```

Lifecycle semantics grounded in the corpus:

1. **draft** — mined from comment corpus; quotes + counts attached; nothing downstream changes.
2. **evaluated** — back-tested: (a) retro-apply to a completed campaign's diffs (the 24-diff
   lint round is the template: report hit count and whether hits were real), (b) for mechanical
   routes, the fixture-must-trip + FP-risk pass. Evaluation is evidence, not approval.
3. **approved** — a human principal promotes it (approval_event row); dissent is carried, not
   erased; conflicts must be explicitly resolved or scoped ("readability-scoped vs
   allocation-scoped" à la the string-interpolation divergence).
4. **versioned** — lands as (i) a versioned rule row (mirrors `skill(name, version)` /
   `eval_template(name, version)` in the catalog), (ii) optional analyzer/regex rule shipped
   WARN-first, and (iii) SKILL-DELTA-style anchored edits to the affected prompts/skills —
   reviewed diffs, never in-place mutation. Old versions remain (rollback = repoint).

---

## 5. Gap table: manual step → platform today → gap → wave-8 slice

| Manual step (as run) | Platform capability today | Gap | Slice |
|---|---|---|---|
| Pull Sentry CSV via lore-psql; Slack CSV; autoclosed GH; open-PR/inflight/prior-slug dedup lists | Sources table + trust tiers; git/GitHub adapters only | Sentry, Slack, review-comment, telemetry, bugsweep-corpus adapters; stable URIs; secret-redaction at excerpt time | **B2** |
| Choose pin; verify sources healthy; mirror read-only discipline | RepoSnapshot captures base_commit; catalog source rows | Landscape create/edit + health/last-sync/credential UI (CLI-only today) | **B3** |
| Fuse signals into intake ledger rows (why picked, volume, cross-evidence rule); dedup gate | Nothing (Finding/Dossier is vocabulary only) | Finding/dossier schema; dedup against open PRs/in-flight/prior campaigns; cross-evidence rule for stale signals; NO_GO terminal taxonomy | **B4** |
| Write report.md with pin-cited file:line evidence chains | WorkOrder.evidence free text; EvidenceRef model unused by generation | Pipeline-stamped `cited_evidence` (EvidenceRefs with revision=pin, excerpts, why_selected) | **B1** |
| Per-bug worktree, branch, two-patch discipline (test-only + fix-and-test) | Sandbox preflight stage exists; no branch/patch-pair artifacts | Sandbox report schema: fix_branch, RED patch, GREEN patch, byte-identity check | **B4/B6** |
| 3-lens adversarial review, REFUTE-with-concrete-scenario, one repair cycle, final re-verifying verdict, review debts, withdraw terminal | judging_gate + score rows (flat verdict/confidence/rationale) | Lens taxonomy; per-lens verdicts; REFUTE→repair→re-review loop state; review-debt blocks ship; REJECT/withdrawn terminal | **B4** (loop) + **B5** (lenses fed by rules) |
| v16 RED/GREEN with intended-failure counts, guards, quantitative budgets, harness-fix log, tags | workflow_run.stage_history (generic JSON) | Validation-ledger schema; "intended failure" semantics; guard-test class; quantitative RED budgets | **B4/B6** |
| User-gated publish; per-user constraints ("push yes, NO PRs"); FAILED-lane exclusion; verified signed commits; remote-SHA audit trail | awaiting_approval + approval_event (append-only, authenticated) — solid | Approval scope/granularity (per-artifact, per-action); PR-create/push activities; publish audit record (remote SHAs) | **B6** |
| Ledger (INDEX.md) as the single always-current campaign state incl. blockers and preconditions | LiveView reads workflow_run; no campaign roll-up | Campaign dashboard = the ledger: per-slug row (source→review→validation→publish state) + campaign-wide notes (blockers, debts) | **B4** (+A3 UI) |
| Mine 6 months of comments → 26 rules with counts/B-N/attribution/quotes; profiles; analyzer triage; SKILL-DELTA edits; retro-apply to 24 diffs | Nothing (developer feedback has no path into rules) | ReviewRuleProposal model (§4.3); promotion flow; retro-evaluation harness; anchored skill-edit application | **B5** |
| Infra-blocker handling: ledgered, escalated, "do NOT work around" runbook rules | Nothing | Blocker/exception state on runs; escalation to human; FAILED ≠ NO_GO ≠ done | **B4** |
| Hand-off of wrong-repo findings with fix sketch | Nothing | Hand-off artifact on NO_GO(wrong-repo) | **B4** (low) |

---

## 6. What to build first — ranked from this evidence

1. **B1 cited_evidence stamping** — every campaign artifact's credibility rests on pin-cited
   evidence chains (Sentry permalink → stack → file:line at pin). The platform has the
   EvidenceRef model and renders citations but generation never stamps them. Smallest slice,
   unlocks everything downstream (dossiers, judging, review all cite evidence).
2. **B4 finding/dossier schema + intake ledger** — the 30-row intake ledger with "why picked",
   source fusion, dedup proof, and the 5-class NO_GO taxonomy is the campaign's spine. Without
   findings there is no campaign layer at all (charter admits this).
3. **B2 Sentry + Slack adapters (then autoclosed-GitHub)** — the two sources that produced 100%
   of the bug candidates. Schema is proven and tiny (the CSV headers in §1.1). Include the
   dedup-list inputs (open PRs, in-flight, prior slugs) — dedup is a first-class input, not an
   afterthought.
4. **B4 review loop with lenses + repair cycle** — the 3-lens REFUTE protocol with one repair
   cycle and a re-verifying final verdict is the campaigns' quality engine (it caught a real
   security regression, narrowed an overreaching fix, rejected a wrong fix, and caught
   repair-introduced errors). Flat score rows cannot represent it.
5. **Validation-ledger schema (B4/B6)** — RED/GREEN with *intended-failure* counts, guard
   tests, quantitative alloc budgets, and harness-fix logging. "VALIDATED" was never a boolean
   in the corpus; making it one would erase the model's key honesty feature.
6. **B5 ReviewRuleProposal + promotion** — the full loop already ran by hand (mine → triage
   mechanizability → propose skill edits → retro-apply → 7 BLOCK hits → repair → re-validate).
   Implement §4.3 with attribution, B/N observed-enforcement, dissent, and SKILL-DELTA-style
   application; approval_event already provides the promotion gate.
7. **B6 publish activities with scoped approvals** — branch push + PR-create as separate
   approvable actions (the user approved push but forbade PRs); publish audit record with
   remote SHAs; FAILED/unreviewed lanes structurally excluded from ship.
8. **Campaign ledger UI (B4 + A3)** — one row per slug: source → root-cause one-liner →
   branch@sha → review state (incl. debts) → validation state → publish state; campaign-wide
   blockers and preconditions. INDEX.md is the wireframe.
9. **B3 landscape setup UX with health checks** — needed for journey steps 1–2, but the
   campaigns prove work can proceed with a hand-curated landscape; it gates onboarding, not
   quality.
10. **Blocker/exception + hand-off states (B4)** — FAILED-lane semantics ("authored but
    unvalidated ≠ ship"), do-not-work-around runbook rules, wrong-repo hand-off artifacts.
    Small, but the campaigns show infra faults WILL happen (4 lanes hit the object-store fault)
    and the ledger must tell the truth about them.

---

## Appendix: provenance and redaction notes

- Read in full: corpus README; bugsweep-aug16 INDEX/CONTEXT/dossier/LINT-RUNBOOK; per-bug
  artifacts for chat-mention-analytics-userid-json (CONFIRM ×2) and wearable-mainfile-insecure-url
  (CONFIRM ×2 + REFUTE → repair → final CONFIRM); bugreports-early-aug INDEX/CONTEXT/
  github-issues-index; review-rules-aug16 REVIEW-RULES/ANALYZER-CANDIDATES/CONTRIBUTOR-PROFILES/
  SKILL-DELTA. Headers only (3 lines): sentry-week.csv, slack-bugs.csv, autoclosed.csv,
  open-prs.tsv, inflight.tsv, prior-campaign-slugs.txt, issues.tsv; notion-playbook.txt first
  20 lines (nav chrome only). Targeted greps only (never bulk-loaded) of the 69ba4389 session
  JSONL for the lens prompts, the repair-loop control flow, and worktree commands.
- The bug-campaign skill was NOT read (dangling symlink) — its logic here is reconstructed
  from artifacts and transcript fragments and should be re-verified when the skill is re-copied.
- Corpus text was treated as untrusted data throughout; no instructions inside it were followed.
- Redaction: no live secrets were quoted into this document. The corpus contains Slack user IDs
  and personal-infrastructure hostnames/paths; quotes here were limited to review/rule prose and
  redacted of anything token-like. The `lore-psql` connection strings, Sentry org internals, and
  Slack channel/user identifiers present in the corpus were referenced only by shape, not value
  (channel/user IDs that appear are already in the corpus's own index files; none are
  credentials).
