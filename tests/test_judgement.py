"""Wave-12 slice A: typed judgements, the semantic wheel, subject-aware ratings.

Covers docs/design/judgement-wheel-v2.md:
- output_definition v2 normalization (single code path for legacy templates);
- template registration validates the wheel (positions + verdicts vs enum);
- the pair-wheel-v1 / single-idea-v1 / single-execution-v1 seeds;
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


# ── normalization ─────────────────────────────────────────────────────────

def test_normalize_legacy_outdef_defaults(judgement_mod):
    """Absent v2 keys mean pair / ['execution'] / no wheel — the exact
    semantics every pre-v2 template always had."""
    legacy = {"verdict_enum": ["a", "b"], "confidence_enum": ["low", "mid", "high"]}
    norm = judgement_mod.normalize_output_definition(legacy)
    assert norm["judgement_kind"] == "pair"
    assert norm["subjects"] == ["execution"]
    assert norm["wheel"] == {}
    # Original keys survive; input is not mutated.
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


def test_get_template_normalizes_legacy_seed(judgement_mod):
    """card-prioritizer-v0 flows through the single normalization path."""
    tpl = judgement_mod.get_template("card-prioritizer-v0")
    outdef = tpl["output_definition"]
    assert outdef["judgement_kind"] == "pair"
    assert outdef["subjects"] == ["execution"]
    assert outdef["wheel"] == {}
    # Stored JSON stays byte-identical — old rubrics never reinterpreted.
    raw = json.loads(
        _db(judgement_mod.DATA_HOME).execute(
            "SELECT output_definition FROM eval_template "
            "WHERE name='card-prioritizer-v0'"
        ).fetchone()[0]
    )
    assert "judgement_kind" not in raw and "wheel" not in raw


def test_list_pending_normalizes_output_definition(judgement_mod, tmp_data_home):
    pid = _pending_for_template(tmp_data_home, "card-prioritizer-v0")
    rows = [p for p in judgement_mod.list_pending(rater_type="human", limit=100)
            if p["id"] == pid]
    assert rows and rows[0]["output_definition"]["judgement_kind"] == "pair"
    assert rows[0]["output_definition"]["subjects"] == ["execution"]


# ── registration validation ──────────────────────────────────────────────

def test_register_template_rejects_wheel_verdict_not_in_enum(judgement_mod):
    with pytest.raises(ValueError, match="not in"):
        judgement_mod.register_template(
            name="bad-wheel", version=1,
            output_definition={
                "verdict_enum": ["good", "bad"],
                "wheel": {"n": "great"},  # not in verdict_enum
            },
        )


def test_register_template_rejects_unknown_wheel_position(judgement_mod):
    with pytest.raises(ValueError, match="position"):
        judgement_mod.register_template(
            name="bad-position", version=1,
            output_definition={
                "verdict_enum": ["good"],
                "wheel": {"nne": "good"},  # not a compass position
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


# ── seeds ─────────────────────────────────────────────────────────────────

def test_pair_wheel_v1_seed_present_with_contract_wheel(judgement_mod):
    tpl = judgement_mod.get_template("pair-wheel-v1")
    outdef = tpl["output_definition"]
    assert tpl["version"] == 1
    assert outdef["judgement_kind"] == "pair"
    assert outdef["subjects"] == ["execution"]
    assert outdef["wheel"] == {
        "n": "tie-both-important",
        "ne": "b-slightly-better",
        "e": "b-strongly-better",
        "se": "b-lean-both-invalid",
        "s": "neither-good",
        "sw": "a-lean-both-invalid",
        "w": "a-strongly-better",
        "nw": "a-slightly-better",
    }
    assert outdef["confidence_enum"] == ["low", "mid", "high"]
    # skip + incoherent are operational: in verdict_enum, off the wheel.
    assert "skip" in outdef["verdict_enum"]
    assert "incoherent" in outdef["verdict_enum"]
    assert "skip" not in outdef["wheel"].values()
    assert "incoherent" not in outdef["wheel"].values()
    assert set(outdef["wheel"].values()) | {"incoherent", "skip"} == set(
        outdef["verdict_enum"]
    )


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
    # needs-evidence + skip stay off-wheel.
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
    for name in ("pair-wheel-v1", "single-idea-v1", "single-execution-v1"):
        n = db.execute(
            "SELECT COUNT(*) FROM eval_template WHERE name=?", (name,)
        ).fetchone()[0]
        assert n == 1, f"{name} seeded {n} times"
        # Exactly one human config per wheel seed (the L6 review bar), no
        # LLM panel fan-out.
        cfgs = db.execute(
            "SELECT c.rater_type FROM job_configuration c "
            "JOIN eval_template t ON t.id = c.template_id WHERE t.name=?",
            (name,),
        ).fetchall()
        assert [c[0] for c in cfgs] == ["human"]
        # The matching judge-instruction prompt exists, pushed exactly once.
        assert fake_langfuse.versions(f"judge-instructions:{name}") == [1]
    # v0 stays untouched.
    assert db.execute(
        "SELECT COUNT(*) FROM eval_template WHERE name='card-prioritizer-v0'"
    ).fetchone()[0] == 1
    db.close()


def test_wheel_seeds_added_to_preexisting_database(
    tmp_data_home, fake_langfuse, monkeypatch
):
    """A DB initialized before wave-12 gains the wheel seeds on re-init."""
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    # Simulate the pre-wave-12 state: drop the wheel seeds, keep v0.
    db = _db(tmp_data_home)
    ids = [r[0] for r in db.execute(
        "SELECT id FROM eval_template WHERE name IN "
        "('pair-wheel-v1','single-idea-v1','single-execution-v1')"
    )]
    qmarks = ",".join("?" for _ in ids)
    db.execute(f"DELETE FROM job_configuration WHERE template_id IN ({qmarks})", ids)
    db.execute(f"DELETE FROM eval_template WHERE id IN ({qmarks})", ids)
    db.commit()
    db.close()
    # Re-init takes the existing-database path and must re-seed.
    judgement.init_db()
    tpl = judgement.get_template("pair-wheel-v1")
    assert tpl["version"] == 1


# ── subject-aware write_judgement ────────────────────────────────────────

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
    # The failed write must not resolve the pending row.
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
    # Per-subject rationale rides on that subject's verdict row only.
    assert json.loads(
        by_name["judgement.idea.verdict"]["metadata"]
    )["rationale"] == "solid premise"
    assert "rationale" not in json.loads(
        by_name["judgement.execution.verdict"]["metadata"]
    )
    # The pending row resolved exactly once.
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


# ── backward compatibility ────────────────────────────────────────────────

def test_legacy_call_shape_still_works_on_card_prioritizer_v0(
    judgement_mod, tmp_data_home
):
    """Regression: the existing single-subject call shape is unchanged —
    same metric names, 2 score rows, one rating_id."""
    pid = _pending_for_template(tmp_data_home, "card-prioritizer-v0", payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    rating_id = judgement_mod.write_judgement(
        pending_id=pid,
        verdict="a-marginally-better",
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


def test_single_subject_v1_templates_accept_legacy_shape_and_skip(
    judgement_mod, tmp_data_home
):
    """skip stays valid everywhere: legacy shape against every wheel seed."""
    for name, verdict in (
        ("pair-wheel-v1", "skip"),
        ("single-idea-v1", "skip"),
        ("single-execution-v1", "skip"),
    ):
        pid = _pending_for_template(tmp_data_home, name)
        rating_id = judgement_mod.write_judgement(
            pending_id=pid, verdict=verdict, confidence="low",
            rationale=None, rater={"type": "human", "userId": "u1"},
        )
        assert rating_id


def test_pair_wheel_v1_accepts_wheel_verdicts(judgement_mod, tmp_data_home):
    pid = _pending_for_template(tmp_data_home, "pair-wheel-v1", payload={
        "label": "R1-1",
        "card_a": {"title": "A", "body": "a"},
        "card_b": {"title": "B", "body": "b"},
    })
    rating_id = judgement_mod.write_judgement(
        pending_id=pid, verdict="tie-both-important", confidence="high",
        rationale="both survive", rater={"type": "human", "userId": "u1"},
    )
    assert rating_id
    # Old enum values are NOT valid against the new rubric.
    pid2 = _pending_for_template(tmp_data_home, "pair-wheel-v1")
    with pytest.raises(ValueError, match="not in rubric enum"):
        judgement_mod.write_judgement(
            pending_id=pid2, verdict="a-clearly-better", confidence="mid",
            rationale=None, rater={"type": "human", "userId": "u1"},
        )


# ── wave-13 slice A: append-only judgement revision ───────────────────────

def _judged_pending(judgement_mod, tmp_data_home, *, template="card-prioritizer-v0",
                    verdict="a-clearly-better") -> tuple[int, str]:
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
        verdict="b-clearly-better",
        confidence="high",
        rationale="B is the correctness bug",
    )
    assert new_rid and new_rid != rid

    # Old score rows are byte-identical — never touched.
    assert _score_rows(tmp_data_home, rid) == original_rows

    # New rating has its own score rows via the same write path.
    new_rows = _score_rows(tmp_data_home, new_rid)
    assert len(new_rows) == 2
    db = _db(tmp_data_home)
    by_name = {r["name"]: r for r in db.execute(
        "SELECT name, value FROM score WHERE rating_id=?", (new_rid,))}
    assert by_name["judgement.verdict"]["value"] == "b-clearly-better"
    assert by_name["judgement.confidence"]["value"] == "high"
    # Pending stays 'done' — no status churn; rating_id column untouched.
    prow = db.execute(
        "SELECT status, rating_id FROM pending_judgement WHERE id=?", (pid,)
    ).fetchone()
    assert prow["status"] == "done"
    assert prow["rating_id"] == rid
    db.close()

    # Chain: original first, tip last; effective = new.
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
        verdict="b-marginally-better", confidence="mid",
    )
    rid3 = judgement_mod.revise_judgement(
        pid, previous_rating_id=rid2, revised_by="u3", reason="second fix",
        rater={"type": "human", "userId": "u3"},
        verdict="b-clearly-better", confidence="high",
    )
    assert judgement_mod.effective_rating_id(pid) == rid3
    chain = judgement_mod.get_revision_chain(pid)
    assert [c["rating_id"] for c in chain] == [rid, rid2, rid3]

    # Stale previous_rating_id (the ORIGINAL, already superseded) refused.
    with pytest.raises(ValueError, match="stale revision"):
        judgement_mod.revise_judgement(
            pid, previous_rating_id=rid, revised_by="u4", reason="too late",
            rater={"type": "human", "userId": "u4"},
            verdict="a-clearly-better", confidence="low",
        )
    # The refused attempt must not leave orphan score rows.
    db = _db(tmp_data_home)
    n = db.execute(
        "SELECT COUNT(*) FROM score WHERE pending_id=?", (pid,)
    ).fetchone()[0]
    db.close()
    assert n == 6  # 3 ratings x 2 rows


def test_revise_refusals(judgement_mod, tmp_data_home):
    # pending not 'done'
    pid = _pending_for_template(tmp_data_home, "card-prioritizer-v0")
    with pytest.raises(ValueError, match="not 'done'"):
        judgement_mod.revise_judgement(
            pid, previous_rating_id="whatever", revised_by="u1", reason="r",
            rater={"type": "human", "userId": "u1"},
            verdict="a-clearly-better", confidence="mid",
        )

    done_pid, rid = _judged_pending(judgement_mod, tmp_data_home)
    # empty reason
    with pytest.raises(ValueError, match="reason"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="u1", reason="  ",
            rater={"type": "human", "userId": "u1"},
            verdict="a-clearly-better", confidence="mid",
        )
    # empty revised_by
    with pytest.raises(ValueError, match="revised_by"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="", reason="real",
            rater={"type": "human", "userId": "u1"},
            verdict="a-clearly-better", confidence="mid",
        )
    # wrong previous id
    with pytest.raises(ValueError, match="stale revision"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id="not-the-tip", revised_by="u1",
            reason="real", rater={"type": "human", "userId": "u1"},
            verdict="a-clearly-better", confidence="mid",
        )
    # invalid verdict still validated by the shared path
    with pytest.raises(ValueError, match="not in rubric enum"):
        judgement_mod.revise_judgement(
            done_pid, previous_rating_id=rid, revised_by="u1", reason="real",
            rater={"type": "human", "userId": "u1"},
            verdict="not-a-verdict", confidence="mid",
        )
    # No revision row survived any refusal.
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
        verdict="b-clearly-better", confidence="mid",
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
            pending_id=pid, verdict="b-clearly-better", confidence="mid",
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
        "--verdict", "b-clearly-better",
        "--confidence", "high",
        "--rationale", "cli says B",
    ])
    judgement_mod.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["pending_id"] == pid
    assert out["previous_rating_id"] == rid
    assert out["new_rating_id"]
    assert judgement_mod.effective_rating_id(pid) == out["new_rating_id"]
