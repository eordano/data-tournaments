"""Tests for bin/domains.py — domain CRUD over the fabric DB."""
import pytest


@pytest.fixture
def fresh_fabric(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home / "judgements.db"


def test_create_domain_writes_row_with_derived_prompt_names(fresh_fabric):
    from bin import domains
    did = domains.create_domain(
        name="memory-extraction",
        description="Extract durable memories from chat archives",
        corpus_source={"kind": "inline", "items": [{"title": "x", "body": "y"}]},
    )
    assert isinstance(did, int) and did >= 1

    spec = domains.get_domain("memory-extraction")
    assert spec.name == "memory-extraction"
    assert spec.generator_prompt == "card-generator:memory-extraction"
    assert spec.judge_prompt == "judge-instructions:memory-extraction"
    assert spec.rubric == "card-prioritizer-v0"
    assert spec.corpus_source["kind"] == "inline"
    assert spec.status == "active"


def test_list_domains_returns_only_active(fresh_fabric):
    from bin import domains
    domains.create_domain(name="a", description="", corpus_source={"kind": "inline", "items": []})
    domains.create_domain(name="b", description="", corpus_source={"kind": "inline", "items": []})
    domains.archive_domain("a")
    rows = domains.list_domains()
    names = {r.name for r in rows}
    assert names == {"b"}


def test_archive_domain_flips_status(fresh_fabric):
    from bin import domains
    domains.create_domain(name="x", description="", corpus_source={"kind": "inline", "items": []})
    domains.archive_domain("x")
    spec = domains.get_domain("x")
    assert spec.status == "archived"


def test_create_domain_unique_name(fresh_fabric):
    from bin import domains
    domains.create_domain(name="dup", description="", corpus_source={"kind": "inline", "items": []})
    with pytest.raises(ValueError, match="already exists"):
        domains.create_domain(name="dup", description="other", corpus_source={"kind": "inline", "items": []})


def test_corpus_source_rejects_unknown_kind(fresh_fabric):
    from bin import domains
    with pytest.raises(ValueError, match="kind"):
        domains.create_domain(name="bad", description="", corpus_source={"kind": "magic-pixie-dust"})


def test_corpus_source_sqlite_requires_path_and_query(fresh_fabric):
    from bin import domains
    with pytest.raises(ValueError, match="sqlite"):
        domains.create_domain(name="bad-sql", description="", corpus_source={"kind": "sqlite", "path": "/tmp/x.db"})
    with pytest.raises(ValueError, match="sqlite"):
        domains.create_domain(name="bad-sql2", description="", corpus_source={"kind": "sqlite", "query": "SELECT 1"})


def test_corpus_source_filesystem_requires_root_and_glob(fresh_fabric):
    from bin import domains
    with pytest.raises(ValueError, match="filesystem"):
        domains.create_domain(name="bad-fs", description="", corpus_source={"kind": "filesystem", "root": "/tmp"})


def test_get_domain_missing_raises_lookup_error(fresh_fabric):
    from bin import domains
    with pytest.raises(LookupError):
        domains.get_domain("does-not-exist")


def test_init_db_adds_domain_table_and_pending_column(fresh_fabric):
    import sqlite3
    db = sqlite3.connect(str(fresh_fabric))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "domain" in tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(pending_judgement)")}
    assert "domain_id" in cols


def test_schema_migration_is_idempotent(fresh_fabric):
    """Calling init_db a second time on an existing DB should not raise."""
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()  # already initialized in fixture; this is the 2nd call
    judgement.init_db()  # 3rd call, just to be sure
    # If we got here without OperationalError, migration is idempotent.
    import sqlite3
    db = sqlite3.connect(str(fresh_fabric))
    cols = {r[1] for r in db.execute("PRAGMA table_info(pending_judgement)")}
    assert "domain_id" in cols
