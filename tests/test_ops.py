"""Tests for bin/ops.py — backup/restore, CAS integrity, GC dry-run (wave 7)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.conftest import git_bin_dir

from bin import catalog, ops
from bin.landscape import EvidenceRef, SourceType, TrustTier

@pytest.fixture
def seeded_home(tmp_data_home):
    """Catalog with one inline evidence row and one CAS-stored body."""
    catalog.init()
    catalog.create_project(name="p1")
    catalog.create_source(project="p1", name="s", kind="docs", locator="doc://")
    src = catalog.get_source("p1", "s")

    small = EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri="doc://small",
        trust_tier=TrustTier.TIER2_INTERNAL,
        excerpt="small body",
        why_selected="test",
    )
    catalog.insert_evidence_ref(small, source_id=src["id"])

    big = EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri="doc://big",
        trust_tier=TrustTier.TIER2_INTERNAL,
        excerpt="x" * 4000,
        why_selected="test",
    )
    catalog.insert_evidence_ref(big, source_id=src["id"])
    return tmp_data_home

def test_backup_creates_archive_with_manifest(seeded_home, tmp_path):
    archive = ops.backup(tmp_path / "backups")
    assert archive.exists() and archive.suffix == ".gz"
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "judgements.db" in names
    assert "manifest.json" in names

def test_backup_no_partial_archive_on_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_TOURNAMENTS_HOME", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError):
        ops.backup(tmp_path / "backups")
    assert not list((tmp_path / "backups").glob("*")) if (tmp_path / "backups").exists() else True

def test_restore_round_trip_verifies_sha(seeded_home, tmp_path):
    archive = ops.backup(tmp_path / "backups")
    dest = tmp_path / "restored"
    manifest = ops.restore(archive, dest)
    assert (dest / "judgements.db").exists()
    actual = hashlib.sha256((dest / "judgements.db").read_bytes()).hexdigest()
    assert actual == manifest["db_sha256"]

def test_restore_refuses_overwrite_without_force(seeded_home, tmp_path):
    archive = ops.backup(tmp_path / "backups")
    dest = tmp_path / "restored"
    ops.restore(archive, dest)
    with pytest.raises(FileExistsError):
        ops.restore(archive, dest)
    ops.restore(archive, dest, force=True)

def test_restore_fails_on_tampered_db(seeded_home, tmp_path):
    archive = ops.backup(tmp_path / "backups")
    work = tmp_path / "tamper"
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    (work / "judgements.db").write_bytes(b"corrupted")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for f in work.rglob("*"):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(work)))
    with pytest.raises(RuntimeError, match="verification FAILED"):
        ops.restore(bad, tmp_path / "restored2")

def test_cas_verify_clean_state_ok(seeded_home):
    report = ops.cas_verify()
    assert report["ok"] is True
    assert report["problems"] == []

def test_cas_verify_detects_missing_cas_file(seeded_home):
    home = seeded_home
    cas_root = home / "cas" / "sha256"
    victims = [p for p in cas_root.rglob("*") if p.is_file()]
    if not victims:
        pytest.skip("no CAS-stored bodies in this seed (all inline)")
    victims[0].chmod(0o644)
    victims[0].unlink()
    report = ops.cas_verify()
    assert report["ok"] is False
    assert any("CAS file missing" in p for p in report["problems"])

def test_cas_verify_detects_content_tamper(seeded_home):
    home = seeded_home
    cas_root = home / "cas" / "sha256"
    victims = [p for p in cas_root.rglob("*") if p.is_file()]
    if not victims:
        pytest.skip("no CAS-stored bodies in this seed (all inline)")
    victims[0].chmod(0o644)
    victims[0].write_text("tampered content")
    report = ops.cas_verify()
    assert report["ok"] is False
    assert any("hash mismatch" in p for p in report["problems"])

def test_gc_dry_run_reports_orphans_never_deletes(seeded_home):
    home = seeded_home
    orphan_dir = home / "cas" / "sha256" / "ab"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    body = b"orphaned content"
    digest = hashlib.sha256(body).hexdigest()
    orphan = home / "cas" / "sha256" / digest[:2] / digest
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(body)
    report = ops.gc_dry_run()
    assert digest in report["would_delete"]
    assert orphan.exists()
    assert "deferred" in report["note"]

def test_cli_backup_and_verify(seeded_home, tmp_path):
    env_home = str(seeded_home)
    r = subprocess.run(
        [sys.executable, "bin/ops.py", "backup", "--dest", str(tmp_path / "b")],
        capture_output=True, text=True,
        env={"PATH": git_bin_dir(), "DATA_TOURNAMENTS_HOME": env_home,
             "PYTHONPATH": str(Path.cwd())},
        cwd=str(Path.cwd()),
    )
    assert r.returncode == 0, r.stderr
    assert "backup written" in r.stdout

    r2 = subprocess.run(
        [sys.executable, "bin/ops.py", "cas-verify"],
        capture_output=True, text=True,
        env={"PATH": git_bin_dir(), "DATA_TOURNAMENTS_HOME": env_home,
             "PYTHONPATH": str(Path.cwd())},
        cwd=str(Path.cwd()),
    )
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)["ok"] is True
