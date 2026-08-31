"""The export carries the artifacts that were judged.

The judgements are the product, and a verdict with no artifact attached is
not one. Two failures this pins down, both from re-reading the source at
export time instead of snapshotting it at enqueue time:

- a domain-generated pair's `tournament_db_path` is the handle `domain:<id>`,
  not a path, so the lazy re-read resolved to nothing and the exported record
  carried input_a=None / input_b=None;
- a re-read reflects whatever the source says now, which makes both the
  exported record and the pair hash drift under a mutable source.
"""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

CARD_A = {"title": "Leaky handle", "body": "closes on GC only", "source_ref": "a.cs"}
CARD_B = {"title": "Slow startup", "body": "1.2s of blocking IO", "source_ref": "b.cs"}

@pytest.fixture
def judgement_mod(tmp_data_home, fake_langfuse, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    return judgement

def _judge(judgement_mod, pending_id, verdict="a-wins-big"):
    return judgement_mod.write_judgement(
        pending_id=pending_id, verdict=verdict, confidence="mid",
        rationale="A is the one to fix", rater={"type": "human", "userId": "u1"},
    )

def _only_record(judgement_mod, rubric="pair-wheel-v2"):
    records = judgement_mod.export_jsonl(rubric=rubric)
    assert len(records) == 1, f"expected exactly one exported record, got {records}"
    return records[0]

def _match_db(tmp_path, path_a, path_b) -> str:
    db_path = tmp_path / "tournament.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE matches (id INTEGER PRIMARY KEY, round INTEGER, "
        "slot INTEGER, input_a TEXT, input_b TEXT, is_bye INTEGER, "
        "conclusion TEXT, synthesis TEXT, winner_id INTEGER, "
        "winner_reasoning TEXT, trace_id TEXT)"
    )
    conn.execute(
        "INSERT INTO matches(id, round, slot, input_a, input_b, is_bye, "
        "conclusion, synthesis, winner_id, winner_reasoning, trace_id) "
        "VALUES (1, 1, 0, ?, ?, 0, 'concluded', 'the synthesis', 1, 'because', NULL)",
        (str(path_a), str(path_b)),
    )
    conn.commit()
    conn.close()
    return str(db_path)

def test_domain_pair_exports_the_bodies_it_was_judged_on(judgement_mod):
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:1", match_id=0,
        template_name="pair-wheel-v2",
        payload={"label": "R1-1", "card_a": CARD_A, "card_b": CARD_B},
    )
    rating_id = _judge(judgement_mod, outcome[0])

    record = _only_record(judgement_mod)

    assert record["ratingId"] == rating_id
    assert record["trace"]["input_a"] == CARD_A["body"]
    assert record["trace"]["input_b"] == CARD_B["body"]
    assert record["pairKey"] == outcome.pair_key

def test_domain_handle_is_not_a_path_the_export_could_re_read(judgement_mod):
    """The regression this pins: `domain:<id>` resolves to nothing, so an
    export that re-reads the source has nothing to attach."""
    assert judgement_mod._trace_payload("domain:1", 0) is None

def test_export_and_pair_key_survive_a_mutated_source(judgement_mod, tmp_path):
    path_a = tmp_path / "a.md"
    path_b = tmp_path / "b.md"
    path_a.write_text("the original A body", encoding="utf-8")
    path_b.write_text("the original B body", encoding="utf-8")
    db_path = _match_db(tmp_path, path_a, path_b)

    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path=db_path, match_id=1, template_name="pair-wheel-v2",
    )
    _judge(judgement_mod, outcome[0])
    before = _only_record(judgement_mod)
    assert before["trace"]["input_a"] == "the original A body"

    path_a.write_text("REWRITTEN after the judgement", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE matches SET input_a='/nowhere/gone.md' WHERE id=1")
    conn.commit()
    conn.close()

    after = _only_record(judgement_mod)
    assert after["trace"]["input_a"] == before["trace"]["input_a"]
    assert after["trace"]["input_b"] == before["trace"]["input_b"]
    assert after["pairKey"] == before["pairKey"] == outcome.pair_key

def test_deleting_the_source_does_not_empty_the_export(judgement_mod, tmp_path):
    path_a = tmp_path / "a.md"
    path_b = tmp_path / "b.md"
    path_a.write_text("A body", encoding="utf-8")
    path_b.write_text("B body", encoding="utf-8")
    db_path = _match_db(tmp_path, path_a, path_b)

    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path=db_path, match_id=1, template_name="pair-wheel-v2",
    )
    _judge(judgement_mod, outcome[0])

    path_a.unlink()
    path_b.unlink()

    record = _only_record(judgement_mod)
    assert record["trace"]["input_a"] == "A body"
    assert record["trace"]["input_b"] == "B body"

def test_legacy_rows_without_a_snapshot_still_export(judgement_mod, tmp_data_home):
    """A pending row written by an older writer has no snapshot columns; the
    export falls back to its stored payload rather than dropping the bodies."""
    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    cfg_id = db.execute(
        "SELECT c.id FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE t.name='pair-wheel-v2' AND c.rater_type='human'"
    ).fetchone()[0]
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload) VALUES (?, 'domain:1', 5, ?)",
        (cfg_id, json.dumps({"label": "R1-6", "card_a": CARD_A, "card_b": CARD_B})),
    ).lastrowid
    db.commit()
    db.close()

    _judge(judgement_mod, pid)

    record = _only_record(judgement_mod)
    assert record["trace"]["input_a"] == CARD_A["body"]
    assert record["trace"]["input_b"] == CARD_B["body"]
