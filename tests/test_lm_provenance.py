"""Regression tests: LM identity provenance across interleaved multi-model drains.

Historical bug: the queue worker configured the first row's LM globally, so
later rows for other models silently ran through the first row's model while
being *labelled* with their own configured model. The fix scopes each row's LM
inside ``dspy.context(lm=...)`` (see bin/judgement.py run_llm_judge_for_pending).

These tests pin that fix by interleaving the three seeded frontier models
across queue rows and asserting, for EVERY row, that both:

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
    # Start from a clean ambient state — the worker must not depend on (or
    # mutate) the global LM.
    dspy.settings.configure(lm=None)

    # The seed panel must be exactly the three frontier models under test.
    assert tuple(judgement.FRONTIER_OPENROUTER_MODELS) == MODELS

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    cfg_by_model = {}
    for row in db.execute(
        "SELECT id, rater_config FROM job_configuration "
        "WHERE rater_type='llm' AND status='active'"
    ):
        cfg_by_model[json.loads(row["rater_config"])["model"]] = row["id"]
    assert set(MODELS) <= set(cfg_by_model)

    # Six queue rows, interleaved: kimi, glm, opus, kimi, glm, opus.
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

    # Fake LM factory: one DummyLM per row, tagged with the configured model.
    # The rationale it emits is the provenance tracer — if a previous row's LM
    # leaked into this row, the persisted rationale carries the WRONG tag.
    def fake_build_lm(cfg):
        model = cfg["model"]
        # Enough responses that a *leaked* LM would keep serving later rows
        # (with the wrong tag) instead of erroring out — the mislabeling is
        # what this test must catch, so make the leak "succeed".
        lm = dspy.utils.DummyLM([{
            "rationale": f"served-by:{model}",
            "confidence": "mid",
            "verdict": "a-marginally-better",
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
        # (1) Recorded rater identity == configured model for this row.
        assert meta["rater"] == {
            "type": "llm",
            "model": model,
            "base_url": "https://openrouter.ai/api/v1",
        }, f"row {pid}: rater identity mismatch"
        # (2) The LM that actually generated the judgement was this row's LM.
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

    # The drain must not have leaked any LM into global DSPy state.
    assert dspy.settings.lm is None
