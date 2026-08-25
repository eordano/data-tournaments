"""Tests for bin/review_rules.py — developer-opinion learning loop
(wave-8 B5; docs/reviews/bugsweep-product-model.md §4.3).

All rules/quotes/authors here are INVENTED — they mirror the *shape* of
the mined bar (attribution, B/N observed enforcement, dissent), never its
content.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bin.approvals import ApprovalDenied


@pytest.fixture
def rules(tmp_data_home):
    from bin import review_rules as mod

    mod.init()
    return mod


@pytest.fixture
def catalog(tmp_data_home):
    from bin import catalog as mod

    return mod


@pytest.fixture
def raw(tmp_data_home):
    """Raw connection for asserting stored values / firing triggers."""
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _evidence(n: int = 2) -> list[dict]:
    return [
        {
            "quote": f"invented reviewer comment #{i}",
            "author": f"reviewer-{i}",
            "link": f"https://forge.example/pr/{100 + i}",
            "occurred_at": "2026-03-01",
            "enforcement": "blocking" if i % 2 == 0 else "nit",
            "verified_verbatim": True,
        }
        for i in range(n)
    ]


def _proposal(rules, **overrides) -> int:
    kwargs = dict(
        category="nullability",
        rule_text="Never use the null-forgiving operator on locals.",
        evidence=_evidence(),
        sub_forms=["nullable-local", "null-forgiving-bang"],
        approx_frequency=17,
        window="2026-02-16..2026-08-16",
        blocking_class="B",
        written_status="partial",
        doc_pointer="CLAUDE.md:133",
        top_enforcers=["reviewer-0", "reviewer-1"],
        mechanization={
            "kind": "regex",
            "pattern_or_sketch": r"!\\s*;",
            "fp_risk": "low",
            "ship_severity": "warn",
        },
        application_targets=["campaign-skill:review-stage"],
    )
    kwargs.update(overrides)
    return rules.create_proposal(**kwargs)


def _policy(catalog, approvers=("esteban",), scope="rule:*", name="rule-gate"):
    return catalog.create_policy(
        name=name, kind="approval", rule={"approvers": list(approvers), "scope": scope}
    )


# ── init / schema ─────────────────────────────────────────────────────────


class TestInit:
    def test_init_twice_is_idempotent(self, rules):
        rules.init()
        rules.init()

    def test_tables_and_triggers_exist(self, rules, raw):
        tables = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "review_rule_proposal" in tables
        assert "review_rule" in tables
        triggers = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "review_rule_immutable" in triggers
        assert "review_rule_no_delete" in triggers


# ── Proposal creation: only as strong as its evidence ─────────────────────


class TestCreateProposal:
    def test_round_trip(self, rules):
        pid = _proposal(rules)
        p = rules.get_proposal(pid)
        assert p["status"] == "draft"
        assert p["category"] == "nullability"
        assert p["blocking_class"] == "B"
        assert p["approx_frequency"] == 17
        assert p["sub_forms"] == ["nullable-local", "null-forgiving-bang"]
        assert p["top_enforcers"] == ["reviewer-0", "reviewer-1"]
        assert len(p["evidence"]) == 2
        assert p["evidence"][0]["author"] == "reviewer-0"
        assert p["mechanization"]["kind"] == "regex"
        assert p["application_targets"] == ["campaign-skill:review-stage"]

    def test_requires_two_evidence_quotes(self, rules):
        with pytest.raises(ValueError, match="evidence"):
            _proposal(rules, evidence=_evidence(1))
        with pytest.raises(ValueError, match="evidence"):
            _proposal(rules, evidence=[])

    def test_bad_blocking_class_raises(self, rules):
        with pytest.raises(ValueError, match="blocking_class"):
            _proposal(rules, blocking_class="X")

    def test_list_filters_by_status_and_category(self, rules):
        a = _proposal(rules, category="nullability")
        b = _proposal(rules, category="perf-alloc")
        rules.record_evaluation(b, evaluated_by="retro-24", result="7 hits")
        assert [p["id"] for p in rules.list_proposals(status="draft")] == [a]
        assert [p["id"] for p in rules.list_proposals(category="perf-alloc")] == [b]
        assert len(rules.list_proposals()) == 2
        with pytest.raises(ValueError, match="unknown status"):
            rules.list_proposals(status="shipped")


# ── Lifecycle: draft -> evaluated -> versioned; rejected/versioned sticky ─


class TestLifecycle:
    def test_record_evaluation_draft_to_evaluated(self, rules):
        pid = _proposal(rules)
        rules.record_evaluation(
            pid, evaluated_by="retro-24-diffs",
            result="retro-applied to 24 diffs: 7 BLOCK hits, 0 FPs",
        )
        p = rules.get_proposal(pid)
        assert p["status"] == "evaluated"
        assert p["evaluated_by"] == "retro-24-diffs"
        assert "7 BLOCK hits" in p["evaluation_result"]

    def test_evaluate_twice_raises(self, rules):
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r1", result="ok")
        with pytest.raises(ValueError, match="draft"):
            rules.record_evaluation(pid, evaluated_by="r2", result="again")

    def test_evaluate_rejected_raises(self, rules):
        pid = _proposal(rules)
        rules.reject(pid, reason="duplicate of an existing written rule")
        with pytest.raises(ValueError, match="draft"):
            rules.record_evaluation(pid, evaluated_by="r", result="x")

    def test_promote_from_draft_fails(self, rules, catalog):
        _policy(catalog)
        pid = _proposal(rules)
        with pytest.raises(ValueError, match="evaluated"):
            rules.promote(pid, principal="esteban", name="no-null-bang")

    def test_promote_rejected_fails(self, rules, catalog):
        _policy(catalog)
        pid = _proposal(rules)
        rules.reject(pid, reason="not a real rule")
        with pytest.raises(ValueError, match="evaluated"):
            rules.promote(pid, principal="esteban", name="no-null-bang")

    def test_reject_requires_reason(self, rules):
        pid = _proposal(rules)
        with pytest.raises(ValueError, match="reason"):
            rules.reject(pid, reason="")

    def test_rejected_is_sticky(self, rules):
        pid = _proposal(rules)
        rules.reject(pid, reason="first reason")
        rules.reject(pid, reason="second reason")  # silent no-op
        assert rules.get_proposal(pid)["evaluation_result"] == "first reason"

    def test_versioned_is_sticky_against_reject(self, rules, catalog):
        _policy(catalog)
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        rules.promote(pid, principal="esteban", name="no-null-bang")
        rules.reject(pid, reason="too late")  # silent no-op
        assert rules.get_proposal(pid)["status"] == "versioned"

    def test_promote_versioned_again_fails(self, rules, catalog):
        _policy(catalog)
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        rules.promote(pid, principal="esteban", name="no-null-bang")
        with pytest.raises(ValueError, match="evaluated"):
            rules.promote(pid, principal="esteban", name="no-null-bang")

    def test_missing_proposal_raises_lookup(self, rules):
        with pytest.raises(LookupError):
            rules.get_proposal(999)
        with pytest.raises(LookupError):
            rules.record_evaluation(999, evaluated_by="r", result="x")
        with pytest.raises(LookupError):
            rules.reject(999, reason="gone")


# ── Promotion gate: fail closed, audit, freeze ────────────────────────────


class TestPromotionGate:
    def test_no_policy_fails_closed_with_no_side_effects(self, rules, raw):
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        with pytest.raises(ApprovalDenied):
            rules.promote(pid, principal="esteban", name="no-null-bang")
        # proposal unchanged, no rule row, no audit row
        assert rules.get_proposal(pid)["status"] == "evaluated"
        assert raw.execute("SELECT COUNT(*) FROM review_rule").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM approval_event").fetchone()[0] == 0

    def test_unlisted_principal_denied(self, rules, catalog, raw):
        _policy(catalog, approvers=("esteban",))
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        with pytest.raises(ApprovalDenied):
            rules.promote(pid, principal="mallory", name="no-null-bang")
        assert raw.execute("SELECT COUNT(*) FROM review_rule").fetchone()[0] == 0

    def test_out_of_scope_policy_denied(self, rules, catalog):
        _policy(catalog, scope="release:*")
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        with pytest.raises(ApprovalDenied):
            rules.promote(pid, principal="esteban", name="no-null-bang")

    def test_promotion_writes_rule_and_audit(self, rules, catalog, raw):
        policy_id = _policy(catalog)
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        out = rules.promote(pid, principal="esteban", name="no-null-bang")
        assert out["version"] == 1
        assert out["policy_id"] == policy_id
        # immutable rule row, frozen from the proposal
        r = rules.get_rule("no-null-bang", 1)
        assert r["rule_text"].startswith("Never use the null-forgiving")
        assert r["category"] == "nullability"
        assert r["approved_by"] == "esteban"
        assert r["proposal_id"] == pid
        assert r["attribution"] == ["reviewer-0", "reviewer-1"]
        assert [e["quote"] for e in r["evidence"]] == [
            "invented reviewer comment #0",
            "invented reviewer comment #1",
        ]
        # audit row: decision approved, workflow_id rule:<name>:v<n>
        ev = raw.execute(
            "SELECT * FROM approval_event WHERE id=?", (r["approval_event_id"],)
        ).fetchone()
        assert ev["temporal_workflow_id"] == "rule:no-null-bang:v1"
        assert ev["decision"] == "approved"
        assert ev["approver"] == "esteban"
        assert ev["policy_id"] == policy_id
        # proposal terminal
        assert rules.get_proposal(pid)["status"] == "versioned"

    def test_rule_rows_are_immutable_and_undeletable(self, rules, catalog, raw):
        _policy(catalog)
        pid = _proposal(rules)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        rules.promote(pid, principal="esteban", name="no-null-bang")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            raw.execute("UPDATE review_rule SET rule_text='weakened'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute("DELETE FROM review_rule")

    def test_dissent_and_conflicts_survive_promotion_verbatim(
        self, rules, catalog, raw
    ):
        _policy(catalog)
        dissent = [
            {
                "author": "reviewer-9",
                "position": "demanded evidence for the invented ban",
                "link": None,
                "resolution": "open",
            }
        ]
        conflicts = ["guide-says-interpolate vs bar-blocks-in-hot-paths"]
        pid = _proposal(rules, dissent=dissent, conflicts_with=conflicts)
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        rules.promote(pid, principal="esteban", name="no-null-bang")
        # dissent frozen verbatim into the rule row
        assert rules.get_rule("no-null-bang", 1)["dissent"] == dissent
        # and still present verbatim on the (now versioned) proposal
        p = rules.get_proposal(pid)
        assert p["dissent"] == dissent
        assert p["conflicts_with"] == conflicts
        stored = raw.execute(
            "SELECT dissent FROM review_rule WHERE name='no-null-bang'"
        ).fetchone()[0]
        assert json.loads(stored) == dissent

    def test_frequency_alone_never_promotes(self, rules, raw):
        """Module surface check: no code path from mining/evaluation to a
        review_rule row. Only promote() writes review_rule, and it demands
        authorize() — a high-frequency, fully-evaluated proposal writes
        nothing downstream by itself."""
        pid = _proposal(rules, approx_frequency=9999)
        rules.record_evaluation(
            pid, evaluated_by="retro", result="huge hit count, all real"
        )
        assert raw.execute("SELECT COUNT(*) FROM review_rule").fetchone()[0] == 0
        # promote() is the only module function whose source inserts into
        # review_rule; every other lifecycle function cannot.
        import inspect

        for fn_name in ("create_proposal", "record_evaluation", "reject"):
            src = inspect.getsource(getattr(rules, fn_name))
            assert "INSERT INTO review_rule(" not in src
        assert "INSERT INTO review_rule(" in inspect.getsource(rules.promote)
        assert "approvals.authorize" in inspect.getsource(rules.promote)


# ── Versioning: auto-increment, supersedes chain, active_rules ────────────


class TestVersioning:
    def test_auto_increment_and_supersedes_chain(self, rules, catalog):
        _policy(catalog)
        p1 = _proposal(rules)
        rules.record_evaluation(p1, evaluated_by="r", result="ok")
        out1 = rules.promote(p1, principal="esteban", name="no-null-bang")
        assert out1["version"] == 1

        p2 = _proposal(rules, rule_text="Never use ! on locals; scoped to src/.")
        rules.record_evaluation(p2, evaluated_by="r2", result="ok")
        out2 = rules.promote(
            p2, principal="esteban", name="no-null-bang",
            supersedes="no-null-bang:v1",
        )
        assert out2["version"] == 2

        hist = rules.rule_history("no-null-bang")
        assert [h["version"] for h in hist] == [1, 2]
        assert hist[0]["supersedes"] is None
        assert hist[1]["supersedes"] == "no-null-bang:v1"
        # old version remains (rollback = repoint)
        assert rules.get_rule("no-null-bang", 1)["rule_text"].startswith("Never use the")

    def test_explicit_duplicate_version_raises(self, rules, catalog):
        _policy(catalog)
        p1 = _proposal(rules)
        rules.record_evaluation(p1, evaluated_by="r", result="ok")
        rules.promote(p1, principal="esteban", name="no-null-bang", version=1)
        p2 = _proposal(rules)
        rules.record_evaluation(p2, evaluated_by="r", result="ok")
        with pytest.raises(ValueError, match="already exists"):
            rules.promote(p2, principal="esteban", name="no-null-bang", version=1)

    def test_active_rules_picks_latest_per_name(self, rules, catalog):
        _policy(catalog)
        for name, versions in (("rule-a", 2), ("rule-b", 1)):
            for _ in range(versions):
                pid = _proposal(rules)
                rules.record_evaluation(pid, evaluated_by="r", result="ok")
                rules.promote(pid, principal="esteban", name=name)
        active = rules.active_rules()
        assert [(r["name"], r["version"]) for r in active] == [
            ("rule-a", 2), ("rule-b", 1),
        ]

    def test_rule_history_unknown_name_raises(self, rules):
        with pytest.raises(LookupError):
            rules.rule_history("never-promoted")


# ── CLI smoke ─────────────────────────────────────────────────────────────


class TestCli:
    def test_cli_round_trip(self, rules, catalog, tmp_path, capsys):
        _policy(catalog)
        assert rules.main(["init"]) == 0
        f = tmp_path / "proposal.json"
        f.write_text(json.dumps({
            "category": "perf-alloc",
            "rule_text": "No LINQ in hot paths.",
            "evidence": _evidence(),
        }))
        assert rules.main(["create-proposal", "--from-json", str(f)]) == 0
        out = capsys.readouterr().out
        pid = json.loads(out[out.rindex("{"):])["id"]
        rules.record_evaluation(pid, evaluated_by="r", result="ok")
        assert rules.main(["list", "--status", "evaluated"]) == 0
        assert rules.main([
            "promote", "--id", str(pid), "--principal", "esteban",
            "--name", "no-linq-hot",
        ]) == 0
        capsys.readouterr()  # flush list/promote output
        assert rules.main(["history", "--name", "no-linq-hot"]) == 0
        out = capsys.readouterr().out
        hist = json.loads(out)
        assert hist[0]["name"] == "no-linq-hot"
        assert hist[0]["version"] == 1
