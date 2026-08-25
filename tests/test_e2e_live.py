"""End-to-end smoke test against real langfuse.example + llm.example.

Skipped unless RUN_LIVE_TESTS=1. Run before declaring v1 done::

    RUN_LIVE_TESTS=1 nix develop --command pytest tests/test_e2e_live.py -v -s
"""
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to enable",
    ),
]


TEST_PROMPT_NAME = f"e2e-test-{uuid.uuid4().hex[:8]}"


def test_can_push_and_get_a_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_TOURNAMENTS_HOME", str(tmp_path / "dt"))
    sys.modules.pop("bin.prompts", None)
    from bin import prompts

    text = "live smoke prompt body — pick the better card"
    v = prompts.push(TEST_PROMPT_NAME, text, labels=["production"])
    assert v >= 1
    assert prompts.get(TEST_PROMPT_NAME) == text
    print(f"[e2e] pushed {TEST_PROMPT_NAME} v{v}")


def test_full_loop_judge_optimize_promote(monkeypatch, tmp_path):
    """Push baseline → seed fabric with synthetic human verdicts → run GEPA →
    candidate appears in Langfuse → promote → production label moves."""
    home = tmp_path / "dt"
    home.mkdir()
    monkeypatch.setenv("DATA_TOURNAMENTS_HOME", str(home))

    for mod in ("bin.prompts", "bin.judges.match_judge", "bin.optimize", "judgement"):
        sys.modules.pop(mod, None)

    import judgement
    from bin import optimize, prompts

    judgement.init_db()
    print(f"[e2e] init_db OK at {home}")

    # Seed enough examples for train / validation / untouched holdout.
    db = sqlite3.connect(str(home / "judgements.db"))
    cfg_id = db.execute(
        "SELECT id FROM job_configuration WHERE rater_type='human'"
    ).fetchone()[0]
    tpl_id = db.execute(
        "SELECT template_id FROM job_configuration WHERE id=?", (cfg_id,)
    ).fetchone()[0]

    cards = [
        ("Cyclomatic hotspot in handler.py: 14 branches",
         "README mentions Python 3.10",
         "a-clearly-better"),
        ("Typo in error message",
         "Auth token logged in plaintext",
         "b-clearly-better"),
        ("Unused import",
         "Race condition in cache eviction",
         "b-clearly-better"),
        ("Function name shadows builtin",
         "Misspelled variable in dead code path",
         "a-marginally-better"),
        ("README has stale install instructions",
         "Memory leak in long-running worker",
         "b-clearly-better"),
        ("Cancellation is ignored during shutdown",
         "A public API can silently corrupt its cache file",
         "b-clearly-better"),
        ("A crash has an exact source path and trigger",
         "A broad style concern has no source evidence",
         "a-clearly-better"),
    ]
    for i, (a, b, verdict) in enumerate(cards):
        rid = str(uuid.uuid4())
        payload = {
            "card_a": {"title": a, "body": f"{a} (details)"},
            "card_b": {"title": b, "body": f"{b} (details)"},
        }
        pid = db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, status, rating_id, completed_at) "
            "VALUES (?, '/synth.db', ?, ?, 'done', ?, datetime('now'))",
            (cfg_id, i, json.dumps(payload), rid),
        ).lastrowid
        for name, val in [("judgement.verdict", verdict), ("judgement.confidence", "high")]:
            db.execute(
                "INSERT INTO score(rating_id, pending_id, template_id, "
                "rubric_version, name, data_type, value, metadata, "
                "tournament_db_path, match_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, pid, tpl_id, 1, name, "CATEGORICAL", val,
                 json.dumps({"rater": {"type": "human", "userId": "e2e-bot"}}),
                 "/synth.db", i),
            )
    db.commit()
    db.close()
    print("[e2e] seeded 7 human judgements")

    # Run GEPA against the real LLM gateway.
    result = optimize.run(
        rubric="card-prioritizer-v0",
        max_metric_calls=16,
        min_trainset=7,
        prompt_name="judge-instructions",
    )
    print(f"[e2e] decision={result.decision} improvement={result.improvement:+.3f}")
    assert result.decision in {"accepted", "plateau", "regression"}
    assert result.trainset_size and result.validation_size and result.holdout_size

    if not result.accepted:
        assert result.candidate_version is None
        print("[e2e] conservative gate retained production")
        return

    assert result.candidate_version >= 2

    candidate_text = prompts.get("judge-instructions", label="candidate")
    production_text = prompts.get("judge-instructions", label="production")
    assert candidate_text != production_text
    assert "context-playbook:start" in candidate_text

    # Promote candidate → production.
    prompts.set_label("judge-instructions", result.candidate_version, "production")
    new_production = prompts.get("judge-instructions", label="production")
    assert new_production == candidate_text
    print(f"[e2e] promoted v{result.candidate_version} to production")
