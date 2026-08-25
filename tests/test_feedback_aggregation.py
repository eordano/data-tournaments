"""Tests for aggregating per-domain card feedback from the fabric DB.

`list_card_feedback_for_domain` reads the score table joined with
pending_judgement.trace_payload, projects each scored pair through
derive_card_feedback(), and returns a flat list of per-card feedback rows
ready for generator-prompt optimization.
"""
from __future__ import annotations
import json
import sqlite3
import uuid

import pytest

from bin.feedback import list_card_feedback_for_domain
from bin import judgement


@pytest.fixture
def fabric_with_scores(fake_langfuse, tmp_data_home, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    import importlib, judgement as _j
    importlib.reload(_j)
    _j.init_db()

    from bin import domains
    domains.create_domain(
        name="commit-msg",
        description="commit messages",
        corpus_source={"kind": "inline", "items": []},
        generator_prompt="gen",
        judge_prompt="judge",
    )

    db = sqlite3.connect(tmp_data_home / "judgements.db")
    cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
    domain_id = db.execute("SELECT id FROM domain WHERE name='commit-msg'").fetchone()[0]
    template_id, rv = db.execute("SELECT id, version FROM eval_template").fetchone()

    def _seed_pair(match_id, card_a, card_b, verdict):
        payload = json.dumps({"label": f"R1-{match_id}", "card_a": card_a, "card_b": card_b})
        db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
            "trace_payload, domain_id, status) VALUES (?,?,?,?,?,?)",
            (cfg_id, "domain:commit-msg", match_id, payload, domain_id, "done"),
        )
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        rating_id = str(uuid.uuid4())
        meta = {"rater": {"type": "human", "userId": "test"}}
        for name, value in [("judgement.verdict", verdict), ("judgement.confidence", "mid")]:
            db.execute(
                "INSERT INTO score(rating_id, pending_id, template_id, rubric_version, "
                "name, data_type, value, metadata, tournament_db_path, match_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rating_id, pid, template_id, rv, name, "CATEGORICAL", value,
                 json.dumps(meta), "domain:commit-msg", match_id),
            )

    _seed_pair(0,
        {"title": "strong card", "body": "useful", "source_ref": "git:aaa"},
        {"title": "weak card", "body": "vague", "source_ref": "git:bbb"},
        "a-clearly-better")
    _seed_pair(1,
        {"title": "good 1", "body": "x", "source_ref": "git:ccc"},
        {"title": "good 2", "body": "y", "source_ref": "git:ddd"},
        "tie-both-strong")
    _seed_pair(2,
        {"title": "bad 1", "body": "z", "source_ref": "git:eee"},
        {"title": "bad 2", "body": "w", "source_ref": "git:fff"},
        "tie-both-weak")
    db.commit()
    return tmp_data_home


def test_returns_one_feedback_row_per_card_per_scored_pair(fabric_with_scores):
    rows = list_card_feedback_for_domain("commit-msg")
    # 3 pairs scored × 2 cards = 6 rows
    assert len(rows) == 6


def test_feedback_rows_carry_domain_and_provenance(fabric_with_scores):
    rows = list_card_feedback_for_domain("commit-msg")
    for r in rows:
        assert r["domain"] == "commit-msg"
        assert r["source_ref"].startswith("git:")
        assert "quality" in r and "weight" in r


def test_aggregate_quality_counts(fabric_with_scores):
    rows = list_card_feedback_for_domain("commit-msg")
    qualities = [r["quality"] for r in rows]
    # a-clearly-better → 1 positive, 1 weak
    # tie-both-strong → 2 positive
    # tie-both-weak → 2 negative
    assert qualities.count("positive") == 3
    assert qualities.count("weak") == 1
    assert qualities.count("negative") == 2


def test_filters_by_minimum_weight(fabric_with_scores):
    # weight > 0 drops the "neutral" rows; we have none here so length unchanged
    rows = list_card_feedback_for_domain("commit-msg", min_weight=0.5)
    # Excludes the weak (0.4) row but keeps positives (1.0) and negatives (1.0)
    assert all(r["weight"] >= 0.5 for r in rows)
    assert len(rows) == 5  # 3 positive + 2 negative


def test_returns_empty_for_unknown_domain(fabric_with_scores):
    assert list_card_feedback_for_domain("does-not-exist") == []
