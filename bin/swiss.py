"""Swiss pairing engine for the priority tournament.

Football points: a win is 3, a draw is 1, a loss is 0. Round one is a seeded
shuffle with no standing input; every later round sorts the active pool by
matches played ascending, then points descending, and pairs neighbours. A
pairing that would repeat a pair key is skipped for the next candidate down,
so two items meet at most once under one rubric version. Rounds run to
``ceil(log2 N)`` and standings are well defined after every one of them, so
stopping early costs resolution rather than validity. That makes the round
count a dial: :func:`rounds_cap` takes a campaign's override, :func:`is_settled`
says when a pool has played it out, and :func:`has_pairable_pair` separates
"finished" from "every legal comparison is already made".

A pair key is sha256 over the two item contents (order-normalised) plus the
rubric id and rubric version. A rubric revision therefore produces different
keys: matches judged under the superseded version degrade to stale -- still
returned by :func:`history` as prior context, contributing no points, and no
longer blocking a rematch under the new version.

Discards are verdicts, not losses, and they are PER SIDE. ``discard-a`` ejects
A from the pool for good and ``discard-b`` ejects B; neither one touches the
item beside it. An item leaves on its own merits, never as collateral from a
malformed card it happened to be drawn against. A discarded item is absent
from standings entirely: it does not score zero, because zero is the honest
position of an item that lost every match it played.

Three things a round can do establish nothing: a bye, the survivor of a
discard, and a ``skip`` (the judge could not call it). All three go through
ONE path, :func:`no_result`: no Result is appended, so the item's played count
does not move and :func:`pairing_order` seats it before every item that has
played. A verdict that counted as played while scoring nothing is exactly what
turns an unscored rubric into a confident wrong ordering instead of an empty
one, so skip awards no rank at all.

Only a bye is fair-shared: an item that has already had one is not given
another while an item without one remains.

The engine is storage-agnostic: a :class:`Pool` is an in-memory value that
callers rebuild from whatever they persist. :func:`cancel_pending` is the one
exception, because a discard has to reach the judge queue immediately.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

WIN_POINTS = 3
DRAW_POINTS = 1
LOSS_POINTS = 0

OUTCOME_A = "a"
OUTCOME_B = "b"
OUTCOME_DRAW = "draw"
OUTCOME_DISCARD_A = "discard-a"
OUTCOME_DISCARD_B = "discard-b"
OUTCOME_SKIP = "skip"

VERDICT_OUTCOMES: dict[str, str] = {
    "discard-a": OUTCOME_DISCARD_A,
    "discard-b": OUTCOME_DISCARD_B,
    "a-wins-big": OUTCOME_A,
    "a-wins": OUTCOME_A,
    "tie": OUTCOME_DRAW,
    "b-wins": OUTCOME_B,
    "b-wins-big": OUTCOME_B,
    "skip": OUTCOME_SKIP,
}

CANONICAL_VERDICT_FOR_OUTCOME: dict[str, str] = {
    OUTCOME_A: "a-wins",
    OUTCOME_B: "b-wins",
    OUTCOME_DRAW: "tie",
    OUTCOME_DISCARD_A: "discard-a",
    OUTCOME_DISCARD_B: "discard-b",
    OUTCOME_SKIP: "skip",
}

AN_OUTCOME_REPLAYS_THROUGH_THE_ENGINES_OWN_VERDICT_NEVER_A_CALLERS_COPY = (
    "a caller holding a stored outcome ('a', 'draw', ...) and needing a "
    "verdict to feed back into record() reads CANONICAL_VERDICT_FOR_OUTCOME "
    "rather than restating a vocabulary it does not own -- that restatement "
    "is what left the orchestrator raising on every match after the rename."
)
assert set(CANONICAL_VERDICT_FOR_OUTCOME) == set(VERDICT_OUTCOMES.values()), (
    f"every engine outcome needs a verdict to replay through. "
    f"{AN_OUTCOME_REPLAYS_THROUGH_THE_ENGINES_OWN_VERDICT_NEVER_A_CALLERS_COPY}"
)
assert all(
    VERDICT_OUTCOMES[verdict] == outcome
    for outcome, verdict in CANONICAL_VERDICT_FOR_OUTCOME.items()
), (
    f"a canonical verdict must score as the outcome it stands for. "
    f"{AN_OUTCOME_REPLAYS_THROUGH_THE_ENGINES_OWN_VERDICT_NEVER_A_CALLERS_COPY}"
)

BOTH_WIN_MAGNITUDES_SCORE_THE_SAME_THREE_POINTS_MAGNITUDE_IS_RUBRIC_SIGNAL = (
    "a-wins-big and a-wins are both worth WIN_POINTS, and so are their b "
    "mirrors: the magnitude is signal for the rubric optimizer, never a "
    "different number of points on the table."
)
assert {VERDICT_OUTCOMES["a-wins"], VERDICT_OUTCOMES["a-wins-big"]} == {OUTCOME_A}, (
    BOTH_WIN_MAGNITUDES_SCORE_THE_SAME_THREE_POINTS_MAGNITUDE_IS_RUBRIC_SIGNAL
)
assert {VERDICT_OUTCOMES["b-wins"], VERDICT_OUTCOMES["b-wins-big"]} == {OUTCOME_B}, (
    BOTH_WIN_MAGNITUDES_SCORE_THE_SAME_THREE_POINTS_MAGNITUDE_IS_RUBRIC_SIGNAL
)

EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP = (
    "a rubric verdict absent from VERDICT_OUTCOMES resolves to OUTCOME_SKIP for any "
    "caller that supplies a default, and a skip establishes NOTHING: no result, no "
    "played count, no rank. A fully judged pool whose comparison verdicts all miss "
    "the map therefore reports an empty ordering rather than a wrong one. Any rubric "
    "registered in bin/judgement.py must have every comparison verdict listed above, "
    "or its campaign silently orders nothing."
)

EJECTED_SIDE_BY_VERDICT: dict[str, str] = {
    "discard-a": OUTCOME_A,
    "discard-b": OUTCOME_B,
}
DISCARD_VERDICTS = frozenset(EJECTED_SIDE_BY_VERDICT)

A_DISCARD_EJECTS_THE_NAMED_SIDE_ONLY_NEVER_THE_ITEM_BESIDE_IT = (
    "discard-a ejects A and leaves B in the pool; discard-b ejects B and "
    "leaves A. An item is ejected on its OWN merits and never as collateral "
    "from the card it was drawn against. There is deliberately no "
    "'both are bad' verdict: a judge facing two bad items discards one, and "
    "the other returns to the pool to be judged on its own next time."
)

A_DISCARDED_PAIRING_PRODUCES_NO_RESULT_SO_THE_SURVIVOR_IS_SEATED_LIKE_A_BYE = (
    "the survivor of a discarded pairing scores nothing and its played count "
    "does not move, so pairing_order seats it before every item that has "
    "played -- the same treatment a bye gets, for the same reason: nothing "
    "was established about it."
)

NO_RESULT_CAUSE_BYE = "bye"
NO_RESULT_CAUSE_DISCARD_SURVIVOR = "discard-survivor"
NO_RESULT_CAUSE_SKIP = "skip"
NO_RESULT_CAUSES = frozenset({
    NO_RESULT_CAUSE_BYE,
    NO_RESULT_CAUSE_DISCARD_SURVIVOR,
    NO_RESULT_CAUSE_SKIP,
})

NO_RESULT_OUTCOMES = frozenset({
    OUTCOME_DISCARD_A, OUTCOME_DISCARD_B, OUTCOME_SKIP,
})

NO_RESULT_CAUSE_FOR_OUTCOME: dict[str, str] = {
    OUTCOME_DISCARD_A: NO_RESULT_CAUSE_DISCARD_SURVIVOR,
    OUTCOME_DISCARD_B: NO_RESULT_CAUSE_DISCARD_SURVIVOR,
    OUTCOME_SKIP: NO_RESULT_CAUSE_SKIP,
}

A_SKIP_MUST_NOT_AWARD_A_RANK_SO_IT_TAKES_THE_SAME_PATH_A_BYE_TAKES = (
    "skip is judge-facing -- a rater who genuinely cannot call a pairing says "
    "so instead of guessing -- but it establishes nothing, so it appends no "
    "result, moves no played count and awards no rank. A verdict that counted "
    "as played while scoring nothing is the mechanism that turns an unscored "
    "rubric into a confident WRONG ordering instead of an empty one, so the "
    "bye, the discard survivor and the skip all leave through no_result()."
)

_PAIRING_STEP_BUDGET = 20000
_UNSET = object()


def known_verdicts() -> frozenset[str]:
    """Every verdict this engine scores.

    A SUPERSET of every rubric bin/judgement.py registers, never a subset:
    a rubric verdict the engine does not know resolves to OUTCOME_SKIP and
    scores nothing while still counting as played.
    """
    return frozenset(VERDICT_OUTCOMES)


def outcome_for_verdict(verdict: str, default=_UNSET) -> str:
    """Map a rubric verdict onto an engine outcome.

    Raises ValueError on a verdict from some other vocabulary unless the
    caller supplies a default -- a loader replaying a foreign rubric's rows
    wants them inert, not fatal.
    """
    try:
        return VERDICT_OUTCOMES[verdict]
    except KeyError:
        if default is not _UNSET:
            return default
        raise ValueError(
            f"verdict {verdict!r} is not scored by any registered rubric; "
            f"known: {sorted(VERDICT_OUTCOMES)}. "
            f"{EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP}"
        ) from None


@dataclass(frozen=True)
class Item:
    """One tournament entrant. ``content`` is what the pair key hashes."""

    id: str
    content: str
    payload: Optional[dict] = None


@dataclass(frozen=True)
class Match:
    round: int
    slot: int
    item_a: str
    item_b: str
    pair_key: str


@dataclass(frozen=True)
class Round:
    number: int
    matches: list[Match]
    byes: list[str]


@dataclass(frozen=True)
class Result:
    round: int
    item_a: str
    item_b: str
    verdict: str
    outcome: str
    pair_key: str
    rubric_id: str
    rubric_version: int


@dataclass(frozen=True)
class Discard:
    """One item's exit from the pool: which item, which side of the pairing
    named it, the verdict that named it, and whom it was drawn against."""

    item_id: str
    verdict: str
    round: int
    side: str
    opponent: Optional[str] = None


@dataclass(frozen=True)
class HistoryEntry:
    round: int
    item_a: str
    item_b: str
    verdict: str
    outcome: str
    pair_key: str
    rubric_id: str
    rubric_version: int
    stale: bool


@dataclass(frozen=True)
class NoResult:
    """One round that established nothing about one item still in the pool."""

    round: int
    item_id: str
    cause: str
    pair_key: Optional[str] = None


@dataclass(frozen=True)
class Standing:
    rank: int
    item_id: str
    points: int
    played: int
    wins: int
    draws: int
    losses: int
    byes: int


@dataclass
class Pool:
    rubric_id: str
    rubric_version: int
    seed: int = 0
    items: dict[str, Item] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)
    discarded: dict[str, Discard] = field(default_factory=dict)
    no_results: list[NoResult] = field(default_factory=list)

    def content(self, item_id: str) -> str:
        return self.items[item_id].content


ASKED_IS_NOT_PLAYED_SO_A_SKIPPED_PAIR_IS_REMEMBERED_WITHOUT_BEING_SCORED = (
    "a skip establishes nothing, so it appends no Result and moves no points -- but "
    "the pair WAS put in front of a judge, and re-drawing it wastes the one resource "
    "the tournament is built to spend. Both sides also sort first in pairing_order "
    "(played == 0), so an unremembered skip is re-drawn near-deterministically rather "
    "than by chance. played_keys therefore answers 'have these two been asked?', not "
    "'have these two been scored?'. A row replayed inert through default_outcome is a "
    "different case and carries no key: its verdict belongs to some other vocabulary, "
    "so this pool has never put the question to anyone and must still be able to."
)


def no_result(pool: Pool, *, round: int, item_id: str, cause: str,
              pair_key: Optional[str] = None) -> NoResult:
    """Record that a round established NOTHING about one item.

    The ONE path for a bye, the survivor of a discard, and a skip. Appends no
    :class:`Result`, so the item's played count does not move and
    :func:`pairing_order` seats it before every item that has played.

    ``pair_key`` is set when the item was actually shown to a judge alongside
    another item -- a skip -- so the no-rematch rule can remember the question
    without recording an answer.
    """
    if cause not in NO_RESULT_CAUSES:
        raise ValueError(
            f"unknown no-result cause {cause!r}; known: {sorted(NO_RESULT_CAUSES)}. "
            f"{A_SKIP_MUST_NOT_AWARD_A_RANK_SO_IT_TAKES_THE_SAME_PATH_A_BYE_TAKES}"
        )
    entry = NoResult(round=round, item_id=item_id, cause=cause, pair_key=pair_key)
    pool.no_results.append(entry)
    return entry


def byes(pool: Pool) -> list[tuple[int, str]]:
    """``(round, item_id)`` for every bye, derived from the no-result ledger.

    A bye is the only no-result the standings table counts, because it is the
    only one the draw hands out and therefore the only one that has to be
    shared fairly.
    """
    return [
        (entry.round, entry.item_id)
        for entry in pool.no_results
        if entry.cause == NO_RESULT_CAUSE_BYE
    ]


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def item_from_payload(payload: dict) -> Item:
    """Build an Item from a judge display payload.

    The id and the hashed content both derive from the payload itself, so the
    same work order enqueued twice is the same entrant without anybody having
    to allocate ids.
    """
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return Item(id=content_digest(content)[:16], content=content, payload=payload)


def pair_key(content_a: str, content_b: str, rubric_id: str,
             rubric_version: int) -> str:
    """sha256(item contents, order-normalised, + rubric id + rubric version).

    Order-normalised because "have these two met?" is symmetric: the same two
    items must key identically whichever side the judge saw them on.
    """
    first, second = sorted([content_a, content_b])
    digest = hashlib.sha256()
    for part in (first, second, rubric_id, str(rubric_version)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def new_pool(items: Iterable[Item], *, rubric_id: str, rubric_version: int,
             seed: int = 0) -> Pool:
    pool = Pool(rubric_id=rubric_id, rubric_version=rubric_version, seed=seed)
    for item in items:
        add_item(pool, item)
    return pool


def add_item(pool: Pool, item: Item) -> Item:
    """Enter an item into the pool at zero points.

    An item generated after round two enters here and plays from there;
    nothing already judged is re-asked, because the pair keys of decided
    matches are untouched.
    """
    pool.items[item.id] = item
    return item


def active_ids(pool: Pool) -> list[str]:
    return [i for i in pool.items if i not in pool.discarded]


def rounds_total(pool: Pool) -> int:
    """``ceil(log2 N)`` over every item that ever entered the pool.

    Counting entrants rather than survivors keeps the schedule stable when a
    discard shrinks the pool mid-campaign, and lengthens it when late items
    arrive.
    """
    n = len(pool.items)
    return 0 if n < 2 else math.ceil(math.log2(n))


def max_rounds(pool: Pool) -> int:
    """The most rounds a pool can still seat: ``n - 1`` over the SURVIVORS.

    Every two active items may meet exactly once, so after ``n - 1`` rounds
    the no-rematch rule has no legal pairing left and any further draw is an
    all-bye round. Counts survivors, not entrants, because a discarded item
    is never paired again -- the mirror of :func:`rounds_total`, which counts
    entrants so the schedule does not shrink under a discard.
    """
    n = len(active_ids(pool))
    return 0 if n < 2 else n - 1


def rounds_cap(pool: Pool, override: Optional[int] = None) -> int:
    """How many rounds this campaign plays: :func:`rounds_total` by default.

    The default yields a full ordering. A campaign may set a shorter cap:
    stopping early costs RESOLUTION, not validity, because every item has a
    score after every round -- so a caller that stops at round 3 of 6 gets a
    coarser ordering, never an error. Rounds are a dial, not a structural
    constant.

    A cap below one is refused: a tournament that plays no round has ordered
    nothing, and answering that with a coarse ordering would be a lie.
    """
    if override is None:
        return rounds_total(pool)
    if isinstance(override, bool) or not isinstance(override, int):
        raise ValueError(f"rounds cap must be an int, got {override!r}")
    if override < 1:
        raise ValueError(
            f"rounds cap must be at least 1, got {override}: a campaign that "
            "plays no round establishes no position"
        )
    return override


def rounds_remaining(pool: Pool, rounds_played: int, *,
                     override: Optional[int] = None) -> int:
    return max(0, rounds_cap(pool, override) - rounds_played)


def is_settled(pool: Pool, rounds_played: int, *,
               override: Optional[int] = None) -> bool:
    """True once the cap is reached: the ordering is as resolved as this
    campaign asked for, and drawing again could only repeat pairs."""
    return rounds_remaining(pool, rounds_played, override=override) == 0


def unplayed_pairs(pool: Pool) -> list[tuple[str, str]]:
    """Every pair of active items that has not met under the current rubric
    version. Empty means the comparison space is exhausted -- a different
    terminal state from the cap being reached."""
    ids = active_ids(pool)
    played = played_keys(pool)
    return [
        (a, b)
        for index, a in enumerate(ids)
        for b in ids[index + 1:]
        if _key_for(pool, a, b) not in played
    ]


def has_pairable_pair(pool: Pool) -> bool:
    ids = active_ids(pool)
    played = played_keys(pool)
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            if _key_for(pool, a, b) not in played:
                return True
    return False


def is_stale(pool: Pool, result: Result) -> bool:
    return (result.rubric_id, result.rubric_version) != (
        pool.rubric_id, pool.rubric_version)


def live_results(pool: Pool) -> list[Result]:
    return [r for r in pool.results if not is_stale(pool, r)]


def played_keys(pool: Pool) -> set[str]:
    """Every pair already put to a judge -- scored or skipped.

    See ASKED_IS_NOT_PLAYED_SO_A_SKIPPED_PAIR_IS_REMEMBERED_WITHOUT_BEING_SCORED.
    """
    asked = {r.pair_key for r in live_results(pool)}
    asked.update(n.pair_key for n in pool.no_results if n.pair_key is not None)
    return asked


def repeated_pairs(pool: Pool) -> list[str]:
    """Pair keys judged more than once under the current rubric version."""
    seen: set[str] = set()
    repeats: list[str] = []
    for result in live_results(pool):
        if result.pair_key in seen:
            repeats.append(result.pair_key)
        seen.add(result.pair_key)
    return repeats


def _stats(pool: Pool) -> dict[str, dict[str, int]]:
    stats = {
        item_id: {"points": 0, "played": 0, "wins": 0, "draws": 0,
                  "losses": 0, "byes": 0}
        for item_id in active_ids(pool)
    }
    for result in live_results(pool):
        for side, item_id in (("a", result.item_a), ("b", result.item_b)):
            row = stats.get(item_id)
            if row is None:
                continue
            row["played"] += 1
            if result.outcome == OUTCOME_DRAW:
                row["draws"] += 1
                row["points"] += DRAW_POINTS
            elif result.outcome == side:
                row["wins"] += 1
                row["points"] += WIN_POINTS
            elif result.outcome in (OUTCOME_A, OUTCOME_B):
                row["losses"] += 1
                row["points"] += LOSS_POINTS
    for _round, item_id in byes(pool):
        row = stats.get(item_id)
        if row is None:
            continue
        row["byes"] += 1
    return stats


def standings(pool: Pool) -> list[Standing]:
    """The points table, best first. Discarded items are absent, not zero.

    An item that has not been compared carries rank 0, not a position: an
    unplayed item sorts level with every other unplayed item, and numbering
    them would publish an order that no judgement established. This is the
    same rule `WorkOrder.TournamentStanding` enforces on the way in.
    """
    stats = _stats(pool)
    ordered = sorted(
        stats.items(),
        key=lambda kv: (-kv[1]["points"], -kv[1]["wins"], kv[0]),
    )
    rank = 0
    out = []
    for item_id, row in ordered:
        if row["played"]:
            rank += 1
        out.append(Standing(rank=rank if row["played"] else 0,
                            item_id=item_id, **row))
    return out


def standing_for(pool: Pool, item_id: str) -> Optional[Standing]:
    for standing in standings(pool):
        if standing.item_id == item_id:
            return standing
    return None


def pairing_order(pool: Pool) -> list[str]:
    """Matches played ascending, then points descending: the pairing order
    for every round after the first."""
    stats = _stats(pool)
    return sorted(
        stats,
        key=lambda item_id: (stats[item_id]["played"],
                             -stats[item_id]["points"],
                             item_id),
    )


def _round_order(pool: Pool, number: int) -> list[str]:
    if number <= 1:
        shuffled = active_ids(pool)
        random.Random(pool.seed + number).shuffle(shuffled)
        return shuffled
    return pairing_order(pool)


def _key_for(pool: Pool, a: str, b: str) -> str:
    return pair_key(pool.content(a), pool.content(b),
                    pool.rubric_id, pool.rubric_version)


def _choose_bye(pool: Pool, order: Sequence[str]) -> str:
    had_bye = {item_id for _round, item_id in byes(pool)}
    for item_id in reversed(order):
        if item_id not in had_bye:
            return item_id
    return order[-1]


def _search(order: tuple[str, ...], played: set[str], pool: Pool,
            budget: list[int]) -> tuple[list[tuple[str, str, str]], list[str]]:
    if len(order) < 2:
        return [], list(order)
    head = order[0]
    best: Optional[tuple[list[tuple[str, str, str]], list[str]]] = None
    for j in range(1, len(order)):
        key = _key_for(pool, head, order[j])
        if key in played:
            continue
        if budget[0] <= 0 and best is not None:
            break
        budget[0] -= 1
        rest = order[1:j] + order[j + 1:]
        pairs, stranded = _search(rest, played, pool, budget)
        candidate = ([(head, order[j], key)] + pairs, stranded)
        if not stranded:
            return candidate
        if best is None or len(stranded) < len(best[1]):
            best = candidate
    if best is not None:
        return best
    pairs, stranded = _search(order[1:], played, pool, budget)
    return pairs, [head] + stranded


def pair_round(pool: Pool, number: int) -> Round:
    """Pair one round. Round one is a seeded shuffle; later rounds pair
    neighbours in :func:`pairing_order`, skipping rematches.

    An odd pool yields exactly one bye. An item the no-rematch rule leaves
    without any legal opponent also sits the round out as a bye -- never as a
    repeat of a comparison somebody already made.
    """
    order = _round_order(pool, number)
    sitting_out: list[str] = []
    if len(order) % 2 == 1:
        bye_id = _choose_bye(pool, order)
        order = [item_id for item_id in order if item_id != bye_id]
        sitting_out.append(bye_id)
    pairs, stranded = _search(tuple(order), played_keys(pool), pool,
                              [_PAIRING_STEP_BUDGET])
    sitting_out.extend(stranded)
    matches = [
        Match(round=number, slot=slot, item_a=a, item_b=b, pair_key=key)
        for slot, (a, b, key) in enumerate(pairs)
    ]
    for item_id in sitting_out:
        no_result(pool, round=number, item_id=item_id,
                  cause=NO_RESULT_CAUSE_BYE)
    return Round(number=number, matches=matches, byes=sitting_out)


def record(pool: Pool, *, round: int, item_a: str, item_b: str, verdict: str,
           rubric_id: Optional[str] = None, rubric_version: Optional[int] = None,
           default_outcome=_UNSET, db_path: Optional[str] = None,
           domain_id: Optional[int] = None) -> Optional[Result]:
    """Record one judged comparison. Returns None when it produced no result.

    A discard verdict is not a result: the NAMED side leaves the pool and the
    other side is untouched. A skip is not a result either: neither side is
    ejected and neither is scored. Both leave through :func:`no_result`, so
    the items still in the pool keep the played count they had and
    pairing_order seats them like byed items. Anything else appends a Result
    keyed to the pair under the rubric version it was judged at, which is what
    makes a later rubric bump able to stale it.
    """
    outcome = outcome_for_verdict(verdict, default_outcome)
    if outcome in NO_RESULT_OUTCOMES:
        ejected = {OUTCOME_DISCARD_A: item_a,
                   OUTCOME_DISCARD_B: item_b}.get(outcome)
        if ejected is not None:
            discard(pool, ejected, verdict, round=round,
                    opponent=item_b if ejected == item_a else item_a,
                    db_path=db_path, domain_id=domain_id)
        judged_here = verdict in VERDICT_OUTCOMES
        asked = None if (ejected is not None or not judged_here) else pair_key(
            pool.content(item_a), pool.content(item_b),
            rubric_id if rubric_id is not None else pool.rubric_id,
            rubric_version if rubric_version is not None else pool.rubric_version,
        )
        for item_id in (item_a, item_b):
            if item_id != ejected:
                no_result(pool, round=round, item_id=item_id,
                          cause=NO_RESULT_CAUSE_FOR_OUTCOME[outcome],
                          pair_key=asked)
        return None
    result = Result(
        round=round,
        item_a=item_a,
        item_b=item_b,
        verdict=verdict,
        outcome=outcome,
        pair_key=pair_key(
            pool.content(item_a), pool.content(item_b),
            rubric_id if rubric_id is not None else pool.rubric_id,
            rubric_version if rubric_version is not None else pool.rubric_version,
        ),
        rubric_id=rubric_id if rubric_id is not None else pool.rubric_id,
        rubric_version=(rubric_version if rubric_version is not None
                        else pool.rubric_version),
    )
    pool.results.append(result)
    return result


def record_match(pool: Pool, match: Match, verdict: str, **kwargs) -> Optional[Result]:
    return record(pool, round=match.round, item_a=match.item_a,
                  item_b=match.item_b, verdict=verdict, **kwargs)


def discard(pool: Pool, item_id: str, verdict: str, *, round: int = 0,
            opponent: Optional[str] = None, db_path: Optional[str] = None,
            domain_id: Optional[int] = None) -> Discard:
    """Remove ONE item from the pool for good.

    The verdict names which side of the pairing is ejected, and only that
    item leaves: the opponent stays in the pool with nothing recorded about
    it. The ejected item is never paired again and never appears in
    standings. When a ``db_path`` is given, every still-pending judge queue
    row that shows this item is cancelled in the same breath -- otherwise a
    judge who threw an item out keeps being shown it.
    """
    if verdict not in DISCARD_VERDICTS:
        raise ValueError(
            f"verdict {verdict!r} does not discard; "
            f"discarding verdicts: {sorted(DISCARD_VERDICTS)}. "
            f"{A_DISCARD_EJECTS_THE_NAMED_SIDE_ONLY_NEVER_THE_ITEM_BESIDE_IT}"
        )
    entry = Discard(item_id=item_id, verdict=verdict, round=round,
                    side=EJECTED_SIDE_BY_VERDICT[verdict], opponent=opponent)
    pool.discarded.setdefault(item_id, entry)
    if db_path is not None:
        cancel_pending(db_path, [item_id], reason=f"discarded: {verdict}",
                       domain_id=domain_id)
    return pool.discarded[item_id]


def discards(pool: Pool) -> list[Discard]:
    """The discard set: every item that left the pool, with the verdict that
    caused it. Reported separately from standings by design."""
    return sorted(pool.discarded.values(), key=lambda d: (d.round, d.item_id))


def history(pool: Pool) -> list[HistoryEntry]:
    """Every recorded comparison, stale ones flagged rather than dropped."""
    return [
        HistoryEntry(
            round=r.round,
            item_a=r.item_a,
            item_b=r.item_b,
            verdict=r.verdict,
            outcome=r.outcome,
            pair_key=r.pair_key,
            rubric_id=r.rubric_id,
            rubric_version=r.rubric_version,
            stale=is_stale(pool, r),
        )
        for r in pool.results
    ]


def revise_rubric(pool: Pool, *, rubric_version: int,
                  rubric_id: Optional[str] = None) -> Pool:
    """Point the pool at a new rubric version.

    Every match judged under the old one degrades to stale in place: visible
    in history, worth no points, and re-pairable because the new version keys
    the pair differently.
    """
    pool.rubric_version = rubric_version
    if rubric_id is not None:
        pool.rubric_id = rubric_id
    return pool


def _payload_item_ids(payload: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("item_a", "item_b", "item"):
        value = payload.get(key)
        if isinstance(value, str):
            ids.add(value)
    for key in ("card_a", "card_b", "card"):
        value = payload.get(key)
        if isinstance(value, dict):
            ids.add(item_from_payload(value).id)
    return ids


def cancel_pending(db_path: str, item_ids: Iterable[str], *, reason: str = "",
                   domain_id: Optional[int] = None) -> int:
    """Cancel every outstanding judge queue row that shows one of these items.

    Returns the number of rows flipped to ``cancelled``. Rows already resolved
    are left exactly as they are: a discard withdraws future work, it never
    rewrites a judgement somebody already made.
    """
    wanted = set(item_ids)
    if not wanted:
        return 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cancelled = 0
    try:
        sql = "SELECT id, trace_payload FROM pending_judgement WHERE status='pending'"
        params: tuple = ()
        if domain_id is not None:
            sql += " AND domain_id=?"
            params = (domain_id,)
        for row in conn.execute(sql, params).fetchall():
            try:
                payload = json.loads(row["trace_payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if not (_payload_item_ids(payload) & wanted):
                continue
            conn.execute(
                "UPDATE pending_judgement SET status='cancelled', "
                "error_message=?, completed_at=datetime('now') "
                "WHERE id=? AND status='pending'",
                (reason or "discarded from pool", row["id"]),
            )
            cancelled += 1
        conn.commit()
    finally:
        conn.close()
    return cancelled


def format_standings(pool: Pool, *, label_for=None) -> str:
    """Render the points table as text. The queue reads this; the judging
    surface never does -- showing a score anchors the next comparison."""
    label = label_for or (lambda item_id: item_id)
    header = f"{'#':>3}  {'points':>6} {'played':>6} {'W':>3} {'D':>3} {'L':>3} {'bye':>3}  item"
    lines = [header, "-" * len(header)]
    for row in standings(pool):
        lines.append(
            f"{row.rank:>3}  {row.points:>6} {row.played:>6} {row.wins:>3} "
            f"{row.draws:>3} {row.losses:>3} {row.byes:>3}  {label(row.item_id)}"
        )
    dropped = discards(pool)
    if dropped:
        lines.append("")
        lines.append(f"discarded ({len(dropped)}), not scored:")
        for entry in dropped:
            beside = (f"  (drawn against {label(entry.opponent)}, which stayed "
                      "in the pool)" if entry.opponent else "")
            lines.append(
                f"     {entry.verdict:>22}  {label(entry.item_id)}{beside}"
            )
    return "\n".join(lines)
