"""Tests for bin/catalog.py — project-landscape catalog persistence
(ADR 0001 steps 1–3, ADR 0002 CAS + immutability)."""
from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from bin.landscape import (
    EvidenceRef,
    LandscapeSnapshot,
    Role,
    SourceType,
    TrustTier,
    WorkflowSpec,
    WorkflowStep,
    build_pack,
    canonical_json,
    content_digest,
)


@pytest.fixture
def catalog(tmp_data_home):
    from bin import catalog as mod

    mod.init()
    return mod


@pytest.fixture
def raw(tmp_data_home):
    """Raw connection for asserting on stored (unresolved) column values."""
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _evidence(uri="doc://guide", tier=TrustTier.TIER2_INTERNAL, **kw) -> EvidenceRef:
    return EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri=uri,
        trust_tier=tier,
        why_selected="relevant",
        **kw,
    )


# ── init / schema ───────────────────────────────────────────────────────


class TestInit:
    def test_init_twice_is_idempotent(self, catalog):
        catalog.init()  # second call on an existing DB must not raise
        catalog.init()  # third, for luck

    def test_all_catalog_tables_and_triggers_exist(self, catalog, raw):
        tables = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for t in (
            "project", "component", "source", "capability", "skill",
            "environment", "policy", "component_capability", "project_skill",
            "evidence_ref", "landscape_snapshot", "snapshot_evidence",
            "context_pack", "workflow_spec",
            # workflow_run unblocked once the Temporal spike landed (b9585be):
            # ADR 0001 §4 step 6, written only by Python Temporal Activities.
            "workflow_run",
        ):
            assert t in tables, f"missing table {t}"
        triggers = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for tr in (
            "evidence_ref_immutable", "landscape_snapshot_immutable",
            "snapshot_evidence_immutable", "context_pack_immutable",
            "workflow_spec_immutable",
        ):
            assert tr in triggers, f"missing trigger {tr}"

    def test_judgement_init_db_also_creates_catalog_tables(
        self, tmp_data_home, fake_langfuse, monkeypatch
    ):
        """The shared executescript path in judgement.init_db picks up the
        appended DDL — fresh fixtures relying on init_db get the catalog."""
        monkeypatch.setattr(
            "bin.prompts._client_factory", lambda: fake_langfuse.as_client()
        )
        fake_langfuse.enable("create_prompt")
        fake_langfuse.enable("list_prompts")
        fake_langfuse.enable("set_label")
        import importlib
        import judgement

        importlib.reload(judgement)
        judgement.init_db()
        judgement.init_db()  # idempotent with the new DDL too
        conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert {"project", "evidence_ref", "workflow_spec"} <= tables


# ── busy_timeout hygiene (ADR 0001 §2 / migration step 1) ───────────────


class TestBusyTimeout:
    def test_catalog_connection_sets_busy_timeout(self, catalog):
        conn = catalog._connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()

    def test_domains_connection_sets_busy_timeout(self, catalog):
        from bin import domains

        conn = domains._connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()

    def test_optimizer_runs_connection_sets_busy_timeout(self, catalog):
        from bin import optimizer_runs

        conn = optimizer_runs._connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()

    def test_judgement_connection_sets_busy_timeout(self, catalog):
        import importlib
        import judgement

        importlib.reload(judgement)  # re-read DATA_TOURNAMENTS_HOME
        conn = judgement._connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()


# ── Mutable catalog CRUD ─────────────────────────────────────────────────


class TestMutableCrud:
    def test_project_round_trip(self, catalog):
        pid = catalog.create_project(
            name="unity-explorer", description="the app", metadata={"x": 1}
        )
        proj = catalog.get_project("unity-explorer")
        assert proj["id"] == pid
        assert proj["description"] == "the app"
        assert proj["metadata"] == {"x": 1}
        assert proj["status"] == "active"
        assert [p["name"] for p in catalog.list_projects()] == ["unity-explorer"]
        catalog.archive_project("unity-explorer")
        assert catalog.list_projects() == []
        assert catalog.get_project("unity-explorer")["status"] == "archived"

    def test_project_is_not_domain(self, catalog):
        """'project' (landscape) and 'domain' (evaluation lens) are separate
        tables — creating a project must not touch domain."""
        catalog.create_project(name="p1")
        conn = catalog._connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM domain").fetchone()[0] == 0
        finally:
            conn.close()

    def test_component_round_trip_scoped_by_project(self, catalog):
        catalog.create_project(name="p1")
        cid = catalog.create_component(project="p1", name="editor", kind="app")
        comp = catalog.get_component("p1", "editor")
        assert comp["id"] == cid and comp["kind"] == "app"
        assert [c["name"] for c in catalog.list_components("p1")] == ["editor"]
        catalog.archive_component("p1", "editor")
        assert catalog.list_components("p1") == []

    def test_source_round_trip(self, catalog):
        catalog.create_project(name="p1")
        sid = catalog.create_source(
            project="p1", name="repo", kind="git",
            locator="git@github.com:acme/x.git", trust_tier=1,
            config={"branch": "main"},
        )
        src = catalog.get_source("p1", "repo")
        assert src["id"] == sid
        assert src["trust_tier"] == 1
        assert src["config"] == {"branch": "main"}
        catalog.archive_source("p1", "repo")
        assert catalog.list_sources("p1") == []

    def test_source_rejects_bad_trust_tier(self, catalog):
        catalog.create_project(name="p1")
        with pytest.raises(ValueError):
            catalog.create_source(
                project="p1", name="s", kind="git", locator="x", trust_tier=7
            )

    def test_capability_round_trip(self, catalog):
        catalog.create_capability(name="build", description="run builds")
        assert catalog.get_capability("build")["description"] == "run builds"
        assert [c["name"] for c in catalog.list_capabilities()] == ["build"]
        catalog.archive_capability("build")
        assert catalog.list_capabilities() == []

    def test_skill_round_trip_and_latest_version(self, catalog):
        catalog.create_skill(name="release", version=1, locator="/skills/release")
        catalog.create_skill(
            name="release", version=2, locator="/skills/release", digest="abc"
        )
        assert catalog.get_skill("release")["version"] == 2  # latest
        assert catalog.get_skill("release", 1)["version"] == 1
        assert len(catalog.list_skills()) == 2
        catalog.archive_skill("release", 1)
        assert [s["version"] for s in catalog.list_skills()] == [2]

    def test_environment_round_trip(self, catalog):
        catalog.create_environment(
            name="e2b-preflight", kind="sandbox", config={"image": "x"}
        )
        env = catalog.get_environment("e2b-preflight")
        assert env["kind"] == "sandbox" and env["config"] == {"image": "x"}
        catalog.archive_environment("e2b-preflight")
        assert catalog.list_environments() == []

    def test_policy_round_trip(self, catalog):
        catalog.create_policy(
            name="deploy-gate", kind="approval", rule={"requires": "human"}
        )
        pol = catalog.get_policy("deploy-gate")
        assert pol["rule"] == {"requires": "human"}
        catalog.archive_policy("deploy-gate")
        assert catalog.list_policies() == []

    def test_join_tables(self, catalog):
        pid = catalog.create_project(name="p1")
        cid = catalog.create_component(project="p1", name="c", kind="app")
        capid = catalog.create_capability(name="judge")
        skid = catalog.create_skill(name="s", version=1, locator="/s")
        catalog.link_component_capability(cid, capid)
        catalog.link_component_capability(cid, capid)  # idempotent
        catalog.link_project_skill(pid, skid)
        conn = catalog._connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM component_capability"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM project_skill"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_get_missing_raises_lookup_error(self, catalog):
        with pytest.raises(LookupError):
            catalog.get_project("nope")
        with pytest.raises(LookupError):
            catalog.get_capability("nope")


class TestUniqueConstraints:
    def test_duplicate_project_name(self, catalog):
        catalog.create_project(name="p1")
        with pytest.raises(ValueError):
            catalog.create_project(name="p1")

    def test_duplicate_component_within_project_but_ok_across(self, catalog):
        catalog.create_project(name="p1")
        catalog.create_project(name="p2")
        catalog.create_component(project="p1", name="c", kind="app")
        with pytest.raises(ValueError):
            catalog.create_component(project="p1", name="c", kind="app")
        catalog.create_component(project="p2", name="c", kind="app")  # fine

    def test_duplicate_source_within_project(self, catalog):
        catalog.create_project(name="p1")
        catalog.create_source(project="p1", name="s", kind="git", locator="x")
        with pytest.raises(ValueError):
            catalog.create_source(project="p1", name="s", kind="docs", locator="y")

    def test_duplicate_capability_environment_policy(self, catalog):
        catalog.create_capability(name="x")
        with pytest.raises(ValueError):
            catalog.create_capability(name="x")
        catalog.create_environment(name="e", kind="ci")
        with pytest.raises(ValueError):
            catalog.create_environment(name="e", kind="ci")
        catalog.create_policy(name="p", kind="egress", rule={})
        with pytest.raises(ValueError):
            catalog.create_policy(name="p", kind="egress", rule={})

    def test_duplicate_skill_name_version(self, catalog):
        catalog.create_skill(name="s", version=1, locator="/s")
        with pytest.raises(ValueError):
            catalog.create_skill(name="s", version=1, locator="/other")


# ── Immutability triggers (ADR 0002 §4) ─────────────────────────────────


class TestImmutability:
    @pytest.fixture
    def seeded(self, catalog):
        catalog.create_project(name="p1")
        catalog.create_source(project="p1", name="s", kind="docs", locator="doc://")
        src = catalog.get_source("p1", "s")
        proj = catalog.get_project("p1")
        ref = _evidence()
        ref_digest = catalog.insert_evidence_ref(ref, source_id=src["id"])
        snapshot = LandscapeSnapshot(
            project="p1", created_at="2026-08-17T00:00:00Z", evidence=(ref,)
        )
        snap_digest = catalog.insert_landscape_snapshot(
            snapshot, project_id=proj["id"]
        )
        catalog.link_snapshot_evidence(snap_digest, ref_digest)
        pack = build_pack(snapshot, Role.JUDGE)
        pack_digest = catalog.insert_context_pack(pack)
        spec = WorkflowSpec(name="w", steps=(WorkflowStep(id="a", kind="gather"),))
        spec_digest = catalog.insert_workflow_spec(
            spec, project_id=proj["id"], pack_digest=pack_digest
        )
        return {
            "evidence_ref": ref_digest,
            "landscape_snapshot": snap_digest,
            "context_pack": pack_digest,
            "workflow_spec": spec_digest,
        }

    @pytest.mark.parametrize(
        "table,column",
        [
            ("evidence_ref", "summary"),
            ("landscape_snapshot", "schema_version"),
            ("context_pack", "role"),
            ("workflow_spec", "name"),
        ],
    )
    def test_update_raises(self, catalog, seeded, table, column):
        conn = catalog._connect()
        try:
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(
                    f"UPDATE {table} SET {column}='tampered' WHERE digest=?",
                    (seeded[table],),
                )
        finally:
            conn.close()

    def test_snapshot_evidence_update_raises(self, catalog, seeded):
        conn = catalog._connect()
        try:
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(
                    "UPDATE snapshot_evidence SET evidence_digest='x' "
                    "WHERE snapshot_digest=?",
                    (seeded["landscape_snapshot"],),
                )
        finally:
            conn.close()

    def test_reinsert_same_digest_is_noop(self, catalog, seeded, raw):
        src = catalog.get_source("p1", "s")
        catalog.insert_evidence_ref(_evidence(), source_id=src["id"])
        assert raw.execute("SELECT COUNT(*) FROM evidence_ref").fetchone()[0] == 1


# ── CAS threshold behavior (ADR 0002 §2–3) ──────────────────────────────


class TestCas:
    def test_small_body_stored_inline(self, catalog, raw, tmp_data_home):
        catalog.create_project(name="p1")
        catalog.create_source(project="p1", name="s", kind="docs", locator="doc://")
        src = catalog.get_source("p1", "s")
        digest = catalog.insert_evidence_ref(_evidence(), source_id=src["id"])
        row = raw.execute(
            "SELECT body FROM evidence_ref WHERE digest=?", (digest,)
        ).fetchone()
        assert row["body"] is not None  # inline
        assert not catalog.cas_path(digest).exists()
        # readback resolves to the identical canonical body
        got = catalog.get_evidence_ref(digest)
        assert content_digest(json.loads(got["body"])) == digest

    def test_large_body_goes_to_cas(self, catalog, raw, tmp_data_home):
        catalog.create_project(name="p1")
        catalog.create_source(project="p1", name="s", kind="docs", locator="doc://")
        src = catalog.get_source("p1", "s")
        # Canonical dict payload (dict path bypasses the model's excerpt
        # bound) with a body comfortably above the 64 KiB inline threshold.
        payload = {
            "source_type": "doc",
            "canonical_uri": "doc://big",
            "revision": "",
            "retrieved_at": "",
            "trust_tier": "tier2_internal",
            "excerpt": "x" * (catalog.INLINE_MAX_BYTES + 1),
            "browsable_link": None,
            "why_selected": "big one",
        }
        digest = catalog.insert_evidence_ref(payload, source_id=src["id"])
        assert digest == content_digest(payload)

        row = raw.execute(
            "SELECT body FROM evidence_ref WHERE digest=?", (digest,)
        ).fetchone()
        assert row["body"] is None  # column NULL → CAS

        path = catalog.cas_path(digest)
        assert path.exists()
        # fan-out layout: cas/sha256/<2hex>/<hex>
        assert path == tmp_data_home / "cas" / "sha256" / digest[:2] / digest
        # read-only 0444
        assert stat.S_IMODE(path.stat().st_mode) == 0o444

        # readback resolves the CAS body and is digest-identical
        got = catalog.get_evidence_ref(digest)
        assert got["body"] == canonical_json(payload)
        assert content_digest(json.loads(got["body"])) == digest

    def test_cas_write_rewrite_is_noop(self, catalog):
        digest = content_digest({"a": 1})
        p1 = catalog.cas_write(digest, canonical_json({"a": 1}))
        p2 = catalog.cas_write(digest, canonical_json({"a": 1}))  # no-op, no raise
        assert p1 == p2
        assert catalog.cas_read(digest) == canonical_json({"a": 1})

    def test_cas_read_missing_is_hard_error(self, catalog):
        with pytest.raises(FileNotFoundError):
            catalog.cas_read("f" * 64)


# ── Integration: real bin.landscape artifacts round-trip ────────────────


class TestLandscapeIntegration:
    def test_snapshot_pack_spec_persist_and_read_back_digest_identical(
        self, catalog, raw
    ):
        pid = catalog.create_project(name="unity-explorer")
        catalog.create_source(
            project="unity-explorer", name="docs", kind="docs",
            locator="doc://", trust_tier=2,
        )
        src = catalog.get_source("unity-explorer", "docs")

        refs = (
            _evidence("doc://a"),
            _evidence("doc://b", tier=TrustTier.TIER3_EXTERNAL),
        )
        snapshot = LandscapeSnapshot(
            project="unity-explorer",
            created_at="2026-08-17T12:00:00Z",
            evidence=refs,
        )
        for ref in snapshot.evidence:
            catalog.insert_evidence_ref(ref, source_id=src["id"])
        snap_digest = catalog.insert_landscape_snapshot(snapshot, project_id=pid)
        assert snap_digest == snapshot.digest
        for ref in snapshot.evidence:
            catalog.link_snapshot_evidence(snap_digest, ref.digest)

        # Snapshot manifest reads back digest-identical.
        row = catalog.get_landscape_snapshot(snap_digest)
        manifest = json.loads(row["manifest"])
        assert content_digest(manifest) == snapshot.digest
        assert manifest == snapshot._content_payload()
        # Join rows cover exactly the snapshot's evidence digests.
        assert catalog.list_snapshot_evidence(snap_digest) == sorted(
            ref.digest for ref in snapshot.evidence
        )
        # Each evidence body reads back digest-identical.
        for ref in snapshot.evidence:
            body = json.loads(catalog.get_evidence_ref(ref.digest)["body"])
            assert content_digest(body) == ref.digest

        # ContextPack (judge role flags the tier-3 ref).
        pack = build_pack(snapshot, Role.JUDGE)
        pack_digest = catalog.insert_context_pack(pack)
        assert pack_digest == pack.digest
        pack_row = catalog.get_context_pack(pack_digest)
        assert pack_row["role"] == "judge"
        assert pack_row["snapshot_digest"] == snap_digest
        assert content_digest(json.loads(pack_row["manifest"])) == pack.digest

        # WorkflowSpec.
        spec = WorkflowSpec(
            name="release",
            steps=(
                WorkflowStep(id="gather", kind="gather"),
                WorkflowStep(id="deploy", kind="deploy", depends_on=("gather",)),
            ),
        )
        spec_digest = catalog.insert_workflow_spec(
            spec, project_id=pid, pack_digest=pack_digest
        )
        assert spec_digest == spec.digest
        spec_row = catalog.get_workflow_spec(spec_digest)
        assert content_digest(json.loads(spec_row["spec"])) == spec.digest
        assert spec_row["pack_digest"] == pack_digest
        # The validator forced needs_approval on the deploy step, and that
        # is part of the persisted canonical body.
        persisted = json.loads(spec_row["spec"])
        deploy = [s for s in persisted["steps"] if s["id"] == "deploy"][0]
        assert deploy["needs_approval"] is True

    def test_insert_accepts_canonical_dict_equivalently(self, catalog):
        pid = catalog.create_project(name="p1")
        snapshot = LandscapeSnapshot(project="p1", created_at="2026-01-01T00:00:00Z")
        d1 = catalog.insert_landscape_snapshot(snapshot, project_id=pid)
        # Same content as canonical dict → same digest, still one row.
        d2 = catalog.insert_landscape_snapshot(
            snapshot._content_payload(), project_id=pid
        )
        assert d1 == d2 == snapshot.digest
        conn = catalog._connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM landscape_snapshot"
            ).fetchone()[0] == 1
        finally:
            conn.close()
