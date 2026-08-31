"""Operational tooling: backup/restore, CAS integrity, GC dry-run (wave 7).

Scope: $DATA_TOURNAMENTS_HOME is the unit of state — judgements.db and cas/
live under the same root and MUST be backed up together (ADR 0002
consequence: a row without its CAS file is a hard error). Temporal state is
NOT here: it lives in the Temporal server and the workflow_run table is a
rebuildable projection.

Commands (CLI mirrors bin/domains.py conventions):

    python3 bin/ops.py backup  --dest DIR
    python3 bin/ops.py restore --archive FILE --dest DIR [--force]
    python3 bin/ops.py cas-verify
    python3 bin/ops.py gc --dry-run   (only mode implemented; ADR 0002 §6)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

def _data_home() -> Path:
    return Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))

def _db_path() -> Path:
    return _data_home() / "judgements.db"

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def backup(dest_dir: Path) -> Path:
    """Consistent snapshot of judgements.db + cas/ into one tar.gz.

    The DB is copied via sqlite3's online backup API (safe against
    concurrent writers — a plain file copy of a WAL-mode DB is not), then
    the CAS tree is added. Returns the archive path. A manifest.json inside
    records counts + the db sha256 for restore verification.
    """
    home = _data_home()
    if not _db_path().exists():
        raise FileNotFoundError(f"no fabric DB at {_db_path()}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"dt-backup-{_now_stamp()}.tar.gz"

    with tempfile.TemporaryDirectory() as td:
        db_copy = Path(td) / "judgements.db"
        src = sqlite3.connect(str(_db_path()))
        try:
            dst = sqlite3.connect(str(db_copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        cas_root = home / "cas"
        cas_files = (
            sorted(p for p in cas_root.rglob("*") if p.is_file())
            if cas_root.exists()
            else []
        )
        manifest = {
            "created_at": _now_stamp(),
            "db_sha256": hashlib.sha256(db_copy.read_bytes()).hexdigest(),
            "cas_file_count": len(cas_files),
        }
        manifest_path = Path(td) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        tmp_archive = archive.with_suffix(".tmp")
        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(db_copy, arcname="judgements.db")
            tar.add(manifest_path, arcname="manifest.json")
            for f in cas_files:
                tar.add(f, arcname=str(Path("cas") / f.relative_to(cas_root)))
        tmp_archive.rename(archive)
    return archive

def restore(archive: Path, dest: Path, *, force: bool = False) -> dict[str, Any]:
    """Restore an archive into ``dest`` (a DATA_TOURNAMENTS_HOME root).

    Refuses to overwrite an existing judgements.db unless force=True.
    Verifies the manifest's db sha256 after extraction; on mismatch the
    restore FAILS (corrupt archive must never masquerade as success).
    """
    if not archive.exists():
        raise FileNotFoundError(str(archive))
    db_target = dest / "judgements.db"
    if db_target.exists() and not force:
        raise FileExistsError(
            f"{db_target} exists; pass --force to overwrite"
        )
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(dest, filter="data")
    manifest = json.loads((dest / "manifest.json").read_text())
    actual = hashlib.sha256(db_target.read_bytes()).hexdigest()
    if actual != manifest["db_sha256"]:
        raise RuntimeError(
            "restore verification FAILED: db sha256 mismatch "
            f"(manifest {manifest['db_sha256'][:12]}…, actual {actual[:12]}…)"
        )
    return manifest

def cas_verify() -> dict[str, Any]:
    """Verify CAS <-> DB consistency (ADR 0002).

    Checks:
      1. every digest-keyed row whose body/manifest is NULL has its CAS file
      2. every CAS file's content hashes to its own path digest
      3. orphaned CAS files (no referencing row) are REPORTED, not deleted
    """
    from bin import catalog

    home = _data_home()
    problems: list[str] = []
    referenced: set[str] = set()

    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        specs = [
            ("evidence_ref", "body"),
            ("landscape_snapshot", "manifest"),
            ("context_pack", "manifest"),
            ("workflow_spec", "spec"),
        ]
        for table, body_col in specs:
            try:
                rows = conn.execute(
                    f"SELECT digest, {body_col} IS NULL AS external FROM {table}"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                if not row["external"]:
                    continue
                referenced.add(row["digest"])
                path = catalog.cas_path(row["digest"])
                if not path.exists():
                    problems.append(
                        f"{table} {row['digest'][:16]}…: CAS file missing"
                    )
    conn.close()

    cas_root = home / "cas" / "sha256"
    scanned = 0
    orphans: list[str] = []
    if cas_root.exists():
        for f in sorted(cas_root.rglob("*")):
            if not f.is_file():
                continue
            scanned += 1
            actual = hashlib.sha256(f.read_bytes()).hexdigest()
            if actual != f.name:
                problems.append(f"cas/{f.name[:16]}…: content hash mismatch")
            digest_forms = {f.name, f"sha256:{f.name}"}
            if not (digest_forms & referenced):
                orphans.append(f.name)

    return {
        "ok": not problems,
        "problems": problems,
        "cas_files_scanned": scanned,
        "orphans": orphans,
    }

def gc_dry_run() -> dict[str, Any]:
    """ADR 0002 §6: GC is deferred; only a dry-run report exists.

    Reports CAS files unreachable from any digest-keyed row. Deletion is a
    human decision executed with explicit tooling once disk pressure is
    real — this function never removes anything.
    """
    report = cas_verify()
    return {
        "would_delete": report["orphans"],
        "count": len(report["orphans"]),
        "note": "dry-run only; deletion tooling deferred per ADR 0002 §6",
    }

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--dest", required=True, type=Path)

    r = sub.add_parser("restore")
    r.add_argument("--archive", required=True, type=Path)
    r.add_argument("--dest", required=True, type=Path)
    r.add_argument("--force", action="store_true")

    sub.add_parser("cas-verify")

    g = sub.add_parser("gc")
    g.add_argument("--dry-run", action="store_true", required=True)

    args = p.parse_args(argv)
    if args.cmd == "backup":
        archive = backup(args.dest)
        print(f"backup written: {archive}")
    elif args.cmd == "restore":
        manifest = restore(args.archive, args.dest, force=args.force)
        print(f"restored (db sha256 verified): {json.dumps(manifest)}")
    elif args.cmd == "cas-verify":
        report = cas_verify()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    elif args.cmd == "gc":
        print(json.dumps(gc_dry_run(), indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
