"""Pair-keyed judgements: the never-repeat-a-pair enforcement mechanism.

Covers docs/design/priority-tournament.md "Match results are keyed to the
pair, not to the round":
- sha256 over (both contents, rubric id, rubric version), order-independent;
- a rubric revision changes the key, retiring exactly the old judgements;
- pending_judgement.pair_key / score.pair_key exist and are populated, on a
  fresh DB and by an idempotent migration over an existing one;
- find_judgement_by_pair answers "have these two met?" with the prior rating.
"""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

CARD_A = {"title": "Leaky handle", "body": "closes on GC only", "source_ref": "a.cs"}
CARD_B = {"title": "Slow startup", "body": "1.2s of blocking IO", "source_ref": "b.cs"}
A_TEXT = CARD_A["body"]
B_TEXT = CARD_B["body"]

@pytest.fixture
def judgement_mod(tmp_data_home, fake_langfuse, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    return judgement

def _db(tmp_data_home) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    return conn

def _columns(tmp_data_home, table: str) -> set[str]:
    db = _db(tmp_data_home)
    cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    db.close()
    return cols

def _enqueue_pair(judgement_mod, *, match_id: int, card_a=CARD_A, card_b=CARD_B,
                  template="pair-wheel-v2"):
    return judgement_mod.enqueue_for_match(
        tournament_db_path="domain:1",
        match_id=match_id,
        template_name=template,
        payload={"label": f"R1-{match_id}", "card_a": card_a, "card_b": card_b},
    )

def _judge(judgement_mod, pending_id: int, verdict="a-wins-big") -> str:
    return judgement_mod.write_judgement(
        pending_id=pending_id, verdict=verdict, confidence="mid",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )

def test_pair_key_is_pinned_to_a_known_digest(judgement_mod):
    """An independent oracle, so a change to the hashing breaks a test.

    Every other assertion here compares pair_key against itself and would
    survive any consistent rewrite of the function.
    """
    assert judgement_mod.pair_key("alpha", "beta", "r", 1) == (
        "079917505ce537b41bafd04b0ec837c6ea26df5ab1387dc407e434e455fe2cfe"
    )

def test_pair_key_is_order_independent(judgement_mod):
    """(A, B) and (B, A) are the same question and must hash identically."""
    assert judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", 1) == \
        judgement_mod.pair_key(B_TEXT, A_TEXT, "pair-wheel-v2", 1)

def test_pair_key_is_the_one_the_engine_uses(judgement_mod):
    """judgement and the swiss engine write pair keys into the same DB; two
    definitions would be two rematch checks that can never agree."""
    from bin import swiss
    assert judgement_mod.pair_key("x", "y", "r", 1) == swiss.pair_key("x", "y", "r", 1)

def test_pair_key_changes_with_rubric_version(judgement_mod):
    """A rubric revision must invalidate exactly the matches judged under
    the old rubric — which it can only do if the key moves with it."""
    assert judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", 1) != \
        judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", 2)

def test_pair_key_changes_with_rubric_id(judgement_mod):
    assert judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", 1) != \
        judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-idea-wheel-v2", 1)

def test_pair_key_changes_with_content(judgement_mod):
    assert judgement_mod.pair_key(A_TEXT, B_TEXT, "r", 1) != \
        judgement_mod.pair_key(A_TEXT, "1.9s of blocking IO", "r", 1)

def test_the_same_text_from_a_moved_file_keys_identically(judgement_mod):
    """The key is over content and nothing else.

    Hashing a path or a title would re-ask a settled pair whenever the file
    moved or a line number shifted, which is what "nothing already judged is
    re-asked" forbids.
    """
    moved = {"title": "Renamed", "body": CARD_A["body"],
             "source_ref": "/somewhere/else/a.cs:91"}
    assert judgement_mod._side_snapshot(moved, None) == \
        judgement_mod._side_snapshot(CARD_A, None)

def test_schema_carries_pair_key_columns(judgement_mod, tmp_data_home):
    assert {"pair_key", "content_a", "content_b"} <= _columns(
        tmp_data_home, "pending_judgement")
    assert "pair_key" in _columns(tmp_data_home, "score")

def test_enqueue_populates_pair_key_and_snapshot(judgement_mod, tmp_data_home):
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    assert len(outcome) == len(judgement_mod.list_active_configs("pair-wheel-v2"))
    expected = judgement_mod.pair_key(
        A_TEXT, B_TEXT, "pair-wheel-v2",
        judgement_mod.get_template("pair-wheel-v2")["version"])
    assert outcome.pair_key == expected

    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT pair_key, content_a, content_b FROM pending_judgement WHERE id=?",
        (outcome[0],),
    ).fetchone()
    db.close()
    assert row["pair_key"] == expected
    assert row["content_a"] == CARD_A["body"]
    assert row["content_b"] == CARD_B["body"]

def test_score_rows_carry_the_pair_key(judgement_mod, tmp_data_home):
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    rating_id = _judge(judgement_mod, outcome[0])
    db = _db(tmp_data_home)
    keys = {r["pair_key"] for r in db.execute(
        "SELECT pair_key FROM score WHERE rating_id=?", (rating_id,))}
    db.close()
    assert keys == {outcome.pair_key}

def _strip_pair_key_columns(tmp_data_home) -> None:
    """Rewind the DB to its pre-pair-key shape, so init_db has to migrate."""
    db = _db(tmp_data_home)
    db.execute("DROP INDEX IF EXISTS idx_pending_pair_key")
    db.execute("DROP INDEX IF EXISTS idx_score_pair_key")
    db.execute("DROP INDEX IF EXISTS idx_pending_one_open_row_per_config_pair")
    for column in ("pair_key", "content_a", "content_b"):
        db.execute(f"ALTER TABLE pending_judgement DROP COLUMN {column}")
    db.execute("ALTER TABLE score DROP COLUMN pair_key")
    db.commit()
    db.close()

def _legacy_pending(tmp_data_home, *, match_id: int, template="pair-wheel-v2") -> int:
    db = _db(tmp_data_home)
    cfg_id = db.execute(
        "SELECT c.id FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE t.name=? AND c.rater_type='human' AND c.status='active'",
        (template,),
    ).fetchone()[0]
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload) VALUES (?, ?, ?, ?)",
        (cfg_id, "domain:1", match_id,
         json.dumps({"label": "R1-1", "card_a": CARD_A, "card_b": CARD_B})),
    ).lastrowid
    db.commit()
    db.close()
    return pid

def test_migration_adds_and_backfills_pair_key_on_an_existing_db(
    judgement_mod, tmp_data_home
):
    _strip_pair_key_columns(tmp_data_home)
    assert "pair_key" not in _columns(tmp_data_home, "pending_judgement")
    pid = _legacy_pending(tmp_data_home, match_id=7)

    judgement_mod.init_db()

    assert {"pair_key", "content_a", "content_b"} <= _columns(
        tmp_data_home, "pending_judgement")
    assert "pair_key" in _columns(tmp_data_home, "score")
    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT pair_key, content_a FROM pending_judgement WHERE id=?", (pid,)
    ).fetchone()
    db.close()
    expected = judgement_mod.pair_key(
        A_TEXT, B_TEXT, "pair-wheel-v2",
        judgement_mod.get_template("pair-wheel-v2")["version"])
    assert row["pair_key"] == expected
    assert row["content_a"] == A_TEXT

def test_migration_is_idempotent(judgement_mod, tmp_data_home):
    _strip_pair_key_columns(tmp_data_home)
    pid = _legacy_pending(tmp_data_home, match_id=7)
    judgement_mod.init_db()
    db = _db(tmp_data_home)
    first = db.execute(
        "SELECT pair_key FROM pending_judgement WHERE id=?", (pid,)).fetchone()[0]
    db.close()

    judgement_mod.init_db()
    judgement_mod.init_db()

    db = _db(tmp_data_home)
    again = db.execute(
        "SELECT pair_key FROM pending_judgement WHERE id=?", (pid,)).fetchone()[0]
    rows = db.execute("SELECT COUNT(*) FROM pending_judgement").fetchone()[0]
    db.close()
    assert again == first
    assert rows == 1

def test_migration_leaves_non_pair_rows_unkeyed(judgement_mod, tmp_data_home):
    """A single-artifact judgement has no pair; it must stay NULL rather
    than acquire a bogus key that a rematch check could collide with."""
    db = _db(tmp_data_home)
    cfg_id = db.execute(
        "SELECT c.id FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE t.name='single-idea-v1' AND c.rater_type='human'"
    ).fetchone()[0]
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload) VALUES (?, 'domain:1', 3, ?)",
        (cfg_id, json.dumps({"label": "S1-1", "card": CARD_A})),
    ).lastrowid
    db.commit()
    db.close()

    judgement_mod.init_db()

    db = _db(tmp_data_home)
    key = db.execute(
        "SELECT pair_key FROM pending_judgement WHERE id=?", (pid,)).fetchone()[0]
    db.close()
    assert key is None

def test_find_judgement_by_pair_returns_prior_rating_and_none_otherwise(
    judgement_mod
):
    version = judgement_mod.get_template("pair-wheel-v2")["version"]
    key = judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", version)
    assert judgement_mod.find_judgement_by_pair(key) is None

    outcome = _enqueue_pair(judgement_mod, match_id=1)
    assert judgement_mod.find_judgement_by_pair(key) is None

    rating_id = _judge(judgement_mod, outcome[0])
    assert judgement_mod.find_judgement_by_pair(key) == rating_id
    assert judgement_mod.find_judgement_by_pair(
        judgement_mod.pair_key(B_TEXT, A_TEXT, "pair-wheel-v2", version)
    ) == rating_id

def test_find_judgement_by_pair_ignores_a_pair_judged_under_another_rubric(
    judgement_mod
):
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    _judge(judgement_mod, outcome[0])
    assert judgement_mod.find_judgement_by_pair(
        judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", 99)) is None

def test_find_judgement_by_pair_follows_the_revision_chain(judgement_mod):
    version = judgement_mod.get_template("pair-wheel-v2")["version"]
    key = judgement_mod.pair_key(A_TEXT, B_TEXT, "pair-wheel-v2", version)
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    rating_id = _judge(judgement_mod, outcome[0])
    new_rating_id = judgement_mod.revise_judgement(
        outcome[0], previous_rating_id=rating_id, revised_by="u2",
        reason="misread B", rater={"type": "human", "userId": "u2"},
        verdict="b-wins-big", confidence="high",
    )
    assert judgement_mod.find_judgement_by_pair(key) == new_rating_id

def _judge_as(judgement_mod, pending_id: int, rater: dict,
              verdict="a-wins-big") -> str:
    return judgement_mod.write_judgement(
        pending_id=pending_id, verdict=verdict, confidence="mid",
        rationale=None, rater=rater,
    )

def _pair_wheel_key(judgement_mod) -> str:
    return judgement_mod.pair_key(
        A_TEXT, B_TEXT, "pair-wheel-v2",
        judgement_mod.get_template("pair-wheel-v2")["version"])

def test_satisfying_rater_types_is_asymmetric(judgement_mod):
    """A human verdict may stand in for a machine's; never the reverse.

    The judgements a PERSON makes are the product; a model's opinion is
    cheap confirmation. An unknown rater type is answered only by itself,
    so a machine can never silence a rater the table does not know about.
    """
    assert judgement_mod.satisfying_rater_types("human") == ("human",)
    assert set(judgement_mod.satisfying_rater_types("llm")) == {"human", "llm"}
    assert judgement_mod.satisfying_rater_types("agent") == ("agent",)

def test_a_machine_verdict_does_not_answer_the_human_lookup(judgement_mod):
    key = _pair_wheel_key(judgement_mod)
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    rating_id = _judge_as(judgement_mod, outcome[0],
                          {"type": "llm", "model": "kimi-k3"})

    assert judgement_mod.find_judgement_by_pair(key) == rating_id
    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="llm") == rating_id
    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="human") is None

def test_a_human_verdict_answers_both_lookups(judgement_mod):
    key = _pair_wheel_key(judgement_mod)
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    rating_id = _judge_as(judgement_mod, outcome[0],
                          {"type": "human", "userId": "u1"})

    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="human") == rating_id
    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="llm") == rating_id

def test_the_human_lookup_follows_a_revision_written_by_a_person(judgement_mod):
    """A machine verdict a PERSON revised is a human verdict now."""
    key = _pair_wheel_key(judgement_mod)
    outcome = _enqueue_pair(judgement_mod, match_id=1)
    machine = _judge_as(judgement_mod, outcome[0], {"type": "llm", "model": "kimi-k3"})
    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="human") is None

    revised = judgement_mod.revise_judgement(
        outcome[0], previous_rating_id=machine, revised_by="u2",
        reason="the model misread B", rater={"type": "human", "userId": "u2"},
        verdict="b-wins-big", confidence="high",
    )
    assert judgement_mod.find_judgement_by_pair(key, for_rater_type="human") == revised
