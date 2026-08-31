"""Tests for the sweeps layer: SweepSpec validation/digests, campaign spec
freezing, round lifecycle (cap, batching, convergence), spec-driven repair
policy, perf budgets, the foundry_stories adapter, and the old-DB
migration path.

All slugs/specs here are INVENTED; the fixture stories mirror the *shape*
of catalyrst story.md files, never their content.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pydantic
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
STORIES_ROOT = FIXTURES / "stories"

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

def _spec_dict(**over) -> dict:
    spec = {
        "kind": "bugsweep",
        "panel": {
            "lenses": [
                {"name": "root-cause", "prompt_ref": "lens:root-cause"},
                {"name": "lifecycle", "prompt_ref": "lens:lifecycle-regression"},
            ]
        },
        "rounds": {"max": 2},
    }
    spec.update(over)
    return spec

@pytest.fixture
def sweep(campaigns, project):
    campaigns.create_campaign(
        project=project,
        name="sweep-test",
        kind="bugsweep",
        base_commit="deadbeef",
        spec=_spec_dict(),
    )
    return "sweep-test"

@pytest.fixture
def finding(campaigns, sweep):
    campaigns.create_finding(campaign=sweep, slug="widget-crash")
    return "widget-crash"


@pytest.fixture
def raw(tmp_data_home):
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()

class TestSweepSpec:
    def test_digest_is_canonical(self):
        from bin import sweep_spec

        a = sweep_spec.validate_spec(_spec_dict())
        b = sweep_spec.validate_spec(dict(reversed(list(_spec_dict().items()))))
        assert a.digest() == b.digest()

    def test_defaults(self):
        from bin import sweep_spec

        s = sweep_spec.validate_spec(_spec_dict())
        assert s.rounds.batching == "required"
        assert s.rounds.convergence == "no_new_confirmed_findings"
        assert s.rounds.repair_max_cycles_per_finding == 1
        assert s.publish.gate == "human"

    def test_empty_lenses_rejected(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError, match="at least one lens"):
            sweep_spec.validate_spec(_spec_dict(panel={"lenses": []}))

    def test_duplicate_lens_names_rejected(self):
        from bin import sweep_spec

        lens = {"name": "root-cause", "prompt_ref": "lens:root-cause"}
        with pytest.raises(pydantic.ValidationError, match="duplicate lens"):
            sweep_spec.validate_spec(_spec_dict(panel={"lenses": [lens, lens]}))

    def test_perfsweep_requires_perf_budget_mode(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError, match="perf_budget"):
            sweep_spec.validate_spec(_spec_dict(kind="perfsweep"))

    def test_perf_budgets_only_with_perf_mode(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError, match="only valid"):
            sweep_spec.validate_spec(
                _spec_dict(
                    validation={
                        "mode": "red_green",
                        "perf_budgets": [{"metric": "m", "budget": 1}],
                    }
                )
            )

    def test_repair_cycles_capped_at_one(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError, match="0 or 1"):
            sweep_spec.validate_spec(
                _spec_dict(rounds={"repair_max_cycles_per_finding": 2})
            )

    def test_slopsweep_rejects_red_green(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError, match="rubric_only"):
            sweep_spec.validate_spec(
                _spec_dict(kind="slopsweep", validation={"mode": "red_green"})
            )

    def test_example_configs_validate(self):
        from bin import sweep_spec

        examples = sorted(
            (Path(__file__).parent.parent / "configs" / "sweeps").glob("*.json")
        )
        assert examples, "no example sweep configs found"
        for path in examples:
            sweep_spec.validate_spec(json.loads(path.read_text()))

class TestCampaignSpec:
    def test_spec_frozen_on_row(self, campaigns, sweep):
        camp = campaigns.get_campaign(sweep)
        assert camp["spec_digest"]
        assert json.loads(camp["spec_json"])["kind"] == "bugsweep"
        spec = campaigns.get_sweep_spec(sweep)
        assert spec.digest() == camp["spec_digest"]

    def test_kind_mismatch_rejected(self, campaigns, project):
        with pytest.raises(ValueError, match="does not match spec.kind"):
            campaigns.create_campaign(
                project=project,
                name="mismatch",
                kind="perfsweep",
                spec=_spec_dict(),
            )

    def test_new_kinds_accepted(self, campaigns, project):
        campaigns.create_campaign(project=project, name="ps", kind="perfsweep")
        assert campaigns.get_campaign("ps")["kind"] == "perfsweep"

    def test_speclless_campaign_has_no_spec(self, campaigns, project):
        campaigns.create_campaign(project=project, name="plain", kind="bugsweep")
        assert campaigns.get_sweep_spec("plain") is None

class TestRounds:
    def test_open_close_converged(self, campaigns, sweep, finding):
        rnd = campaigns.open_round(sweep)
        assert rnd["round_no"] == 1
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="REFUTE", rationale="r"
        )
        campaigns.add_lens_verdict(
            sweep, finding, lens="lifecycle", verdict="REFUTE", rationale="r"
        )
        out = campaigns.close_round(sweep)
        assert out["outcome"] == "converged"
        assert out["summary"]["per_lens"]["root-cause"]["REFUTE"] == 1

    def test_confirm_means_not_converged(self, campaigns, sweep, finding):
        campaigns.open_round(sweep)
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="CONFIRM"
        )
        campaigns.add_lens_verdict(
            sweep, finding, lens="lifecycle", verdict="REFUTE"
        )
        assert campaigns.close_round(sweep)["outcome"] == "not_converged"

    def test_only_one_open_round(self, campaigns, sweep):
        campaigns.open_round(sweep)
        with pytest.raises(ValueError, match="already has an open round"):
            campaigns.open_round(sweep)

    def test_round_cap_enforced(self, campaigns, sweep, finding):
        for _ in range(2):
            campaigns.open_round(sweep)
            campaigns.add_lens_verdict(
                sweep, finding, lens="root-cause", verdict="CONFIRM"
            )
            campaigns.add_lens_verdict(
                sweep, finding, lens="lifecycle", verdict="REFUTE"
            )
            campaigns.close_round(sweep)
        with pytest.raises(ValueError, match=r"rounds\.max \(2\) reached"):
            campaigns.open_round(sweep)

    def test_batching_required_blocks_partial_close(
        self, campaigns, sweep, finding
    ):
        campaigns.open_round(sweep)
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="CONFIRM"
        )
        with pytest.raises(ValueError, match="no verdict from lens.*lifecycle"):
            campaigns.close_round(sweep)

    def test_spec_campaign_refuses_verdicts_outside_round(
        self, campaigns, sweep, finding
    ):
        with pytest.raises(ValueError, match="no open round"):
            campaigns.add_lens_verdict(
                sweep, finding, lens="root-cause", verdict="CONFIRM"
            )

    def test_specless_campaign_verdicts_unrounded(
        self, campaigns, catalog, project
    ):
        campaigns.create_campaign(project=project, name="plain", kind="bugsweep")
        campaigns.create_finding(campaign="plain", slug="s")
        campaigns.add_lens_verdict("plain", "s", lens="l", verdict="CONFIRM")
        f = campaigns.get_finding("plain", "s")
        assert f["lens_verdicts"][0]["round_id"] is None

    def test_verdicts_attach_to_open_round(self, campaigns, sweep, finding):
        campaigns.open_round(sweep)
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="REFUTE"
        )
        f = campaigns.get_finding(sweep, finding)
        rid = campaigns.current_open_round(sweep)["id"]
        assert f["lens_verdicts"][0]["round_id"] == rid

    def test_all_findings_settled_criterion(self, campaigns, project):
        campaigns.create_campaign(
            project=project,
            name="settled",
            kind="bugsweep",
            spec=_spec_dict(
                rounds={"max": 3, "convergence": "all_findings_settled"},
                panel={"lenses": [{"name": "l1", "prompt_ref": "lens:x"}]},
            ),
        )
        campaigns.create_finding(campaign="settled", slug="a")
        campaigns.open_round("settled")
        campaigns.add_lens_verdict("settled", "a", lens="l1", verdict="REFUTE")
        assert campaigns.close_round("settled")["outcome"] == "not_converged"
        campaigns.set_finding_state(
            "settled", "a", "no_go", no_go_reason="by-design"
        )
        campaigns.open_round("settled")
        campaigns.add_lens_verdict("settled", "a", lens="l1", verdict="REFUTE")
        assert campaigns.close_round("settled")["outcome"] == "converged"

    def test_repairs_forbidden_when_spec_says_zero(self, campaigns, project):
        campaigns.create_campaign(
            project=project,
            name="norepair",
            kind="bugsweep",
            spec=_spec_dict(
                rounds={"max": 1, "repair_max_cycles_per_finding": 0},
                panel={"lenses": [{"name": "l1", "prompt_ref": "lens:x"}]},
            ),
        )
        campaigns.create_finding(campaign="norepair", slug="a")
        campaigns.open_round("norepair")
        vid = campaigns.add_lens_verdict(
            "norepair", "a", lens="l1", verdict="REFUTE"
        )
        with pytest.raises(ValueError, match="forbids repairs"):
            campaigns.add_lens_verdict(
                "norepair", "a", lens="l1", verdict="CONFIRM", repair_of=vid
            )

    def test_metrics_rollup(self, campaigns, sweep, finding):
        campaigns.open_round(sweep)
        vid = campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="REFUTE"
        )
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="CONFIRM", repair_of=vid
        )
        campaigns.add_lens_verdict(
            sweep, finding, lens="lifecycle", verdict="CONFIRM"
        )
        campaigns.close_round(sweep)
        m = campaigns.sweep_metrics(sweep)
        assert m["rounds_run"] == 1
        assert m["converged_at_round"] is None
        assert m["per_lens"]["root-cause"]["repair_rate"] == 1.0
        assert m["per_lens"]["lifecycle"]["refute_rate"] == 0.0

class TestPerfBudgets:
    def test_perf_rows_recorded_and_summarized(self, campaigns, sweep, finding):
        campaigns.add_validation_row(
            sweep,
            finding,
            red_intended=1,
            red_observed=1,
            green_total=3,
            green_passed=3,
            perf=[
                {"metric": "allocs", "measured": 0, "budget": 0},
                {"metric": "p95_ms", "measured": 20.0, "budget": 16.6},
            ],
        )
        f = campaigns.get_finding(sweep, finding)
        assert f["validation"][0]["perf_json"][0]["metric"] == "allocs"
        ledger = campaigns.campaign_ledger(sweep)
        row = next(r for r in ledger["findings"] if r["slug"] == finding)
        assert "PERF 1/2" in row["validation"]

    def test_bad_perf_entry_rejected(self, campaigns, sweep, finding):
        with pytest.raises(ValueError, match="missing keys"):
            campaigns.add_validation_row(sweep, finding, perf=[{"metric": "m"}])
        with pytest.raises(ValueError, match="direction"):
            campaigns.add_validation_row(
                sweep,
                finding,
                perf=[{"metric": "m", "measured": 1, "budget": 2, "direction": "up"}],
            )

    def test_min_direction(self, campaigns):
        assert campaigns.perf_within_budget(
            {"metric": "fps", "measured": 60, "budget": 30, "direction": "min"}
        )
        assert not campaigns.perf_within_budget(
            {"metric": "fps", "measured": 20, "budget": 30, "direction": "min"}
        )

class TestFoundryStories:
    def test_collect_parses_stories(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter

        refs = get_adapter("foundry_stories").collect(
            {"root": str(STORIES_ROOT)}, why="test sweep"
        )
        assert len(refs) == 2
        by_uri = {r.canonical_uri: r for r in refs}
        tour = by_uri["story:foundry/tour-activation#foundry-tour-activation"]
        assert tour.trust_tier.value == "tier2_internal"
        assert "fd_pledge_submitted_rate" in tour.excerpt
        assert "hypothesis:" in tour.excerpt
        assert tour.revision

    def test_surface_filter(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter

        refs = get_adapter("foundry_stories").collect(
            {"root": str(STORIES_ROOT), "surfaces": ["landings"]}, why="t"
        )
        assert [r.canonical_uri for r in refs] == [
            "story:landings/home#landings-home"
        ]

    def test_deterministic_digests(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter

        a = get_adapter("foundry_stories").collect({"root": str(STORIES_ROOT)}, why="t")
        b = get_adapter("foundry_stories").collect({"root": str(STORIES_ROOT)}, why="t")
        assert [r.digest for r in a] == [r.digest for r in b]

    def test_missing_root_raises(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter
        from bin.landscape.adapters.foundry_stories import FoundryStoryError

        with pytest.raises(FoundryStoryError, match="requires 'root'"):
            get_adapter("foundry_stories").collect({}, why="t")

    def test_bad_frontmatter_raises(self, tmp_data_home, tmp_path):
        from bin.landscape.adapters import get_adapter
        from bin.landscape.adapters.foundry_stories import FoundryStoryError

        bad = tmp_path / "s" / "x" / "story.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("no frontmatter here\n")
        with pytest.raises(FoundryStoryError, match="no YAML frontmatter"):
            get_adapter("foundry_stories").collect(
                {"root": str(tmp_path)}, why="t"
            )

class TestIngestFromSpec:
    def test_ingest_from_spec_foundry(self, campaigns, catalog, project):
        from bin import campaign_intake

        campaigns.create_campaign(
            project=project,
            name="story-sweep",
            kind="featuresweep",
            spec={
                "kind": "featuresweep",
                "corpus": [
                    {
                        "adapter": "foundry_stories",
                        "config": {"root": str(STORIES_ROOT)},
                    }
                ],
                "intake": {"max_candidates": 1},
                "panel": {
                    "lenses": [
                        {"name": "spec-honesty", "prompt_ref": "lens:spec-honesty"}
                    ]
                },
                "validation": {"mode": "rubric_only"},
            },
        )
        out = campaign_intake.ingest_from_spec("story-sweep")
        assert len(out["created"]) == 1
        assert out["truncated"] is True
        assert out["evidence_count"] == 2

    def test_specless_campaign_rejected(self, campaigns, project):
        from bin import campaign_intake

        campaigns.create_campaign(project=project, name="plain", kind="bugsweep")
        with pytest.raises(campaign_intake.IntakeError, match="no sweep spec"):
            campaign_intake.ingest_from_spec("plain")

class TestLenses:
    def test_seed_and_resolve(self, tmp_data_home, monkeypatch):
        monkeypatch.setenv("PROMPT_BACKEND", "local")
        from bin import lenses

        versions = lenses.ensure_default_lenses()
        assert versions["lens:root-cause"] >= 1
        assert versions == lenses.ensure_default_lenses()
        text = lenses.resolve("lens:slop")
        assert "internal rules" in text

    def test_resolve_panel(self, tmp_data_home, monkeypatch):
        monkeypatch.setenv("PROMPT_BACKEND", "local")
        from bin import lenses, sweep_spec

        lenses.ensure_default_lenses()
        spec = sweep_spec.validate_spec(
            _spec_dict(
                panel={
                    "lenses": [
                        {"name": "slop", "prompt_ref": "lens:slop"},
                    ]
                }
            )
        )
        panel = lenses.resolve_panel(spec)
        assert set(panel) == {"slop"}

class TestMigration:
    def test_old_campaign_table_upgraded(self, tmp_data_home):
        db = tmp_data_home / "judgements.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE project (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'active'
            );
            INSERT INTO project(name) VALUES ('widget-engine');
            CREATE TABLE campaign (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id   INTEGER NOT NULL REFERENCES project(id),
              name         TEXT NOT NULL UNIQUE,
              kind         TEXT NOT NULL CHECK (kind IN ('bugsweep','release')),
              objective    TEXT NOT NULL DEFAULT '',
              time_window  TEXT NOT NULL DEFAULT '',
              status       TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','closed')),
              base_commit  TEXT NOT NULL DEFAULT '',
              created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO campaign(project_id, name, kind)
              VALUES (1, 'old-sweep', 'bugsweep');
            """
        )
        conn.commit()
        conn.close()

        from bin import campaigns

        campaigns.init()
        old = campaigns.get_campaign("old-sweep")
        assert old["kind"] == "bugsweep"
        assert old["spec_json"] == ""
        campaigns.create_campaign(
            project="widget-engine", name="new-perf", kind="perfsweep"
        )
        assert campaigns.get_campaign("new-perf")["kind"] == "perfsweep"


class TestDispositions:
    def _run_two_unconverged_rounds(self, campaigns, sweep, finding):
        for _ in range(2):
            campaigns.open_round(sweep)
            campaigns.add_lens_verdict(
                sweep, finding, lens="root-cause", verdict="CONFIRM"
            )
            campaigns.add_lens_verdict(
                sweep, finding, lens="lifecycle", verdict="REFUTE"
            )
            campaigns.close_round(sweep)

    def test_close_blocked_until_disposed(self, campaigns, sweep, finding):
        self._run_two_unconverged_rounds(campaigns, sweep, finding)
        with pytest.raises(ValueError, match="human tie-break required"):
            campaigns.close_campaign(sweep)
        campaigns.dispose_finding(
            sweep, finding, decision="ship_anyway", rationale="known cosmetic"
        )
        campaigns.close_campaign(sweep)
        assert campaigns.get_campaign(sweep)["status"] == "closed"

    def test_open_round_blocks_close(self, campaigns, sweep):
        campaigns.open_round(sweep)
        with pytest.raises(ValueError, match="open round"):
            campaigns.close_campaign(sweep)

    def test_converged_campaign_closes_without_dispositions(
        self, campaigns, sweep, finding
    ):
        campaigns.open_round(sweep)
        campaigns.add_lens_verdict(sweep, finding, lens="root-cause", verdict="REFUTE")
        campaigns.add_lens_verdict(sweep, finding, lens="lifecycle", verdict="REFUTE")
        campaigns.close_round(sweep)
        campaigns.close_campaign(sweep)
        assert campaigns.get_campaign(sweep)["status"] == "closed"

    def test_rationale_required(self, campaigns, sweep, finding):
        with pytest.raises(ValueError, match="rationale"):
            campaigns.dispose_finding(
                sweep, finding, decision="needs_fix", rationale="  "
            )

    def test_no_go_disposition_moves_state(self, campaigns, sweep, finding):
        campaigns.dispose_finding(
            sweep,
            finding,
            decision="no_go",
            rationale="signal was stale",
            no_go_reason="stale-signal",
        )
        f = campaigns.get_finding(sweep, finding)
        assert f["state"] == "no_go"
        assert campaigns.campaign_dispositions(sweep)[finding]["decision"] == "no_go"

    def test_no_go_reason_only_with_no_go(self, campaigns, sweep, finding):
        with pytest.raises(ValueError, match="only valid"):
            campaigns.dispose_finding(
                sweep,
                finding,
                decision="ship_anyway",
                rationale="r",
                no_go_reason="by-design",
            )

    def test_latest_disposition_wins(self, campaigns, sweep, finding):
        campaigns.dispose_finding(
            sweep, finding, decision="needs_fix", rationale="first look"
        )
        campaigns.dispose_finding(
            sweep, finding, decision="ship_anyway", rationale="second look"
        )
        assert (
            campaigns.campaign_dispositions(sweep)[finding]["decision"]
            == "ship_anyway"
        )

    def test_disposition_rows_append_only(self, campaigns, sweep, finding, raw):
        campaigns.dispose_finding(
            sweep, finding, decision="needs_fix", rationale="r"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE finding_disposition SET decision='ship_anyway'")


class TestExportCorpus:
    def test_export_writes_signal_excerpts(
        self, campaigns, catalog, project, tmp_path
    ):
        from bin import campaign_intake

        campaigns.create_campaign(
            project=project,
            name="story-export",
            kind="featuresweep",
            spec={
                "kind": "featuresweep",
                "corpus": [
                    {
                        "adapter": "foundry_stories",
                        "config": {"root": str(STORIES_ROOT)},
                    }
                ],
                "panel": {
                    "lenses": [{"name": "spec-honesty", "prompt_ref": "lens:x"}]
                },
                "validation": {"mode": "rubric_only"},
            },
        )
        campaign_intake.ingest_from_spec("story-export")
        out = campaigns.export_corpus("story-export", str(tmp_path / "corpus"))
        assert len(out) == 2
        text = Path(out[0]).read_text()
        assert text.startswith("# ")
        assert "story:" in text


class TestAdversarialFixes:
    """Regression pins for the double-adversarial verification findings."""

    def test_closed_round_insert_refused_at_sql(self, campaigns, sweep, finding, raw):
        campaigns.open_round(sweep)
        campaigns.add_lens_verdict(sweep, finding, lens="root-cause", verdict="REFUTE")
        campaigns.add_lens_verdict(sweep, finding, lens="lifecycle", verdict="REFUTE")
        campaigns.close_round(sweep)
        rid = raw.execute("SELECT id FROM sweep_round").fetchone()["id"]
        fid = raw.execute("SELECT id FROM finding").fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError, match="round is not open"):
            raw.execute(
                "INSERT INTO review_lens_verdict(finding_id, lens, verdict, round_id) "
                "VALUES (?, 'root-cause', 'CONFIRM', ?)",
                (fid, rid),
            )

    def test_second_repair_refused_at_sql(self, campaigns, sweep, finding, raw):
        campaigns.open_round(sweep)
        vid = campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="REFUTE"
        )
        campaigns.add_lens_verdict(
            sweep, finding, lens="root-cause", verdict="CONFIRM", repair_of=vid
        )
        rid = raw.execute(
            "SELECT id FROM sweep_round WHERE status='open'"
        ).fetchone()["id"]
        fid = raw.execute("SELECT id FROM finding").fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            raw.execute(
                "INSERT INTO review_lens_verdict"
                "(finding_id, lens, verdict, repair_of, round_id) "
                "VALUES (?, 'root-cause', 'CONFIRM', ?, ?)",
                (fid, vid, rid),
            )

    def test_empty_lens_rejected(self, campaigns, sweep, finding):
        campaigns.open_round(sweep)
        with pytest.raises(ValueError, match="non-empty"):
            campaigns.add_lens_verdict(sweep, finding, lens="  ", verdict="CONFIRM")

    def test_off_panel_lens_rejected(self, campaigns, sweep, finding):
        campaigns.open_round(sweep)
        with pytest.raises(ValueError, match="not in the sweep panel"):
            campaigns.add_lens_verdict(
                sweep, finding, lens="not-a-lens", verdict="CONFIRM"
            )

    def test_surfaces_string_rejected(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter
        from bin.landscape.adapters.foundry_stories import FoundryStoryError

        with pytest.raises(FoundryStoryError, match="list of directory names"):
            get_adapter("foundry_stories").collect(
                {"root": str(STORIES_ROOT), "surfaces": "foundry"}, why="t"
            )

    def test_unknown_surface_rejected(self, tmp_data_home):
        from bin.landscape.adapters import get_adapter
        from bin.landscape.adapters.foundry_stories import FoundryStoryError

        with pytest.raises(FoundryStoryError, match="foundries.*match no story"):
            get_adapter("foundry_stories").collect(
                {"root": str(STORIES_ROOT), "surfaces": ["foundries"]}, why="t"
            )

    def test_nan_budget_rejected(self):
        from bin import sweep_spec

        with pytest.raises(pydantic.ValidationError):
            sweep_spec.validate_spec(
                _spec_dict(
                    kind="perfsweep",
                    validation={
                        "mode": "perf_budget",
                        "perf_budgets": [
                            {"metric": "m", "budget": float("nan")}
                        ],
                    },
                )
            )

    def test_stray_campaign_new_recovered(self, campaigns, tmp_data_home, raw):
        raw.execute("CREATE TABLE campaign_new (id INTEGER PRIMARY KEY)")
        raw.commit()
        conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='campaign'"
        ).fetchone()
        conn.close()
        campaigns.init()
        with sqlite3.connect(str(tmp_data_home / "judgements.db")) as chk:
            names = {
                r[0]
                for r in chk.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "campaign" in names

    def test_reingest_title_drift_skipped(
        self, campaigns, catalog, project, tmp_path
    ):
        import shutil

        from bin import campaign_intake

        stories = tmp_path / "stories"
        shutil.copytree(STORIES_ROOT, stories)
        campaigns.create_campaign(
            project=project,
            name="drift",
            kind="featuresweep",
            spec={
                "kind": "featuresweep",
                "corpus": [
                    {"adapter": "foundry_stories", "config": {"root": str(stories)}}
                ],
                "panel": {
                    "lenses": [{"name": "spec-honesty", "prompt_ref": "lens:x"}]
                },
                "validation": {"mode": "rubric_only"},
            },
        )
        first = campaign_intake.ingest_from_spec("drift")
        assert len(first["created"]) == 2
        story = stories / "landings" / "home" / "story.md"
        story.write_text(
            story.read_text().replace("status: draft", "status: running")
        )
        second = campaign_intake.ingest_from_spec("drift")
        assert second["created"] == []
        assert len(campaigns.list_findings("drift")) == 2

    def test_export_corpus_rejects_unsafe_slug(self, campaigns, sweep, tmp_path):
        campaigns.create_finding(campaign=sweep, slug="../escape")
        with pytest.raises(ValueError, match="not a safe"):
            campaigns.export_corpus(sweep, str(tmp_path / "out"))


class TestValidateSpecCli:
    def test_validate_spec_prints_digest(self, campaigns, tmp_path, capsys):
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(_spec_dict()))
        assert campaigns.main(["validate-spec", "--spec-file", str(path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "bugsweep"
        assert len(out["digest"]) == 64

    def test_validate_spec_fails_loudly(self, campaigns, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"kind": "bugsweep"}))
        with pytest.raises(Exception):
            campaigns.main(["validate-spec", "--spec-file", str(path)])


class TestEnactWorkflow:
    RUN_DIR = str(FIXTURES / "workflow-run")

    def test_enact_replays_run_as_campaign(self, campaigns, catalog, project):
        from bin import enact_workflow

        out = enact_workflow.enact(
            self.RUN_DIR, project=project, campaign="enacted-1"
        )
        assert out["findings"] == ["widget-frobs-twice-on-reload-" + out["findings"][0].rsplit("-", 1)[-1]]
        assert out["verdicts"] == 2
        assert out["round"]["outcome"] == "not_converged"
        slug = out["findings"][0]
        dossier = campaigns.get_finding("enacted-1", slug)
        assert {v["lens"] for v in dossier["lens_verdicts"]} == {"reproduce-it", "defender"}
        assert dossier["root_cause"].startswith("reload path")
        assert len(dossier["evidence"]) == 1
        spec = campaigns.get_sweep_spec("enacted-1")
        assert [l.name for l in spec.panel.lenses] == ["defender", "reproduce-it"]

    def test_enact_requires_verifiers(self, campaigns, project, tmp_path):
        import json as j

        from bin import enact_workflow

        (tmp_path / "journal.jsonl").write_text(
            j.dumps({"type": "result", "agentId": "x", "result": "just text"})
        )
        with pytest.raises(enact_workflow.EnactError, match="no verifier"):
            enact_workflow.enact(str(tmp_path), project=project, campaign="empty-run")


class TestRunnerSpecAndPack:
    def _spec_with_runner(self):
        spec = _spec_dict()
        spec["runner"] = {"driver": "opencode", "model": "anthropic/claude-sonnet-5", "parallel": 3}
        return spec

    def test_runner_optional_and_validated(self):
        from bin import sweep_spec

        assert sweep_spec.validate_spec(_spec_dict()).runner is None
        s = sweep_spec.validate_spec(self._spec_with_runner())
        assert s.runner.driver == "opencode"
        assert s.runner.parallel == 3
        with pytest.raises(pydantic.ValidationError):
            sweep_spec.validate_spec(
                dict(_spec_dict(), runner={"driver": "opencode", "parallel": 0})
            )
        with pytest.raises(pydantic.ValidationError):
            sweep_spec.validate_spec(dict(_spec_dict(), runner={"driver": "cron"}))

    def test_pack_generation(self, tmp_path, tmp_data_home, monkeypatch):
        monkeypatch.setenv("PROMPT_BACKEND", "local")
        from bin import lenses, runner_pack

        lenses.ensure_default_lenses()
        spec = self._spec_with_runner()
        spec["panel"]["lenses"] = [
            {"name": "root-cause", "prompt_ref": "lens:root-cause", "burden": "refute"}
        ]
        out = runner_pack.generate(spec, name="dataroom-3", out_dir=str(tmp_path / "oc"))
        assert len(out) == 3
        cmd = (tmp_path / "oc" / "commands" / "sweep-dataroom-3.md").read_text()
        assert "agent: sweep-orch-dataroom-3" in cmd
        assert "$ARGUMENTS" in cmd
        orch = (tmp_path / "oc" / "agents" / "sweep-orch-dataroom-3.md").read_text()
        assert "mode: primary" in orch
        assert "model: anthropic/claude-sonnet-5" in orch
        assert "close-round --campaign dataroom-3" in orch
        assert "NEVER call dispose-finding yourself" in orch
        assert "at most 3 in flight" in orch
        worker = (
            tmp_path / "oc" / "agents" / "sweep-lens-root-cause-dataroom-3.md"
        ).read_text()
        assert "mode: subagent" in worker
        assert "ROOT-CAUSE lens" in worker
        assert "VERDICT: CONFIRM" in worker

    def test_pack_refuses_non_opencode_driver(self, tmp_path):
        from bin import runner_pack

        with pytest.raises(runner_pack.RunnerPackError, match="must be 'opencode'"):
            runner_pack.generate(_spec_dict(), name="x", out_dir=str(tmp_path))
