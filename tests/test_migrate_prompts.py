"""Tests for the seed-prompt migration: instructions blob → Langfuse Prompts.

After the migration:
- The seeded rubric is `pair-wheel-v2`; `code-style-tournament` is gone.
- The `instructions` text lives in Langfuse Prompts as `judge-instructions:production`.
- `eval_template.output_definition` no longer carries an `instructions` key.
- `eval_template` gains a `langfuse_prompt_name` column pointing at the prompt.
"""
import json
import sqlite3
import pytest


@pytest.fixture
def fresh_fabric(fake_langfuse, monkeypatch, tmp_data_home):
    """Initialize a fabric DB with the new schema + seed via Langfuse."""
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home / "judgements.db"


def test_init_db_pushes_judge_instructions_to_langfuse(fresh_fabric, fake_langfuse):
    p = fake_langfuse.get_prompt("judge-instructions", label="production")
    assert "card" in p.prompt.lower() or "two" in p.prompt.lower()
    assert len(p.prompt) > 100, "seed prompt should not be a stub"


def test_init_db_is_idempotent(fresh_fabric, fake_langfuse, monkeypatch):
    """Re-initializing should not push a second prompt version or duplicate the rubric."""
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    judgement.init_db()
    assert fake_langfuse.versions("judge-instructions") == [1]
    db = sqlite3.connect(str(fresh_fabric))
    n = db.execute("SELECT COUNT(*) FROM eval_template WHERE name='pair-wheel-v2'").fetchone()[0]
    assert n == 1


def test_eval_template_has_langfuse_prompt_name_column(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    cols = {r[1] for r in db.execute("PRAGMA table_info(eval_template)")}
    assert "langfuse_prompt_name" in cols


def test_seeded_rubric_points_at_langfuse_prompt(fresh_fabric):
    import judgement

    db = sqlite3.connect(str(fresh_fabric))
    row = db.execute(
        "SELECT langfuse_prompt_name FROM eval_template WHERE name='pair-wheel-v2'"
    ).fetchone()
    assert row is not None
    assert row[0] == judgement.PAIR_WHEEL_PROMPT_NAME == "judge-instructions:pair-wheel-v2"


def test_output_definition_no_longer_carries_instructions(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    raw = db.execute(
        "SELECT output_definition FROM eval_template WHERE name='pair-wheel-v2'"
    ).fetchone()[0]
    outdef = json.loads(raw)
    assert "instructions" not in outdef
    assert outdef["verdict_enum"], "verdict_enum still present"
    assert outdef["confidence_enum"] == ["low", "mid", "high"]


def test_old_rubric_name_is_not_seeded(fresh_fabric):
    """code-style-tournament is gone; only the renamed rubric exists."""
    db = sqlite3.connect(str(fresh_fabric))
    row = db.execute(
        "SELECT COUNT(*) FROM eval_template WHERE name='code-style-tournament'"
    ).fetchone()
    assert row[0] == 0


def test_init_db_seeds_one_human_and_one_machine_opinion(fresh_fabric):
    """One enqueue must fan out to exactly two rows, not four.

    The panel exists to produce human-versus-machine disagreement and one
    machine opinion produces it; three turned a 33-item campaign into ~288
    machine judgements for a spread nothing reads.
    """
    import judgement

    judgement.init_db()
    db = sqlite3.connect(str(fresh_fabric))
    rows = db.execute("""
        SELECT c.rater_type, json_extract(c.rater_config, '$.model') AS model
        FROM job_configuration c
        JOIN eval_template t ON t.id = c.template_id
        WHERE t.name='pair-wheel-v2' AND c.status='active'
    """).fetchall()
    assert [model for rater_type, model in rows if rater_type == "llm"] == list(
        judgement.DEFAULT_JUDGE_PANEL_MODELS
    )
    assert len(judgement.DEFAULT_JUDGE_PANEL_MODELS) == 1, (
        judgement.ONE_MACHINE_OPINION_IS_ENOUGH_TO_DISAGREE_WITH_A_HUMAN
    )
    assert sum(rater_type == "human" for rater_type, _model in rows) == 1
    assert len(rows) == 2


def test_a_widened_panel_seeds_a_row_per_model_and_narrowing_archives_it(
    fresh_fabric, monkeypatch
):
    """The seeding stays trivially widenable: the panel slice is the dial."""
    import judgement

    monkeypatch.setattr(
        judgement, "DEFAULT_JUDGE_PANEL_MODELS",
        judgement.FRONTIER_OPENROUTER_MODELS[:3],
    )
    judgement.init_db()
    db = sqlite3.connect(str(fresh_fabric))
    widened = db.execute("""
        SELECT json_extract(c.rater_config, '$.model') FROM job_configuration c
        JOIN eval_template t ON t.id = c.template_id
        WHERE t.name='pair-wheel-v2' AND c.status='active' AND c.rater_type='llm'
    """).fetchall()
    assert sorted(m for (m,) in widened) == sorted(
        judgement.FRONTIER_OPENROUTER_MODELS[:3]
    )

    monkeypatch.undo()
    judgement.init_db()
    narrowed = db.execute("""
        SELECT json_extract(c.rater_config, '$.model') FROM job_configuration c
        JOIN eval_template t ON t.id = c.template_id
        WHERE t.name='pair-wheel-v2' AND c.status='active' AND c.rater_type='llm'
    """).fetchall()
    assert [m for (m,) in narrowed] == list(judgement.DEFAULT_JUDGE_PANEL_MODELS)
