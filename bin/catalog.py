#!/usr/bin/env python3
"""Project-landscape catalog over the fabric SQLite DB (ADR 0001 steps 2–3).

Two families of rows, both living in $DATA_TOURNAMENTS_HOME/judgements.db
(schema: bin/judgement_schema.sql, applied by ``judgement.init_db`` or the
``init()`` here — both idempotent):

* Mutable catalog — project, component, source, capability, skill,
  environment, policy (+ join tables). Plain CRUD mirroring bin/domains.py.
  NOTE: 'project' (landscape entity) is distinct from 'domain' (evaluation
  lens/corpus in bin/domains.py) — different concepts, different tables.

* Immutable, digest-keyed artifacts — evidence_ref, landscape_snapshot,
  snapshot_evidence, context_pack, workflow_spec. Insert-only (BEFORE UPDATE
  triggers RAISE(ABORT) in the schema). Digests are computed by the
  bin.landscape contracts (canonical.py), NEVER here: insert functions accept
  an already-digested bin.landscape model instance or its canonical dict.

Payload placement (ADR 0002): canonical bodies ≤ 64 KiB are stored inline in
the row; larger bodies go to the filesystem CAS at
``$DATA_TOURNAMENTS_HOME/cas/sha256/<first-2-hex>/<hex>`` and the row column
is NULL. Write ordering is CAS-file-first-then-row (an orphan CAS file is
harmless garbage; a row without its CAS file is a hard error readers raise).

CLI is a debug aid mirroring the module functions:
  catalog.py init
  catalog.py create-project --name N [--description D]
  catalog.py get-project --name N
  catalog.py list-projects [--status S]
  catalog.py archive-project --name N
  (same verbs for component/source/capability/skill/environment/policy)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin.landscape.canonical import canonical_json, content_digest  # noqa: E402

INLINE_MAX_BYTES = 64 * 1024

SCHEMA_PATH = Path(__file__).parent / "judgement_schema.sql"

def _data_home() -> Path:
    return Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))

def _db_path() -> Path:
    return _data_home() / "judgements.db"

class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block also CLOSES on exit.

    Stock sqlite3 ``with conn:`` only commits/rolls back — it never closes.
    This module has 40+ ``with _connect() as conn:`` call sites; leaking a
    descriptor per call exhausts the process fd limit mid-test-suite and
    surfaces as order-dependent 'unable to open database file' errors.
    Direct callers (tests, REPL) still get a normal connection object with
    ``.execute()``/``.close()``.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def init() -> None:
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS).

    ``judgement.init_db()`` runs the same executescript; this entry point
    exists so catalog-only callers/tests don't need the Langfuse-touching
    rubric seeding that init_db performs.
    """
    _data_home().mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect() as conn:
        conn.executescript(schema_sql)

def cas_path(digest: str) -> Path:
    """$DATA_TOURNAMENTS_HOME/cas/sha256/<first-2-hex>/<hex> for a hex digest."""
    hexd = digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
    return _data_home() / "cas" / "sha256" / hexd[:2] / hexd

def cas_write(digest: str, body: Union[str, bytes]) -> Path:
    """Write ``body`` to the CAS: temp file + atomic rename, then chmod 0444.

    Re-writing an existing digest is a no-op (content-addressed: same digest
    ⇒ same bytes by definition).
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    path = cas_path(digest)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".cas-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.rename(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, 0o444)
    return path

def cas_read(digest: str) -> str:
    """Read a CAS body back as text. Missing file is a hard error (ADR 0002:
    a row pointing at an absent CAS file means the store is corrupt)."""
    path = cas_path(digest)
    if not path.exists():
        raise FileNotFoundError(
            f"CAS body for digest {digest!r} missing at {path} "
            "(row exists but its payload file is gone)"
        )
    return path.read_text(encoding="utf-8")

def _place_body(digest: str, canonical_body: str) -> Optional[str]:
    """ADR 0002 placement: return the inline column value (or None → CAS).

    Write ordering: the CAS file is durably in place BEFORE the caller
    inserts the row.
    """
    if len(canonical_body.encode("utf-8")) <= INLINE_MAX_BYTES:
        return canonical_body
    cas_write(digest, canonical_body)
    return None

def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict:
    d = dict(row)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d

def create_project(*, name: str, description: str = "", metadata: Optional[dict] = None) -> int:
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM project WHERE name=?", (name,)).fetchone()
        if existing is not None:
            raise ValueError(f"project {name!r} already exists")
        cur = conn.execute(
            "INSERT INTO project(name, description, metadata) VALUES (?, ?, ?)",
            (name, description, json.dumps(metadata or {})),
        )
        conn.commit()
        return cur.lastrowid

def get_project(name: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM project WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no project named {name!r}")
        return _row_to_dict(row, ("metadata",))

def list_projects(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project WHERE status=? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_dict(r, ("metadata",)) for r in rows]

def archive_project(name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE project SET status='archived', updated_at=datetime('now') WHERE name=?",
            (name,),
        )
        conn.commit()

def create_component(
    *, project: str, name: str, kind: str, metadata: Optional[dict] = None
) -> int:
    pid = get_project(project)["id"]
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO component(project_id, name, kind, metadata) "
                "VALUES (?, ?, ?, ?)",
                (pid, name, kind, json.dumps(metadata or {})),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"component {name!r} already exists in project {project!r}")
        conn.commit()
        return cur.lastrowid

def get_component(project: str, name: str) -> dict:
    pid = get_project(project)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM component WHERE project_id=? AND name=?", (pid, name)
        ).fetchone()
        if row is None:
            raise LookupError(f"no component {name!r} in project {project!r}")
        return _row_to_dict(row, ("metadata",))

def list_components(project: str, status: str = "active") -> list[dict]:
    pid = get_project(project)["id"]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM component WHERE project_id=? AND status=? ORDER BY name",
            (pid, status),
        ).fetchall()
        return [_row_to_dict(r, ("metadata",)) for r in rows]

def archive_component(project: str, name: str) -> None:
    pid = get_project(project)["id"]
    with _connect() as conn:
        conn.execute(
            "UPDATE component SET status='archived', updated_at=datetime('now') "
            "WHERE project_id=? AND name=?",
            (pid, name),
        )
        conn.commit()

def create_source(
    *,
    project: str,
    name: str,
    kind: str,
    locator: str,
    trust_tier: int = 3,
    config: Optional[dict] = None,
) -> int:
    if trust_tier not in (1, 2, 3):
        raise ValueError(f"trust_tier must be 1..3, got {trust_tier!r}")
    pid = get_project(project)["id"]
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO source(project_id, name, kind, locator, trust_tier, config) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pid, name, kind, locator, trust_tier, json.dumps(config or {})),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"source {name!r} already exists in project {project!r}")
        conn.commit()
        return cur.lastrowid

def get_source(project: str, name: str) -> dict:
    pid = get_project(project)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM source WHERE project_id=? AND name=?", (pid, name)
        ).fetchone()
        if row is None:
            raise LookupError(f"no source {name!r} in project {project!r}")
        return _row_to_dict(row, ("config",))

def list_sources(project: str, status: str = "active") -> list[dict]:
    pid = get_project(project)["id"]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM source WHERE project_id=? AND status=? ORDER BY name",
            (pid, status),
        ).fetchall()
        return [_row_to_dict(r, ("config",)) for r in rows]

def archive_source(project: str, name: str) -> None:
    pid = get_project(project)["id"]
    with _connect() as conn:
        conn.execute(
            "UPDATE source SET status='archived', updated_at=datetime('now') "
            "WHERE project_id=? AND name=?",
            (pid, name),
        )
        conn.commit()

def create_capability(*, name: str, description: str = "") -> int:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO capability(name, description) VALUES (?, ?)",
                (name, description),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"capability {name!r} already exists")
        conn.commit()
        return cur.lastrowid

def get_capability(name: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM capability WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no capability named {name!r}")
        return _row_to_dict(row)

def list_capabilities(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM capability WHERE status=? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

def archive_capability(name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE capability SET status='archived' WHERE name=?", (name,))
        conn.commit()

def create_skill(
    *,
    name: str,
    version: int,
    locator: str,
    digest: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO skill(name, version, locator, digest, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, version, locator, digest, json.dumps(metadata or {})),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"skill {name!r} v{version} already exists")
        conn.commit()
        return cur.lastrowid

def get_skill(name: str, version: Optional[int] = None) -> dict:
    """Fetch a skill by name (latest version when version is None)."""
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT * FROM skill WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM skill WHERE name=? AND version=?", (name, version)
            ).fetchone()
        if row is None:
            raise LookupError(f"no skill {name!r} v{version}")
        return _row_to_dict(row, ("metadata",))

def list_skills(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM skill WHERE status=? ORDER BY name, version", (status,)
        ).fetchall()
        return [_row_to_dict(r, ("metadata",)) for r in rows]

def archive_skill(name: str, version: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE skill SET status='archived' WHERE name=? AND version=?",
            (name, version),
        )
        conn.commit()

def create_environment(*, name: str, kind: str, config: Optional[dict] = None) -> int:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO environment(name, kind, config) VALUES (?, ?, ?)",
                (name, kind, json.dumps(config or {})),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"environment {name!r} already exists")
        conn.commit()
        return cur.lastrowid

def get_environment(name: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM environment WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no environment named {name!r}")
        return _row_to_dict(row, ("config",))

def list_environments(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM environment WHERE status=? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_dict(r, ("config",)) for r in rows]

def archive_environment(name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE environment SET status='archived' WHERE name=?", (name,))
        conn.commit()

def create_policy(*, name: str, kind: str, rule: dict) -> int:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO policy(name, kind, rule) VALUES (?, ?, ?)",
                (name, kind, json.dumps(rule)),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"policy {name!r} already exists")
        conn.commit()
        return cur.lastrowid

def get_policy(name: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM policy WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no policy named {name!r}")
        return _row_to_dict(row, ("rule",))

def list_policies(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM policy WHERE status=? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_dict(r, ("rule",)) for r in rows]

def archive_policy(name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE policy SET status='archived' WHERE name=?", (name,))
        conn.commit()

def link_component_capability(component_id: int, capability_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO component_capability(component_id, capability_id) "
            "VALUES (?, ?)",
            (component_id, capability_id),
        )
        conn.commit()

def link_project_skill(project_id: int, skill_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO project_skill(project_id, skill_id) VALUES (?, ?)",
            (project_id, skill_id),
        )
        conn.commit()

_TRUST_TIER_TO_INT = {"tier1_system": 1, "tier2_internal": 2, "tier3_external": 3}

def _payload_and_digest(obj: Any) -> tuple[dict, str]:
    """Normalize a landscape model instance or canonical dict to
    (canonical_payload, digest)."""
    if isinstance(obj, dict):
        return obj, content_digest(obj)
    payload = obj._content_payload()
    return payload, obj.digest

def insert_evidence_ref(ref: Any, *, source_id: int) -> str:
    """Persist an EvidenceRef (model or canonical dict). Returns the digest.

    Re-inserting an existing digest is a no-op (content-addressed identity).
    ``source_id`` is the mutable catalog FK captured at insert time;
    ``trust_tier`` is copied from the payload so it cannot drift (ADR 0002 §5).
    """
    payload, digest = _payload_and_digest(ref)
    body = canonical_json(payload)
    tier = _TRUST_TIER_TO_INT[payload["trust_tier"]]
    inline = _place_body(digest, body)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO evidence_ref"
            "(digest, source_id, kind, locator, trust_tier, summary, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                source_id,
                payload["source_type"],
                payload["canonical_uri"],
                tier,
                payload.get("why_selected", ""),
                inline,
            ),
        )
        conn.commit()
    return digest

def get_evidence_ref(digest: str) -> dict:
    """Fetch an evidence_ref row; ``body`` is always resolved (inline or CAS)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM evidence_ref WHERE digest=?", (digest,)
        ).fetchone()
    if row is None:
        raise LookupError(f"no evidence_ref with digest {digest!r}")
    d = dict(row)
    if d["body"] is None:
        d["body"] = cas_read(digest)
    return d

def list_evidence_refs_for_source(source_id: int) -> list[dict]:
    """All frozen evidence_ref rows captured for one catalog source.

    Bodies are resolved (inline or CAS) and carry the canonical EvidenceRef
    payload — immutable, digest-addressed. Assembly uses this as the
    fallback when a source's live config is unusable (wave-9 L2): frozen
    evidence already collected by intake is a valid pack input.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_ref WHERE source_id=? ORDER BY digest",
            (source_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d["body"] is None:
            d["body"] = cas_read(d["digest"])
        out.append(d)
    return out

def insert_landscape_snapshot(
    snapshot: Any, *, project_id: int, schema_version: int = 1
) -> str:
    """Persist a LandscapeSnapshot (model or canonical dict). Returns digest.

    Only the snapshot row is written; evidence_ref rows and snapshot_evidence
    links are the caller's explicit responsibility (evidence needs a
    source_id this model does not carry).
    """
    payload, digest = _payload_and_digest(snapshot)
    manifest = canonical_json(payload)
    inline = _place_body(digest, manifest)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO landscape_snapshot"
            "(digest, project_id, schema_version, manifest) VALUES (?, ?, ?, ?)",
            (digest, project_id, schema_version, inline),
        )
        conn.commit()
    return digest

def get_landscape_snapshot(digest: str) -> dict:
    """Fetch a landscape_snapshot row; ``manifest`` is always resolved."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM landscape_snapshot WHERE digest=?", (digest,)
        ).fetchone()
    if row is None:
        raise LookupError(f"no landscape_snapshot with digest {digest!r}")
    d = dict(row)
    if d["manifest"] is None:
        d["manifest"] = cas_read(digest)
    return d

def link_snapshot_evidence(snapshot_digest: str, evidence_digest: str) -> None:
    """Record that a snapshot includes an evidence ref (insert-only join)."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO snapshot_evidence(snapshot_digest, evidence_digest) "
            "VALUES (?, ?)",
            (snapshot_digest, evidence_digest),
        )
        conn.commit()

def list_snapshot_evidence(snapshot_digest: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT evidence_digest FROM snapshot_evidence "
            "WHERE snapshot_digest=? ORDER BY evidence_digest",
            (snapshot_digest,),
        ).fetchall()
        return [r["evidence_digest"] for r in rows]

def insert_context_pack(pack: Any, *, schema_version: int = 1) -> str:
    """Persist a ContextPack (model or canonical dict). Returns the digest."""
    payload, digest = _payload_and_digest(pack)
    manifest = canonical_json(payload)
    inline = _place_body(digest, manifest)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO context_pack"
            "(digest, snapshot_digest, role, schema_version, manifest) "
            "VALUES (?, ?, ?, ?, ?)",
            (digest, payload["snapshot_digest"], payload["role"], schema_version, inline),
        )
        conn.commit()
    return digest

def get_context_pack(digest: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM context_pack WHERE digest=?", (digest,)
        ).fetchone()
    if row is None:
        raise LookupError(f"no context_pack with digest {digest!r}")
    d = dict(row)
    if d["manifest"] is None:
        d["manifest"] = cas_read(digest)
    return d

def insert_workflow_spec(
    spec: Any,
    *,
    project_id: int,
    pack_digest: Optional[str] = None,
    schema_version: int = 1,
) -> str:
    """Persist a WorkflowSpec (model or canonical dict). Returns the digest."""
    payload, digest = _payload_and_digest(spec)
    body = canonical_json(payload)
    inline = _place_body(digest, body)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO workflow_spec"
            "(digest, project_id, name, schema_version, spec, pack_digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (digest, project_id, payload["name"], schema_version, inline, pack_digest),
        )
        conn.commit()
    return digest

def get_workflow_spec(digest: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_spec WHERE digest=?", (digest,)
        ).fetchone()
    if row is None:
        raise LookupError(f"no workflow_spec with digest {digest!r}")
    d = dict(row)
    if d["spec"] is None:
        d["spec"] = cas_read(digest)
    return d

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="catalog.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="apply the shared schema (idempotent)")

    sp = sub.add_parser("create-project")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description", default="")
    sp = sub.add_parser("get-project")
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-projects")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-project")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-component")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True)
    sp = sub.add_parser("get-component")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-components")
    sp.add_argument("--project", required=True)
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-component")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-source")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True)
    sp.add_argument("--locator", required=True)
    sp.add_argument("--trust-tier", type=int, default=3)
    sp = sub.add_parser("get-source")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-sources")
    sp.add_argument("--project", required=True)
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-source")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-capability")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description", default="")
    sp = sub.add_parser("get-capability")
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-capabilities")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-capability")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-skill")
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", type=int, required=True)
    sp.add_argument("--locator", required=True)
    sp.add_argument("--digest")
    sp = sub.add_parser("get-skill")
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", type=int)
    sp = sub.add_parser("list-skills")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-skill")
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", type=int, required=True)

    sp = sub.add_parser("create-environment")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True)
    sp.add_argument("--config", default="{}")
    sp = sub.add_parser("get-environment")
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-environments")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-environment")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-policy")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True)
    sp.add_argument("--rule", required=True, help="JSON rule body")
    sp = sub.add_parser("get-policy")
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-policies")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("archive-policy")
    sp.add_argument("--name", required=True)

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "init":
        init()
        print(f"schema applied at {_db_path()}")
    elif cmd == "create-project":
        _print({"id": create_project(name=args.name, description=args.description)})
    elif cmd == "get-project":
        _print(get_project(args.name))
    elif cmd == "list-projects":
        _print(list_projects(args.status))
    elif cmd == "archive-project":
        archive_project(args.name)
    elif cmd == "create-component":
        _print({"id": create_component(project=args.project, name=args.name, kind=args.kind)})
    elif cmd == "get-component":
        _print(get_component(args.project, args.name))
    elif cmd == "list-components":
        _print(list_components(args.project, args.status))
    elif cmd == "archive-component":
        archive_component(args.project, args.name)
    elif cmd == "create-source":
        _print(
            {
                "id": create_source(
                    project=args.project,
                    name=args.name,
                    kind=args.kind,
                    locator=args.locator,
                    trust_tier=args.trust_tier,
                )
            }
        )
    elif cmd == "get-source":
        _print(get_source(args.project, args.name))
    elif cmd == "list-sources":
        _print(list_sources(args.project, args.status))
    elif cmd == "archive-source":
        archive_source(args.project, args.name)
    elif cmd == "create-capability":
        _print({"id": create_capability(name=args.name, description=args.description)})
    elif cmd == "get-capability":
        _print(get_capability(args.name))
    elif cmd == "list-capabilities":
        _print(list_capabilities(args.status))
    elif cmd == "archive-capability":
        archive_capability(args.name)
    elif cmd == "create-skill":
        _print(
            {
                "id": create_skill(
                    name=args.name,
                    version=args.version,
                    locator=args.locator,
                    digest=args.digest,
                )
            }
        )
    elif cmd == "get-skill":
        _print(get_skill(args.name, args.version))
    elif cmd == "list-skills":
        _print(list_skills(args.status))
    elif cmd == "archive-skill":
        archive_skill(args.name, args.version)
    elif cmd == "create-environment":
        _print(
            {
                "id": create_environment(
                    name=args.name, kind=args.kind, config=json.loads(args.config)
                )
            }
        )
    elif cmd == "get-environment":
        _print(get_environment(args.name))
    elif cmd == "list-environments":
        _print(list_environments(args.status))
    elif cmd == "archive-environment":
        archive_environment(args.name)
    elif cmd == "create-policy":
        _print({"id": create_policy(name=args.name, kind=args.kind, rule=json.loads(args.rule))})
    elif cmd == "get-policy":
        _print(get_policy(args.name))
    elif cmd == "list-policies":
        _print(list_policies(args.status))
    elif cmd == "archive-policy":
        archive_policy(args.name)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
