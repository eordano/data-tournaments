"""The points table is computed once, in Python, and materialised for the UI.

Every assertion here fixes a way the two former implementations disagreed:
the Elixir table classified verdicts by prefix while Python used an exact
map, so identical rows produced a real ordering on one surface and all zeros
on the other; Elixir folded pairings by ``{db_path, match_id}`` while the
design makes the pair key load-bearing, so one comparison scored twice as
soon as two match rows carried the same two texts.

Expected numbers here are written out by hand -- 3/1/0 arithmetic done on
paper -- never taken from a second call into the code under test.
"""
import importlib
import json
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
    from bin import domains
    domains.create_domain(
        name="order-review",
        description="items competing for a position in the queue",
        corpus_source={"kind": "inline", "items": []},
        rubric="pair-wheel-v2",
    )
    return tmp_data_home / "judgements.db"


def _conn(fabric):
    conn = sqlite3.connect(str(fabric))
    conn.row_factory = sqlite3.Row
    return conn


def _human_config(conn, template="pair-wheel-v2"):
    return conn.execute(
        "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id=c.template_id "
        "WHERE c.rater_type='human' AND c.status='active' AND t.name=?",
        (template,),
    ).fetchone()["id"]


def _payload(a, b, rnd, slot):
    return {
        "label": f"R{rnd}-{slot}",
        "round": rnd,
        "card_a": {"title": a, "body": f"body {a}", "source_ref": f"{a.lower()}.md"},
        "card_b": {"title": b, "body": f"body {b}", "source_ref": f"{b.lower()}.md"},
    }


def _judge(fabric, plan, *, domain="order-review", template="pair-wheel-v2",
           rater=None, match_offset=0):
    """Seed one judged pairing per plan entry. Returns the pending ids."""
    import judgement
    conn = _conn(fabric)
    domain_id = conn.execute(
        "SELECT id FROM domain WHERE name=?", (domain,)
    ).fetchone()["id"] if domain else None
    config_id = _human_config(conn, template)
    pending = []
    for index, (a, b, rnd, slot, verdict) in enumerate(plan):
        pid = conn.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
            "trace_payload, domain_id) VALUES (?,?,?,?,?)",
            (config_id, f"domain:{domain}", match_offset + index + 1,
             json.dumps(_payload(a, b, rnd, slot)), domain_id),
        ).lastrowid
        pending.append((pid, verdict))
    conn.commit()
    conn.close()
    for pid, verdict in pending:
        judgement.write_judgement(
            pending_id=pid,
            verdict=verdict,
            confidence="high",
            rationale="seeded",
            rater=rater or {"type": "human", "userId": "reviewer"},
        )
    return [pid for pid, _ in pending]


THREE_ROUND_PLAN = [
    ("ALPHA", "BETA", 1, 1, "a-wins-big"),
    ("GAMMA", "DELTA", 1, 2, "tie"),
    ("ECHO", "FOXTROT", 1, 3, "a-wins"),
    ("ALPHA", "GAMMA", 2, 1, "a-wins"),
    ("BETA", "DELTA", 2, 2, "b-wins-big"),
]


def _document(fabric, *, rater_type="human", domain="order-review"):
    from bin import standings_view
    with standings_view.connect(str(fabric)) as conn:
        standings_view.materialise(conn)
        return standings_view.read_document(conn, rater_type=rater_type, domain=domain)


def _only_table(document):
    assert len(document["tables"]) == 1, document["tables"]
    return document["tables"][0]


def _by_title(table, key="standings"):
    return {row["title"]: row for row in table[key]}


def test_points_are_three_one_zero_and_the_order_follows_them(fabric):
    _judge(fabric, THREE_ROUND_PLAN)
    table = _only_table(_document(fabric))
    rows = _by_title(table)

    assert [row["title"] for row in table["standings"]] == [
        "ALPHA", "DELTA", "ECHO", "GAMMA", "BETA", "FOXTROT"
    ]
    assert (rows["ALPHA"]["points"], rows["ALPHA"]["wins"], rows["ALPHA"]["rank"]) == (6, 2, 1)
    assert (rows["DELTA"]["points"], rows["DELTA"]["draws"], rows["DELTA"]["rank"]) == (4, 1, 2)
    assert (rows["GAMMA"]["points"], rows["GAMMA"]["losses"]) == (1, 1)
    assert (rows["BETA"]["points"], rows["BETA"]["played"]) == (0, 2)
    assert table["matches"] == 5
    assert table["round"] == 2


def test_both_win_magnitudes_are_worth_the_same_three_points(fabric):
    """The magnitude is signal for the rubric optimizer, not a bigger score."""
    _judge(fabric, [
        ("BIG", "SMALL", 1, 1, "a-wins-big"),
        ("NARROW", "LOSER", 1, 2, "a-wins"),
    ])
    rows = _by_title(_only_table(_document(fabric)))

    assert rows["BIG"]["points"] == 3
    assert rows["NARROW"]["points"] == 3
    assert rows["BIG"]["wins"] == rows["NARROW"]["wins"] == 1
    assert {rows["BIG"]["rank"], rows["NARROW"]["rank"]} == {1, 2}, (
        "bin/swiss.py numbers ranks sequentially rather than sharing one across "
        "equal totals; the table now says exactly what the engine says"
    )


def test_a_tie_is_one_point_each_and_is_not_a_skip(fabric):
    _judge(fabric, [("LEFT", "RIGHT", 1, 1, "tie")])
    table = _only_table(_document(fabric))
    rows = _by_title(table)

    assert rows["LEFT"]["points"] == rows["RIGHT"]["points"] == 1
    assert rows["LEFT"]["draws"] == rows["RIGHT"]["draws"] == 1
    assert table["matches"] == 1


def test_discard_a_ejects_only_a_and_the_survivor_stays_in_the_table(fabric):
    """The whole point of the change: no collateral ejection."""
    _judge(fabric, [
        ("KEEPER", "RIVAL", 1, 1, "a-wins"),
        ("JUNK", "KEEPER", 2, 1, "discard-a"),
    ])
    table = _only_table(_document(fabric))
    titles = [row["title"] for row in table["standings"]]

    assert "JUNK" not in titles
    assert "KEEPER" in titles, "the survivor of a discard must not leave as collateral"
    assert [d["title"] for d in table["discards"]] == ["JUNK"]
    assert table["discards"][0]["side"] == "a"
    assert table["discards"][0]["survivor_title"] == "KEEPER"


def test_a_discarded_pairing_produces_no_result_for_the_survivor(fabric):
    """Nothing was established about it: zero played, seated like a bye."""
    _judge(fabric, [("JUNK", "UNTOUCHED", 1, 1, "discard-a")])
    table = _only_table(_document(fabric))
    rows = _by_title(table)

    assert rows["UNTOUCHED"]["played"] == 0
    assert rows["UNTOUCHED"]["points"] == 0
    assert rows["UNTOUCHED"]["rank"] == 0
    assert rows["UNTOUCHED"]["awaiting_first_result"] is True
    assert rows["UNTOUCHED"]["lost_honestly"] is False
    assert table["matches"] == 0
    assert table["top_group_points"] is None
    assert not any(row["top_group"] for row in table["standings"]), (
        "an item that has played nothing is not the top group to start work on, "
        "however few points everything else has"
    )


def test_zero_points_from_losing_is_told_apart_from_zero_from_a_discard(fabric):
    """Two different facts; a table that renders them the same way lies."""
    _judge(fabric, [
        ("WINNER", "LOSER", 1, 1, "a-wins"),
        ("JUNK", "SURVIVOR", 1, 2, "discard-a"),
    ])
    rows = _by_title(_only_table(_document(fabric)))

    assert rows["LOSER"]["points"] == rows["SURVIVOR"]["points"] == 0
    assert rows["LOSER"]["lost_honestly"] is True
    assert rows["LOSER"]["awaiting_first_result"] is False
    assert rows["SURVIVOR"]["lost_honestly"] is False
    assert rows["SURVIVOR"]["awaiting_first_result"] is True


def test_a_discarded_item_is_absent_even_after_winning_a_match(fabric):
    _judge(fabric, [
        ("DOOMED", "OTHER", 1, 1, "a-wins-big"),
        ("DOOMED", "THIRD", 2, 1, "discard-a"),
    ])
    table = _only_table(_document(fabric))

    assert "DOOMED" not in [row["title"] for row in table["standings"]]
    assert [d["title"] for d in table["discards"]] == ["DOOMED"]


def test_one_pair_judged_through_two_match_rows_scores_once(fabric):
    """Identity is the pair key, not the match row.

    Folding by ``{db_path, match_id}`` counted this twice and handed ALPHA
    six points for one decision.
    """
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    _judge(fabric, [("ALPHA", "BETA", 2, 1, "a-wins")], match_offset=50)
    table = _only_table(_document(fabric))
    rows = _by_title(table)

    assert table["matches"] == 1
    assert rows["ALPHA"]["points"] == 3
    assert rows["ALPHA"]["played"] == 1


def test_the_latest_verdict_on_a_pair_is_the_one_that_scores(fabric):
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    _judge(fabric, [("ALPHA", "BETA", 2, 1, "b-wins")], match_offset=50)
    rows = _by_title(_only_table(_document(fabric)))

    assert rows["BETA"]["points"] == 3
    assert rows["ALPHA"]["points"] == 0


def test_a_revised_verdict_supersedes_the_original(fabric):
    import judgement
    [pending_id] = _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    conn = _conn(fabric)
    original = conn.execute(
        "SELECT rating_id FROM score WHERE pending_id=? AND name='judgement.verdict'",
        (pending_id,),
    ).fetchone()["rating_id"]
    conn.close()
    judgement.revise_judgement(
        pending_id,
        previous_rating_id=original,
        revised_by="reviewer",
        reason="misread the pair",
        rater={"type": "human", "userId": "reviewer"},
        verdict="b-wins",
        confidence="high",
    )
    rows = _by_title(_only_table(_document(fabric)))

    assert rows["BETA"]["points"] == 3
    assert rows["ALPHA"]["points"] == 0


def test_the_revision_chain_tip_wins_even_when_it_sorts_earlier(fabric):
    """Effective verdict is the tip of the chain, not the newest timestamp.

    The two writers stamp created_at differently -- Python
    "YYYY-MM-DD HH:MM:SS", the LiveView ISO-8601 with a "T" -- so a revision
    written by one can sort BEFORE the rating it replaces. Only the
    judgement_revision chain settles it.
    """
    import judgement
    [pending_id] = _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    conn = _conn(fabric)
    original = conn.execute(
        "SELECT rating_id FROM score WHERE pending_id=? AND name='judgement.verdict'",
        (pending_id,),
    ).fetchone()["rating_id"]
    conn.execute("UPDATE score SET created_at='2026-01-01T10:00:00' WHERE rating_id=?",
                 (original,))
    conn.commit()
    conn.close()
    revised = judgement.revise_judgement(
        pending_id,
        previous_rating_id=original,
        revised_by="reviewer",
        reason="misread the pair",
        rater={"type": "human", "userId": "reviewer"},
        verdict="b-wins",
        confidence="high",
    )
    conn = _conn(fabric)
    conn.execute("UPDATE score SET created_at='2026-01-01 09:00:00' WHERE rating_id=?",
                 (revised,))
    conn.commit()
    conn.close()

    rows = _by_title(_only_table(_document(fabric)))

    assert rows["BETA"]["points"] == 3
    assert rows["ALPHA"]["points"] == 0


def test_a_verdict_the_engine_does_not_score_is_named_not_folded_in(fabric):
    """A nil-point played match would put a fully judged pool on zero."""
    conn = _conn(fabric)
    retired = conn.execute(
        "INSERT INTO eval_template(name, version, output_definition) VALUES (?,?,?)",
        ("retired-pair-v0", 1, json.dumps({
            "judgement_kind": "pair",
            "verdict_enum": ["incoherent", "a-clearly-better"],
            "confidence_enum": ["low", "mid", "high"],
        })),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_configuration(template_id, rater_type, status) VALUES (?,?,?)",
        (retired, "human", "active"),
    )
    conn.commit()
    conn.close()
    _judge(fabric, [("OLDA", "OLDB", 1, 1, "incoherent")], template="retired-pair-v0")
    _judge(fabric, [("NEWA", "NEWB", 1, 1, "a-wins")], match_offset=90)

    document = _document(fabric)

    assert document["unscored_verdicts"] == [{"verdict": "incoherent", "count": 1}]
    assert [t["rubric"] for t in document["tables"]] == ["pair-wheel-v2"]
    assert "OLDA" not in [
        row["title"] for table in document["tables"] for row in table["standings"]
    ]


def test_a_stale_rubric_version_contributes_no_points(fabric):
    """A rubric revision invalidates exactly the matches judged under it."""
    conn = _conn(fabric)
    old = conn.execute(
        "SELECT id, output_definition FROM eval_template WHERE name='pair-wheel-v2'"
    ).fetchone()
    conn.execute(
        "INSERT INTO eval_template(name, version, output_definition) VALUES (?,?,?)",
        ("pair-wheel-v2", 99, old["output_definition"]),
    )
    conn.commit()
    conn.close()
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins-big")])

    table = _only_table(_document(fabric))

    assert table["rubric_version"] == 99
    assert table["matches"] == 0
    assert table["stale_matches"] == 1
    assert all(row["points"] == 0 for row in table["standings"])


def test_only_human_verdicts_order_the_default_scope(fabric):
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")],
           rater={"type": "llm", "model": "z-ai/glm-5.2"}, match_offset=70)

    human = _document(fabric, rater_type="human")
    every = _document(fabric, rater_type="")

    assert human["tables"] == []
    assert _only_table(every)["matches"] == 1


def test_a_domain_scope_excludes_other_domains(fabric):
    from bin import domains
    domains.create_domain(
        name="other-review", description="elsewhere",
        corpus_source={"kind": "inline", "items": []}, rubric="pair-wheel-v2",
    )
    _judge(fabric, [("HERE", "ALSO", 1, 1, "a-wins")])
    _judge(fabric, [("THERE", "TOO", 1, 1, "a-wins")], domain="other-review",
           match_offset=40)

    scoped = _only_table(_document(fabric, domain="order-review"))
    everywhere = _only_table(_document(fabric, domain=""))

    assert sorted(row["title"] for row in scoped["standings"]) == ["ALSO", "HERE"]
    assert everywhere["matches"] == 2


def test_an_unjudged_scope_materialises_an_empty_document_not_an_error(fabric):
    document = _document(fabric)

    assert document["tables"] == []
    assert document["totals"] == {"rubrics": 0, "items": 0, "matches": 0, "discarded": 0}


def test_a_scope_for_a_deleted_domain_is_dropped_rather_than_left_to_age(fabric):
    from bin import standings_view
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    with standings_view.connect(str(fabric)) as conn:
        standings_view.materialise(conn)
        assert standings_view.read_document(conn, domain="order-review") is not None
        conn.execute("DELETE FROM domain WHERE name='order-review'")
        conn.commit()
        standings_view.materialise(conn)
        assert standings_view.read_document(conn, domain="order-review") is None


def test_the_stored_row_count_tells_a_stale_view_from_an_old_one(fabric):
    from bin import standings_view
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    with standings_view.connect(str(fabric)) as conn:
        standings_view.materialise(conn)
        stored = conn.execute(
            "SELECT source_verdict_rows FROM standings_view "
            "WHERE rater_type='human' AND domain=''"
        ).fetchone()["source_verdict_rows"]
        assert stored == standings_view.source_verdict_rows(conn)
    _judge(fabric, [("GAMMA", "DELTA", 1, 2, "a-wins")], match_offset=30)
    with standings_view.connect(str(fabric)) as conn:
        assert standings_view.source_verdict_rows(conn) > stored


def test_pair_rubrics_are_read_from_the_fabric_not_listed(fabric):
    """The list that drifted is gone; the DB answers instead."""
    from bin import standings_view
    with standings_view.connect(str(fabric)) as conn:
        found = dict(standings_view.pair_rubrics(conn))

    assert set(found) == {"pair-wheel-v2", "pair-idea-wheel-v2"}
    assert "single-idea-v1" not in found
    assert "single-execution-v1" not in found


def test_a_template_predating_judgement_kind_still_counts_as_a_pair(fabric):
    from bin import standings_view
    conn = _conn(fabric)
    conn.execute(
        "INSERT INTO eval_template(name, version, output_definition) VALUES (?,?,?)",
        ("legacy-pair", 1, json.dumps({"verdict_enum": ["a-wins", "b-wins"]})),
    )
    conn.commit()
    conn.close()
    with standings_view.connect(str(fabric)) as conn:
        assert "legacy-pair" in dict(standings_view.pair_rubrics(conn))


def test_an_item_key_is_the_digest_the_pair_key_hashes(fabric):
    """One identity for a card in the queue and in the table."""
    from bin import standings_view
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    table = _only_table(_document(fabric))
    alpha = _by_title(table)["ALPHA"]

    assert alpha["item_key"] == swiss.content_digest("body ALPHA")[:16]
    assert alpha["item_key"] == standings_view.item_key("body ALPHA")


def test_a_pair_rubric_with_no_judgements_produces_no_table(fabric):
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    document = _document(fabric)

    assert [t["rubric"] for t in document["tables"]] == ["pair-wheel-v2"]


def test_two_pair_rubrics_get_one_table_each(fabric):
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    _judge(fabric, [("IDEA1", "IDEA2", 1, 1, "b-wins")],
           template="pair-idea-wheel-v2", match_offset=60)
    document = _document(fabric)

    assert sorted(t["rubric"] for t in document["tables"]) == [
        "pair-idea-wheel-v2", "pair-wheel-v2"
    ]
    assert document["totals"]["rubrics"] == 2
    assert document["totals"]["matches"] == 2


def test_the_top_group_is_every_item_on_the_highest_total(fabric):
    _judge(fabric, THREE_ROUND_PLAN)
    table = _only_table(_document(fabric))

    assert table["top_group_points"] == 6
    assert [row["title"] for row in table["standings"] if row["top_group"]] == ["ALPHA"]


def test_an_all_zero_table_has_no_top_group_to_start_work_on(fabric):
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "tie")])
    _judge(fabric, [("JUNK", "KEEP", 1, 2, "discard-a")], match_offset=20)
    table = _only_table(_document(fabric))
    ties = _by_title(table)

    assert table["top_group_points"] == 1
    assert ties["KEEP"]["top_group"] is False, "an item with no result is not the top group"


def test_the_cli_refresh_writes_every_scope(fabric, tmp_data_home):
    import subprocess
    import sys
    _judge(fabric, [("ALPHA", "BETA", 1, 1, "a-wins")])
    result = subprocess.run(
        [sys.executable, "bin/standings_view.py", "refresh"],
        capture_output=True, text=True,
        env={"DATA_TOURNAMENTS_HOME": str(tmp_data_home), "PATH": "/usr/bin:/bin",
             "PROMPT_BACKEND": "local"},
    )

    assert result.returncode == 0, result.stderr
    conn = _conn(fabric)
    scopes = {
        (row["rater_type"], row["domain"])
        for row in conn.execute("SELECT rater_type, domain FROM standings_view")
    }
    conn.close()
    assert ("human", "") in scopes
    assert ("human", "order-review") in scopes
