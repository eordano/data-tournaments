"""Tests for the optimizer-runs persistence integration with bin/optimize.py.

When called with --domain, optimize.py should:
  1. Create a row in optimizer_run before starting
  2. Stream stdout lines to optimizer_run.log
  3. Mark status=done with the candidate_version on success
  4. Mark status=error on failure
"""
from __future__ import annotations
import json
import sqlite3
import uuid

import pytest

from bin import optimizer_runs, optimize
from tests.conftest import make_evaluation_summary

@pytest.fixture
def seeded(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.add_prompt(
        "judge-instructions:commit-msg",
        text="seed",
        version=1,
        labels=["production"],
    )
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    optimizer_runs.init()

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    tpl_id, rv = db.execute(
        "SELECT id, version FROM eval_template WHERE name=?",
        (judgement.DEFAULT_TEMPLATE_NAME,),
    ).fetchone()
    cfg_id = db.execute(
        "SELECT id FROM job_configuration WHERE rater_type='human' AND template_id=?",
        (tpl_id,),
    ).fetchone()[0]
    domain_id = db.execute(
        "INSERT INTO domain(name, description, generator_prompt, judge_prompt, corpus_source) "
        "VALUES ('commit-msg', 'Commit messages', 'card-generator:commit-msg', "
        "'judge-instructions:commit-msg', '{\"kind\":\"inline\",\"items\":[]}')"
    ).lastrowid
    for i, v in enumerate(["a-wins-big", "b-wins-big", "tie"]):
        payload = {"card_a": {"title": f"A{i}", "body": "x"},
                   "card_b": {"title": f"B{i}", "body": "y"}}
        db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, status, domain_id) VALUES (?,?,?,?,?,?)",
            (cfg_id, "domain:commit-msg", i, json.dumps(payload), "done", domain_id),
        )
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        rid = str(uuid.uuid4())
        meta = json.dumps({"rater": {"type": "human"}})
        for n, val in [("judgement.verdict", v), ("judgement.confidence", "mid")]:
            db.execute(
                "INSERT INTO score(rating_id, pending_id, template_id, rubric_version, "
                "name, data_type, value, metadata, tournament_db_path, match_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, pid, tpl_id, rv, n, "CATEGORICAL", val, meta,
                 "domain:commit-msg", i),
            )
    db.commit()
    db.close()
    return tmp_data_home

class _StubLM:
    def __init__(self, name="stub"):
        self.model = name

_summary = make_evaluation_summary

def test_run_with_run_id_writes_log_and_finishes(seeded, monkeypatch):
    """Optimize writes status transitions and log lines to optimizer_run."""
    def fake_compile(program, trainset, metric, **kwargs):
        return program
    monkeypatch.setattr(optimize, "_compile_with_gepa", fake_compile)
    monkeypatch.setattr(optimize, "_build_lm", lambda model=None: _StubLM("openai/stub-judge"))
    monkeypatch.setattr(optimize, "_build_reflection_lm", lambda model=None: _StubLM("openai/stub-reflect"))
    monkeypatch.setattr(optimize, "_build_curator_lm", lambda model=None: _StubLM("openai/stub-curator"))
    scores = iter([0.0, 1.0])
    monkeypatch.setattr(optimize, "_evaluate_program", lambda program, examples: _summary(examples, next(scores)))
    monkeypatch.setattr(
        optimize,
        "_curate_context",
        lambda seed, evolved, evidence, lm, **kw: (seed + "\nCURATED", {"added": 1, "removed": 0, "reinforced": 0, "weakened": 0}, [{"section": "strategy", "content": "A detailed reusable lesson for this test run."}]),
    )
    monkeypatch.setenv("OPTIMIZER_MIN_VALIDATION", "1")
    monkeypatch.setenv("OPTIMIZER_MIN_HOLDOUT", "1")

    import dspy
    class _FakeLM:
        def __call__(self, *a, **kw): return ["a-wins-big"]
    dspy.settings.configure(lm=_FakeLM(), bypass_test=True)

    run_id = optimizer_runs.start(
        domain="commit-msg", target="judge",
        rubric="pair-wheel-v2",
        prompt_name="judge-instructions:commit-msg",
    )

    optimize.run_with_persistence(
        run_id=run_id,
        rubric="pair-wheel-v2",
        auto="light",
        min_trainset=2,
        prompt_name="judge-instructions:commit-msg",
        domain="commit-msg",
    )

    row = optimizer_runs.get(run_id)
    assert row["status"] == "done"
    assert row["exit_code"] == 0
    assert "loaded 3 valid human examples" in row["log"]
    assert row["result"]["candidate_version"] >= 2
    assert row["result"]["total_examples"] == 3
    assert row["result"]["trainset_size"] == 1
    assert row["result"]["accepted"] is True

def test_run_with_persistence_records_error_on_failure(seeded, monkeypatch):
    """If optimize raises, the row records status=error and the message."""
    monkeypatch.setattr(optimize, "_build_lm", lambda model=None: _StubLM())
    monkeypatch.setattr(optimize, "_build_reflection_lm", lambda model=None: _StubLM())
    monkeypatch.setattr(optimize, "_build_curator_lm", lambda model=None: _StubLM())

    run_id = optimizer_runs.start(
        domain="commit-msg", target="judge",
        rubric="pair-wheel-v2",
        prompt_name="judge-instructions:commit-msg",
    )

    optimize.run_with_persistence(
        run_id=run_id,
        rubric="pair-wheel-v2",
        auto="light",
        min_trainset=999,
        prompt_name="judge-instructions:commit-msg",
        domain="commit-msg",
    )

    row = optimizer_runs.get(run_id)
    assert row["status"] == "error"
    assert row["exit_code"] != 0
    assert "need at least 999" in row["log"] or "need at least 999" in (row["result"] or {}).get("error", "")
