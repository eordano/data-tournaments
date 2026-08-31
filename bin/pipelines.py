#!/usr/bin/env python3
"""Pipeline spec v1 — declarative, versioned, IMMUTABLE pipelines
(wave-12; docs/design/judgement-wheel-v2.md §4).

A pipeline names ORDERED stages; each stage binds either a judgement
(subject + judgement kind + rubric) or a platform action. v1 is a SPEC +
registry, NOT an executor — a generic DAG engine is explicitly out of
scope. Domains bind a (pipeline, version); generate/judge consult the
binding for kind + rubric.

Validation happens at REGISTRATION time (fail closed):

* stages must be a non-empty ordered list, each with a unique ``key``;
* judgement stages: subject in ('idea','execution'), judgement in
  ('pair','single'), rubric naming an EXISTING eval_template (direct SQL
  lookup — deliberately decoupled from bin/judgement.py internals);
* action stages: action in the ACTIONS registry;
* optional ``foreach`` in ('branch','candidate','artifact');
* FAIL-CLOSED RULE (the contract's rule, non-negotiable): for every
  release action stage (RELEASE_ACTIONS), the stages BEFORE it must
  include at least one judgement stage with subject='execution' AND
  judgement='single'. A pipeline whose only preceding execution-subject
  judgement is a PAIR judgement — or that has none at all — is REFUSED.
  Per-branch single execution review is non-negotiable.

Storage (bin/judgement_schema.sql): the ``pipeline`` table holds the
CANONICAL JSON definition (sorted keys, compact separators) plus its
sha256 digest; rows are append-only (UPDATE/DELETE raise via triggers,
approval_event precedent). Re-registering a name inserts version = max+1.

Domain binding semantics (deliberate, documented): ``domain_pipeline``
has UNIQUE(domain_id) and append-only triggers — a binding is PERMANENT
for a domain. Rebinding requires a new domain. The binding function lives
HERE (not as a create_domain parameter in bin/domains.py) to avoid churn
with in-flight sibling work on that module; binding after creation is one
extra call and keeps ownership boundaries clean.

CLI is a debug aid mirroring the module functions (campaigns.py
conventions):
  pipelines.py register --name N --definition-file spec.json
  pipelines.py get --name N [--version V]
  pipelines.py list
  pipelines.py bind --domain D --pipeline N [--version V]
  pipelines.py show-binding --domain D
  pipelines.py seed-defaults
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Union

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SUBJECTS = ("idea", "execution")
JUDGEMENTS = ("pair", "single")
FOREACH = ("branch", "candidate", "artifact")

ACTIONS = [
    "branch_author",
    "branch_validation",
    "audited_release",
    "generate_workorders",
    "assemble_pack",
]

RELEASE_ACTIONS = ["audited_release"]

_STAGE_KEYS_JUDGEMENT = {"key", "subject", "judgement", "rubric", "foreach"}
_STAGE_KEYS_ACTION = {"key", "action", "foreach"}

def _data_home() -> Path:
    return Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))

def _db_path() -> Path:
    return _data_home() / "judgements.db"

class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block also CLOSES on exit
    (see bin/catalog.py for the fd-exhaustion rationale)."""

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
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()

def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators (landscape
    canonical.py convention) — identical content ⇒ identical digest,
    independent of dict insertion order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def definition_digest(definition: dict) -> str:
    """sha256 hex over the canonical JSON form of ``definition``."""
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()

def _rubric_exists(conn: sqlite3.Connection, rubric: str) -> bool:
    """Direct SQL lookup against eval_template by NAME — deliberately
    decoupled from bin/judgement.py internals (wave-12 parallel slices)."""
    row = conn.execute(
        "SELECT 1 FROM eval_template WHERE name=? LIMIT 1", (rubric,)
    ).fetchone()
    return row is not None

def _rubric_shape(conn: sqlite3.Connection, rubric: str) -> Optional[dict]:
    """Return {'judgement_kind', 'subjects'} for the LATEST version of the
    named rubric (normalized legacy defaults: pair / ['execution']), or None
    when the rubric doesn't exist. Direct SQL + local normalization keeps
    this decoupled from bin/judgement.py."""
    row = conn.execute(
        "SELECT output_definition FROM eval_template WHERE name=? "
        "ORDER BY version DESC LIMIT 1",
        (rubric,),
    ).fetchone()
    if row is None:
        return None
    outdef = json.loads(row["output_definition"])
    return {
        "judgement_kind": outdef.get("judgement_kind", "pair"),
        "subjects": outdef.get("subjects", ["execution"]),
    }

def validate_definition(definition: dict, conn: sqlite3.Connection) -> None:
    """Raise ValueError when ``definition`` violates the pipeline contract.

    Checks stage shape, key uniqueness, vocabulary membership, rubric
    existence (against eval_template at registration time), and the
    fail-closed release gate.
    """
    if not isinstance(definition, dict):
        raise ValueError("pipeline definition must be a dict")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("pipeline definition requires a non-empty 'stages' list")

    seen_keys: set[str] = set()
    judgement_stages: list[tuple[int, str, str]] = []

    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stage[{i}] must be a dict")
        key = stage.get("key")
        if not key or not isinstance(key, str):
            raise ValueError(f"stage[{i}] requires a non-empty string 'key'")
        if key in seen_keys:
            raise ValueError(f"duplicate stage key {key!r}")
        seen_keys.add(key)

        foreach = stage.get("foreach")
        if foreach is not None and foreach not in FOREACH:
            raise ValueError(
                f"stage {key!r}: foreach={foreach!r} not in {list(FOREACH)}"
            )

        is_judgement = "judgement" in stage or "subject" in stage or "rubric" in stage
        is_action = "action" in stage
        if is_judgement and is_action:
            raise ValueError(
                f"stage {key!r} mixes judgement fields with 'action' — a stage "
                "is EITHER a judgement or a platform action"
            )
        if not is_judgement and not is_action:
            raise ValueError(
                f"stage {key!r} must declare either (subject, judgement, rubric) "
                "or an 'action'"
            )

        if is_action:
            extra = set(stage) - _STAGE_KEYS_ACTION
            if extra:
                raise ValueError(f"stage {key!r}: unknown fields {sorted(extra)}")
            action = stage["action"]
            if action not in ACTIONS:
                raise ValueError(
                    f"stage {key!r}: unknown action {action!r} — known actions: "
                    f"{ACTIONS}"
                )
            if action in RELEASE_ACTIONS:
                gate = [
                    (j, s, k)
                    for (j, s, k) in judgement_stages
                    if s == "execution" and k == "single"
                ]
                if not gate:
                    raise ValueError(
                        "pipeline refused: release requires a single execution "
                        f"judgement gate — stage {key!r} (action {action!r}) is "
                        "not preceded by any judgement stage with "
                        "subject='execution' AND judgement='single' (a pair "
                        "execution comparison does not count; per-branch single "
                        "execution review is non-negotiable)"
                    )
        else:
            extra = set(stage) - _STAGE_KEYS_JUDGEMENT
            if extra:
                raise ValueError(f"stage {key!r}: unknown fields {sorted(extra)}")
            subject = stage.get("subject")
            judgement = stage.get("judgement")
            rubric = stage.get("rubric")
            if subject not in SUBJECTS:
                raise ValueError(
                    f"stage {key!r}: subject={subject!r} not in {list(SUBJECTS)}"
                )
            if judgement not in JUDGEMENTS:
                raise ValueError(
                    f"stage {key!r}: judgement={judgement!r} not in {list(JUDGEMENTS)}"
                )
            if not rubric or not isinstance(rubric, str):
                raise ValueError(f"stage {key!r}: requires a 'rubric' template name")
            shape = _rubric_shape(conn, rubric)
            if shape is None:
                raise ValueError(
                    f"stage {key!r}: rubric {rubric!r} names no existing "
                    "eval_template — register the rubric first"
                )
            if shape["judgement_kind"] != judgement:
                raise ValueError(
                    f"stage {key!r}: judgement={judgement!r} but rubric "
                    f"{rubric!r} declares judgement_kind="
                    f"{shape['judgement_kind']!r}"
                )
            if subject not in shape["subjects"]:
                raise ValueError(
                    f"stage {key!r}: subject={subject!r} not among rubric "
                    f"{rubric!r} subjects {shape['subjects']}"
                )
            judgement_stages.append((i, subject, judgement))

def register_pipeline(name: str, definition: dict) -> dict:
    """Validate + insert ``definition`` as the next version of ``name``.

    Returns {id, name, version, digest}. Raises ValueError on any contract
    violation (fail closed) — nothing is written unless validation passes.
    """
    if not name or not isinstance(name, str):
        raise ValueError("pipeline name must be a non-empty string")
    with _connect() as conn:
        validate_definition(definition, conn)
        row = conn.execute(
            "SELECT MAX(version) AS v FROM pipeline WHERE name=?", (name,)
        ).fetchone()
        version = (row["v"] or 0) + 1
        canon = canonical_json(definition)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT INTO pipeline(name, version, definition, definition_digest) "
            "VALUES (?, ?, ?, ?)",
            (name, version, canon, digest),
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "version": version, "digest": digest}

def get_pipeline(name: str, version: Optional[int] = None) -> dict:
    """Fetch one pipeline (latest version when ``version`` is None)."""
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT * FROM pipeline WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM pipeline WHERE name=? AND version=?", (name, version)
            ).fetchone()
        if row is None:
            suffix = "" if version is None else f" version {version}"
            raise LookupError(f"no pipeline named {name!r}{suffix}")
        return _row_to_dict(row)

def list_pipelines() -> list[dict]:
    """All pipeline versions, ordered by (name, version)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline ORDER BY name, version"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "definition": json.loads(row["definition"]),
        "digest": row["definition_digest"],
        "created_at": row["created_at"],
    }

def _resolve_domain_id(conn: sqlite3.Connection, domain: Union[str, int]) -> int:
    if isinstance(domain, int):
        row = conn.execute("SELECT id FROM domain WHERE id=?", (domain,)).fetchone()
    else:
        row = conn.execute("SELECT id FROM domain WHERE name=?", (domain,)).fetchone()
    if row is None:
        raise LookupError(f"no domain {domain!r}")
    return row["id"]

def bind_domain(
    domain: Union[str, int], pipeline_name: str, version: Optional[int] = None
) -> dict:
    """Bind ``domain`` (name or id) to (pipeline_name, version) — PERMANENT.

    version=None binds the latest registered version (pinned at bind time —
    the row references the exact pipeline id, so later registrations never
    move an existing binding). A second bind for the same domain raises
    ValueError: bindings are append-only evidence; rebinding = new domain.
    """
    with _connect() as conn:
        domain_id = _resolve_domain_id(conn, domain)
        pipe = get_pipeline(pipeline_name, version)
        existing = conn.execute(
            "SELECT dp.id, p.name, p.version FROM domain_pipeline dp "
            "JOIN pipeline p ON p.id = dp.pipeline_id WHERE dp.domain_id=?",
            (domain_id,),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"domain {domain!r} is already bound to pipeline "
                f"{existing['name']!r} v{existing['version']} — bindings are "
                "permanent; create a new domain to use a different pipeline"
            )
        cur = conn.execute(
            "INSERT INTO domain_pipeline(domain_id, pipeline_id) VALUES (?, ?)",
            (domain_id, pipe["id"]),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "domain_id": domain_id,
            "pipeline": pipe["name"],
            "version": pipe["version"],
        }

def get_domain_binding(domain: Union[str, int]) -> Optional[dict]:
    """Return {pipeline, version, definition} for ``domain``, or None."""
    with _connect() as conn:
        domain_id = _resolve_domain_id(conn, domain)
        row = conn.execute(
            "SELECT p.name, p.version, p.definition FROM domain_pipeline dp "
            "JOIN pipeline p ON p.id = dp.pipeline_id WHERE dp.domain_id=?",
            (domain_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "pipeline": row["name"],
            "version": row["version"],
            "definition": json.loads(row["definition"]),
        }

BRANCH_FIX_REVIEW = {
    "name": "branch-fix-review",
    "stages": [
        {"key": "idea-compare", "subject": "idea", "judgement": "pair",
         "rubric": "pair-idea-wheel-v2"},
        {"key": "author", "action": "branch_author"},
        {"key": "validate-each", "action": "branch_validation"},
        {"key": "execution-each", "subject": "execution", "judgement": "single",
         "rubric": "single-execution-v1", "foreach": "branch"},
        {"key": "release", "action": "audited_release"},
    ],
}

def seed_defaults() -> dict:
    """Register the contract's branch-fix-review pipeline (idempotent).

    Requires the pair-idea-wheel-v2 / single-execution-v1 rubrics to already
    exist (seeded by the judgement-template slice); raises a clear error
    otherwise. If branch-fix-review already exists with the SAME digest,
    returns the existing latest version instead of inserting a new one.
    """
    with _connect() as conn:
        missing = [
            r for r in ("pair-idea-wheel-v2", "single-execution-v1")
            if not _rubric_exists(conn, r)
        ]
    if missing:
        raise ValueError(
            f"cannot seed branch-fix-review: rubric templates {missing} do not "
            "exist yet — seed the judgement templates first "
            "(bin/judgement.py seed-templates), then re-run seed-defaults"
        )
    try:
        latest = get_pipeline("branch-fix-review")
    except LookupError:
        latest = None
    if latest is not None and latest["digest"] == definition_digest(BRANCH_FIX_REVIEW):
        return {k: latest[k] for k in ("id", "name", "version", "digest")}
    return register_pipeline("branch-fix-review", BRANCH_FIX_REVIEW)

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="pipelines.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="apply the shared schema (idempotent)")

    sp = sub.add_parser("register")
    sp.add_argument("--name", required=True)
    sp.add_argument("--definition-file", required=True,
                    help="path to a JSON pipeline definition")

    sp = sub.add_parser("get")
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", type=int)

    sub.add_parser("list")

    sp = sub.add_parser("bind")
    sp.add_argument("--domain", required=True)
    sp.add_argument("--pipeline", required=True)
    sp.add_argument("--version", type=int)

    sp = sub.add_parser("show-binding")
    sp.add_argument("--domain", required=True)

    sub.add_parser("seed-defaults",
                   help="register the contract's branch-fix-review pipeline")

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "init":
        init()
        print(f"schema applied at {_db_path()}")
    elif cmd == "register":
        definition = json.loads(Path(args.definition_file).read_text(encoding="utf-8"))
        _print(register_pipeline(args.name, definition))
    elif cmd == "get":
        _print(get_pipeline(args.name, args.version))
    elif cmd == "list":
        _print(list_pipelines())
    elif cmd == "bind":
        _print(bind_domain(args.domain, args.pipeline, args.version))
    elif cmd == "show-binding":
        _print(get_domain_binding(args.domain))
    elif cmd == "seed-defaults":
        _print(seed_defaults())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
