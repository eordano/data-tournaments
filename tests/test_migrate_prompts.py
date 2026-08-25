"""Tests for the seed-prompt migration: instructions blob → Langfuse Prompts.

After the migration:
- The rubric is renamed `code-style-tournament` → `card-prioritizer-v0`.
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
    n = db.execute("SELECT COUNT(*) FROM eval_template WHERE name='card-prioritizer-v0'").fetchone()[0]
    assert n == 1


def test_eval_template_has_langfuse_prompt_name_column(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    cols = {r[1] for r in db.execute("PRAGMA table_info(eval_template)")}
    assert "langfuse_prompt_name" in cols


def test_seeded_rubric_points_at_langfuse_prompt(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    row = db.execute(
        "SELECT langfuse_prompt_name FROM eval_template WHERE name='card-prioritizer-v0'"
    ).fetchone()
    assert row is not None
    assert row[0] == "judge-instructions"


def test_output_definition_no_longer_carries_instructions(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    raw = db.execute(
        "SELECT output_definition FROM eval_template WHERE name='card-prioritizer-v0'"
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


def test_init_db_seeds_human_and_top_three_llm_configs(fresh_fabric):
    import judgement

    # Re-running init exercises the existing-database sync path and must not
    # create duplicate raters.
    judgement.init_db()
    db = sqlite3.connect(str(fresh_fabric))
    rows = db.execute("""
        SELECT c.rater_type, json_extract(c.rater_config, '$.model') AS model
        FROM job_configuration c
        JOIN eval_template t ON t.id = c.template_id
        WHERE t.name='card-prioritizer-v0' AND c.status='active'
    """).fetchall()
    assert sorted(model for rater_type, model in rows if rater_type == "llm") == [
        "anthropic/claude-opus-5",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
    ]
    assert sum(rater_type == "human" for rater_type, _model in rows) == 1
