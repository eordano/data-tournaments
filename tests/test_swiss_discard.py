"""Discard is a per-side pool exit, not a loss (docs/design/priority-tournament.md).

`discard-a` ejects A and `discard-b` ejects B. The item named leaves the pool
immediately, is never paired again, is absent from standings rather than
sitting on zero, and takes its outstanding queue rows with it. The item beside
it is untouched: no result, no points, no played count, and a seat at the front
of the next round's draw — exactly how a bye is treated, and for the same
reason.
"""
import importlib
import json
import random
import sqlite3

import pytest

from bin import swiss


@pytest.fixture
def fabric(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home / "judgements.db"


def _wheel_domain(name="discard-domain", count=9):
    from bin import domains, prompts
    prompts.push(f"card-generator:{name}", "Generate work orders.",
                 labels=["production"])
    prompts.push(f"judge-instructions:{name}", "Judge work orders.",
                 labels=["production"])
    domain_id = domains.create_domain(
        name=name,
        description="work orders for the priority tournament",
        corpus_source={"kind": "inline", "items": [{"text": "x"}]},
        rubric="pair-wheel-v2",
    )
    payloads = [
        {"kind": "work-order", "title": f"order {i}", "body": f"body {i}",
         "source_ref": f"ref-{i}"}
        for i in range(count)
    ]
    return domain_id, payloads


def _pool(*ids, seed=5):
    return swiss.new_pool(
        [swiss.Item(id=i, content=f"content of {i}") for i in ids],
        rubric_id="pair-wheel-v2", rubric_version=1, seed=seed,
    )


def test_the_engine_vocabulary_covers_every_rubric_rather_than_one():
    """The direction of this containment is what the P0 turned on.

    The engine used to be a SUBSET of one rubric, on the rule that it reads
    that rubric and never adds to it. That made any other rubric's verdicts
    unscored the moment one became the default. The engine must be a SUPERSET
    of every rubric a judge can be handed.
    """
    from bin import judgement

    assert swiss.DISCARD_VERDICTS == {"discard-a", "discard-b"}
    pair_rubrics = [
        (name, definition)
        for name, _v, definition, _p, _i in judgement.WHEEL_SEED_TEMPLATES
        if definition.get("judgement_kind") == "pair"
    ]
    assert pair_rubrics
    for name, definition in pair_rubrics:
        assert set(definition["verdict_enum"]) <= swiss.known_verdicts(), (
            f"{name} can emit a verdict the engine does not score"
        )
        assert swiss.DISCARD_VERDICTS <= set(definition["verdict_enum"]), (
            f"{name} cannot say 'this item does not belong in the pool'"
        )


def test_discard_a_ejects_a_and_leaves_b_in_the_pool():
    pool = _pool("bad", "good", "c", "d")
    assert swiss.record(pool, round=1, item_a="bad", item_b="good",
                        verdict="discard-a") is None
    assert swiss.active_ids(pool) == ["good", "c", "d"]
    assert [d.item_id for d in swiss.discards(pool)] == ["bad"]
    assert {s.item_id for s in swiss.standings(pool)} == {"good", "c", "d"}


def test_discard_b_ejects_b_and_leaves_a_in_the_pool():
    pool = _pool("good", "bad", "c", "d")
    assert swiss.record(pool, round=1, item_a="good", item_b="bad",
                        verdict="discard-b") is None
    assert swiss.active_ids(pool) == ["good", "c", "d"]
    assert [d.item_id for d in swiss.discards(pool)] == ["bad"]


def test_a_malformed_a_never_takes_a_good_b_down_with_it():
    """The defect this whole vocabulary exists to fix.

    A discard used to remove BOTH sides, so a judge who saw one malformed
    card destroyed the perfectly good card beside it as collateral. The good
    card must survive and must still be pairable.
    """
    pool = _pool("malformed", "good", "c", "d")
    swiss.record(pool, round=1, item_a="malformed", item_b="good",
                 verdict="discard-a")

    assert "good" in swiss.active_ids(pool)
    assert swiss.standing_for(pool, "good") is not None
    still_askable = {frozenset(pair) for pair in swiss.unplayed_pairs(pool)}
    assert frozenset({"good", "c"}) in still_askable
    assert frozenset({"good", "d"}) in still_askable
    assert all("malformed" not in pair for pair in still_askable)


def test_the_survivor_of_a_discarded_pairing_gets_no_result():
    pool = _pool("bad", "survivor", "c", "d")
    swiss.record(pool, round=1, item_a="bad", item_b="survivor",
                 verdict="discard-a")

    assert pool.results == [], "a discard is not a match result"
    survivor = swiss.standing_for(pool, "survivor")
    assert survivor.played == 0, (
        swiss.A_DISCARDED_PAIRING_PRODUCES_NO_RESULT_SO_THE_SURVIVOR_IS_SEATED_LIKE_A_BYE
    )
    assert (survivor.points, survivor.wins, survivor.draws,
            survivor.losses, survivor.byes) == (0, 0, 0, 0, 0)


def test_the_survivor_is_seated_before_items_that_have_played_like_a_bye():
    """Nothing was established about the survivor, so it is drawn first —
    the same seat pairing_order gives a byed item."""
    pool = _pool("w", "l", "bad", "survivor")
    swiss.record(pool, round=1, item_a="w", item_b="l", verdict="a-wins-big")
    swiss.record(pool, round=1, item_a="bad", item_b="survivor",
                 verdict="discard-a")

    order = swiss.pairing_order(pool)
    assert order[0] == "survivor", (
        f"expected the survivor drawn first, got {order}"
    )
    played = {s.item_id: s.played for s in swiss.standings(pool)}
    assert played == {"survivor": 0, "w": 1, "l": 1}

    byed = _pool("x", "y", "z")
    first = swiss.pair_round(byed, 1)
    (bye_id,) = first.byes
    for match in first.matches:
        swiss.record_match(byed, match, "a-wins-big")
    assert swiss.pairing_order(byed)[0] == bye_id
    assert swiss.standing_for(byed, bye_id).played == 0


def test_the_discard_record_names_the_side_and_the_opponent():
    pool = _pool("a", "b", "c", "d")
    swiss.record(pool, round=1, item_a="a", item_b="b", verdict="discard-b")
    swiss.record(pool, round=2, item_a="c", item_b="d", verdict="discard-a")

    by_item = {d.item_id: d for d in swiss.discards(pool)}
    assert set(by_item) == {"b", "c"}
    assert (by_item["b"].side, by_item["b"].opponent) == ("b", "a")
    assert (by_item["c"].side, by_item["c"].opponent) == ("a", "d")
    assert by_item["b"].verdict == "discard-b" and by_item["b"].round == 1
    assert by_item["c"].verdict == "discard-a" and by_item["c"].round == 2

    rendered = swiss.format_standings(pool)
    assert "discarded (2), not scored:" in rendered
    assert "drawn against a, which stayed in the pool" in rendered


def test_discard_removes_the_item_from_every_later_round():
    pool = _pool(*[f"i{n}" for n in range(9)])
    first = swiss.pair_round(pool, 1)
    for match in first.matches:
        swiss.record_match(pool, match, "a-wins-big")

    gone = first.matches[0].item_b
    swiss.discard(pool, gone, "discard-b", round=1)

    assert gone not in swiss.active_ids(pool)
    for number in range(2, swiss.rounds_total(pool) + 1):
        drawn = swiss.pair_round(pool, number)
        appearing = {m.item_a for m in drawn.matches} | {m.item_b for m in drawn.matches}
        assert gone not in appearing | set(drawn.byes), (
            f"round {number} paired an item the judge threw out"
        )
        for match in drawn.matches:
            swiss.record_match(pool, match, "a-wins-big")


def test_a_discarded_item_is_absent_from_standings_not_zero():
    pool = _pool("winner", "loser", "thrown-out")
    swiss.record(pool, round=1, item_a="winner", item_b="loser",
                 verdict="a-wins-big")
    swiss.record(pool, round=2, item_a="thrown-out", item_b="loser",
                 verdict="a-wins-big")
    swiss.discard(pool, "thrown-out", "discard-a", round=2)

    table = {s.item_id: s for s in swiss.standings(pool)}
    assert "thrown-out" not in table, "a discard is not a score of zero"
    assert table["loser"].points == 0 and table["loser"].played == 2, (
        "zero is a real position, occupied by items that lost honestly"
    )
    assert swiss.standing_for(pool, "thrown-out") is None


def test_discard_refuses_a_verdict_that_does_not_discard():
    pool = _pool("a", "b")
    for verdict in ("a-wins-big", "tie", "neither-good", "incoherent"):
        with pytest.raises(ValueError, match="does not discard"):
            swiss.discard(pool, "a", verdict)
    assert swiss.discards(pool) == []


def test_the_retired_both_sides_verdicts_are_gone_from_the_engine():
    """Loud abandonment: the vocabulary that ejected both cards at once no
    longer resolves to anything, so a stored row carrying it is refused
    rather than quietly re-interpreted."""
    pool = _pool("a", "b")
    for retired in ("neither-good", "incoherent",
                    "a-lean-both-invalid", "b-lean-both-invalid"):
        assert retired not in swiss.VERDICT_OUTCOMES
        with pytest.raises(ValueError, match="not scored by any registered rubric"):
            swiss.record(pool, round=1, item_a="a", item_b="b", verdict=retired)
    assert swiss.discards(pool) == [] and pool.results == []


def test_pending_rows_showing_a_discarded_item_are_cancelled(fabric):
    import judgement
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, random.Random(3))

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, trace_payload FROM pending_judgement ORDER BY match_id"
    ).fetchall()
    db.close()
    assert len(rows) == 4

    resolved, target = rows[0], rows[1]
    judgement.write_judgement(
        pending_id=resolved["id"], verdict="a-wins-big",
        confidence="mid", rater={"type": "human", "userId": "tester"},
    )
    gone = json.loads(target["trace_payload"])["item_a"]

    pool = swiss.new_pool([swiss.Item(id=gone, content="already judged bad")],
                          rubric_id="pair-wheel-v2", rubric_version=1)
    swiss.discard(pool, gone, "discard-a", round=1, db_path=str(fabric),
                  domain_id=domain_id)

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    statuses = {
        r["id"]: (r["status"], r["error_message"])
        for r in db.execute(
            "SELECT id, status, error_message FROM pending_judgement"
        ).fetchall()
    }
    db.close()
    assert statuses[target["id"]][0] == "cancelled"
    assert "discard-a" in statuses[target["id"]][1]
    assert statuses[resolved["id"]][0] == "done", (
        "a discard withdraws future work; it never rewrites a made judgement"
    )
    assert [statuses[r["id"]][0] for r in rows[2:]] == ["pending", "pending"]


def test_cancelling_matches_a_payload_by_its_cards_when_ids_are_absent(fabric):
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(name="legacy-domain", count=4)
    generate_cards._enqueue_pairs(domain_id, payloads, random.Random(1))

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT id, trace_payload FROM pending_judgement ORDER BY match_id"
    ).fetchone()
    payload = json.loads(row["trace_payload"])
    legacy = {"label": payload["label"], "card_a": payload["card_a"],
              "card_b": payload["card_b"]}
    db.execute("UPDATE pending_judgement SET trace_payload=? WHERE id=?",
               (json.dumps(legacy), row["id"]))
    db.commit()
    db.close()

    gone = swiss.item_from_payload(payload["card_a"]).id
    assert gone == payload["item_a"]
    assert swiss.cancel_pending(str(fabric), [gone], reason="discarded: discard-a",
                                domain_id=domain_id) == 1


def test_advancing_a_round_drops_only_the_discarded_side(fabric):
    """End to end through the queue: nine items, one discard, EIGHT survivors
    — four matches and no bye, because the item beside the discarded one is
    still in the pool."""
    import judgement
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(name="advance-discard", count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, random.Random(3))

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, trace_payload FROM pending_judgement ORDER BY match_id"
    ).fetchall()
    db.close()
    thrown_out = json.loads(rows[0]["trace_payload"])
    for row in rows:
        judgement.write_judgement(
            pending_id=row["id"],
            verdict="discard-a" if row["id"] == rows[0]["id"] else "a-wins-big",
            confidence="mid", rater={"type": "human", "userId": "tester"},
        )

    drawn = generate_cards.advance_round(domain_id)
    assert set(drawn["discarded"]) == {thrown_out["item_a"]}
    assert thrown_out["item_b"] not in drawn["discarded"], (
        "the survivor of a discarded pairing stays in the pool"
    )
    assert drawn["pairs_enqueued"] == 4 and drawn["byes"] == []

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    second = [
        json.loads(r["trace_payload"])
        for r in db.execute("SELECT trace_payload FROM pending_judgement").fetchall()
    ]
    db.close()
    second = [p for p in second if p["round"] == 2]
    shown = {p["item_a"] for p in second} | {p["item_b"] for p in second}
    shown |= {b["item_id"] for p in second for b in p["byes"]}
    assert shown.isdisjoint(set(drawn["discarded"]))
    assert thrown_out["item_b"] in shown, (
        "and it is drawn again next round, judged on its own merits"
    )
