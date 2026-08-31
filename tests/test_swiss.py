"""Tests for bin/swiss.py — the Swiss pairing engine.

The design of record is docs/design/priority-tournament.md: football points,
a random first round, later rounds paired on similar standing, never a
repeated pair, rounds to ceil(log2 N), standings valid after every round.
"""
import math
import random

import pytest

from bin import swiss


def _items(*ids):
    return [swiss.Item(id=i, content=f"content of {i}") for i in ids]


def _pool(*ids, seed=7, rubric_version=1):
    return swiss.new_pool(_items(*ids), rubric_id="pair-wheel-v2",
                          rubric_version=rubric_version, seed=seed)


def _numbered_pool(n, seed=7):
    ids = [f"i{n:02d}" for n in range(n)]
    return _pool(*ids, seed=seed), ids


def test_win_is_three_draw_is_one_loss_is_zero():
    pool = _pool("a", "b", "c", "d")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big")
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="tie")
    points = {s.item_id: s.points for s in swiss.standings(pool)}
    assert points == {"a": swiss.WIN_POINTS, "b": swiss.LOSS_POINTS,
                      "c": swiss.DRAW_POINTS, "d": swiss.DRAW_POINTS}
    assert (swiss.WIN_POINTS, swiss.DRAW_POINTS, swiss.LOSS_POINTS) == (3, 1, 0)


def test_both_win_verdict_strengths_score_the_same_three_points():
    pool = _pool("a", "b", "c", "d")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins")
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="b-wins-big")
    points = {s.item_id: s.points for s in swiss.standings(pool)}
    assert points == {"a": 3, "b": 0, "c": 0, "d": 3}, (
        "margin is preference strength, not a different number of points"
    )


def test_standings_rank_by_points_descending():
    pool = _pool("a", "b", "c")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big")
    swiss.record(pool, round=2, item_a="a", item_b="c",
                 verdict="tie")
    table = swiss.standings(pool)
    assert [s.item_id for s in table] == ["a", "c", "b"]
    assert [s.rank for s in table] == [1, 2, 3]
    assert table[0].points == 4 and table[0].wins == 1 and table[0].draws == 1


def test_round_one_is_a_seeded_shuffle_with_no_standing_input():
    pool, ids = _numbered_pool(8, seed=99)
    expected = list(ids)
    random.Random(99 + 1).shuffle(expected)

    first = swiss.pair_round(pool, 1)

    paired = [(m.item_a, m.item_b) for m in first.matches]
    assert paired == list(zip(expected[0::2], expected[1::2])), (
        "round one pairs neighbours of a seeded shuffle of the pool"
    )
    assert first.byes == []


def test_round_one_is_reproducible_and_seed_dependent():
    def draw(seed):
        pool, _ids = _numbered_pool(8, seed=seed)
        return [(m.item_a, m.item_b) for m in swiss.pair_round(pool, 1).matches]

    assert draw(11) == draw(11)
    assert draw(11) != draw(12)


def test_later_rounds_sort_by_played_then_points_and_pair_neighbours():
    pool = _pool("a", "b", "c", "d", "e", "f")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big")
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="a-wins-big")
    swiss.add_item(pool, swiss.Item(id="g", content="content of g"))

    order = swiss.pairing_order(pool)
    assert order[:2] == ["e", "f"] or set(order[:3]) == {"e", "f", "g"}, (
        "items with no matches played sort ahead of items that have played"
    )
    played = {s.item_id: s.played for s in swiss.standings(pool)}
    assert all(played[i] == 0 for i in order[:3])
    assert [played[i] for i in order] == sorted(played[i] for i in order)

    points = {s.item_id: s.points for s in swiss.standings(pool)}
    tail = order[3:]
    assert [points[i] for i in tail] == sorted(
        (points[i] for i in tail), reverse=True
    ), "within equal matches-played, points sort descending"

    second = swiss.pair_round(pool, 2)
    survivors = [i for i in order if i not in second.byes]
    assert [(m.item_a, m.item_b) for m in second.matches] == list(
        zip(survivors[0::2], survivors[1::2])
    )


def test_a_rematch_is_skipped_for_the_next_candidate_down():
    pool = _pool("a", "b", "c", "d")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big")
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="a-wins-big")
    swiss.record(pool, round=2, item_a="a", item_b="c",
                 verdict="a-wins-big")
    swiss.record(pool, round=2, item_a="b", item_b="d",
                 verdict="a-wins-big")

    assert swiss.pairing_order(pool) == ["a", "b", "c", "d"]
    third = swiss.pair_round(pool, 3)
    assert [(m.item_a, m.item_b) for m in third.matches] == [("a", "d"), ("b", "c")], (
        "a's neighbours b and c have both been met; the pairing falls to d"
    )


def test_odd_pool_yields_exactly_one_bye_per_round():
    pool, ids = _numbered_pool(9)
    for number in range(1, swiss.rounds_total(pool) + 1):
        played = swiss.pair_round(pool, number)
        assert len(played.byes) == 1
        assert len(played.matches) == 4
        assert len(set(played.byes) | {m.item_a for m in played.matches}
                   | {m.item_b for m in played.matches}) == 9
        for match in played.matches:
            swiss.record_match(pool, match, "a-wins-big")


def _registered_pair_rubrics():
    """Every pair rubric a judge can be handed, read from the registry.

    Enumerated from WHEEL_SEED_TEMPLATES rather than listed here, so a rubric
    added to judgement.py is covered by this rule without anybody remembering
    to add it.
    """
    from bin import judgement

    return {
        name: definition
        for name, _v, definition, _p, _i in judgement.WHEEL_SEED_TEMPLATES
        if definition.get("judgement_kind") == "pair"
    }


def test_every_registered_rubric_verdict_is_scored_by_the_engine():
    """The oracle is the rubric registry, not the engine's own table.

    A verdict a rubric can emit but the engine does not know resolves to
    OUTCOME_SKIP: it scores nothing while still counting as played, so a fully
    judged pool presents as settled with every item on zero points and ranks
    handed out in item-id order. That is how a default rubric once shipped
    with every one of its comparison verdicts silently inert. With one rubric
    this is nearly trivial -- and it is kept because it is the rule that
    caught that defect.
    """
    registered = _registered_pair_rubrics()
    assert registered, "the registry names no pair rubric at all"
    unscored = {
        name: sorted(set(d["verdict_enum"]) - set(swiss.VERDICT_OUTCOMES))
        for name, d in registered.items()
        if set(d["verdict_enum"]) - set(swiss.VERDICT_OUTCOMES)
    }
    assert not unscored, (
        f"{unscored} would resolve to OUTCOME_SKIP. "
        f"{swiss.EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP}"
    )


def test_the_eight_verdicts_are_the_whole_vocabulary():
    """The vocabulary is closed, and it is the same list on both sides.

    Seven wheel verdicts plus an off-wheel skip: nothing survives from the
    card-prioritizer or strongly/slightly wheels, and there is no 'both are
    bad' verdict for a judge to reach for.
    """
    from bin import judgement

    eight = {
        "discard-a", "discard-b", "a-wins-big", "a-wins", "tie",
        "b-wins", "b-wins-big", "skip",
    }
    assert set(swiss.VERDICT_OUTCOMES) == eight
    assert set(
        judgement.PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"]
    ) == eight
    assert swiss.DISCARD_VERDICTS == {"discard-a", "discard-b"}


def test_the_default_rubric_can_actually_separate_a_pool():
    """A regression test for the shipped default specifically: judging a pool
    entirely under DEFAULT_TEMPLATE_NAME must produce distinct points, not a
    flat zero table ranked by item id."""
    from bin import judgement

    definition = _registered_pair_rubrics()[judgement.DEFAULT_TEMPLATE_NAME]
    a_wins = next(
        v for v in definition["verdict_enum"]
        if v.startswith("a-") and v not in swiss.DISCARD_VERDICTS
    )

    pool, ids = _numbered_pool(4)
    played = swiss.pair_round(pool, 1)
    for match in played.matches:
        swiss.record_match(pool, match, a_wins)

    table = swiss.standings(pool)
    assert {s.points for s in table} != {0}, (
        "every comparison under the default rubric scored nothing"
    )
    assert max(s.points for s in table) == swiss.WIN_POINTS


def test_a_bye_is_not_repeated_while_an_item_has_none():
    pool, _ids = _numbered_pool(5)
    seen = []
    for number in range(1, 4):
        played = swiss.pair_round(pool, number)
        seen.extend(played.byes)
        for match in played.matches:
            swiss.record_match(pool, match, "tie")
    assert len(seen) == len(set(seen)), "five items, three rounds, no repeat bye"


def test_a_bye_is_not_a_result_and_is_paired_first_next_round():
    """A bye that scored 0 and counted as played would be arithmetically
    identical to a loss, and pairing_order would seat the unjudged item among
    the items that actually lost."""
    pool, _ids = _numbered_pool(3)
    played = swiss.pair_round(pool, 1)
    (bye_id,) = played.byes
    for match in played.matches:
        swiss.record_match(pool, match, "a-wins-big")

    table = {s.item_id: s for s in swiss.standings(pool)}
    assert table[bye_id].points == 0
    assert table[bye_id].played == 0 and table[bye_id].byes == 1

    loser = next(s.item_id for s in table.values()
                 if s.losses == 1 and s.item_id != bye_id)
    assert table[loser].points == 0 and table[loser].played == 1
    assert swiss.pairing_order(pool)[0] == bye_id


def test_round_count_is_ceil_log2_of_the_pool():
    for n in (2, 3, 8, 9, 32, 33, 64):
        pool, _ids = _numbered_pool(n)
        assert swiss.rounds_total(pool) == math.ceil(math.log2(n))
    assert swiss.rounds_total(_pool("solo")) == 0


def _verdict_for(a, b):
    """A deterministic stand-in judge: higher index matters more, with a
    scattering of genuine draws."""
    if (int(a[1:]) + int(b[1:])) % 7 == 0:
        return "tie"
    return "a-wins-big" if int(a[1:]) > int(b[1:]) else "b-wins-big"


def test_full_thirty_three_item_run_never_repeats_a_pair():
    pool, ids = _numbered_pool(33)
    assert swiss.rounds_total(pool) == 6

    all_keys = []
    for number in range(1, swiss.rounds_total(pool) + 1):
        played = swiss.pair_round(pool, number)
        assert len(played.byes) == 1, f"round {number} must have exactly one bye"
        assert len(played.matches) == 16
        for match in played.matches:
            all_keys.append(match.pair_key)
            swiss.record_match(pool, match,
                               _verdict_for(match.item_a, match.item_b))

        table = swiss.standings(pool)
        assert [s.item_id for s in table] and len(table) == 33, (
            f"standings must be well defined after round {number}"
        )
        wins = sum(1 for r in pool.results if r.outcome in ("a", "b"))
        draws = sum(1 for r in pool.results if r.outcome == "draw")
        assert sum(s.points for s in table) == 3 * wins + 2 * draws
        assert sum(s.played for s in table) == 2 * len(pool.results)

    assert len(all_keys) == len(set(all_keys)) == 96
    assert swiss.repeated_pairs(pool) == []


def test_pair_key_is_orientation_independent_and_rubric_scoped():
    forward = swiss.pair_key("A", "B", "pair-wheel-v2", 1)
    backward = swiss.pair_key("B", "A", "pair-wheel-v2", 1)
    assert forward == backward
    assert forward != swiss.pair_key("A", "B", "pair-wheel-v2", 2)
    assert forward != swiss.pair_key("A", "B", "single-idea-v1", 1)


def test_an_item_entering_late_starts_at_zero_and_reasks_nothing():
    pool = _pool("a", "b", "c", "d")
    first = swiss.pair_round(pool, 1)
    for match in first.matches:
        swiss.record_match(pool, match, "a-wins-big")
    keys_before = {r.pair_key for r in pool.results}

    swiss.add_item(pool, swiss.Item(id="late", content="content of late"))
    assert swiss.standing_for(pool, "late").points == 0
    assert swiss.pairing_order(pool)[0] == "late"

    second = swiss.pair_round(pool, 2)
    assert keys_before.isdisjoint({m.pair_key for m in second.matches})


def test_unknown_verdict_is_refused_unless_a_default_is_given():
    pool = _pool("a", "b")
    assert "a-verdict-no-rubric-emits" not in swiss.VERDICT_OUTCOMES
    with pytest.raises(ValueError, match="not scored by any registered rubric"):
        swiss.record(pool, round=1, item_a="a", item_b="b",
                     verdict="a-verdict-no-rubric-emits")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-verdict-no-rubric-emits",
                 default_outcome=swiss.OUTCOME_SKIP)
    assert [s.points for s in swiss.standings(pool)] == [0, 0]


def test_a_judged_skip_is_never_asked_again():
    """A skip scores nothing, but the pair WAS put in front of a judge.

    Both sides come out of a skip on played == 0, so pairing_order seats them
    first and the same two items are re-drawn near-deterministically. Without
    remembering the question, a pool of skips re-asks the identical pairings
    every round forever.
    """
    pool, _ids = _numbered_pool(4)
    first = swiss.pair_round(pool, 1)
    asked = [frozenset({m.item_a, m.item_b}) for m in first.matches]
    for match in first.matches:
        swiss.record_match(pool, match, "skip")

    assert all(s.played == 0 and s.rank == 0 for s in swiss.standings(pool)), (
        swiss.A_SKIP_MUST_NOT_AWARD_A_RANK_SO_IT_TAKES_THE_SAME_PATH_A_BYE_TAKES
    )
    second = swiss.pair_round(pool, 2)
    redrawn = [frozenset({m.item_a, m.item_b}) for m in second.matches]
    assert not (set(asked) & set(redrawn)), (
        swiss.ASKED_IS_NOT_PLAYED_SO_A_SKIPPED_PAIR_IS_REMEMBERED_WITHOUT_BEING_SCORED
    )


def test_a_foreign_row_replayed_inert_establishes_nothing_at_all():
    """OUTCOME_SKIP is what a loader gets for a row from some other
    vocabulary: no result, no played count, no rank, and the pair is free to
    be asked again under the vocabulary the pool is actually on."""
    pool = _pool("a", "b", "c", "d")
    assert swiss.record(pool, round=1, item_a="a", item_b="b",
                        verdict="tie-both-important",
                        default_outcome=swiss.OUTCOME_SKIP) is None
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="a-wins-big")
    table = {s.item_id: s for s in swiss.standings(pool)}
    assert (table["a"].points, table["a"].played, table["a"].rank) == (0, 0, 0)
    assert (table["b"].points, table["b"].played, table["b"].rank) == (0, 0, 0)
    assert frozenset({"a", "b"}) in {
        frozenset(pair) for pair in swiss.unplayed_pairs(pool)
    }, (
        swiss.A_SKIP_MUST_NOT_AWARD_A_RANK_SO_IT_TAKES_THE_SAME_PATH_A_BYE_TAKES
    )


def test_a_skip_is_judge_facing_and_awards_no_rank_to_either_side():
    """Decision of record: skip is an eighth verdict a rater can choose, and
    it takes the bye's path -- no result, no played count, no rank, and both
    sides seated first in the next draw."""
    pool = _pool("w", "l", "x", "y")
    swiss.record(pool, round=1, item_a="w", item_b="l", verdict="a-wins-big")
    assert swiss.record(pool, round=1, item_a="x", item_b="y",
                        verdict="skip") is None

    assert [r.item_a for r in pool.results] == ["w"], "a skip is not a result"
    table = {s.item_id: s for s in swiss.standings(pool)}
    for skipped in ("x", "y"):
        assert (table[skipped].played, table[skipped].rank,
                table[skipped].points, table[skipped].byes) == (0, 0, 0, 0)
    assert swiss.pairing_order(pool)[:2] == ["x", "y"]
    assert [(e.item_id, e.cause) for e in pool.no_results] == [
        ("x", swiss.NO_RESULT_CAUSE_SKIP), ("y", swiss.NO_RESULT_CAUSE_SKIP),
    ]


def test_the_bye_the_discard_survivor_and_the_skip_share_one_path():
    """One named path, not three copies: every no-result lands in the same
    ledger with the cause that produced it."""
    pool = _pool("a", "b", "c", "d", "e")
    drawn = swiss.pair_round(pool, 1)
    (bye_id,) = drawn.byes
    paired = [m for m in drawn.matches]
    swiss.record_match(pool, paired[0], "discard-a")
    swiss.record_match(pool, paired[1], "skip")

    by_cause: dict[str, set[str]] = {}
    for entry in pool.no_results:
        by_cause.setdefault(entry.cause, set()).add(entry.item_id)
    assert set(by_cause) == set(swiss.NO_RESULT_CAUSES)
    assert by_cause[swiss.NO_RESULT_CAUSE_BYE] == {bye_id}
    assert by_cause[swiss.NO_RESULT_CAUSE_DISCARD_SURVIVOR] == {paired[0].item_b}
    assert by_cause[swiss.NO_RESULT_CAUSE_SKIP] == {paired[1].item_a,
                                                    paired[1].item_b}
    assert pool.results == []
    assert all(s.played == 0 and s.rank == 0 for s in swiss.standings(pool))
    assert swiss.byes(pool) == [(1, bye_id)], (
        "only the bye is counted in the byes column"
    )


def test_no_result_refuses_a_cause_it_does_not_know():
    pool = _pool("a", "b")
    with pytest.raises(ValueError, match="unknown no-result cause"):
        swiss.no_result(pool, round=1, item_id="a", cause="unjudgeable")
    assert pool.no_results == []


def test_format_standings_is_a_table_and_never_a_single_conclusion():
    pool = _pool("a", "b")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="b-wins-big")
    rendered = swiss.format_standings(pool)
    assert "points" in rendered
    assert rendered.splitlines()[2].split()[1] == "3"
    assert len([ln for ln in rendered.splitlines() if ln.strip()]) >= 4


def _points_from_history(pool):
    """Independent points table: re-derive from history, not from _stats."""
    totals = {item_id: 0 for item_id in swiss.active_ids(pool)}
    for entry in swiss.history(pool):
        if entry.stale:
            continue
        if entry.outcome == swiss.OUTCOME_DRAW:
            totals[entry.item_a] += 1
            totals[entry.item_b] += 1
        elif entry.outcome == swiss.OUTCOME_A:
            totals[entry.item_a] += 3
        elif entry.outcome == swiss.OUTCOME_B:
            totals[entry.item_b] += 3
    return totals


def _play(pool, number):
    drawn = swiss.pair_round(pool, number)
    for match in drawn.matches:
        swiss.record_match(pool, match, _verdict_for(match.item_a, match.item_b))
    return drawn


def test_rounds_cap_defaults_to_the_full_ordering():
    for n, expected in ((2, 1), (3, 2), (9, 4), (33, 6)):
        pool, _ids = _numbered_pool(n)
        assert swiss.rounds_cap(pool) == expected == swiss.rounds_total(pool)


def test_a_campaign_may_cap_the_rounds_short_of_a_full_ordering():
    pool, _ids = _numbered_pool(33)
    assert swiss.rounds_cap(pool, 3) == 3
    assert swiss.rounds_remaining(pool, 0, override=3) == 3
    assert swiss.rounds_remaining(pool, 2, override=3) == 1
    assert swiss.rounds_remaining(pool, 3, override=3) == 0
    assert swiss.rounds_remaining(pool, 9, override=3) == 0, (
        "past the cap the remainder floors at zero, it does not go negative"
    )
    assert swiss.is_settled(pool, 3, override=3) is True
    assert swiss.is_settled(pool, 2, override=3) is False
    assert swiss.is_settled(pool, 5) is False, (
        "without an override the default cap of six still governs"
    )
    assert swiss.is_settled(pool, 6) is True


def test_a_cap_of_zero_or_less_or_a_non_int_is_refused():
    pool, _ids = _numbered_pool(9)
    for bad in (0, -1, -6):
        with pytest.raises(ValueError, match="at least 1"):
            swiss.rounds_cap(pool, bad)
    for bad in ("3", 3.0, True):
        with pytest.raises(ValueError, match="must be an int"):
            swiss.rounds_cap(pool, bad)


def test_stopping_early_costs_resolution_and_not_validity():
    """The graceful-stop property: a campaign that stops at round 3 of 6 gets
    a coarser ordering over the SAME thirty-three items, never an error and
    never a hole in the table."""
    coarse, _ids = _numbered_pool(33, seed=11)
    for number in range(1, 4):
        _play(coarse, number)
    assert swiss.is_settled(coarse, 3, override=3)
    assert not swiss.is_settled(coarse, 3)

    table = swiss.standings(coarse)
    assert len(table) == 33, "every item still has a position after round three"
    assert {s.item_id: s.points for s in table} == _points_from_history(coarse), (
        "the coarse table is the same football arithmetic, just less of it"
    )
    assert all(s.played + s.byes == 3 for s in table), (
        "everything plays every round: three rounds, three appearances each"
    )
    assert max(s.points for s in table) == 9 and min(s.points for s in table) == 0

    fine, _ids = _numbered_pool(33, seed=11)
    for number in range(1, 7):
        _play(fine, number)
    coarse_bands = len({s.points for s in swiss.standings(coarse)})
    fine_bands = len({s.points for s in swiss.standings(fine)})
    assert coarse_bands == 8 and fine_bands == 14, (
        "six rounds resolve the pool into more distinct point bands than "
        f"three do; got {coarse_bands} coarse vs {fine_bands} fine"
    )

    def _true_quality(pool, k):
        """_verdict_for makes the higher index the genuinely better item, so
        the index of a standing is its ground-truth quality."""
        return [int(s.item_id[1:]) for s in swiss.standings(pool)[:k]]

    coarse_top = _true_quality(coarse, 8)
    fine_top = _true_quality(fine, 8)
    assert sorted(coarse_top) == [10, 12, 14, 21, 27, 28, 31, 32]
    assert sorted(fine_top) == [22, 23, 25, 28, 29, 30, 31, 32]
    assert sum(coarse_top) / 8 > 16, (
        "the coarse top eight already beats the pool average of 16: three "
        "rounds is a valid ordering, just a noisy one"
    )
    assert min(fine_top) > max(sorted(coarse_top)[:3]), (
        "and the full run's top eight is drawn entirely from above the coarse "
        "run's worst three picks — the extra rounds bought resolution"
    )


def test_max_rounds_is_the_last_round_that_can_seat_a_new_pair():
    for n in (2, 3, 5, 9, 33):
        pool, _ids = _numbered_pool(n)
        assert swiss.max_rounds(pool) == n - 1
    assert swiss.max_rounds(_pool("solo")) == 0

    pool = _pool("a", "b", "c", "d", "e")
    assert swiss.max_rounds(pool) == 4 and swiss.rounds_total(pool) == 3
    swiss.discard(pool, "e", "discard-a", round=1)
    assert swiss.max_rounds(pool) == 3, "a discard shrinks what can still be played"
    assert swiss.rounds_total(pool) == 3, (
        "while the schedule counts entrants and does not shrink"
    )


def test_pairable_pairs_run_out_only_when_every_pair_has_met():
    pool = _pool("a", "b", "c")
    assert swiss.has_pairable_pair(pool)
    assert sorted(swiss.unplayed_pairs(pool)) == [("a", "b"), ("a", "c"), ("b", "c")]

    swiss.record(pool, round=1, item_a="a", item_b="b", verdict="a-wins-big")
    swiss.record(pool, round=2, item_a="a", item_b="c", verdict="tie")
    assert swiss.unplayed_pairs(pool) == [("b", "c")]
    assert swiss.has_pairable_pair(pool)

    swiss.record(pool, round=3, item_a="b", item_b="c", verdict="b-wins-big")
    assert swiss.unplayed_pairs(pool) == []
    assert not swiss.has_pairable_pair(pool), (
        "three items have three comparisons in them and no more"
    )
    assert swiss.max_rounds(pool) == 2 and len(swiss.history(pool)) == 3


def test_a_rubric_revision_reopens_the_comparison_space():
    pool = _pool("a", "b", "c")
    for number, (x, y) in enumerate([("a", "b"), ("a", "c"), ("b", "c")], 1):
        swiss.record(pool, round=number, item_a=x, item_b=y,
                     verdict="a-wins-big")
    assert not swiss.has_pairable_pair(pool)

    swiss.revise_rubric(pool, rubric_version=2)
    assert len(swiss.unplayed_pairs(pool)) == 3, (
        "stale matches neither score nor block a rematch, so the new rubric "
        "version has every pair to ask again"
    )
    assert swiss.has_pairable_pair(pool)


def test_a_discard_can_empty_the_comparison_space():
    pool = _pool("a", "b", "c")
    swiss.discard(pool, "b", "discard-b", round=1)
    swiss.discard(pool, "c", "discard-a", round=1)
    assert swiss.active_ids(pool) == ["a"]
    assert swiss.unplayed_pairs(pool) == [] and not swiss.has_pairable_pair(pool)
    assert swiss.max_rounds(pool) == 0
