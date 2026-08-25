"""Tests for bin/campaigns.py — campaign/finding/dossier persistence
(wave-8 B4 spine; docs/reviews/bugsweep-product-model.md).

All slugs/root-causes here are INVENTED — they mirror the *shape* of the
aug16 corpus ledger rows, never their content.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bin.landscape import EvidenceRef, SourceType, TrustTier


@pytest.fixture
def campaigns(tmp_data_home):
    from bin import campaigns as mod

    mod.init()
    return mod


@pytest.fixture
def catalog(tmp_data_home):
    from bin import catalog as mod

    return mod


@pytest.fixture
def project(campaigns, catalog):
    catalog.create_project(name="widget-engine")
    return "widget-engine"


@pytest.fixture
def campaign(campaigns, project):
    campaigns.create_campaign(
        project=project,
        name="bugsweep-test",
        kind="bugsweep",
        objective="~5 landed fixes",
        time_window="tracker 7d",
        base_commit="deadbeef1234",
    )
    return "bugsweep-test"


@pytest.fixture
def raw(tmp_data_home):
    """Raw connection for asserting stored values / firing triggers."""
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _insert_evidence(catalog, project: str, uri: str) -> str:
    """Insert an evidence_ref via the catalog path; return its digest."""
    sid = catalog.create_source(
        project=project,
        name=f"src-{uri.split('/')[-1]}",
        kind="api",
        locator=uri,
    )
    ref = EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri=uri,
        trust_tier=TrustTier.TIER2_INTERNAL,
        why_selected="invented test evidence",
    )
    return catalog.insert_evidence_ref(ref, source_id=sid)


# ── init / schema ─────────────────────────────────────────────────────────


class TestInit:
    def test_init_twice_is_idempotent(self, campaigns):
        campaigns.init()
        campaigns.init()

    def test_tables_and_triggers_exist(self, campaigns, raw):
        tables = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for t in ("campaign", "finding", "finding_evidence",
                  "review_lens_verdict", "validation_ledger"):
            assert t in tables, f"missing table {t}"
        triggers = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for tr in (
            "review_lens_verdict_immutable", "review_lens_verdict_no_delete",
            "validation_ledger_immutable", "validation_ledger_no_delete",
        ):
            assert tr in triggers, f"missing trigger {tr}"


# ── Campaign CRUD ─────────────────────────────────────────────────────────


class TestCampaignCrud:
    def test_create_get_round_trip(self, campaigns, campaign):
        c = campaigns.get_campaign(campaign)
        assert c["kind"] == "bugsweep"
        assert c["objective"] == "~5 landed fixes"
        assert c["time_window"] == "tracker 7d"
        assert c["base_commit"] == "deadbeef1234"
        assert c["status"] == "active"

    def test_duplicate_name_raises(self, campaigns, project, campaign):
        with pytest.raises(ValueError, match="already exists"):
            campaigns.create_campaign(
                project=project, name=campaign, kind="bugsweep"
            )

    def test_bad_kind_raises(self, campaigns, project):
        with pytest.raises(ValueError, match="kind"):
            campaigns.create_campaign(project=project, name="x", kind="party")

    def test_missing_campaign_raises(self, campaigns):
        with pytest.raises(LookupError):
            campaigns.get_campaign("nope")

    def test_close_and_list(self, campaigns, project, campaign):
        campaigns.create_campaign(project=project, name="second", kind="release")
        assert {c["name"] for c in campaigns.list_campaigns()} == {
            campaign, "second"
        }
        campaigns.close_campaign("second")
        assert {c["name"] for c in campaigns.list_campaigns()} == {campaign}
        assert campaigns.list_campaigns("closed")[0]["name"] == "second"

    def test_close_missing_raises(self, campaigns):
        with pytest.raises(LookupError):
            campaigns.close_campaign("nope")


# ── Finding CRUD ──────────────────────────────────────────────────────────


class TestFindingCrud:
    def test_create_get_round_trip(self, campaigns, campaign):
        campaigns.create_finding(
            campaign=campaign,
            slug="gizmo-cache-stale-read",
            title="Gizmo cache serves stale reads after eviction",
            source_kind="sentry",
            root_cause="eviction races the refill",
            tracking_links=[{"kind": "fixes", "ref": "#1234"}],
            dedup_notes="no hits in open-prs / inflight / prior slugs",
        )
        f = campaigns.get_finding(campaign, "gizmo-cache-stale-read")
        assert f["state"] == "candidate"
        assert f["source_kind"] == "sentry"
        assert f["tracking_links"] == [{"kind": "fixes", "ref": "#1234"}]
        assert f["dedup_notes"].startswith("no hits")
        assert f["evidence"] == []
        assert f["lens_verdicts"] == []
        assert f["validation"] == []

    def test_slug_unique_per_campaign_not_globally(
        self, campaigns, project, campaign
    ):
        campaigns.create_finding(campaign=campaign, slug="dup-slug")
        with pytest.raises(ValueError, match="already exists"):
            campaigns.create_finding(campaign=campaign, slug="dup-slug")
        campaigns.create_campaign(project=project, name="other", kind="bugsweep")
        campaigns.create_finding(campaign="other", slug="dup-slug")  # fine

    def test_list_findings_filter_by_state(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="a-one")
        campaigns.create_finding(campaign=campaign, slug="b-two")
        campaigns.set_finding_state(campaign, "b-two", "investigating")
        assert [f["slug"] for f in campaigns.list_findings(campaign)] == [
            "a-one", "b-two"
        ]
        got = campaigns.list_findings(campaign, state="investigating")
        assert [f["slug"] for f in got] == ["b-two"]
        with pytest.raises(ValueError, match="unknown state"):
            campaigns.list_findings(campaign, state="limbo")

    def test_missing_finding_raises(self, campaigns, campaign):
        with pytest.raises(LookupError):
            campaigns.get_finding(campaign, "nope")


# ── State machine ─────────────────────────────────────────────────────────


class TestFindingStateMachine:
    def test_valid_transition_updates_state_and_updated_at(
        self, campaigns, campaign, raw
    ):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.set_finding_state(campaign, "s", "investigating")
        campaigns.set_finding_state(
            campaign, "s", "workorder_generated", root_cause="off-by-one in pager"
        )
        f = campaigns.get_finding(campaign, "s")
        assert f["state"] == "workorder_generated"
        assert f["root_cause"] == "off-by-one in pager"

    def test_unknown_state_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="unknown state"):
            campaigns.set_finding_state(campaign, "s", "limbo")

    def test_no_go_without_reason_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="no_go_reason"):
            campaigns.set_finding_state(campaign, "s", "no_go")

    def test_no_go_with_bad_reason_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="no_go_reason"):
            campaigns.set_finding_state(
                campaign, "s", "no_go", no_go_reason="felt-like-it"
            )

    def test_reason_on_non_no_go_state_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="only valid"):
            campaigns.set_finding_state(
                campaign, "s", "investigating", no_go_reason="wrong-repo"
            )

    def test_no_go_with_taxonomy_reason(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.set_finding_state(
            campaign, "s", "no_go", no_go_reason="already-fixed"
        )
        f = campaigns.get_finding(campaign, "s")
        assert f["state"] == "no_go"
        assert f["no_go_reason"] == "already-fixed"

    @pytest.mark.parametrize(
        "terminal", ["confirmed_validated", "failed_infra"]
    )
    def test_terminal_states_are_sticky(self, campaigns, campaign, terminal):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.set_finding_state(campaign, "s", terminal)
        # A late update is a silent no-op (workflow_runs precedent).
        campaigns.set_finding_state(campaign, "s", "candidate")
        assert campaigns.get_finding(campaign, "s")["state"] == terminal

    def test_no_go_is_sticky_too(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.set_finding_state(
            campaign, "s", "no_go", no_go_reason="stale-signal"
        )
        campaigns.set_finding_state(campaign, "s", "confirmed_validated")
        f = campaigns.get_finding(campaign, "s")
        assert f["state"] == "no_go"
        assert f["no_go_reason"] == "stale-signal"

    def test_schema_check_rejects_raw_no_go_without_reason(
        self, campaigns, campaign, raw
    ):
        """Belt-and-braces: the CHECK constraint blocks even raw SQL."""
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE finding SET state='no_go' WHERE slug='s'")


# ── Evidence links ────────────────────────────────────────────────────────


class TestFindingEvidence:
    def test_link_and_fetch(self, campaigns, catalog, project, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        digest = _insert_evidence(catalog, project, "doc://tracker/issue-1")
        campaigns.link_finding_evidence(campaign, "s", digest, role="signal")
        campaigns.link_finding_evidence(campaign, "s", digest, role="root-cause")
        # duplicate (digest, role) is a no-op
        campaigns.link_finding_evidence(campaign, "s", digest, role="signal")
        f = campaigns.get_finding(campaign, "s")
        assert [(e["evidence_digest"], e["role"]) for e in f["evidence"]] == [
            (digest, "root-cause"),
            (digest, "signal"),
        ]

    def test_bad_role_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="role"):
            campaigns.link_finding_evidence(campaign, "s", "abc123", role="vibes")

    def test_dangling_digest_raises_integrity_error(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(sqlite3.IntegrityError):
            campaigns.link_finding_evidence(
                campaign, "s", "0" * 64, role="signal"
            )


# ── Review lens verdicts: append-only + one repair cycle ─────────────────


class TestLensVerdicts:
    def test_confirms_accumulate(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="CONFIRM", rationale="ok"
        )
        campaigns.add_lens_verdict(
            campaign, "s", lens="lifecycle-regression", verdict="CONFIRM"
        )
        f = campaigns.get_finding(campaign, "s")
        assert [v["verdict"] for v in f["lens_verdicts"]] == ["CONFIRM", "CONFIRM"]

    def test_bad_verdict_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(ValueError, match="verdict"):
            campaigns.add_lens_verdict(
                campaign, "s", lens="root-cause", verdict="MAYBE"
            )

    def test_one_repair_cycle_allows_single_repair(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        refute_id = campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="REFUTE",
            rationale="symptom patch, concrete failure scenario attached",
        )
        repair_id = campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="CONFIRM",
            rationale="repair verified against worktree + pin",
            repair_of=refute_id,
        )
        f = campaigns.get_finding(campaign, "s")
        assert f["lens_verdicts"][1]["id"] == repair_id
        assert f["lens_verdicts"][1]["repair_of"] == refute_id

    def test_second_repair_on_same_refute_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        refute_id = campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="REFUTE"
        )
        campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="CONFIRM",
            repair_of=refute_id,
        )
        with pytest.raises(ValueError, match="one-repair-cycle"):
            campaigns.add_lens_verdict(
                campaign, "s", lens="root-cause", verdict="CONFIRM",
                repair_of=refute_id,
            )

    def test_repair_of_repair_raises(self, campaigns, campaign):
        """No chains: even if the answer to a REFUTE is itself a REFUTE,
        it cannot be repaired again — one cycle, then terminal."""
        campaigns.create_finding(campaign=campaign, slug="s")
        refute_id = campaigns.add_lens_verdict(
            campaign, "s", lens="ecs-struct-perf", verdict="REFUTE"
        )
        re_refute = campaigns.add_lens_verdict(
            campaign, "s", lens="ecs-struct-perf", verdict="REFUTE",
            repair_of=refute_id,
        )
        with pytest.raises(ValueError, match="repair a repair"):
            campaigns.add_lens_verdict(
                campaign, "s", lens="ecs-struct-perf", verdict="CONFIRM",
                repair_of=re_refute,
            )

    def test_repair_of_confirm_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        confirm_id = campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="CONFIRM"
        )
        with pytest.raises(ValueError, match="REFUTE"):
            campaigns.add_lens_verdict(
                campaign, "s", lens="root-cause", verdict="CONFIRM",
                repair_of=confirm_id,
            )

    def test_repair_of_other_finding_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="a-one")
        campaigns.create_finding(campaign=campaign, slug="b-two")
        refute_id = campaigns.add_lens_verdict(
            campaign, "a-one", lens="root-cause", verdict="REFUTE"
        )
        with pytest.raises(ValueError, match="different finding"):
            campaigns.add_lens_verdict(
                campaign, "b-two", lens="root-cause", verdict="CONFIRM",
                repair_of=refute_id,
            )

    def test_repair_of_missing_row_raises(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        with pytest.raises(LookupError):
            campaigns.add_lens_verdict(
                campaign, "s", lens="root-cause", verdict="CONFIRM", repair_of=999
            )

    def test_append_only_trigger_blocks_update_and_delete(
        self, campaigns, campaign, raw
    ):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="CONFIRM"
        )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            raw.execute("UPDATE review_lens_verdict SET verdict='REFUTE' WHERE id=1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute("DELETE FROM review_lens_verdict WHERE id=1")


# ── Validation ledger ─────────────────────────────────────────────────────


class TestValidationLedger:
    def test_rows_append_and_attach(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_validation_row(
            campaign, "s",
            red_intended=2, red_observed=2, green_total=5, green_passed=5,
            guards=1, harness_notes={"tag": "test-r0-b0", "harness_fixes": []},
        )
        campaigns.add_validation_row(
            campaign, "s",
            red_intended=3, red_observed=3, green_total=6, green_passed=6,
        )
        f = campaigns.get_finding(campaign, "s")
        assert len(f["validation"]) == 2
        assert f["validation"][0]["harness_notes"]["tag"] == "test-r0-b0"
        assert f["validation"][1]["red_intended"] == 3

    def test_append_only_trigger_blocks_update_and_delete(
        self, campaigns, campaign, raw
    ):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_validation_row(campaign, "s", red_intended=1, red_observed=1)
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            raw.execute("UPDATE validation_ledger SET red_observed=0 WHERE id=1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute("DELETE FROM validation_ledger WHERE id=1")


# ── Campaign ledger rollup (INDEX.md shape) ──────────────────────────────


class TestCampaignLedger:
    def _seed(self, campaigns, catalog, project, campaign):
        """Seed a mini-campaign mirroring the SHAPE of a real aug16 ledger
        (all data invented)."""
        # Finding 1: clean two-lens CONFIRM, fully validated.
        campaigns.create_finding(
            campaign=campaign, slug="frobnicator-nre-on-teardown",
            source_kind="sentry",
            root_cause="handler outlives its pool rental",
        )
        digest = _insert_evidence(catalog, project, "doc://tracker/grp-42")
        campaigns.link_finding_evidence(
            campaign, "frobnicator-nre-on-teardown", digest, role="signal"
        )
        for lens in ("root-cause", "lifecycle-regression"):
            campaigns.add_lens_verdict(
                campaign, "frobnicator-nre-on-teardown",
                lens=lens, verdict="CONFIRM",
            )
        campaigns.add_validation_row(
            campaign, "frobnicator-nre-on-teardown",
            red_intended=2, red_observed=2, green_total=5, green_passed=5,
        )
        campaigns.set_finding_state(
            campaign, "frobnicator-nre-on-teardown", "confirmed_validated"
        )
        # Finding 2: REFUTE → repaired, validated with guards.
        campaigns.create_finding(
            campaign=campaign, slug="widget-list-double-free",
            source_kind="slack",
            root_cause="dispose called twice on cancel path",
        )
        campaigns.add_lens_verdict(
            campaign, "widget-list-double-free",
            lens="root-cause", verdict="CONFIRM",
        )
        refute_id = campaigns.add_lens_verdict(
            campaign, "widget-list-double-free",
            lens="lifecycle-regression", verdict="REFUTE",
            rationale="teardown pair missing for the moved subscription",
        )
        campaigns.add_lens_verdict(
            campaign, "widget-list-double-free",
            lens="lifecycle-regression", verdict="CONFIRM",
            rationale="repair re-verified against pin",
            repair_of=refute_id,
        )
        campaigns.add_validation_row(
            campaign, "widget-list-double-free",
            red_intended=1, red_observed=1, green_total=3, green_passed=3,
            guards=2,
        )
        campaigns.set_finding_state(
            campaign, "widget-list-double-free", "confirmed_validated"
        )
        # Finding 3: NO_GO (already-fixed), never reviewed/validated.
        campaigns.create_finding(
            campaign=campaign, slug="sprocket-timeout-flood",
            source_kind="autoclosed",
        )
        campaigns.set_finding_state(
            campaign, "sprocket-timeout-flood", "no_go",
            no_go_reason="already-fixed",
        )

    def test_rollup_shape(self, campaigns, catalog, project, campaign):
        self._seed(campaigns, catalog, project, campaign)
        ledger = campaigns.campaign_ledger(campaign)
        assert ledger["campaign"] == campaign
        assert ledger["base_commit"] == "deadbeef1234"
        assert ledger["counts"] == {"confirmed_validated": 2, "no_go": 1}
        rows = {r["slug"]: r for r in ledger["findings"]}
        assert set(rows) == {
            "frobnicator-nre-on-teardown",
            "widget-list-double-free",
            "sprocket-timeout-flood",
        }
        r1 = rows["frobnicator-nre-on-teardown"]
        assert r1["source"] == "sentry"
        assert r1["state"] == "confirmed_validated"
        assert r1["root_cause"] == "handler outlives its pool rental"
        assert r1["review"] == "CONFIRM ×2"
        assert r1["validation"] == "RED 2/2 GREEN 5/5"
        r2 = rows["widget-list-double-free"]
        assert r2["review"] == "CONFIRM ×1 + REFUTE→repaired"
        assert r2["validation"] == "RED 1/1 GREEN 3/3 + 2 guards"
        r3 = rows["sprocket-timeout-flood"]
        assert r3["source"] == "autoclosed"
        assert r3["state"] == "no_go (already-fixed)"
        assert r3["review"] == "—"
        assert r3["validation"] == "—"

    def test_open_refute_shows_in_summary(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_lens_verdict(
            campaign, "s", lens="root-cause", verdict="REFUTE"
        )
        ledger = campaigns.campaign_ledger(campaign)
        assert ledger["findings"][0]["review"] == "REFUTE ×1 open"

    def test_validation_summary_uses_latest_row(self, campaigns, campaign):
        campaigns.create_finding(campaign=campaign, slug="s")
        campaigns.add_validation_row(
            campaign, "s", red_intended=2, red_observed=1,
            green_total=4, green_passed=3,
        )
        campaigns.add_validation_row(
            campaign, "s", red_intended=2, red_observed=2,
            green_total=4, green_passed=4,
        )
        ledger = campaigns.campaign_ledger(campaign)
        assert ledger["findings"][0]["validation"] == "RED 2/2 GREEN 4/4"


# ── CLI smoke ─────────────────────────────────────────────────────────────


class TestCli:
    def test_cli_round_trip(self, campaigns, project, capsys):
        assert campaigns.main(["init"]) == 0
        assert campaigns.main([
            "create-campaign", "--project", project, "--name", "cli-camp",
            "--kind", "bugsweep", "--base-commit", "cafe1234",
        ]) == 0
        assert campaigns.main([
            "create-finding", "--campaign", "cli-camp",
            "--slug", "cli-slug", "--source-kind", "slack",
        ]) == 0
        assert campaigns.main(["ledger", "--campaign", "cli-camp"]) == 0
        out = capsys.readouterr().out
        ledger = json.loads(out[out.index('{\n  "campaign"'):])
        assert ledger["campaign"] == "cli-camp"
        assert ledger["findings"][0]["slug"] == "cli-slug"
