"""Tests for bin/optimize.py — GEPA loop reading from fabric, writing to Langfuse."""
import json
import sqlite3
import uuid
from pathlib import Path

import dspy
import pytest

from tests.conftest import make_evaluation_summary

def _seed_human_judgements(fabric_path, n: int, domain_name=None):
    """Create `n` done human-judgement rows in the fabric for the seeded rubric."""
    db = sqlite3.connect(str(fabric_path))
    cfg_id = db.execute(
        "SELECT id FROM job_configuration WHERE rater_type='human'"
    ).fetchone()[0]
    tpl_id = db.execute(
        "SELECT template_id FROM job_configuration WHERE id=?", (cfg_id,)
    ).fetchone()[0]
    domain_id = None
    if domain_name is not None:
        domain_id = db.execute(
            "INSERT INTO domain(name, description, generator_prompt, judge_prompt, corpus_source) "
            "VALUES (?, '', ?, ?, '{\"kind\":\"inline\",\"items\":[]}')",
            (
                domain_name,
                f"card-generator:{domain_name}",
                f"judge-instructions:{domain_name}",
            ),
        ).lastrowid
    verdicts = ["a-wins-big", "b-wins-big", "tie", "a-wins", "b-wins"]
    for i in range(n):
        v = verdicts[i % len(verdicts)]
        rid = str(uuid.uuid4())
        payload = {
            "card_a": {"title": f"A{i}", "body": f"body of card A #{i}"},
            "card_b": {"title": f"B{i}", "body": f"body of card B #{i}"},
        }
        pending_id = db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, status, rating_id, completed_at, domain_id) "
            "VALUES (?, '/synth.db', ?, ?, 'done', ?, datetime('now'), ?)",
            (cfg_id, i, json.dumps(payload), rid, domain_id),
        ).lastrowid
        for name, val in [("judgement.verdict", v), ("judgement.confidence", "high")]:
            db.execute(
                "INSERT INTO score(rating_id, pending_id, template_id, "
                " rubric_version, name, data_type, value, metadata, "
                " tournament_db_path, match_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, pending_id, tpl_id, 1, name, "CATEGORICAL", val,
                 json.dumps({"rater": {"type": "human", "userId": "tester"}}),
                 "/synth.db", i),
            )
    db.commit()
    db.close()

@pytest.fixture
def seeded_fabric(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home / "judgements.db"

def test_load_trainset_filters_to_human_only(seeded_fabric, fake_langfuse, monkeypatch):
    _seed_human_judgements(seeded_fabric, n=3)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin.optimize import load_trainset
    trainset = load_trainset(rubric="pair-wheel-v2")
    assert len(trainset) == 3
    for ex in trainset:
        assert hasattr(ex, "verdict")
        assert hasattr(ex, "card_a_title")

def test_load_trainset_examples_have_inputs_marked(seeded_fabric):
    _seed_human_judgements(seeded_fabric, n=2)
    from bin.optimize import load_trainset
    trainset = load_trainset(rubric="pair-wheel-v2")
    inputs = trainset[0].inputs()
    assert {
        "card_a_title", "card_a_body", "card_a_source_ref",
        "card_b_title", "card_b_body", "card_b_source_ref",
    } <= set(inputs.keys())

def test_load_trainset_scopes_category_specific_examples_by_domain(seeded_fabric):
    _seed_human_judgements(seeded_fabric, n=2, domain_name="security-review")
    _seed_human_judgements(seeded_fabric, n=3, domain_name="style-review")

    from bin.optimize import load_trainset

    security = load_trainset(
        rubric="pair-wheel-v2", domain="security-review"
    )
    style = load_trainset(rubric="pair-wheel-v2", domain="style-review")

    assert len(security) == 2
    assert len(style) == 3

def test_partition_examples_is_reproducible_and_leakage_free(seeded_fabric):
    _seed_human_judgements(seeded_fabric, n=9)
    from bin.optimize import load_trainset, partition_examples

    examples = load_trainset()
    first = partition_examples(examples, seed=17)
    second = partition_examples(examples, seed=17)
    first_ids = [[item.example_id for item in split] for split in (first.train, first.validation, first.holdout)]
    second_ids = [[item.example_id for item in split] for split in (second.train, second.validation, second.holdout)]

    assert first_ids == second_ids
    fingerprints = [
        {item.example_fingerprint for item in split}
        for split in (first.train, first.validation, first.holdout)
    ]
    assert fingerprints[0].isdisjoint(fingerprints[1])
    assert fingerprints[0].isdisjoint(fingerprints[2])
    assert fingerprints[1].isdisjoint(fingerprints[2])

def test_pair_fingerprint_ignores_a_b_orientation():
    from bin.optimize import _example_fingerprint

    a = {"title": "A", "body": "alpha", "source_ref": "a.cs:1"}
    b = {"title": "B", "body": "beta", "source_ref": "b.cs:2"}
    assert _example_fingerprint(a, b) == _example_fingerprint(b, a)

def test_scope_deltas_forces_all_deltas_into_active_run_domain():
    from bin.optimize import _scope_deltas

    deltas = [
        {"section": "strategy", "content": "a lesson without explicit domain"},
        {"section": "strategy", "content": "already scoped", "domain": "other"},
    ]
    scoped = _scope_deltas(deltas, "explorer-bugs")
    assert scoped[0]["domain"] == "explorer-bugs"
    assert scoped[1]["domain"] == "explorer-bugs"
    assert _scope_deltas(deltas, "") is deltas

def test_curate_context_threads_active_domain_into_rendered_playbook(monkeypatch):
    """Regression: dropping domain= from the merge would silently disable
    playbook boundaries while every other test stayed green."""
    from bin.optimize import _curate_context

    delta = {
        "entries": [
            {
                "section": "strategy",
                "content": "A domain lesson about deterministic reproduction of link bugs.",
            }
        ]
    }
    lm = dspy.utils.DummyLM([{"delta_json": json.dumps(delta)}])
    prompt, changes, deltas = _curate_context(
        "Seed rubric.", "Evolved.", {}, lm, provenance="run-1", domain="explorer-bugs"
    )
    assert changes["added"] == 1
    assert changes["removed"] == 0
    assert "domain=explorer-bugs" in prompt
    assert deltas[0]["domain"] == "explorer-bugs"

def test_playbook_change_stats_counts_ops_not_net_growth():
    """A merge that retires 2 and adds 1 nets -1 entries but IS effective change."""
    from bin.context_playbook import merge_entries
    from bin.optimize import _playbook_change_stats

    existing = merge_entries(
        [],
        [
            {"section": "strategy", "content": "First stale lesson destined for retirement in this test."},
            {"section": "strategy", "content": "Second stale lesson destined for retirement in this test."},
        ],
    )
    merged = merge_entries(
        existing,
        [
            {"op": "retire", "id": existing[0].id},
            {"op": "retire", "id": existing[1].id},
            {"section": "strategy", "content": "A single new lesson replacing the two retired ones."},
        ],
    )
    assert len(merged) - len(existing) == -1
    stats = _playbook_change_stats(existing, merged)
    assert stats == {"added": 1, "removed": 2, "reinforced": 0, "weakened": 0}
    assert sum(stats.values()) > 0

def test_scoped_curate_excludes_foreign_domain_entries_from_rendered_prompt():
    """A seed carrying another domain's lesson must not leak it into a
    domain-scoped candidate prompt (entries_for_domain wired at render time)."""
    from bin.context_playbook import merge_entries, render_prompt
    from bin.optimize import _curate_context

    seed_entries = merge_entries(
        [],
        [{"section": "strategy", "content": "A style-review lesson about imperative mood in subjects."}],
        domain="style-review",
    )
    seed_prompt = render_prompt("Seed rubric.", seed_entries)
    assert "domain=style-review" in seed_prompt

    delta = {
        "entries": [
            {"section": "strategy", "content": "An explorer lesson about deterministic link reproduction."}
        ]
    }
    lm = dspy.utils.DummyLM([{"delta_json": json.dumps(delta)}])
    prompt, changes, _ = _curate_context(
        seed_prompt, "Evolved.", {}, lm, provenance="run-2", domain="explorer-bugs"
    )
    assert "domain=explorer-bugs" in prompt
    assert "domain=style-review" not in prompt
    assert changes["added"] == 1
    assert changes["foreign_excluded"] == 1
    assert changes["removed"] == 0

def test_metric_exact_match_returns_one():
    from bin.optimize import verdict_match_metric
    ex = dspy.Example(verdict="a-wins-big").with_inputs("card_a_title")
    pred = type("P", (), {"verdict": "a-wins-big"})()
    result = verdict_match_metric(ex, pred, trace=None)
    assert result.score == 1.0
    assert "exactly matches" in result.feedback

def test_metric_same_side_different_strength_returns_partial():
    from bin.optimize import verdict_match_metric
    ex = dspy.Example(verdict="a-wins-big").with_inputs("card_a_title")
    pred = type("P", (), {"verdict": "a-wins"})()
    result = verdict_match_metric(ex, pred, trace=None)
    assert result.score == 0.6
    assert "strength/quality" in result.feedback

def test_metric_opposite_sides_returns_zero():
    from bin.optimize import verdict_match_metric
    ex = dspy.Example(verdict="a-wins-big").with_inputs("card_a_title")
    pred = type("P", (), {"verdict": "b-wins-big"})()
    result = verdict_match_metric(ex, pred, trace=None)
    assert result.score == 0.0
    assert "direction is wrong" in result.feedback

def test_metric_tie_has_no_strength_variant_so_only_an_exact_tie_scores():
    """One tie replaced the both-strong/both-weak pair, so the tie bucket
    holds a single label: an exact match or nothing."""
    from bin.optimize import verdict_match_metric, VERDICTS

    assert {v for v in VERDICTS if v.startswith("tie")} == {"tie"}
    ex = dspy.Example(verdict="tie").with_inputs("card_a_title")
    assert verdict_match_metric(
        ex, type("P", (), {"verdict": "tie"})(), trace=None).score == 1.0
    assert verdict_match_metric(
        ex, type("P", (), {"verdict": "a-wins"})(), trace=None).score == 0.0

def test_metric_grades_against_the_rubrics_own_enum():
    """A verdict the seeded rubric declares is never 'invalid' to the metric,
    and one it retired always is."""
    from bin import judgement
    from bin.optimize import (
        VERDICTS,
        THE_OPTIMIZER_GRADES_AGAINST_THE_RUBRIC_THE_JUDGE_WAS_HANDED,
        verdict_match_metric,
    )

    assert VERDICTS == set(
        judgement.PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"]
    ), THE_OPTIMIZER_GRADES_AGAINST_THE_RUBRIC_THE_JUDGE_WAS_HANDED

    ex = dspy.Example(verdict="a-wins-big").with_inputs("card_a_title")
    retired = verdict_match_metric(
        ex, type("P", (), {"verdict": "a-clearly-better"})(), trace=None)
    assert retired.score == 0.0 and "is invalid" in retired.feedback

class _StubCompiled:
    """Mimics a compiled-by-GEPA DSPy module just enough to extract its prompt."""
    def __init__(self, instructions):
        seed_sig = type("Sig", (), {"instructions": "SEED instructions"})()
        optimized_sig = type("Sig", (), {"instructions": instructions})()
        predictor = type("P", (), {"signature": optimized_sig})()
        self.signature = seed_sig
        self.predictor = predictor

_stub_summary = make_evaluation_summary

def _stub_successful_pipeline(monkeypatch, optimized="BETTER instructions for the judge"):
    calls = iter([0.0, 1.0])
    monkeypatch.setattr(
        "bin.optimize._compile_with_gepa",
        lambda program, trainset, metric, **kw: _StubCompiled(optimized),
    )
    monkeypatch.setattr(
        "bin.optimize._curate_context",
        lambda seed, evolved, evidence, lm, **kw: (seed + "\n\nCURATED " + evolved, {"added": 1, "removed": 0, "reinforced": 0, "weakened": 0}, [{"section": "strategy", "content": "A sufficiently detailed reusable lesson for tests."}]),
    )
    monkeypatch.setattr(
        "bin.optimize._evaluate_program",
        lambda program, examples: _stub_summary(examples, next(calls)),
    )

def _allow_tiny_eval_sets(monkeypatch):
    """Tests seeding <7 examples opt out of the default 2/2 evidence gate."""
    monkeypatch.setenv("OPTIMIZER_MIN_VALIDATION", "1")
    monkeypatch.setenv("OPTIMIZER_MIN_HOLDOUT", "1")

def test_run_aborts_when_trainset_too_small(seeded_fabric, fake_langfuse, monkeypatch):
    _seed_human_judgements(seeded_fabric, n=2)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import optimize
    with pytest.raises(RuntimeError, match="need at least"):
        optimize.run(rubric="pair-wheel-v2", min_trainset=5)

def test_run_retains_seed_on_insufficient_evidence_without_spending_lm_budget(
    seeded_fabric, fake_langfuse, monkeypatch
):
    """Default 2/2 gate: 5 examples yield 1-item eval splits -> no LM is built."""
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    monkeypatch.delenv("OPTIMIZER_MIN_VALIDATION", raising=False)
    monkeypatch.delenv("OPTIMIZER_MIN_HOLDOUT", raising=False)

    def _fail_build(*a, **kw):
        raise AssertionError("LM must not be built on the insufficient-evidence path")

    monkeypatch.setattr("bin.optimize._build_lm", _fail_build)
    monkeypatch.setattr("bin.optimize._build_reflection_lm", _fail_build)
    monkeypatch.setattr("bin.optimize._build_curator_lm", _fail_build)
    monkeypatch.setattr("bin.optimize._compile_with_gepa", _fail_build)
    monkeypatch.setattr("bin.optimize._evaluate_program", _fail_build)

    from bin import optimize
    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")

    assert result.accepted is False
    assert result.decision == "insufficient-evidence"
    assert result.candidate_version is None
    reason = result.gepa["insufficient_evidence_reason"]
    assert "validation=1 (min 2)" in reason and "holdout=1 (min 2)" in reason
    manifest = json.loads((Path(result.artifact_dir) / "result.json").read_text())
    assert manifest["decision"] == "insufficient-evidence"
    assert manifest["gepa"]["insufficient_evidence_reason"] == reason

def test_run_pushes_candidate_prompt_to_langfuse(seeded_fabric, fake_langfuse, monkeypatch):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_successful_pipeline(monkeypatch)
    fake_langfuse.enable("set_label")
    from bin import optimize
    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert result.accepted is True
    assert result.baseline["score"] == 0.0
    assert result.candidate["score"] == 1.0
    assert result.candidate_version >= 2
    assert "BETTER" in fake_langfuse.get_prompt("judge-instructions", label="candidate").prompt

def test_run_emits_progress_lines_to_stdout(seeded_fabric, fake_langfuse, monkeypatch, capsys):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_successful_pipeline(monkeypatch, optimized="X")
    fake_langfuse.enable("set_label")
    from bin import optimize
    optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    out = capsys.readouterr().out
    assert "[optimize] loaded 5 valid human examples" in out
    assert "[optimize] starting GEPA" in out
    assert "accepted measured improvement; candidate v" in out
    assert "untouched holdout" in out

def test_run_retains_seed_on_plateau(seeded_fabric, fake_langfuse, monkeypatch):
    _seed_human_judgements(seeded_fabric, n=7)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    monkeypatch.setattr(
        "bin.optimize._compile_with_gepa",
        lambda program, trainset, metric, **kw: _StubCompiled("changed"),
    )
    monkeypatch.setattr(
        "bin.optimize._curate_context",
        lambda seed, evolved, evidence, lm, **kw: (seed + "\nCURATED", {"added": 1, "removed": 0, "reinforced": 0, "weakened": 0}, [{"section": "strategy", "content": "A detailed reusable lesson for plateau testing."}]),
    )
    monkeypatch.setattr(
        "bin.optimize._evaluate_program",
        lambda program, examples: _stub_summary(examples, 1.0),
    )
    from bin import optimize

    result = optimize.run(min_trainset=7, max_metric_calls=8)

    assert result.accepted is False
    assert result.decision == "plateau"
    assert result.candidate_version is None

def test_run_threads_explicit_models_to_lm_builders(seeded_fabric, fake_langfuse, monkeypatch):
    """Passing model= and reflection_model= should build dspy.LMs with those ids."""
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("set_label")

    captured = {}
    from bin import optimize as _opt
    real_build_lm = _opt._build_lm
    real_build_reflection = _opt._build_reflection_lm
    real_build_curator = _opt._build_curator_lm

    def spy_build_lm(model=None):
        captured["judge_model_arg"] = model
        return real_build_lm(model=model)

    def spy_build_reflection_lm(model=None):
        captured["reflection_model_arg"] = model
        return real_build_reflection(model=model)

    def spy_build_curator_lm(model=None):
        captured["curator_model_arg"] = model
        return real_build_curator(model=model)

    monkeypatch.setattr("bin.optimize._build_lm", spy_build_lm)
    monkeypatch.setattr("bin.optimize._build_reflection_lm", spy_build_reflection_lm)
    monkeypatch.setattr("bin.optimize._build_curator_lm", spy_build_curator_lm)
    _allow_tiny_eval_sets(monkeypatch)
    _stub_successful_pipeline(monkeypatch, optimized="X")
    result = _opt.run(
        rubric="pair-wheel-v2", min_trainset=3, auto="light",
        model="judge-alpha", reflection_model="reflect-beta", curator_model="curate-gamma",
    )
    assert captured["judge_model_arg"] == "judge-alpha"
    assert captured["reflection_model_arg"] == "reflect-beta"
    assert captured["curator_model_arg"] == "curate-gamma"
    assert result.judge_model == "openai/judge-alpha"
    assert result.reflection_model == "openai/reflect-beta"
    assert result.curator_model == "openai/curate-gamma"

def test_build_lm_applies_bounded_request_timeout_and_retries(monkeypatch):
    from bin import optimize

    captured = {}
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("LLM_NUM_RETRIES", "2")
    monkeypatch.setattr(
        optimize.dspy,
        "LM",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    optimize._build_lm(model="test-model")

    assert captured["timeout"] == 17.0
    assert captured["num_retries"] == 2
    assert captured["max_tokens"] == 16000

def _stub_pipeline_with_scores(monkeypatch, baseline_score, candidate_score):
    scores = iter([baseline_score, candidate_score])
    monkeypatch.setattr(
        "bin.optimize._compile_with_gepa",
        lambda program, trainset, metric, **kw: _StubCompiled("EVOLVED"),
    )
    monkeypatch.setattr(
        "bin.optimize._curate_context",
        lambda seed, evolved, evidence, lm, **kw: (
            seed + "\nCURATED",
            {"added": 1, "removed": 0, "reinforced": 0, "weakened": 0},
            [{"section": "strategy", "content": "A detailed reusable lesson for margin tests."}],
        ),
    )
    monkeypatch.setattr(
        "bin.optimize._evaluate_program",
        lambda program, examples: _stub_summary(examples, next(scores)),
    )

def test_margin_resolution_floor_names_the_holdout_the_margin_needs():
    from bin.optimize import margin_resolution_floor

    assert margin_resolution_floor(0.01) == 100
    assert margin_resolution_floor(0.05) == 20
    assert margin_resolution_floor(0.5) == 2
    assert margin_resolution_floor(0.0) == 0

def test_margin_policy_reports_which_bound_binds():
    from bin.optimize import margin_policy

    thin = margin_policy(min_improvement=0.01, source="default", holdout_size=4)
    assert thin["resolution_floor_examples"] == 100
    assert thin["holdout_resolution"] == 0.25
    assert thin["effective_min_improvement"] == 0.25
    assert thin["binding"] == "holdout-resolution"

    thick = margin_policy(min_improvement=0.2, source="caller", holdout_size=200)
    assert thick["effective_min_improvement"] == 0.2
    assert thick["binding"] == "configured-margin"

def test_margin_provenance_is_stored_on_the_run_row(
    seeded_fabric, fake_langfuse, monkeypatch
):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_successful_pipeline(monkeypatch)
    fake_langfuse.enable("set_label")
    from bin import optimize

    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert result.accepted is True
    assert result.margin["min_improvement"] == optimize.DEFAULT_MIN_IMPROVEMENT
    assert result.margin["source"] == "default"
    assert result.margin["resolution_floor_examples"] == 100
    assert result.margin["holdout_examples"] == result.holdout_size
    assert result.margin["binding"] == "holdout-resolution"
    assert result.margin["improvement"] == 1.0
    manifest = json.loads((Path(result.artifact_dir) / "result.json").read_text())
    assert manifest["margin"]["source"] == "default"
    assert manifest["margin"]["resolution_floor_examples"] == 100

def test_margin_provenance_distinguishes_caller_and_environment(
    seeded_fabric, fake_langfuse, monkeypatch
):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    fake_langfuse.enable("set_label")
    from bin import optimize

    _stub_successful_pipeline(monkeypatch)
    caller = optimize.run(
        rubric="pair-wheel-v2", min_trainset=3, auto="light",
        min_improvement=0.5,
    )
    assert caller.margin == {**caller.margin, "source": "caller", "min_improvement": 0.5}

    monkeypatch.setenv("OPTIMIZER_MIN_IMPROVEMENT", "0.25")
    _stub_successful_pipeline(monkeypatch)
    from_env = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert from_env.margin["source"] == "env:OPTIMIZER_MIN_IMPROVEMENT"
    assert from_env.margin["min_improvement"] == 0.25
    assert from_env.margin["resolution_floor_examples"] == 4

def test_margin_records_the_conjunct_that_decided_acceptance(
    seeded_fabric, fake_langfuse, monkeypatch
):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_successful_pipeline(monkeypatch)
    fake_langfuse.enable("set_label")
    from bin import optimize

    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert result.decision == "accepted"
    assert result.deciding_conjunct == "all-conjuncts-hold"
    assert result.conjuncts == {
        "effective_change": True,
        "improvement_margin": True,
        "exact_accuracy": True,
        "invalid_rate": True,
    }
    assert set(result.conjuncts) == set(optimize.ACCEPTANCE_CONJUNCTS)

def test_margin_records_the_conjunct_that_decided_a_regression(
    seeded_fabric, fake_langfuse, monkeypatch
):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_pipeline_with_scores(monkeypatch, 1.0, 0.0)
    from bin import optimize

    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert result.accepted is False
    assert result.decision == "regression"
    assert result.deciding_conjunct == "exact_accuracy"
    assert result.conjuncts["improvement_margin"] is False
    assert result.candidate_version is None

def test_margin_below_one_holdout_example_is_insufficient_evidence(
    seeded_fabric, fake_langfuse, monkeypatch, capsys
):
    """0.02 clears min_improvement=0.01 but a 1-example holdout cannot
    resolve anything finer than 1.0 — an unmeasurable win, not a small one."""
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)
    _stub_pipeline_with_scores(monkeypatch, 0.50, 0.52)
    from bin import optimize

    result = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert result.improvement == pytest.approx(0.02)
    assert result.improvement >= result.margin["min_improvement"]
    assert result.holdout_size < result.margin["resolution_floor_examples"]
    assert result.decision == "insufficient-evidence"
    assert result.accepted is False
    assert result.candidate_version is None
    assert result.deciding_conjunct == "improvement_margin"
    assert result.margin["measurable"] is False
    assert "insufficient evidence, not a small win" in capsys.readouterr().out

def test_configured_margin_evidence_floor_refuses_before_spending_budget(
    seeded_fabric, fake_langfuse, monkeypatch
):
    _seed_human_judgements(seeded_fabric, n=5)
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    _allow_tiny_eval_sets(monkeypatch)

    def _fail_build(*a, **kw):
        raise AssertionError("no LM budget may be spent below the evidence floor")

    for name in ("_build_lm", "_build_reflection_lm", "_build_curator_lm",
                 "_compile_with_gepa", "_evaluate_program"):
        monkeypatch.setattr(f"bin.optimize.{name}", _fail_build)
    from bin import optimize

    result = optimize.run(
        rubric="pair-wheel-v2", min_trainset=3, auto="light",
        margin_evidence_floor=25,
    )
    assert result.decision == "insufficient-evidence"
    assert result.accepted is False
    assert "margin evidence floor 25" in result.gepa["insufficient_evidence_reason"]
    assert result.margin["evidence_floor"] == 25
    assert result.margin["evidence_floor_source"] == "caller"

    monkeypatch.setenv("OPTIMIZER_MARGIN_EVIDENCE_FLOOR", "30")
    from_env = optimize.run(rubric="pair-wheel-v2", min_trainset=3, auto="light")
    assert from_env.decision == "insufficient-evidence"
    assert from_env.margin["evidence_floor_source"] == "env:OPTIMIZER_MARGIN_EVIDENCE_FLOOR"
