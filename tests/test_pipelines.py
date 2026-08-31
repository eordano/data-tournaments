"""Tests for bin/pipelines.py — pipeline spec v1 (wave-12;
docs/design/judgement-wheel-v2.md §4).

Rubric templates are inserted via DIRECT SQL into eval_template so these
tests stay decoupled from the judgement-template slice's seeds.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

@pytest.fixture
def pipelines(tmp_data_home):
    from bin import pipelines as mod

    mod.init()
    return mod

@pytest.fixture
def raw(pipelines, tmp_data_home):
    """Raw connection for prerequisites / asserting stored values / triggers."""
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()

def _insert_template(raw, name: str, outdef: dict | None = None) -> None:
    """Direct SQL rubric prerequisite (decoupled from bin/judgement.py)."""
    raw.execute(
        "INSERT INTO eval_template(name, version, output_definition) "
        "VALUES (?, 1, ?)",
        (name, json.dumps(outdef or {"verdict_enum": ["yes", "no"]})),
    )
    raw.commit()

@pytest.fixture
def rubrics(raw):
    _insert_template(raw, "tiny-pair", {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "pair",
        "subjects": ["idea", "execution"],
    })
    _insert_template(raw, "tiny-single", {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "single",
        "subjects": ["idea", "execution"],
    })
    return {"pair": "tiny-pair", "single": "tiny-single"}

def _judgement(key, subject, judgement, rubric, **extra):
    return {"key": key, "subject": subject, "judgement": judgement,
            "rubric": rubric, **extra}

def _valid_definition(rubrics):
    return {
        "name": "tiny-flow",
        "stages": [
            _judgement("idea-compare", "idea", "pair", rubrics["pair"]),
            {"key": "author", "action": "branch_author"},
            _judgement("execution-each", "execution", "single",
                       rubrics["single"], foreach="branch"),
            {"key": "release", "action": "audited_release"},
        ],
    }

def test_register_valid_pipeline_v1_with_digest(pipelines, rubrics):
    out = pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    assert out["name"] == "tiny-flow"
    assert out["version"] == 1
    assert len(out["digest"]) == 64
    got = pipelines.get_pipeline("tiny-flow")
    assert got["version"] == 1
    assert got["digest"] == out["digest"]
    assert got["definition"]["stages"][0]["key"] == "idea-compare"

def test_reregister_same_name_bumps_version(pipelines, rubrics):
    defn = _valid_definition(rubrics)
    v1 = pipelines.register_pipeline("tiny-flow", defn)
    defn2 = _valid_definition(rubrics)
    defn2["stages"][0]["key"] = "idea-compare-2"
    v2 = pipelines.register_pipeline("tiny-flow", defn2)
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v1["digest"] != v2["digest"]
    assert pipelines.get_pipeline("tiny-flow")["version"] == 2
    assert pipelines.get_pipeline("tiny-flow", 1)["digest"] == v1["digest"]
    names = [(p["name"], p["version"]) for p in pipelines.list_pipelines()]
    assert names == [("tiny-flow", 1), ("tiny-flow", 2)]

def test_pipeline_rows_immutable(pipelines, rubrics, raw):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        raw.execute("UPDATE pipeline SET definition='{}' WHERE name='tiny-flow'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM pipeline WHERE name='tiny-flow'")

def test_get_pipeline_missing_raises(pipelines):
    with pytest.raises(LookupError):
        pipelines.get_pipeline("no-such-pipeline")

def test_unknown_rubric_refused(pipelines, rubrics):
    defn = {"stages": [_judgement("j", "idea", "pair", "no-such-rubric")]}
    with pytest.raises(ValueError, match="no existing"):
        pipelines.register_pipeline("bad", defn)

def test_stage_kind_mismatching_rubric_refused(pipelines, rubrics):
    defn = {"stages": [_judgement("j", "execution", "pair", rubrics["single"])]}
    with pytest.raises(ValueError, match="judgement_kind"):
        pipelines.register_pipeline("bad", defn)

def test_stage_subject_not_in_rubric_subjects_refused(pipelines, raw):
    _insert_template(raw, "exec-only-single", {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "single",
        "subjects": ["execution"],
    })
    defn = {"stages": [_judgement("j", "idea", "single", "exec-only-single")]}
    with pytest.raises(ValueError, match="not among rubric"):
        pipelines.register_pipeline("bad", defn)

def test_legacy_bare_rubric_normalizes_to_pair_execution(pipelines, raw):
    _insert_template(raw, "bare-legacy")
    ok = {"stages": [_judgement("j", "execution", "pair", "bare-legacy")]}
    assert pipelines.register_pipeline("legacy-ok", ok)["version"] == 1
    bad = {"stages": [_judgement("j", "idea", "pair", "bare-legacy")]}
    with pytest.raises(ValueError, match="not among rubric"):
        pipelines.register_pipeline("legacy-bad", bad)

def test_unknown_action_refused(pipelines):
    defn = {"stages": [{"key": "a", "action": "teleport"}]}
    with pytest.raises(ValueError, match="unknown action"):
        pipelines.register_pipeline("bad", defn)

def test_duplicate_stage_key_refused(pipelines, rubrics):
    defn = {"stages": [
        _judgement("same", "idea", "pair", rubrics["pair"]),
        {"key": "same", "action": "branch_author"},
    ]}
    with pytest.raises(ValueError, match="duplicate stage key"):
        pipelines.register_pipeline("bad", defn)

def test_empty_stages_refused(pipelines):
    with pytest.raises(ValueError, match="non-empty"):
        pipelines.register_pipeline("bad", {"stages": []})

def test_bad_subject_judgement_foreach_refused(pipelines, rubrics):
    with pytest.raises(ValueError, match="subject"):
        pipelines.register_pipeline(
            "bad", {"stages": [_judgement("j", "vibes", "pair", rubrics["pair"])]}
        )
    with pytest.raises(ValueError, match="judgement"):
        pipelines.register_pipeline(
            "bad", {"stages": [_judgement("j", "idea", "triple", rubrics["pair"])]}
        )
    with pytest.raises(ValueError, match="foreach"):
        pipelines.register_pipeline(
            "bad",
            {"stages": [_judgement("j", "idea", "pair", rubrics["pair"],
                                   foreach="galaxy")]},
        )

def test_stage_mixing_action_and_judgement_refused(pipelines, rubrics):
    defn = {"stages": [{"key": "x", "action": "branch_author",
                        "subject": "idea", "judgement": "pair",
                        "rubric": rubrics["pair"]}]}
    with pytest.raises(ValueError, match="mixes"):
        pipelines.register_pipeline("bad", defn)

def test_release_with_no_judgement_before_refused(pipelines, rubrics):
    defn = {"stages": [
        {"key": "author", "action": "branch_author"},
        {"key": "release", "action": "audited_release"},
    ]}
    with pytest.raises(ValueError, match="pipeline refused: release requires "
                                         "a single execution judgement gate"):
        pipelines.register_pipeline("bad", defn)

def test_release_preceded_only_by_pair_execution_refused(pipelines, rubrics):
    defn = {"stages": [
        _judgement("exec-compare", "execution", "pair", rubrics["pair"]),
        {"key": "release", "action": "audited_release"},
    ]}
    with pytest.raises(ValueError, match="pipeline refused: release requires "
                                         "a single execution judgement gate"):
        pipelines.register_pipeline("bad", defn)

def test_release_after_pair_idea_plus_single_execution_accepted(pipelines, rubrics):
    defn = {"stages": [
        _judgement("idea-compare", "idea", "pair", rubrics["pair"]),
        _judgement("execution-each", "execution", "single", rubrics["single"],
                   foreach="branch"),
        {"key": "release", "action": "audited_release"},
    ]}
    out = pipelines.register_pipeline("gated", defn)
    assert out["version"] == 1

def test_release_after_single_execution_plus_pair_execution_accepted(
    pipelines, rubrics
):
    defn = {"stages": [
        _judgement("execution-each", "execution", "single", rubrics["single"]),
        _judgement("exec-compare", "execution", "pair", rubrics["pair"]),
        {"key": "release", "action": "audited_release"},
    ]}
    out = pipelines.register_pipeline("gated2", defn)
    assert out["version"] == 1

def test_pipeline_without_release_needs_no_gate(pipelines, rubrics):
    defn = {"stages": [
        _judgement("idea-compare", "idea", "pair", rubrics["pair"]),
        {"key": "author", "action": "branch_author"},
        {"key": "validate", "action": "branch_validation"},
    ]}
    out = pipelines.register_pipeline("ungated-ok", defn)
    assert out["version"] == 1

def test_single_execution_after_release_does_not_count(pipelines, rubrics):
    defn = {"stages": [
        {"key": "release", "action": "audited_release"},
        _judgement("too-late", "execution", "single", rubrics["single"]),
    ]}
    with pytest.raises(ValueError, match="pipeline refused"):
        pipelines.register_pipeline("bad", defn)

def test_single_idea_judgement_does_not_satisfy_gate(pipelines, rubrics):
    defn = {"stages": [
        _judgement("idea-check", "idea", "single", rubrics["single"]),
        {"key": "release", "action": "audited_release"},
    ]}
    with pytest.raises(ValueError, match="pipeline refused"):
        pipelines.register_pipeline("bad", defn)

def test_digest_stable_under_key_reordering(pipelines, rubrics):
    stage_a = {"key": "j", "subject": "idea", "judgement": "pair",
               "rubric": rubrics["pair"]}
    stage_b = {"rubric": rubrics["pair"], "judgement": "pair",
               "subject": "idea", "key": "j"}
    d1 = pipelines.definition_digest({"name": "x", "stages": [stage_a]})
    d2 = pipelines.definition_digest({"stages": [stage_b], "name": "x"})
    assert d1 == d2
    r1 = pipelines.register_pipeline("reorder", {"name": "x", "stages": [stage_a]})
    r2 = pipelines.register_pipeline("reorder", {"stages": [stage_b], "name": "x"})
    assert r1["digest"] == r2["digest"]
    assert (r1["version"], r2["version"]) == (1, 2)

@pytest.fixture
def domain(pipelines):
    from bin import domains

    domains.create_domain(
        name="widget-lens",
        description="test lens",
        corpus_source={"kind": "inline", "items": []},
    )
    return "widget-lens"

def test_bind_and_get_binding_round_trip(pipelines, rubrics, domain):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    out = pipelines.bind_domain(domain, "tiny-flow")
    assert out["pipeline"] == "tiny-flow"
    assert out["version"] == 1
    binding = pipelines.get_domain_binding(domain)
    assert binding["pipeline"] == "tiny-flow"
    assert binding["version"] == 1
    assert binding["definition"]["stages"][0]["key"] == "idea-compare"

def test_binding_pins_version_at_bind_time(pipelines, rubrics, domain):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    pipelines.bind_domain(domain, "tiny-flow", version=1)
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    assert pipelines.get_domain_binding(domain)["version"] == 1

def test_unbound_domain_returns_none(pipelines, domain):
    assert pipelines.get_domain_binding(domain) is None

def test_double_bind_refused_binding_is_permanent(pipelines, rubrics, domain):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    pipelines.register_pipeline("other-flow", _valid_definition(rubrics))
    pipelines.bind_domain(domain, "tiny-flow")
    with pytest.raises(ValueError, match="already bound.*permanent"):
        pipelines.bind_domain(domain, "other-flow")
    binding = pipelines.get_domain_binding(domain)
    assert binding["pipeline"] == "tiny-flow"

def test_domain_pipeline_rows_immutable(pipelines, rubrics, domain, raw):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    pipelines.bind_domain(domain, "tiny-flow")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        raw.execute("UPDATE domain_pipeline SET pipeline_id=999")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM domain_pipeline")

def test_bind_unknown_domain_or_pipeline_raises(pipelines, rubrics):
    pipelines.register_pipeline("tiny-flow", _valid_definition(rubrics))
    with pytest.raises(LookupError, match="domain"):
        pipelines.bind_domain("no-such-domain", "tiny-flow")

def test_seed_defaults_requires_contract_rubrics(pipelines):
    with pytest.raises(ValueError, match="pair-idea-wheel-v2"):
        pipelines.seed_defaults()

def test_seed_defaults_registers_contract_pipeline(pipelines, raw):
    _insert_template(raw, "pair-idea-wheel-v2", {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "pair",
        "subjects": ["idea"],
    })
    _insert_template(raw, "single-execution-v1", {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "single",
        "subjects": ["execution"],
    })
    out = pipelines.seed_defaults()
    assert out["name"] == "branch-fix-review"
    assert out["version"] == 1
    got = pipelines.get_pipeline("branch-fix-review")
    keys = [s["key"] for s in got["definition"]["stages"]]
    assert keys == ["idea-compare", "author", "validate-each",
                    "execution-each", "release"]
    again = pipelines.seed_defaults()
    assert again["version"] == 1
    assert again["digest"] == out["digest"]

def test_cli_register_get_bind_show_binding(
    pipelines, rubrics, domain, tmp_path, capsys
):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_valid_definition(rubrics)))
    assert pipelines.main(
        ["register", "--name", "cli-flow", "--definition-file", str(spec)]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 1

    assert pipelines.main(["get", "--name", "cli-flow"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "cli-flow"

    assert pipelines.main(["list"]) == 0
    assert any(p["name"] == "cli-flow" for p in json.loads(capsys.readouterr().out))

    assert pipelines.main(["bind", "--domain", domain, "--pipeline", "cli-flow"]) == 0
    capsys.readouterr()
    assert pipelines.main(["show-binding", "--domain", domain]) == 0
    assert json.loads(capsys.readouterr().out)["pipeline"] == "cli-flow"
