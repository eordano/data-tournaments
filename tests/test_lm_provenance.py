"""Regression tests: LM identity provenance across interleaved multi-model drains.

Historical bug: the queue worker configured the first row's LM globally, so
later rows for other models silently ran through the first row's model while
being *labelled* with their own configured model. The fix scopes each row's LM
inside ``dspy.context(lm=...)`` (see bin/judgement.py run_llm_judge_for_pending).

These tests pin that fix by interleaving three frontier models across queue
rows and asserting, for EVERY row, that both:

  1. the recorded rater identity in score metadata matches the row's
     configured model, and
  2. the LM that actually served the generation was the one built for that
     row's config (each fake LM emits a model-tagged rationale, so a leaked
     LM would leave the wrong tag in the persisted metadata).

No network: the LM factory is monkeypatched to return dspy DummyLM instances,
and Langfuse is the in-memory fake. Everything between the queue row and the
score row (list_pending, MatchJudge, dspy.Predict, write_judgement) is real.
"""
from __future__ import annotations

import importlib
import json
import sqlite3

import dspy

MODELS = (
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "anthropic/claude-opus-5",
)

def test_interleaved_models_record_true_rater_identity(
    tmp_data_home, fake_langfuse, monkeypatch
):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")

    import judgement

    importlib.reload(judgement)
    judgement.init_db()
    dspy.settings.configure(lm=None)

    assert set(MODELS) >= set(judgement.DEFAULT_JUDGE_PANEL_MODELS)
    assert set(MODELS) <= set(judgement.FRONTIER_OPENROUTER_MODELS)

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    template_id = db.execute(
        "SELECT id FROM eval_template WHERE name=?",
        (judgement.DEFAULT_TEMPLATE_NAME,),
    ).fetchone()["id"]
    cfg_by_model = {}
    for row in db.execute(
        "SELECT id, rater_config FROM job_configuration "
        "WHERE rater_type='llm' AND status='active'"
    ):
        cfg_by_model[json.loads(row["rater_config"])["model"]] = row["id"]
    for model in MODELS:
        if model in cfg_by_model:
            continue
        cfg_by_model[model] = db.execute(
            "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
            "VALUES (?, 'llm', ?)",
            (template_id, json.dumps(judgement._openrouter_config(model))),
        ).lastrowid
    db.commit()
    assert set(MODELS) <= set(cfg_by_model)

    expected: dict[int, str] = {}
    for i, model in enumerate(list(MODELS) * 2):
        pid = db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload) VALUES (?, ?, ?, ?)",
            (
                cfg_by_model[model],
                "/tmp/provenance.db",
                i,
                json.dumps({
                    "card_a": {"title": f"A{i}", "body": f"alpha body {i}"},
                    "card_b": {"title": f"B{i}", "body": f"beta body {i}"},
                }),
            ),
        ).lastrowid
        expected[pid] = model
    db.commit()
    db.close()

    def fake_build_lm(cfg):
        model = cfg["model"]
        lm = dspy.utils.DummyLM([{
            "rationale": f"served-by:{model}",
            "confidence": "mid",
            "verdict": "a-wins",
        }] * 12)
        lm.model = model
        return lm

    monkeypatch.setattr(judgement, "_build_dspy_lm", fake_build_lm)

    result = judgement.drain_llm_queue()
    assert result == {"ok": 6, "error": 0, "skipped": 0, "errors": []}

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    for pid, model in expected.items():
        prow = db.execute(
            "SELECT status, rating_id FROM pending_judgement WHERE id=?", (pid,)
        ).fetchone()
        assert prow["status"] == "done"
        assert prow["rating_id"]

        verdict_rows = db.execute(
            "SELECT metadata FROM score WHERE pending_id=? AND name='judgement.verdict'",
            (pid,),
        ).fetchall()
        assert len(verdict_rows) == 1, f"row {pid}: expected exactly one verdict score"
        meta = json.loads(verdict_rows[0]["metadata"])
        assert meta["rater"] == {
            "type": "llm",
            "model": model,
            "base_url": "https://openrouter.ai/api/v1",
        }, f"row {pid}: rater identity mismatch"
        assert meta["rationale"] == f"served-by:{model}", (
            f"row {pid}: generation was served by a leaked LM "
            f"(got {meta['rationale']!r}, configured {model!r})"
        )

        conf_rows = db.execute(
            "SELECT metadata FROM score WHERE pending_id=? AND name='judgement.confidence'",
            (pid,),
        ).fetchall()
        assert len(conf_rows) == 1
        assert json.loads(conf_rows[0]["metadata"])["rater"]["model"] == model
    db.close()

    assert dspy.settings.lm is None
