"""Domain CRUD over the fabric SQLite DB.

A domain bundles (corpus_source, generator_prompt, judge_prompt, rubric)
into a named, runnable card-prioritization tournament.
"""
from __future__ import annotations
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

@dataclass
class DomainSpec:
    id: int
    name: str
    description: str
    generator_prompt: str
    judge_prompt: str
    rubric: str
    corpus_source: dict
    status: str
    created_at: str

_VALID_KINDS = {"sqlite", "filesystem", "inline"}

def _validate_corpus_source(src: dict) -> None:
    if not isinstance(src, dict) or "kind" not in src:
        raise ValueError("corpus_source must be a dict with a 'kind' field")
    kind = src["kind"]
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"corpus_source kind={kind!r} not in {sorted(_VALID_KINDS)}"
        )
    if kind == "sqlite":
        if not src.get("path") or not src.get("query"):
            raise ValueError("sqlite corpus_source requires both 'path' and 'query'")
    elif kind == "filesystem":
        if not src.get("root") or not src.get("glob"):
            raise ValueError("filesystem corpus_source requires both 'root' and 'glob'")
    elif kind == "inline":
        if "items" not in src:
            raise ValueError("inline corpus_source requires 'items'")

THE_SCHEMA_OWNS_THE_DEFAULT_RUBRIC_SO_AN_OMITTED_ONE_IS_OMITTED_FROM_THE_INSERT = (
    "domain.rubric carries a DEFAULT in bin/judgement_schema.sql. Restating "
    "that default as a Python argument makes the column always explicit, so "
    "the schema default goes inert and the two drift silently -- which is how "
    "every new domain ended up naming a template nobody seeds. An unspecified "
    "rubric is left OUT of the INSERT and the schema answers for it."
)

def create_domain(
    *,
    name: str,
    description: str,
    corpus_source: dict,
    rubric: Optional[str] = None,
    generator_prompt: Optional[str] = None,
    judge_prompt: Optional[str] = None,
) -> int:
    _validate_corpus_source(corpus_source)
    gen = generator_prompt or f"card-generator:{name}"
    jud = judge_prompt or f"judge-instructions:{name}"
    columns = ["name", "description", "generator_prompt", "judge_prompt",
               "corpus_source"]
    values = [name, description, gen, jud, json.dumps(corpus_source)]
    if rubric is not None:
        columns.append("rubric")
        values.append(rubric)
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM domain WHERE name=?", (name,)).fetchone()
        if existing is not None:
            raise ValueError(f"domain {name!r} already exists")
        cur = conn.execute(
            f"INSERT INTO domain({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        conn.commit()
        return cur.lastrowid

THE_RUBRIC_REGISTRY_OWNS_THE_DEFAULT_BECAUSE_A_SCHEMA_DEFAULT_CANNOT_BE_MIGRATED = (
    "domain.rubric carries a DEFAULT, but the table is created with CREATE TABLE IF "
    "NOT EXISTS and SQLite cannot ALTER a column default -- so on any database that "
    "predates a rubric rename the schema default still names the retired rubric, which "
    "is exactly the population the vocabulary reset notice is written for. The name a "
    "domain gets is therefore read from the rubric registry, which is the same source "
    "that seeds the template. The schema default mirrors it for hand-written SQL and is "
    "asserted equal on a fresh database, never consulted at runtime."
)

def default_rubric() -> str:
    """The rubric a domain created without one gets.

    See THE_RUBRIC_REGISTRY_OWNS_THE_DEFAULT_BECAUSE_A_SCHEMA_DEFAULT_CANNOT_BE_MIGRATED.
    """
    from bin import judgement

    return judgement.DEFAULT_TEMPLATE_NAME

def schema_default_rubric() -> Optional[str]:
    """What the domain table's own DEFAULT says, or None if it has none.

    Stale on any pre-rename database. Compared against default_rubric() by the
    fresh-database test; never used to decide what a new domain gets.
    """
    with _connect() as conn:
        for row in conn.execute("PRAGMA table_info(domain)"):
            if row["name"] == "rubric":
                value = row["dflt_value"]
                return None if value is None else str(value).strip("'")
    return None

def get_domain(name: str) -> DomainSpec:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM domain WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no domain named {name!r}")
        return _row_to_spec(row)

def list_domains(status: str = "active") -> list[DomainSpec]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM domain WHERE status=? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
        return [_row_to_spec(r) for r in rows]

def archive_domain(name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE domain SET status='archived' WHERE name=?", (name,))
        conn.commit()

def update_domain(
    name: str,
    *,
    description: Optional[str] = None,
    corpus_source: Optional[dict] = None,
) -> None:
    """Update a domain in place. Name is immutable (it's the foreign key to
    Langfuse prompt names — rename = archive + create).

    Prompt edits are not handled here — they're a separate concern owned by
    bin.prompts.push (which is text-equality idempotent). Callers wanting to
    edit prompts should push them and call this with description/corpus_source
    only.
    """
    if description is None and corpus_source is None:
        return
    if corpus_source is not None:
        _validate_corpus_source(corpus_source)
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM domain WHERE name=?", (name,)).fetchone()
        if existing is None:
            raise LookupError(f"no domain named {name!r}")
        sets, params = [], []
        if description is not None:
            sets.append("description=?")
            params.append(description)
        if corpus_source is not None:
            sets.append("corpus_source=?")
            params.append(json.dumps(corpus_source))
        params.append(name)
        conn.execute(f"UPDATE domain SET {', '.join(sets)} WHERE name=?", params)
        conn.commit()

def _row_to_spec(row: sqlite3.Row) -> DomainSpec:
    return DomainSpec(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        generator_prompt=row["generator_prompt"],
        judge_prompt=row["judge_prompt"],
        rubric=row["rubric"],
        corpus_source=json.loads(row["corpus_source"]),
        status=row["status"],
        created_at=row["created_at"],
    )
