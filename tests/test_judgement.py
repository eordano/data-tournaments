"""Wave-12 slice A: typed judgements, the semantic wheel, subject-aware ratings.

Covers docs/design/judgement-wheel-v2.md:
- output_definition v2 normalization (single code path for legacy templates);
- template registration validates the wheel (positions + verdicts vs enum);
- the pair-wheel-v2 / single-idea-v1 / single-execution-v1 seeds;
- subject-aware write_judgement (multi-subject rubrics write per-subject
  score rows under ONE rating_id; the pending row resolves exactly once);
- full backward compatibility for the legacy single-subject call shape.
"""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

@pytest.fixture
def judgement_mod(tmp_data_home, fake_langfuse, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    return judgement

def _db(tmp_data_home) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    return conn

def _pending_for_template(tmp_data_home, template_name: str, *,
                          payload: dict | None = None) -> int:
    """Insert one pending row against the template's human config."""
    db = _db(tmp_data_home)
    cfg_id = db.execute(
        "SELECT c.id FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE t.name=? AND c.rater_type='human' AND c.status='active'",
        (template_name,),
    ).fetchone()[0]
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload) VALUES (?, ?, ?, ?)",
        (cfg_id, "/tmp/wheel.db", 1,
         json.dumps(payload or {"label": "S1-1", "card": {"title": "T", "body": "b"}})),
    ).lastrowid
    db.commit()
    db.close()
    return pid

def test_normalize_legacy_outdef_defaults(judgement_mod):
    """Absent v2 keys mean pair / ['execution'] / no wheel — the exact
    semantics every pre-v2 template always had."""
    legacy = {"verdict_enum": ["a", "b"], "confidence_enum": ["low", "mid", "high"]}
    norm = judgement_mod.normalize_output_definition(legacy)
    assert norm["judgement_kind"] == "pair"
    assert norm["subjects"] == ["execution"]
    assert norm["wheel"] == {}
    assert norm["verdict_enum"] == ["a", "b"]
    assert "judgement_kind" not in legacy

def test_normalize_preserves_explicit_v2_keys(judgement_mod):
    outdef = {
        "verdict_enum": ["yes", "no"],
        "judgement_kind": "single",
        "subjects": ["idea"],
        "wheel": {"n": "yes", "s": "no"},
    }
    norm = judgement_mod.normalize_output_definition(outdef)
    assert norm["judgement_kind"] == "single"
    assert norm["subjects"] == ["idea"]
    assert norm["wheel"] == {"n": "yes", "s": "no"}

def test_get_template_normalizes_a_pre_v2_template(judgement_mod):
    """A template stored without the v2 keys flows through the single
    normalization path, and its stored JSON stays byte-identical."""
    judgement_mod.register_template(
        name="pre-v2-shape", version=1,
        output_definition={"verdict_enum": ["a-wins", "b-wins"],
                           "confidence_enum": ["low", "mid", "high"]},
    )
    outdef = judgement_mod.get_template("pre-v2-shape")["output_definition"]
    assert outdef["judgement_kind"] == "pair"
    assert outdef["subjects"] == ["execution"]
    assert outdef["wheel"] == {}
    raw = json.loads(
        _db(judgement_mod.DATA_HOME).execute(
            "SELECT output_definition FROM eval_template "
            "WHERE name='pre-v2-shape'"
        ).fetchone()[0]
    )
    assert "judgement_kind" not in raw and "wheel" not in raw

def test_list_pending_normalizes_output_definition(judgement_mod, tmp_data_home):
    pid = _pending_for_template(tmp_data_home, "pair-wheel-v2")
    rows = [p for p in judgement_mod.list_pending(rater_type="human", limit=100)
            if p["id"] == pid]
    assert rows and rows[0]["output_definition"]["judgement_kind"] == "pair"
    assert rows[0]["output_definition"]["subjects"] == ["execution"]

def test_register_template_rejects_wheel_verdict_not_in_enum(judgement_mod):
    with pytest.raises(ValueError, match="not in"):
        judgement_mod.register_template(
            name="bad-wheel", version=1,
            output_definition={
                "verdict_enum": ["good", "bad"],
                "wheel": {"n": "great"},
            },
        )

def test_register_template_rejects_unknown_wheel_position(judgement_mod):
    with pytest.raises(ValueError, match="position"):
        judgement_mod.register_template(
            name="bad-position", version=1,
            output_definition={
                "verdict_enum": ["good"],
                "wheel": {"nne": "good"},
            },
        )

def test_register_template_rejects_unknown_kind_and_subject(judgement_mod):
    with pytest.raises(ValueError, match="judgement_kind"):
        judgement_mod.validate_output_definition(
            {"verdict_enum": ["x"], "judgement_kind": "triple"}
        )
    with pytest.raises(ValueError, match="subjects"):
        judgement_mod.validate_output_definition(
            {"verdict_enum": ["x"], "subjects": ["vibes"]}
        )

def test_register_template_accepts_valid_wheel(judgement_mod):
    tpl_id = judgement_mod.register_template(
        name="ok-wheel", version=1,
        output_definition={
            "verdict_enum": ["up", "down", "skip"],
            "judgement_kind": "single",
            "subjects": ["execution"],
            "wheel": {"n": "up", "s": "down"},
        },
    )
    assert tpl_id > 0
    tpl = judgement_mod.get_template("ok-wheel")
    assert tpl["output_definition"]["wheel"] == {"n": "up", "s": "down"}

def test_the_one_pair_rubric_carries_the_seven_verdicts_on_the_wheel(judgement_mod):
    tpl = judgement_mod.get_template("pair-wheel-v2")
    outdef = tpl["output_definition"]
    assert tpl["name"] == judgement_mod.DEFAULT_TEMPLATE_NAME
    assert tpl["version"] == judgement_mod.PAIR_WHEEL_TEMPLATE_VERSION == 1, (
        judgement_mod.A_VOCABULARY_CHANGE_RENAMES_THE_RUBRIC_IT_NEVER_BUMPS_THE_VERSION
    )
    assert outdef["judgement_kind"] == "pair"
    assert outdef["subjects"] == ["execution"]
    assert outdef["verdict_enum"] == [
        "discard-a", "discard-b", "a-wins-big", "a-wins", "tie",
        "b-wins", "b-wins-big", "skip",
    ]
    assert outdef["wheel"] == {
        "n": "tie",
        "ne": "b-wins",
        "e": "b-wins-big",
        "se": "discard-b",
        "sw": "discard-a",
        "w": "a-wins-big",
        "nw": "a-wins",
    }
    assert outdef["confidence_enum"] == ["low", "mid", "high"]
    assert set(outdef["wheel"].values()) == set(outdef["verdict_enum"]) - {"skip"}, (
        judgement_mod.SKIP_IS_ON_THE_RUBRIC_BUT_OFF_THE_WHEEL_BECAUSE_IT_ESTABLISHES_NOTHING
    )

def test_south_is_empty_because_there_is_no_both_are_bad_verdict(judgement_mod):
    """The geometry states the rule: the horizontal axis names the side, the
    vertical says whether to surface it or eject it, and the bottom-centre
    position that used to eject BOTH cards is gone."""
    wheel = judgement_mod.get_template(
        "pair-wheel-v2")["output_definition"]["wheel"]
    assert "s" not in wheel, (
        judgement_mod.THERE_IS_DELIBERATELY_NO_BOTH_ARE_BAD_VERDICT_SO_SOUTH_STAYS_EMPTY
    )
    assert (wheel["sw"], wheel["se"]) == ("discard-a", "discard-b")
    assert (wheel["w"], wheel["nw"]) == ("a-wins-big", "a-wins")
    assert (wheel["e"], wheel["ne"]) == ("b-wins-big", "b-wins")

def test_the_idea_wheel_shares_the_exact_same_vocabulary(judgement_mod):
    """One vocabulary, not two: a second pair rubric that drifted is how a
    verdict ends up unscored by the engine."""
    execution = judgement_mod.get_template("pair-wheel-v2")["output_definition"]
    idea = judgement_mod.get_template("pair-idea-wheel-v2")["output_definition"]
    assert idea["verdict_enum"] == execution["verdict_enum"]
    assert idea["wheel"] == execution["wheel"]
    assert idea["subjects"] == ["idea"]

def test_single_seeds_present_with_axis_wheels(judgement_mod):
    idea = judgement_mod.get_template("single-idea-v1")["output_definition"]
    assert idea["judgement_kind"] == "single"
    assert idea["subjects"] == ["idea"]
    assert idea["verdict_enum"] == [
        "important", "promising", "needs-evidence",
        "not-worth-pursuing", "invalid", "skip",
    ]
    assert idea["wheel"] == {
        "n": "important", "ne": "promising",
        "se": "not-worth-pursuing", "s": "invalid",
    }
    assert "needs-evidence" not in idea["wheel"].values()
    assert "skip" not in idea["wheel"].values()

    execu = judgement_mod.get_template("single-execution-v1")["output_definition"]
    assert execu["judgement_kind"] == "single"
    assert execu["subjects"] == ["execution"]
    assert execu["verdict_enum"] == [
        "approve", "approve-with-notes", "revise", "reject-invalid", "skip",
    ]
    assert execu["wheel"] == {
        "n": "approve", "ne": "approve-with-notes",
        "se": "revise", "s": "reject-invalid",
    }

def test_wheel_seeds_are_idempotent_and_push_prompts(
    judgement_mod, fake_langfuse, tmp_data_home
):
    judgement_mod.init_db()
    judgement_mod.init_db()
    db = _db(tmp_data_home)
    for name in ("pair-wheel-v2", "pair-idea-wheel-v2", "single-idea-v1",
                 "single-execution-v1"):
        n = db.execute(
            "SELECT COUNT(*) FROM eval_template WHERE name=?", (name,)
        ).fetchone()[0]
        assert n == 1, f"{name} seeded {n} times"
        cfgs = sorted(c[0] for c in db.execute(
            "SELECT c.rater_type FROM job_configuration c "
            "JOIN eval_template t ON t.id = c.template_id "
            "WHERE t.name=? AND c.status='active'",
            (name,),
        ).fetchall())
        if name == judgement_mod.DEFAULT_TEMPLATE_NAME:
            assert cfgs == sorted(
                ["human"] + ["llm"] * len(judgement_mod.DEFAULT_JUDGE_PANEL_MODELS)
            ), "new work is judged by a person AND by the machine panel"
        else:
            assert cfgs == ["human"], (
                "only the rubric new work binds to fans out to the LLM panel"
            )
        assert fake_langfuse.versions(f"judge-instructions:{name}") == [1]
    assert db.execute(
        "SELECT COUNT(*) FROM eval_template WHERE name LIKE 'card-prioritizer%'"
    ).fetchone()[0] == 0, "the card-prioritizer rubrics are deleted, not aliased"
    db.close()

def test_wheel_seeds_added_to_preexisting_database(
    tmp_data_home, fake_langfuse, monkeypatch
):
    """A DB that predates a seed gains it on re-init."""
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    db = _db(tmp_data_home)
    ids = [r[0] for r in db.execute(
        "SELECT id FROM eval_template WHERE name IN "
        "('pair-wheel-v2','single-idea-v1','single-execution-v1')"
    )]
    qmarks = ",".join("?" for _ in ids)
    db.execute(f"DELETE FROM job_configuration WHERE template_id IN ({qmarks})", ids)
    db.execute(f"DELETE FROM eval_template WHERE id IN ({qmarks})", ids)
    db.commit()
    db.close()

    judgement.init_db()

    tpl = judgement.get_template("pair-wheel-v2")
    assert tpl["version"] == judgement.PAIR_WHEEL_TEMPLATE_VERSION
    assert tpl["output_definition"]["verdict_enum"][0] == "discard-a"

@pytest.fixture
def multi_subject_pending(judgement_mod, tmp_data_home):
    """A pending row against a registered idea+execution rubric."""
    tpl_id = judgement_mod.register_template(
        name="idea-and-execution-v1", version=1,
        output_definition={
            "verdict_enum": ["good", "bad", "skip"],
            "confidence_enum": ["low", "mid", "high"],
            "judgement_kind": "single",
            "subjects": ["idea", "execution"],
        },
    )
    db = _db(tmp_data_home)
    db.execute(
        "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
        "VALUES (?, 'human', '{}')",
        (tpl_id,),
    )
    db.commit()
    db.close()
    return _pending_for_template(tmp_data_home, "idea-and-execution-v1")

def test_multi_subject_requires_all_subjects(judgement_mod, multi_subject_pending):
    with pytest.raises(ValueError, match=r"missing required subjects.*execution"):
        judgement_mod.write_judgement(
            pending_id=multi_subject_pending,
            subject_verdicts={"idea": {"verdict": "good", "confidence": "mid"}},
            rater={"type": "human", "userId": "u1"},
        )
    db = _db(judgement_mod.DATA_HOME)
    status = db.execute(
        "SELECT status FROM pending_judgement WHERE id=?",
        (multi_subject_pending,),
    ).fetchone()[0]
    assert status == "pending"
    db.close()

def test_multi_subject_legacy_shape_is_refused(judgement_mod, multi_subject_pending):
    with pytest.raises(ValueError, match="subject_verdicts"):
        judgement_mod.write_judgement(
            pending_id=multi_subject_pending,
            verdict="good", confidence="mid",
            rater={"type": "human", "userId": "u1"},
        )

def test_multi_subject_rejects_undeclared_subject(judgement_mod, multi_subject_pending):
    with pytest.raises(ValueError, match="undeclared"):
        judgement_mod.write_judgement(
            pending_id=multi_subject_pending,
            subject_verdicts={
                "idea": {"verdict": "good", "confidence": "mid"},
                "execution": {"verdict": "good", "confidence": "mid"},
                "vibes": {"verdict": "good", "confidence": "mid"},
            },
            rater={"type": "human", "userId": "u1"},
        )

def test_multi_subject_validates_each_verdict_against_enum(
    judgement_mod, multi_subject_pending
):
    with pytest.raises(ValueError, match=r"subject 'execution'.*not in rubric enum"):
        judgement_mod.write_judgement(
            pending_id=multi_subject_pending,
            subject_verdicts={
                "idea": {"verdict": "good", "confidence": "mid"},
                "execution": {"verdict": "meh", "confidence": "mid"},
            },
            rater={"type": "human", "userId": "u1"},
        )

def test_multi_subject_writes_four_rows_under_one_rating(
    judgement_mod, multi_subject_pending, tmp_data_home
):
    rating_id = judgement_mod.write_judgement(
        pending_id=multi_subject_pending,
        subject_verdicts={
            "idea": {"verdict": "good", "confidence": "high",
                     "rationale": "solid premise"},
            "execution": {"verdict": "bad", "confidence": "mid"},
        },
        rater={"type": "human", "userId": "u1"},
    )
    db = _db(tmp_data_home)
    scores = db.execute(
        "SELECT rating_id, name, value, metadata FROM score WHERE pending_id=? "
        "ORDER BY id",
        (multi_subject_pending,),
    ).fetchall()
    prow = db.execute(
        "SELECT status, rating_id FROM pending_judgement WHERE id=?",
        (multi_subject_pending,),
    ).fetchone()
    db.close()

    assert len(scores) == 4
    assert {s["rating_id"] for s in scores} == {rating_id}
    by_name = {s["name"]: s for s in scores}
    assert set(by_name) == {
        "judgement.idea.verdict", "judgement.idea.confidence",
        "judgement.execution.verdict", "judgement.execution.confidence",
    }
    assert by_name["judgement.idea.verdict"]["value"] == "good"
    assert by_name["judgement.idea.confidence"]["value"] == "high"
    assert by_name["judgement.execution.verdict"]["value"] == "bad"
    assert by_name["judgement.execution.confidence"]["value"] == "mid"
    assert json.loads(
        by_name["judgement.idea.verdict"]["metadata"]
    )["rationale"] == "solid premise"
    assert "rationale" not in json.loads(
        by_name["judgement.execution.verdict"]["metadata"]
    )
    assert prow["status"] == "done"
    assert prow["rating_id"] == rating_id

def test_multi_subject_pending_resolves_exactly_once(
    judgement_mod, multi_subject_pending
):
    sv = {
        "idea": {"verdict": "good", "confidence": "mid"},
        "execution": {"verdict": "good", "confidence": "mid"},
    }
    judgement_mod.write_judgement(
        pending_id=multi_subject_pending, subject_verdicts=sv,
        rater={"type": "human", "userId": "u1"},
    )
    with pytest.raises(RuntimeError, match="already resolved"):
        judgement_mod.write_judgement(
            pending_id=multi_subject_pending, subject_verdicts=sv,
            rater={"type": "human", "userId": "u2"},
        )

def test_legacy_single_subject_call_shape_still_works(
    judgement_mod, tmp_data_home
):
    """Regression: the existing single-subject call shape is unchanged —
    same metric names, 2 score rows, one rating_id."""
    pid = _pending_for_template(tmp_data_home, "pair-wheel-v2", payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    rating_id = judgement_mod.write_judgement(
        pending_id=pid,
        verdict="a-wins",
        confidence="mid",
        rationale="A is tighter.",
        rater={"type": "human", "userId": "u1"},
    )
    db = _db(tmp_data_home)
    scores = db.execute(
        "SELECT rating_id, name, value FROM score WHERE pending_id=?", (pid,)
    ).fetchall()
    db.close()
    assert len(scores) == 2
    assert {s["rating_id"] for s in scores} == {rating_id}
    assert {s["name"] for s in scores} == {
        "judgement.verdict", "judgement.confidence"
    }

def test_every_seed_accepts_the_legacy_single_subject_call_shape(
    judgement_mod, tmp_data_home
):
    """The legacy shape works against every seeded rubric, each judged with
    a verdict its OWN enum declares — the verdict comes from the rubric
    rather than from one name every rubric was assumed to share."""
    for name in ("pair-wheel-v2", "pair-idea-wheel-v2", "single-idea-v1",
                 "single-execution-v1"):
        verdict = judgement_mod.get_template(
            name)["output_definition"]["verdict_enum"][0]
        pid = _pending_for_template(tmp_data_home, name)
        rating_id = judgement_mod.write_judgement(
            pending_id=pid, verdict=verdict, confidence="low",
            rationale=None, rater={"type": "human", "userId": "u1"},
        )
        assert rating_id

def test_pair_wheel_v2_accepts_wheel_verdicts(judgement_mod, tmp_data_home):
    pid = _pending_for_template(tmp_data_home, "pair-wheel-v2", payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    rating_id = judgement_mod.write_judgement(
        pending_id=pid, verdict="tie", confidence="high",
        rationale="the order between them does not matter",
        rater={"type": "human", "userId": "u1"},
    )
    assert rating_id
    for retired in ("tie-both-important", "tie-both-strong", "neither-good",
                    "incoherent", "a-strongly-better", "a-lean-both-invalid"):
        pid2 = _pending_for_template(tmp_data_home, "pair-wheel-v2")
        with pytest.raises(ValueError, match="not in rubric enum"):
            judgement_mod.write_judgement(
                pending_id=pid2, verdict=retired, confidence="mid",
                rationale=None, rater={"type": "human", "userId": "u1"},
            )

def test_the_pair_rubric_offers_skip_so_a_rater_never_has_to_guess(
    judgement_mod, tmp_data_home
):
    """skip is judge-facing on the pair rubric, and it is NOT on the wheel:
    the rater reaches it from the operational row, and the swiss engine
    scores it as no result at all."""
    from bin import swiss

    outdef = judgement_mod.get_template("pair-wheel-v2")["output_definition"]
    assert "skip" in outdef["verdict_enum"]
    assert "skip" not in outdef["wheel"].values()
    assert swiss.VERDICT_OUTCOMES["skip"] == swiss.OUTCOME_SKIP

    pid = _pending_for_template(tmp_data_home, "pair-wheel-v2", payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    assert judgement_mod.write_judgement(
        pending_id=pid, verdict="skip", confidence="low",
        rationale="not enough context to call it",
        rater={"type": "human", "userId": "u1"},
    )

def test_both_pair_rubrics_offer_skip_so_neither_can_drift(judgement_mod):
    for name in ("pair-wheel-v2", "pair-idea-wheel-v2"):
        enum = judgement_mod.get_template(name)["output_definition"]["verdict_enum"]
        assert "skip" in enum, f"{name} leaves a rater no way to decline"

def _judged_pending(judgement_mod, tmp_data_home, *, template="pair-wheel-v2",
                    verdict="a-wins-big") -> tuple[int, str]:
    """One done pending on the given (single-subject) template. Returns
    (pending_id, original rating_id)."""
    pid = _pending_for_template(tmp_data_home, template, payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    rid = judgement_mod.write_judgement(
        pending_id=pid, verdict=verdict, confidence="mid",
        rationale="first take", rater={"type": "human", "userId": "u1"},
    )
    return pid, rid

def _score_rows(tmp_data_home, rating_id):
    db = _db(tmp_data_home)
    rows = db.execute(
        "SELECT * FROM score WHERE rating_id=? ORDER BY id", (rating_id,)
    ).fetchall()
    db.close()
    return [tuple(r) for r in rows]

def test_revise_happy_path(judgement_mod, tmp_data_home):
    pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    original_rows = _score_rows(tmp_data_home, rid)
    assert len(original_rows) == 2

    new_rid = judgement_mod.revise_judgement(
        pid,
        previous_rating_id=rid,
        revised_by="reviewer-2",
        reason="misread card B on the first pass",
        rater={"type": "human", "userId": "reviewer-2"},
        verdict="b-wins-big",
        confidence="high",
        rationale="B is the correctness bug",
    )
    assert new_rid and new_rid != rid

    assert _score_rows(tmp_data_home, rid) == original_rows

    new_rows = _score_rows(tmp_data_home, new_rid)
    assert len(new_rows) == 2
    db = _db(tmp_data_home)
    by_name = {r["name"]: r for r in db.execute(
        "SELECT name, value FROM score WHERE rating_id=?", (new_rid,))}
    assert by_name["judgement.verdict"]["value"] == "b-wins-big"
    assert by_name["judgement.confidence"]["value"] == "high"
    prow = db.execute(
        "SELECT status, rating_id FROM pending_judgement WHERE id=?", (pid,)
    ).fetchone()
    assert prow["status"] == "done"
    assert prow["rating_id"] == rid
    db.close()

    chain = judgement_mod.get_revision_chain(pid)
    assert [c["rating_id"] for c in chain] == [rid, new_rid]
    assert chain[0]["revised_by"] is None
    assert chain[1]["revised_by"] == "reviewer-2"
    assert chain[1]["reason"] == "misread card B on the first pass"
    assert chain[1]["created_at"]
    assert judgement_mod.effective_rating_id(pid) == new_rid

def test_double_revise_chains_and_stale_previous_refused(
    judgement_mod, tmp_data_home
):
    pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    rid2 = judgement_mod.revise_judgement(
        pid, previous_rating_id=rid, revised_by="u2", reason="first fix",
        rater={"type": "human", "userId": "u2"},
        verdict="b-wins", confidence="mid",
    )
    rid3 = judgement_mod.revise_judgement(
        pid, previous_rating_id=rid2, revised_by="u3", reason="second fix",
        rater={"type": "human", "userId": "u3"},
        verdict="b-wins-big", confidence="high",
    )
    assert judgement_mod.effective_rating_id(pid) == rid3
    chain = judgement_mod.get_revision_chain(pid)
    assert [c["rating_id"] for c in chain] == [rid, rid2, rid3]

    with pytest.raises(ValueError, match="stale revision"):
        judgement_mod.revise_judgement(
            pid, previous_rating_id=rid, revised_by="u4", reason="too late",
            rater={"type": "human", "userId": "u4"},
            verdict="a-wins-big", confidence="low",
        )
    db = _db(tmp_data_home)
    n = db.execute(
        "SELECT COUNT(*) FROM score WHERE pending_id=?", (pid,)
    ).fetchone()[0]
    db.close()
    assert n == 6

def test_revise_refusals(judgement_mod, tmp_data_home):
    pid = _pending_for_template(tmp_data_home, "pair-wheel-v2")
    with pytest.raises(ValueError, match="not 'done'"):
        judgement_mod.revise_judgement(
            pid, previous_rating_id="whatever", revised_by="u1", reason="r",
            rater={"type": "human", "userId": "u1"},
            verdict="a-wins-big", confidence="mid",
        )

    done_pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    with pytest.raises(ValueError, match="reason"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="u1", reason="  ",
            rater={"type": "human", "userId": "u1"},
            verdict="a-wins-big", confidence="mid",
        )
    with pytest.raises(ValueError, match="revised_by"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="", reason="real",
            rater={"type": "human", "userId": "u1"},
            verdict="a-wins-big", confidence="mid",
        )
    with pytest.raises(ValueError, match="stale revision"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id="not-the-tip", revised_by="u1",
            reason="real", rater={"type": "human", "userId": "u1"},
            verdict="a-wins-big", confidence="mid",
        )
    with pytest.raises(ValueError, match="not in rubric enum"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="u1", reason="real",
            rater={"type": "human", "userId": "u1"},
            verdict="not-a-verdict", confidence="mid",
        )
    db = _db(tmp_data_home)
    n = db.execute("SELECT COUNT(*) FROM judgement_revision").fetchone()[0]
    db.close()
    assert n == 0

def test_revise_subject_aware_on_two_subject_template(
    judgement_mod, multi_subject_pending, tmp_data_home
):
    rid = judgement_mod.write_judgement(
        pending_id=multi_subject_pending,
        subject_verdicts={
            "idea": {"verdict": "good", "confidence": "mid"},
            "execution": {"verdict": "good", "confidence": "mid"},
        },
        rater={"type": "human", "userId": "u1"},
    )
    new_rid = judgement_mod.revise_judgement(
        multi_subject_pending,
        previous_rating_id=rid,
        revised_by="u2",
        reason="execution was actually broken",
        rater={"type": "human", "userId": "u2"},
        subject_verdicts={
            "idea": {"verdict": "good", "confidence": "high"},
            "execution": {"verdict": "bad", "confidence": "high",
                          "rationale": "fails on empty input"},
        },
    )
    db = _db(tmp_data_home)
    names = {r["name"]: r["value"] for r in db.execute(
        "SELECT name, value FROM score WHERE rating_id=?", (new_rid,))}
    db.close()
    assert names == {
        "judgement.idea.verdict": "good",
        "judgement.idea.confidence": "high",
        "judgement.execution.verdict": "bad",
        "judgement.execution.confidence": "high",
    }
    assert judgement_mod.effective_rating_id(multi_subject_pending) == new_rid

def test_judgement_revision_rows_are_immutable(judgement_mod, tmp_data_home):
    pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    judgement_mod.revise_judgement(
        pid, previous_rating_id=rid, revised_by="u2", reason="fix",
        rater={"type": "human", "userId": "u2"},
        verdict="b-wins-big", confidence="mid",
    )
    db = _db(tmp_data_home)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE judgement_revision SET reason='rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM judgement_revision")
    db.close()

def test_write_judgement_still_refuses_resolved_after_refactor(
    judgement_mod, tmp_data_home
):
    """The factored write path keeps write_judgement's contract: a done
    pending refuses a second plain write (revision is the only way)."""
    pid, _rid = _judged_pending(judgement_mod, tmp_data_home)
    with pytest.raises(RuntimeError, match="already resolved"):
        judgement_mod.write_judgement(
            pending_id=pid, verdict="b-wins-big", confidence="mid",
            rationale=None, rater={"type": "human", "userId": "u1"},
        )

def test_cli_revise_smoke(judgement_mod, tmp_data_home, monkeypatch, capsys):
    pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    monkeypatch.setattr("sys.argv", [
        "judgement.py", "revise",
        "--pending-id", str(pid),
        "--previous-rating-id", rid,
        "--revised-by", "cli-user",
        "--reason", "cli smoke revision",
        "--verdict", "b-wins-big",
        "--confidence", "high",
        "--rationale", "cli says B",
    ])
    judgement_mod.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["pending_id"] == pid
    assert out["previous_rating_id"] == rid
    assert out["new_rating_id"]
    assert judgement_mod.effective_rating_id(pid) == out["new_rating_id"]

_REUSE_CARD_A = {"title": "Leaky handle", "body": "closes on GC only"}
_REUSE_CARD_B = {"title": "Slow startup", "body": "1.2s of blocking IO"}

def _reuse_enqueue(judgement_mod, *, match_id, card_a=_REUSE_CARD_A,
                   card_b=_REUSE_CARD_B, template="pair-wheel-v2"):
    return judgement_mod.enqueue_for_match(
        tournament_db_path="domain:1",
        match_id=match_id,
        template_name=template,
        payload={"label": f"R1-{match_id}", "card_a": card_a, "card_b": card_b},
    )

def _reuse_judge(judgement_mod, pending_id, verdict="a-wins-big"):
    return judgement_mod.write_judgement(
        pending_id=pending_id, verdict=verdict, confidence="mid",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )

def _pending_count(tmp_data_home) -> int:
    db = _db(tmp_data_home)
    n = db.execute("SELECT COUNT(*) FROM pending_judgement").fetchone()[0]
    db.close()
    return n

def _fanout(judgement_mod, template="pair-wheel-v2") -> int:
    """How many rows ONE enqueue writes: one per active config."""
    return len(judgement_mod.list_active_configs(template))

def test_reuse_skips_a_pair_that_already_has_a_judgement(
    judgement_mod, tmp_data_home
):
    first = _reuse_enqueue(judgement_mod, match_id=1)
    rating_id = _reuse_judge(judgement_mod, first[0])

    redraw = _reuse_enqueue(judgement_mod, match_id=42)

    assert redraw == []
    assert redraw.existing_rating_id == rating_id
    assert redraw.pair_key == first.pair_key
    assert _pending_count(tmp_data_home) == _fanout(judgement_mod)

def test_reuse_is_order_independent_across_a_redraw(judgement_mod, tmp_data_home):
    first = _reuse_enqueue(judgement_mod, match_id=1)
    rating_id = _reuse_judge(judgement_mod, first[0])

    swapped = _reuse_enqueue(judgement_mod, match_id=43,
                             card_a=_REUSE_CARD_B, card_b=_REUSE_CARD_A)

    assert swapped == []
    assert swapped.existing_rating_id == rating_id
    assert _pending_count(tmp_data_home) == _fanout(judgement_mod)

def test_reuse_does_not_skip_a_pair_only_queued_under_another_match(
    judgement_mod, tmp_data_home
):
    """An unjudged duplicate is not reuse — but it is still a repeat of a
    pair already waiting, so it must not double the queue either."""
    first = _reuse_enqueue(judgement_mod, match_id=1)
    again = _reuse_enqueue(judgement_mod, match_id=2)
    assert first != []
    assert again == []
    assert _pending_count(tmp_data_home) == _fanout(judgement_mod)

def test_reuse_asks_again_after_a_rubric_version_bump(judgement_mod, tmp_data_home):
    """A rubric revision invalidates exactly the matches judged under the
    old rubric: the same two items are a new question."""
    first = _reuse_enqueue(judgement_mod, match_id=1)
    _reuse_judge(judgement_mod, first[0])

    fanout = _fanout(judgement_mod)
    tpl_id = judgement_mod.register_template(
        name="pair-wheel-v2", version=judgement_mod.PAIR_WHEEL_TEMPLATE_VERSION + 1,
        output_definition=judgement_mod.PAIR_WHEEL_TEMPLATE_DEFINITION,
    )
    db = _db(tmp_data_home)
    db.execute(
        "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
        "VALUES (?, 'human', '{}')", (tpl_id,))
    db.commit()
    db.close()

    bumped = _reuse_enqueue(judgement_mod, match_id=44)

    assert len(bumped) == 1
    assert bumped.pair_key != first.pair_key
    assert bumped.existing_rating_id is None
    assert _pending_count(tmp_data_home) == fanout + 1

def test_reuse_leaves_legacy_match_idempotency_unchanged(
    judgement_mod, tmp_data_home
):
    """Rows whose content cannot be snapshotted get no pair_key, and stay
    governed by the original (config_id, db_path, match_id) rule."""
    payload = {"label": "R1-1", "synthesis": "a conclusion with no inputs"}
    kwargs = dict(tournament_db_path="/tmp/legacy.db",
                  template_name="pair-wheel-v2", payload=payload)

    first = judgement_mod.enqueue_for_match(match_id=1, **kwargs)
    repeat = judgement_mod.enqueue_for_match(match_id=1, **kwargs)
    other_match = judgement_mod.enqueue_for_match(match_id=2, **kwargs)

    fanout = _fanout(judgement_mod)
    assert len(first) == fanout
    assert first.pair_key is None
    assert repeat == []
    assert len(other_match) == fanout
    assert _pending_count(tmp_data_home) == 2 * fanout

def _template_row(tmp_data_home, name: str):
    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT id, version, output_definition, langfuse_prompt_name "
        "FROM eval_template WHERE name=? ORDER BY version ASC", (name,)
    ).fetchall()
    db.close()
    return row

def test_the_default_rubric_offers_both_per_side_discards(judgement_mod):
    """swiss owns which verdicts eject an item; the rubric the judge is
    actually handed must offer every one of them, or discard is unreachable
    on the page."""
    from bin import swiss

    tpl = judgement_mod.get_template(judgement_mod.DEFAULT_TEMPLATE_NAME)
    enum = set(tpl["output_definition"]["verdict_enum"])
    assert swiss.DISCARD_VERDICTS <= enum, (
        "the rubric new work is judged under must be able to say 'this item "
        "does not belong in the pool'"
    )
    assert swiss.DISCARD_VERDICTS == {"discard-a", "discard-b"}
    assert enum <= swiss.known_verdicts(), (
        swiss.EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP
    )

def test_the_discard_region_is_the_two_bottom_diagonals_and_nothing_else(
    judgement_mod
):
    from bin import swiss

    wheel = judgement_mod.get_template(
        judgement_mod.DEFAULT_TEMPLATE_NAME)["output_definition"]["wheel"]
    assert {wheel["sw"], wheel["se"]} == swiss.DISCARD_VERDICTS
    for position in ("nw", "n", "ne", "w", "e"):
        assert wheel[position] not in swiss.DISCARD_VERDICTS

def test_enqueue_binds_new_work_to_the_one_rubric(judgement_mod, tmp_data_home):
    """The judge is handed whatever rubric the enqueue chose. With no
    explicit template that must be the default pair rubric."""
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:7", match_id=1,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a body"},
                 "card_b": {"title": "B", "body": "b body"}},
    )
    assert outcome != []
    rows = [p for p in judgement_mod.list_pending(limit=500) if p["id"] in outcome]
    assert rows
    for row in rows:
        assert row["template_name"] == "pair-wheel-v2"
        assert row["template_version"] == judgement_mod.PAIR_WHEEL_TEMPLATE_VERSION
        assert set(row["output_definition"]["verdict_enum"]) >= {
            "discard-a", "discard-b"}

def test_both_per_side_discards_are_writable_under_the_default_rubric(
    judgement_mod, tmp_data_home
):
    """End-to-end reachability: the verdict the design calls first-class is
    accepted, for each side independently."""
    for verdict in ("discard-a", "discard-b"):
        pid = _pending_for_template(
            tmp_data_home, judgement_mod.DEFAULT_TEMPLATE_NAME,
            payload={"label": f"R1-{verdict}",
                     "card_a": {"title": "A", "body": f"a body {verdict}"},
                     "card_b": {"title": "B", "body": f"b body {verdict}"}})
        rating_id = judgement_mod.write_judgement(
            pending_id=pid, verdict=verdict, confidence="high",
            rationale="not a real finding",
            rater={"type": "human", "userId": "u1"},
        )
        assert rating_id

def test_the_retired_rubrics_are_deleted_not_aliased(judgement_mod, tmp_data_home):
    """One rubric, one name, one default. Nothing answers to the old names,
    in the module or in the database."""
    assert not _template_row(tmp_data_home, "card-prioritizer-v0")
    assert not _template_row(tmp_data_home, "card-prioritizer-v1")
    for gone in ("SEED_TEMPLATE_NAME", "SEED_TEMPLATE_DEFINITION",
                 "PRIORITIZER_V1_TEMPLATE_NAME",
                 "PRIORITIZER_V1_TEMPLATE_DEFINITION"):
        assert not hasattr(judgement_mod, gone), f"{gone} is still exported"
    for name in ("card-prioritizer-v0", "card-prioritizer-v1"):
        with pytest.raises(LookupError):
            judgement_mod.get_template(name)

def test_a_database_of_pre_reset_judgements_is_told_the_keys_no_longer_join(
    judgement_mod, tmp_data_home, capsys
):
    """The loud half of the abandonment.

    An operator re-initializing against a database that holds judgements made
    under a retired rubric must be TOLD, by name, that those pair keys no
    longer join and that nothing is backfilled — otherwise the tournament
    silently re-asks a corpus of settled comparisons and reads as data loss.
    """
    db = _db(tmp_data_home)
    tpl_id = db.execute(
        "INSERT INTO eval_template(name, version, output_definition) "
        "VALUES ('card-prioritizer-v0', 1, ?)",
        (json.dumps({"verdict_enum": ["a-clearly-better", "incoherent"]}),),
    ).lastrowid
    for name, value in (("judgement.verdict", "a-clearly-better"),
                        ("judgement.confidence", "mid")):
        db.execute(
            "INSERT INTO score(rating_id, template_id, rubric_version, name, "
            "data_type, value, metadata, tournament_db_path, match_id, pair_key) "
            "VALUES ('rating-from-before', ?, 1, ?, 'CATEGORICAL', ?, "
            "'{\"rater\": {\"type\": \"human\"}}', 'domain:1', 1, 'oldkey')",
            (tpl_id, name, value),
        )
    db.commit()
    db.close()
    capsys.readouterr()

    judgement_mod.init_db()

    notice = capsys.readouterr().err
    assert judgement_mod.VOCABULARY_RESET in notice
    assert "card-prioritizer-v0 v1" in notice
    assert "1 judgement(s)" in notice
    assert "no longer joins" in notice
    assert "NOTHING is backfilled" in notice
    assert "not data loss" in notice
    assert (
        f"now {judgement_mod.PAIR_WHEEL_TEMPLATE_NAME} and "
        f"{judgement_mod.PAIR_IDEA_WHEEL_TEMPLATE_NAME}, both at version "
        f"{judgement_mod.PAIR_WHEEL_TEMPLATE_VERSION}"
    ) in notice, (
        judgement_mod.THE_RESET_NOTICE_NAMES_THE_RUBRIC_THE_OPERATOR_WILL_ACTUALLY_FIND_ON_DISK
    )

def test_a_fresh_database_is_not_told_about_a_reset_it_did_not_live_through(
    judgement_mod, capsys
):
    capsys.readouterr()
    judgement_mod.init_db()
    assert judgement_mod.VOCABULARY_RESET not in capsys.readouterr().err

def test_the_reset_notice_is_silent_about_rubrics_it_did_not_retire(
    judgement_mod, tmp_data_home
):
    """The current rubric's own judgements are not a retired corpus."""
    pid = _pending_for_template(
        tmp_data_home, judgement_mod.DEFAULT_TEMPLATE_NAME,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a"},
                 "card_b": {"title": "B", "body": "b"}})
    judgement_mod.write_judgement(
        pending_id=pid, verdict="a-wins", confidence="mid",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )
    db = _db(tmp_data_home)
    try:
        assert judgement_mod.retired_corpus(db) == {}
        assert judgement_mod.announce_vocabulary_reset(db) is None
    finally:
        db.close()

def test_default_rubric_seed_is_idempotent(
    judgement_mod, tmp_data_home, fake_langfuse
):
    judgement_mod.init_db()
    judgement_mod.init_db()

    rows = _template_row(tmp_data_home, "pair-wheel-v2")
    assert len(rows) == 1
    assert rows[0]["version"] == judgement_mod.PAIR_WHEEL_TEMPLATE_VERSION
    assert fake_langfuse.versions("judge-instructions:pair-wheel-v2") == [1]

    db = _db(tmp_data_home)
    cfgs = db.execute(
        "SELECT c.rater_type, json_extract(c.rater_config, '$.model') AS model "
        "FROM job_configuration c JOIN eval_template t ON t.id = c.template_id "
        "WHERE t.name='pair-wheel-v2' AND c.status='active'"
    ).fetchall()
    db.close()
    assert sum(r["rater_type"] == "human" for r in cfgs) == 1
    assert sorted(r["model"] for r in cfgs if r["rater_type"] == "llm") == sorted(
        judgement_mod.DEFAULT_JUDGE_PANEL_MODELS)

def test_the_lowest_id_human_config_is_the_one_rubric(
    judgement_mod, tmp_data_home
):
    """generate_cards falls back to the first active human configuration
    when a domain names no known rubric; that fallback must land on the
    rubric new work is judged under."""
    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT t.name FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE c.status='active' AND c.rater_type='human' "
        "ORDER BY c.id ASC LIMIT 1"
    ).fetchone()
    db.close()
    assert row["name"] == judgement_mod.DEFAULT_TEMPLATE_NAME

_MACHINE_CARD_A = {"title": "Unchecked cast", "body": "throws on a null row"}
_MACHINE_CARD_B = {"title": "Stale comment", "body": "mentions a removed flag"}

def _enqueue_default_rubric(judgement_mod, *, match_id: int):
    return judgement_mod.enqueue_for_match(
        tournament_db_path="domain:11",
        match_id=match_id,
        payload={"label": f"R1-{match_id}",
                 "card_a": _MACHINE_CARD_A, "card_b": _MACHINE_CARD_B},
    )

def _rater_types_of(tmp_data_home, pending_ids) -> dict[int, str]:
    if not pending_ids:
        return {}
    db = _db(tmp_data_home)
    marks = ",".join("?" for _ in pending_ids)
    rows = db.execute(
        f"SELECT p.id, c.rater_type FROM pending_judgement p "
        f"JOIN job_configuration c ON c.id = p.config_id "
        f"WHERE p.id IN ({marks})", tuple(pending_ids)
    ).fetchall()
    db.close()
    return {r["id"]: r["rater_type"] for r in rows}

def _cancel(tmp_data_home, pending_ids) -> None:
    db = _db(tmp_data_home)
    for pid in pending_ids:
        db.execute("UPDATE pending_judgement SET status='cancelled' WHERE id=?",
                   (pid,))
    db.commit()
    db.close()

def test_a_machine_verdict_never_forecloses_the_human_comparison(
    judgement_mod, tmp_data_home
):
    """The LLM drain writes a verdict on a pair; the person who never saw it
    must still be asked. The judgements a person makes are the product."""
    first = _enqueue_default_rubric(judgement_mod, match_id=1)
    by_rater = _rater_types_of(tmp_data_home, list(first))
    llm_ids = [pid for pid, kind in by_rater.items() if kind == "llm"]
    human_ids = [pid for pid, kind in by_rater.items() if kind == "human"]
    assert llm_ids and human_ids, "the default rubric must fan out to both"

    machine_rating = judgement_mod.write_judgement(
        pending_id=llm_ids[0], verdict="a-wins-big", confidence="mid",
        rationale=None, rater={"type": "llm", "model": "kimi-k3"},
    )
    _cancel(tmp_data_home, human_ids + llm_ids[1:])

    redraw = _enqueue_default_rubric(judgement_mod, match_id=2)

    assert redraw.existing_rating_id == machine_rating
    assert sorted(_rater_types_of(tmp_data_home, list(redraw)).values()) == ["human"], (
        "a machine verdict satisfied the machine queue only; the human queue "
        "must be re-asked"
    )

def test_a_human_verdict_satisfies_the_machine_queue(
    judgement_mod, tmp_data_home
):
    """The asymmetry runs one way: a person answered, so no model is paid to
    answer again."""
    first = _enqueue_default_rubric(judgement_mod, match_id=1)
    by_rater = _rater_types_of(tmp_data_home, list(first))
    human_ids = [pid for pid, kind in by_rater.items() if kind == "human"]
    llm_ids = [pid for pid, kind in by_rater.items() if kind == "llm"]

    human_rating = judgement_mod.write_judgement(
        pending_id=human_ids[0], verdict="b-wins-big", confidence="high",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )
    _cancel(tmp_data_home, llm_ids)

    redraw = _enqueue_default_rubric(judgement_mod, match_id=2)

    assert redraw == []
    assert redraw.existing_rating_id == human_rating

def test_a_pair_still_queued_for_a_rater_is_not_queued_twice(
    judgement_mod, tmp_data_home
):
    """Rater scoping widens the gate; it must not reopen the double-queue
    hole the pair key closed."""
    first = _enqueue_default_rubric(judgement_mod, match_id=1)
    again = _enqueue_default_rubric(judgement_mod, match_id=2)
    assert first != []
    assert again == []

def _write_lock_is_held(db_path) -> bool:
    """True when some other connection already holds the write lock.

    A second connection with a zero busy timeout either takes RESERVED or
    is told the database is locked; there is no third answer, so this is a
    deterministic read of "is a write transaction open right now".
    """
    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        return False
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc) or "busy" in str(exc), exc
        return True
    finally:
        probe.close()

def test_enqueue_holds_the_write_lock_across_its_three_stops(
    judgement_mod, tmp_data_home, monkeypatch
):
    """Every stop is a SELECT the INSERT depends on.

    Read outside a write transaction they are advisory: two enqueues of the
    same pair both see "not queued" and both insert, and the same person is
    asked the same comparison twice. The lock must already be held when the
    FIRST stop runs, not taken later by the INSERT.
    """
    seen = []
    original = judgement_mod._find_judgement_by_pair

    def probing_lookup(conn, key, rater_types=None):
        seen.append(_write_lock_is_held(tmp_data_home / "judgements.db"))
        return original(conn, key, rater_types)

    monkeypatch.setattr(judgement_mod, "_find_judgement_by_pair", probing_lookup)
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:race", match_id=1,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a body"},
                 "card_b": {"title": "B", "body": "b body"}},
    )
    assert outcome != [] and seen, "the reuse stop never ran"
    assert all(seen), (
        "enqueue read its stops without holding the write lock: "
        f"lock-held per stop was {seen}"
    )

def test_seeding_holds_the_write_lock_across_its_check_then_insert(
    judgement_mod, tmp_data_home, monkeypatch
):
    """eval_template carries UNIQUE(name, version), so an unlocked
    check-then-insert lets a concurrent init_db abort on the constraint
    instead of finding the row already there."""
    seen = []
    original = judgement_mod._existing_template

    def probing_check(conn, name, version):
        seen.append(_write_lock_is_held(tmp_data_home / "judgements.db"))
        return original(conn, name, version)

    monkeypatch.setattr(judgement_mod, "_existing_template", probing_check)
    judgement_mod.init_db()

    assert seen, "the seed never checked for an existing template"
    assert all(seen), (
        f"seeding checked for the row unlocked: lock-held per check was {seen}"
    )

def test_a_second_open_row_for_the_same_config_and_pair_is_refused_in_sql(
    judgement_mod, tmp_data_home
):
    """The backing index, exercised the only way that proves it exists:
    bypass enqueue and insert the duplicate directly."""
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:idx", match_id=1,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a body"},
                 "card_b": {"title": "B", "body": "b body"}},
    )
    assert outcome.pair_key
    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT config_id, trace_payload, pair_key FROM pending_judgement "
        "WHERE id=?", (outcome[0],)
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, pair_key) VALUES (?, 'domain:idx', 2, ?, ?)",
            (row["config_id"], row["trace_payload"], row["pair_key"]),
        )
    db.rollback()
    db.close()

def test_the_pair_index_constrains_open_rows_only(judgement_mod, tmp_data_home):
    """Partial on purpose: a resolved pair may be queued again (a rubric
    bump, a revision), and rows with no pair key are not constrained at all."""
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:idx2", match_id=1,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a body"},
                 "card_b": {"title": "B", "body": "b body"}},
    )
    db = _db(tmp_data_home)
    row = db.execute(
        "SELECT config_id, trace_payload, pair_key FROM pending_judgement "
        "WHERE id=?", (outcome[0],)
    ).fetchone()
    db.execute("UPDATE pending_judgement SET status='done' WHERE id=?",
               (outcome[0],))
    db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, "
        "match_id, trace_payload, pair_key) VALUES (?, 'domain:idx2', 2, ?, ?)",
        (row["config_id"], row["trace_payload"], row["pair_key"]),
    )
    db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, "
        "match_id, trace_payload, pair_key) VALUES (?, 'domain:idx2', 3, ?, NULL)",
        (row["config_id"], row["trace_payload"]),
    )
    db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, "
        "match_id, trace_payload, pair_key) VALUES (?, 'domain:idx2', 4, ?, NULL)",
        (row["config_id"], row["trace_payload"]),
    )
    db.commit()
    db.close()

def test_an_unresolved_row_never_answers_the_lookup(judgement_mod, tmp_data_home):
    """A rating_id parked on a row that is not 'done' is not a judgement.

    The lookup used to fall back to a scan of score.pair_key, which bypassed
    the revision chain and could hand back a superseded rating; the pending
    row's resolved state is now the only gate, so it has to hold.
    """
    outcome = judgement_mod.enqueue_for_match(
        tournament_db_path="domain:unresolved", match_id=1,
        payload={"label": "R1-1",
                 "card_a": {"title": "A", "body": "a body"},
                 "card_b": {"title": "B", "body": "b body"}},
    )
    key = outcome.pair_key
    rating_id = judgement_mod.write_judgement(
        pending_id=outcome[0], verdict="a-wins-big", confidence="mid",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )
    assert judgement_mod.find_judgement_by_pair(key) == rating_id

    db = _db(tmp_data_home)
    db.execute("UPDATE pending_judgement SET status='cancelled' WHERE id=?",
               (outcome[0],))
    db.commit()
    scores = db.execute(
        "SELECT COUNT(*) FROM score WHERE pair_key=? AND rating_id=?",
        (key, rating_id),
    ).fetchone()[0]
    db.close()
    assert scores == 2, "the score rows still carry the pair key"
    assert judgement_mod.find_judgement_by_pair(key) is None, (
        "the answer came from the score table, behind the pending row's back"
    )
