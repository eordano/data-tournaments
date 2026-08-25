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
    # ADR 0001 §2 concurrency hygiene: wait instead of failing SQLITE_BUSY.
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


def create_domain(
    *,
    name: str,
    description: str,
    corpus_source: dict,
    rubric: str = "card-prioritizer-v0",
    generator_prompt: Optional[str] = None,
    judge_prompt: Optional[str] = None,
) -> int:
    _validate_corpus_source(corpus_source)
    gen = generator_prompt or f"card-generator:{name}"
    jud = judge_prompt or f"judge-instructions:{name}"
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM domain WHERE name=?", (name,)).fetchone()
        if existing is not None:
            raise ValueError(f"domain {name!r} already exists")
        cur = conn.execute(
            "INSERT INTO domain(name, description, generator_prompt, judge_prompt, "
            "                   rubric, corpus_source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, gen, jud, rubric, json.dumps(corpus_source)),
        )
        conn.commit()
        return cur.lastrowid


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
        return  # nothing to do
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
