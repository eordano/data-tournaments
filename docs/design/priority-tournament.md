# Priority tournament: pairwise comparison into the implementation queue

*2026-08-27. The design of record for how work orders acquire a priority order
and how that order feeds implementation. Covers the bracket machinery in
`bin/run-tournament.py`, the human queue in `bin/judgement.py` and `/judge`,
the `priority` field on `bin/workorder.py`, and the author/validate/ship
pipeline in `bin/branch_{author,validator,ship}.py`. Sibling to speaches-plus
`06.9-roll-over-beats.md`, which specifies the evidence ladder this document
feeds. Supersedes the single-elimination sketch it replaces.*

## Why

`WorkOrderDraft` asks the generating model for a `priority` in `P0..P3`. That
is a self-assessed absolute score, produced by a model that has seen one item
and cannot see the other thirty-two. It is the weakest field on the artifact,
and every downstream decision — what gets implemented, in what order, with
whose attention — currently rests on it.

Absolute scoring is the wrong instrument for the same reason it is the wrong
instrument in the judge UI: people and models are unreliable at "how important
is this, out of ten" and reliable at "which of these two matters more". The
binding constraint on the whole operation is review capacity, so the instrument
that consumes the least human attention per unit of decided order wins.

The longer-range reason is the one that matters most. **The judgements are the
product.** Every comparison a person makes is a labelled example of this
team's priorities, and the point of collecting them is that in a month the
machine makes the same calls unaided. The tournament is how that data gets
generated as a side effect of work somebody had to do anyway.

## The rule

**No item enters the implementation queue without a settled position, and
position is established only by pairwise comparison.** The self-assessed
`priority` field survives as a thing the result can be compared against —
never as the thing that schedules work.

## The pairing is Swiss

Single elimination is the wrong shape here, for three reasons that all showed
up the moment it was drawn:

- **It cannot express a tie**, and ties are common and informative. A binary
  bracket has to break every one of them arbitrarily, which destroys the
  symmetry the bracket depends on.
- **It needs a power of two**, and a generation round produces whatever it
  produces.
- **It throws away most of what was learned.** An item beaten in round one by
  the eventual best item is recorded identically to an item beaten in round one
  by the worst. Both read as "lost immediately".

Swiss fixes all three by scoring instead of eliminating. Nothing is knocked
out; everything plays every round.

### The rules

1. **Round one is random.** No seeding — the self-assessed priority is not
   trusted enough to shape the draw.
2. **Football points: a win is 3, a draw is 1, a loss is 0.**
3. **Later rounds pair on similar standing.** Sort by matches played ascending,
   then score descending, then pair neighbours.
4. **Never repeat a pair.** Two items meet at most once; a pairing that would
   be a rematch is skipped for the next candidate down. This is not a
   nicety — a rematch spends a person's attention on a question already
   answered.
5. **Rounds run to `⌈log₂N⌉`** and the ordering is the final points table.

### The judge never sees the score

The person comparing two items is shown two items. No points, no standings, no
running rank. They decide which of the two matters more, and that is the entire
interaction. Standing is a derived view for the queue, not context for the
judgement — showing it would anchor the next comparison to the last one.

### Cost, and the graceful stop

Thirty-three items, six rounds of about sixteen pairings:

| | Comparisons | Yields |
|---|---|---|
| Swiss, full | ~96 | a complete ordering |
| Swiss, stopped at round 3 | ~48 | a coarse ordering, top group identified |
| single elimination | 32 | one winner, no ordering |
| round-robin | 528 | a complete ordering |

The middle row is the operationally useful one. **Swiss degrades gracefully:**
stopping early costs resolution, not validity, because every item has a score
after every round. An elimination bracket has to finish or it has decided
nothing. Rounds are therefore a dial the campaign sets, not a structural
constant.

## Discard is a verdict, not a loss, and it is PER SIDE

The human can say an item should not be in the tournament at all — it is not a
real finding, or it should never have been generated. That is different from
losing, and it must not be recorded as losing.

`pair-wheel-v2` in `bin/judgement.py` carries exactly two discard verdicts, and
each one names ONE side: `discard-a` ejects A, `discard-b` ejects B. There is
deliberately no "both are bad" verdict — the south wheel position is empty. A
judge facing two bad items discards the worse one; the other returns to the
pool and is judged on its own merits next round. The retired vocabulary
(`incoherent`, `neither-good`, the `*-lean-both-invalid` diagonals) removed
BOTH sides at once, so one malformed card destroyed the perfectly good card it
happened to be drawn against. It is gone, and `bin/swiss.py` refuses those
names rather than reinterpreting them.

A discarded item **leaves the pool immediately** and is never paired again, and
its outstanding judge-queue rows are cancelled in the same breath. It does not
score zero — zero is a real position, occupied by items that lost honestly.
Discards are reported as their own set, and they are training signal in their
own right: they say the generation stage produced something it should not have.

**The survivor of a discarded pairing gets no result.** It is not credited with
a win, it is not charged a played match, and it keeps the standing it walked in
with. `pairing_order` therefore seats it *before* every item that has played —
the same treatment a bye gets, for the same reason: nothing was established
about it. Crediting the survivor would be scoring an item for the failure of
whatever it was drawn against.

## Skip establishes nothing, and it awards no rank

A rater who genuinely cannot call a pairing needs somewhere to say so, or the
honest answer becomes a guessed `tie` that puts points on the table. `skip` is
therefore the eighth verdict on `pair-wheel-v2` — judge-facing, but off the
wheel, sitting with the operational actions.

It takes the same path the bye and the discard survivor take, and `bin/swiss.py`
names that path once (`no_result`): no result row, no played count, no rank,
both sides seated first in the next draw. A verdict that counted as *played*
while scoring *nothing* is precisely the mechanism that turns an unscored
rubric into a confident WRONG ordering — every item on zero points, ranks
handed out in item-id order — instead of an honestly empty one.

## Match results are keyed to the pair, not to the round

A judgement records `sha256(item_a_content || item_b_content || rubric_id ||
rubric_version)`, and the tournament is a derived view over that pool. This is
`06.9-roll-over-beats.md` §3 applied one level up, and under Swiss it does
double duty:

- **It is the rematch check.** "Have these two met?" is a lookup in the pool,
  not a walk of the round history.
- **New items do not invalidate old work.** An item generated after round two
  enters at zero points and plays from there. Nothing already judged is
  re-asked.
- **A rubric revision invalidates exactly the matches judged under the old
  rubric.** Those degrade to stale: still visible as prior context, no longer
  contributing points.

## Ties and noise

**Draws are first-class** and worth one point each. A draw is a statement that
the order between two items does not matter for scheduling, which is
information the queue can use and the optimizer can learn from — not a failure
to decide.

**Intransitivity.** Human pairwise preference is noisy and not always
transitive. Swiss absorbs this far better than a bracket does, because a single
odd verdict moves an item by one match rather than eliminating it. The
mitigation on top is already half-built: the LLM judge plays the same pool in
the background, agreement is cheap confirmation, and **disagreement identifies
the pairs worth a person's second look**.

**The effort bias.** A cheap fix must not outrank a critical one because it
ships sooner. The comparison prompt asks which matters more, never which is
easier, and effort is not an input to the pairing. Worth restating because the
downstream stage — where fast work visibly completes first — will keep applying
pressure the other way.

## The tournament does not block the queue

Standing is meaningful after every round, so the top group is known long before
the last round is played. Work starts on it while the lower pairings are still
being judged.

## The handoff

An item leaves the tournament with a position and enters the existing pipeline,
which is already a beat ladder in everything but name:

```
work order  →  branch_author     authoring failure or empty diff  → REJECT
            →  branch_validator  red / green / guard in an isolated worktree
            →  branch_ship       fail-closed; only the approved branch
                                 at its exact tested SHA
```

Two routing rules the flow needs and does not have:

- **Route by work type.** `WorkOrder.work_type` already distinguishes
  `bug-fix` / `feature` / `change-request` / `refactor` / `investigation`.
  Only the mechanically-authorable types go to `branch_author`. An
  investigation at the top of the table is a person's next task, not a branch —
  and it must not silently become an empty commit.
- **A failed item does not block.** The rest of the top group is already
  settled. An item whose beats fail is dequeued with its `GateOutcome`, not
  retried in place — unless the failure was ours rather than the item's (an
  unavailable runner, a `skipped` rung), in which case it returns at the
  standing it earned.

Review is per work order. Assembling many accepted work orders into a single
review artifact is explicitly out of scope for this document.

## The return edge

The part that makes this a loop rather than a pipeline:

**The beats grade the ordering.** An item that won its comparisons and then
failed to compile is a labelled example that the ranking rubric over-valued
something — exactly the shape of training signal `bin/optimize.py` consumes.
Today the optimizer learns only from judgements a human made about pairs; with
this edge it also learns from what happened when the winner met a compiler.

Symmetrically, an item that sailed through every beat and turned out to be
trivial is evidence the rubric under-weighted difficulty.

Neither signal may promote a rubric on its own — the promotion gate is
unchanged, and remains subject to the same demand for a measured floor.

## What exists, what is missing

| Piece | State |
|---|---|
| Swiss engine: rounds, matches, byes, points, standings, no-rematch | ships (`bin/swiss.py`); single-elimination pairing is retired, not left alongside |
| Orchestrator pairing every round through the engine | ships (`run-tournament.py`, reading `swiss.CANONICAL_VERDICT_FOR_OUTCOME`) |
| Human pairwise judge UI with verdicts and rationale | ships (`/judge`) |
| Per-side discard vocabulary | ships (`pair-wheel-v2`: `discard-a`, `discard-b`) |
| Skip that awards no rank | ships (`pair-wheel-v2`: `skip` → `swiss.no_result`) |
| Pair enqueue from a concluded match | ships (`enqueue_for_match`) |
| Pair-keyed judgements, tournament as derived view | ships (`pair_key` on `score` and `pending_judgement`) |
| Tournament built over work orders for a human, not over files for an agent | ships (`generate_cards.py` `work-order` artifacts, domain-scoped) |
| Author → isolated validate → fail-closed ship | ships (`branch_*.py`) |
| Standing carried onto the work order | ships (`bin/dispatch.py` → `WorkOrder.TournamentStanding`) |
| Beat outcome fed back as ranking evidence | ships (`dispatch.standing_for_branch` → `branch_validator.validate`) |
| **Multiplayer: more than one human on one pool** | missing — wanted, not designed |
| **Reviewer criterion captured, not just the verdict** | missing — deferred by decision |

## Open

- How many rounds a campaign should default to. `⌈log₂N⌉` gives a full
  ordering; the honest answer is however much resolution the implementation
  stage can absorb in a cycle, which is a measurement nobody has taken.
- **Multiplayer.** More than one person judging the same pool, able to weigh in
  on a comparison and to override an agent's verdict. Wanted, not designed.
- Whether the background LLM judge may **fill pairings** a human has not
  reached, marked as machine-decided, or only ever advise. Filling completes the
  table without a person; it also makes the ordering a model's opinion, which is
  the thing the tournament was built to stop.
- Capturing the reviewer's *criterion* — not just the verdict — as continual
  learning signal. Deferred by decision, not resolved.
