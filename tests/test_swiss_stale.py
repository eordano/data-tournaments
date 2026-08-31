"""A rubric revision degrades its matches to stale.

docs/design/priority-tournament.md: "A rubric revision invalidates exactly the
matches judged under the old rubric. Those degrade to stale: still visible as
prior context, no longer contributing points." The pair key carries the rubric
id and version, so the same two items key differently under the new version --
which is what makes the rematch legal and stops the no-rematch check from
reading it as a repeat.
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


def _pool(*ids, version=1):
    return swiss.new_pool(
        [swiss.Item(id=i, content=f"content of {i}") for i in ids],
        rubric_id="pair-wheel-v2", rubric_version=version, seed=4,
    )


def _judged_pool():
    pool = _pool("a", "b", "c", "d")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big")
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="tie")
    return pool


def test_a_rubric_bump_flags_the_old_matches_stale_in_history():
    pool = _judged_pool()
    assert [h.stale for h in swiss.history(pool)] == [False, False]

    swiss.revise_rubric(pool, rubric_version=2)

    entries = swiss.history(pool)
    assert [h.stale for h in entries] == [True, True]
    assert [h.verdict for h in entries] == ["a-wins-big",
                                            "tie"]
    assert [h.rubric_version for h in entries] == [1, 1], (
        "the match keeps the version it was judged under"
    )


def test_stale_matches_contribute_zero_points():
    pool = _judged_pool()
    assert [s.points for s in swiss.standings(pool)] == [3, 1, 1, 0]

    swiss.revise_rubric(pool, rubric_version=2)

    table = swiss.standings(pool)
    assert {s.item_id for s in table} == {"a", "b", "c", "d"}, (
        "a rubric bump invalidates verdicts, never entrants"
    )
    assert [s.points for s in table] == [0, 0, 0, 0]
    assert [s.played for s in table] == [0, 0, 0, 0]
    assert [s.wins for s in table] == [0, 0, 0, 0]


def test_a_stale_pair_is_repairable_and_is_not_a_repeat():
    pool = _judged_pool()
    old_keys = {r.pair_key for r in pool.results}

    swiss.revise_rubric(pool, rubric_version=2)

    drawn = swiss.pair_round(pool, 2)
    met_again = [m for m in drawn.matches if {m.item_a, m.item_b} == {"a", "b"}]
    assert met_again, "under a new rubric the old comparison is an open question"
    assert met_again[0].pair_key not in old_keys, (
        "the pair key carries the rubric version, so the rematch keys anew"
    )

    for match in drawn.matches:
        swiss.record_match(pool, match, "b-wins-big")
    assert swiss.repeated_pairs(pool) == [], (
        "a rematch under a new rubric version is not a repeated pair"
    )
    assert len(swiss.history(pool)) == 4
    assert sum(1 for h in swiss.history(pool) if h.stale) == 2


def test_only_the_superseded_versions_matches_go_stale():
    pool = _pool("a", "b", "c", "d", version=2)
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big", rubric_version=1)
    swiss.record(pool, round=1, item_a="c", item_b="d",
                 verdict="a-wins-big")

    entries = swiss.history(pool)
    assert [h.stale for h in entries] == [True, False]
    points = {s.item_id: s.points for s in swiss.standings(pool)}
    assert points == {"a": 0, "b": 0, "c": 3, "d": 0}


def test_a_different_rubric_entirely_is_stale_too():
    pool = _pool("a", "b")
    swiss.record(pool, round=1, item_a="a", item_b="b",
                 verdict="a-wins-big", rubric_id="single-idea-v1")
    assert swiss.history(pool)[0].stale
    assert [s.points for s in swiss.standings(pool)] == [0, 0]


def _wheel_domain(name="stale-domain", count=4):
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


def _revise_pair_wheel(fabric_path):
    import judgement

    definition = dict(judgement.PAIR_WHEEL_TEMPLATE_DEFINITION)
    definition["description"] = "revised: joint quality is judged first"
    template_id = judgement.register_template(
        name=judgement.PAIR_WHEEL_TEMPLATE_NAME,
        version=2,
        output_definition=definition,
        langfuse_prompt_name=judgement.PAIR_WHEEL_PROMPT_NAME,
    )
    db = sqlite3.connect(str(fabric_path))
    db.execute(
        "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
        "VALUES (?, 'human', '{}')",
        (template_id,),
    )
    db.commit()
    db.close()


def test_a_domains_rubric_revision_stales_its_judged_round(fabric):
    import judgement
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=4)
    generate_cards._enqueue_pairs(domain_id, payloads, random.Random(2))

    db = sqlite3.connect(str(fabric))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, trace_payload FROM pending_judgement ORDER BY match_id"
    ).fetchall()
    db.close()
    for row in rows:
        judgement.write_judgement(
            pending_id=row["id"], verdict="a-wins-big", confidence="mid",
            rater={"type": "human", "userId": "tester"},
        )
    first_keys = {json.loads(r["trace_payload"])["pair_key"] for r in rows}

    conn = sqlite3.connect(str(fabric))
    conn.row_factory = sqlite3.Row
    cfg = generate_cards._human_config_for_rubric(conn, domain_id)
    assert int(cfg["template_version"]) == 1
    before = generate_cards._load_pool(conn, domain_id, cfg)
    conn.close()
    assert [s.points for s in swiss.standings(before)] == [3, 3, 0, 0]

    _revise_pair_wheel(fabric)

    conn = sqlite3.connect(str(fabric))
    conn.row_factory = sqlite3.Row
    cfg = generate_cards._human_config_for_rubric(conn, domain_id)
    assert int(cfg["template_version"]) == 2, "the newest rubric version wins"
    after = generate_cards._load_pool(conn, domain_id, cfg)
    conn.close()

    assert [s.points for s in swiss.standings(after)] == [0, 0, 0, 0]
    assert all(h.stale for h in swiss.history(after))
    assert len(swiss.history(after)) == 2, (
        "stale matches stay visible as prior context"
    )

    drawn = generate_cards.advance_round(domain_id)
    assert drawn["round_drawn"] == 2 and drawn["pairs_enqueued"] == 2

    db = sqlite3.connect(str(fabric))
    second = [
        json.loads(r[0])
        for r in db.execute(
            "SELECT trace_payload FROM pending_judgement"
        ).fetchall()
    ]
    db.close()
    second_keys = {p["pair_key"] for p in second if p["round"] == 2}
    assert len(second_keys) == 2
    assert second_keys.isdisjoint(first_keys), (
        "the new rubric version keys every pair afresh"
    )
    assert all(p["rubric_version"] == 2 for p in second if p["round"] == 2)
