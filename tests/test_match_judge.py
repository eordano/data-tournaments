"""Tests for bin/judges/match_judge.py — DSPy program for the LLM judge."""
import dspy
import pytest

from tests.conftest import _scripted_lm


def test_signature_inputs_match_card_shape():
    from bin.judges.match_judge import JudgeCardSig
    inputs = set(JudgeCardSig.input_fields)
    outputs = set(JudgeCardSig.output_fields)
    assert {
        "card_a_title", "card_a_body", "card_a_source_ref",
        "card_b_title", "card_b_body", "card_b_source_ref",
    } <= inputs
    assert outputs == {"rationale", "confidence", "verdict"}


def test_module_loads_prompt_from_langfuse_at_init(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt(
        "judge-instructions",
        text="You are a triage judge for cards.",
        version=1,
        labels=["production"],
    )
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin.judges.match_judge import MatchJudge
    j = MatchJudge()
    assert "triage judge" in j.signature.instructions.lower()


def test_module_uses_candidate_label_when_requested(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("judge-instructions", text="prod text", version=1, labels=["production"])
    fake_langfuse.add_prompt("judge-instructions", text="candidate text", version=2, labels=["candidate"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin.judges.match_judge import MatchJudge
    j = MatchJudge(prompt_label="candidate")
    assert "candidate text" in j.signature.instructions


def test_forward_returns_named_fields(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("judge-instructions", text="x", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    dspy.settings.configure(lm=_scripted_lm({
        "rationale": "Card A is more concrete.",
        "confidence": "high",
        "verdict": "a-clearly-better",
    }))
    from bin.judges.match_judge import MatchJudge
    j = MatchJudge()
    result = j(
        card_a_title="Cyclomatic hotspot in handler.py",
        card_a_body="`process_request` has 14 branches; consider state machine.",
        card_a_source_ref="src/handler.py:20",
        card_b_title="README mentions Python 3.10",
        card_b_body="Could pin in pyproject.toml too.",
        card_b_source_ref="README.md:4",
    )
    assert result.verdict == "a-clearly-better"
    assert result.confidence == "high"
    assert "concrete" in result.rationale


def test_forward_validates_verdict_against_rubric_enum(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("judge-instructions", text="x", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    dspy.settings.configure(lm=_scripted_lm({
        "rationale": "?",
        "confidence": "mid",
        "verdict": "🤷",
    }))
    from bin.judges.match_judge import MatchJudge
    j = MatchJudge()
    with pytest.raises(ValueError, match="not in rubric enum"):
        j(card_a_title="t", card_a_body="b", card_b_title="t2", card_b_body="b2")


def test_run_for_pending_uses_match_judge(tmp_data_home, fake_langfuse, monkeypatch):
    """Existing run_llm_judge_for_pending() routes through MatchJudge end-to-end."""
    fake_langfuse.add_prompt("judge-instructions", text="x", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    dspy.settings.configure(lm=_scripted_lm({
        "rationale": "A wins.", "confidence": "mid", "verdict": "a-marginally-better",
    }))

    import importlib, judgement, json, sqlite3
    importlib.reload(judgement)
    judgement.init_db()

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='llm'").fetchone()[0]
    db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) "
        "VALUES (?, ?, ?, ?)",
        (cfg_id, "/tmp/synthetic.db", 1, json.dumps({
            "label": "R1-1",
            "card_a": {
                "title": "Hotspot", "body": "process_request has 14 branches",
                "source_ref": "src/handler.py",
            },
            "card_b": {
                "title": "Doc fix", "body": "Pin python in pyproject",
                "source_ref": "README.md",
            },
        }))
    )
    pid = db.execute("SELECT id FROM pending_judgement").fetchone()[0]
    db.commit()
    db.close()

    rid = judgement.run_llm_judge_for_pending(pid)
    assert rid is not None
    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    by_name = dict(db.execute(
        "SELECT name, value FROM score WHERE rating_id=?", (rid,)
    ).fetchall())
    assert by_name["judgement.verdict"] == "a-marginally-better"
    assert by_name["judgement.confidence"] == "mid"


def test_domain_pending_uses_its_category_specific_judge_prompt(
    tmp_data_home, fake_langfuse, monkeypatch
):
    """A domain pair must not silently use the global generic judge brief."""
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")

    import importlib
    import json
    import sqlite3
    from types import SimpleNamespace

    import judgement

    importlib.reload(judgement)
    judgement.init_db()

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    cfg_id = db.execute(
        "SELECT id FROM job_configuration WHERE rater_type='llm'"
    ).fetchone()[0]
    domain_id = db.execute(
        "INSERT INTO domain(name, description, generator_prompt, judge_prompt, corpus_source) "
        "VALUES ('security-review', 'Find security risks', "
        "'card-generator:security-review', 'judge-instructions:security-review', "
        "'{\"kind\":\"inline\",\"items\":[]}')"
    ).lastrowid
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)",
        (
            cfg_id,
            "domain:security-review",
            1,
            json.dumps(
                {
                    "card_a": {"title": "A", "body": "a"},
                    "card_b": {"title": "B", "body": "b"},
                }
            ),
            domain_id,
        ),
    ).lastrowid
    db.commit()
    db.close()

    seen = []
    seen_cards = []

    class RecordingJudge:
        def __init__(self, prompt_name, prompt_label="production"):
            seen.append((prompt_name, prompt_label))

        def __call__(self, **cards):
            seen_cards.append(cards)
            return SimpleNamespace(
                verdict="a-marginally-better",
                confidence="mid",
                rationale="A better matches the security lens.",
            )

    monkeypatch.setattr("bin.judges.match_judge.MatchJudge", RecordingJudge)

    assert judgement.run_llm_judge_for_pending(pid) is not None
    assert seen == [("judge-instructions:security-review", "production")]
    assert seen_cards[0]["card_a_source_ref"] == ""
    assert seen_cards[0]["card_b_source_ref"] == ""


def test_queue_rows_use_their_own_model_without_global_leak(
    tmp_data_home, fake_langfuse, monkeypatch
):
    """A multi-model drain must not reuse the first row's LM for later rows."""
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")

    import importlib
    import json
    import sqlite3
    from types import SimpleNamespace

    import judgement

    importlib.reload(judgement)
    judgement.init_db()
    dspy.settings.configure(lm=None)

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    template_id = db.execute(
        "SELECT id FROM eval_template WHERE name='card-prioritizer-v0'"
    ).fetchone()[0]
    db.execute("DELETE FROM job_configuration WHERE rater_type='llm'")
    for model in ("model-a", "model-b"):
        cfg_id = db.execute(
            "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
            "VALUES (?, 'llm', ?)",
            (template_id, json.dumps({"model": model})),
        ).lastrowid
        db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
            "trace_payload) VALUES (?, ?, ?, ?)",
            (
                cfg_id,
                f"domain:{model}",
                1,
                json.dumps(
                    {
                        "card_a": {"title": "A", "body": "a"},
                        "card_b": {"title": "B", "body": "b"},
                    }
                ),
            ),
        )
    db.commit()
    db.close()

    seen_lms = []

    class RecordingJudge:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, **_cards):
            seen_lms.append(dspy.settings.lm)
            return SimpleNamespace(
                verdict="tie-both-strong", confidence="mid", rationale="Comparable."
            )

    built = {}

    def build_lm(cfg):
        token = object()
        built[cfg["model"]] = token
        return token

    monkeypatch.setattr("bin.judges.match_judge.MatchJudge", RecordingJudge)
    monkeypatch.setattr(judgement, "_build_dspy_lm", build_lm)

    result = judgement.drain_llm_queue()

    assert result["ok"] == 2
    assert seen_lms == [built["model-a"], built["model-b"]]
    assert dspy.settings.lm is None
