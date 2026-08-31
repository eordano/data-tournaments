#!/usr/bin/env python3
"""Settled tournament position -> implementation queue: the dispatch spine.

The tournament computes standings and nothing reads them; ``branch_author``
routes by work type and nobody hands it one. This module is the join, and it
is the only place in the tree where an item stops being a judged artifact and
becomes work somebody (or something) starts.

What it dispatches on
---------------------

**Standing, never the self-assessed priority.** ``WorkOrderDraft.priority``
is a model's absolute guess at an item it saw alone; the order here is
``swiss.standings`` — points earned by pairwise comparison, best first
(docs/design/priority-tournament.md, "The rule"). The priority field rides
along in the payload so the two can be compared later; it never sorts.

**Only items with a settled position.** ``played == 0`` means no comparison
established anything, and ``swiss.standings`` gives such an item rank 0 —
the same rule ``WorkOrder.TournamentStanding`` enforces on the way in. An
unplayed item is skipped, not dispatched at the bottom. The guard here is
decided matches — wins + draws + losses — rather than ``played``, so a
result that scores nothing can never buy a position on this queue.

**Never a discarded item.** A discard is a verdict, not a loss: the item
left the pool and is absent from standings entirely. The pool is re-read
inside the claim transaction, because a dispatch run spends minutes per
item and the judge queue keeps moving underneath it: an item discarded
after the queue was computed must not still be handed to a backend.

Idempotence
-----------

Dispatching a domain twice must not author twice. The claim key is
``sha256`` over the item's pool content — the same content-derived identity
the pair key is built from, applied to one side instead of two, so it is
stable across re-generation, re-ranking and process restarts.

The claim is written BEFORE the backend runs, not after it succeeds. A
``work_dispatch`` row starts at ``claimed`` under a partial UNIQUE index on
(domain, dispatch key), carrying the deterministic branch name, the pool it
was ranked in and the pair keys its standing was earned on; when the beat
resolves, that one row moves to exactly one terminal outcome. A kill
anywhere in between therefore leaves a ledger row NAMING the branch rather
than a real branch nobody recorded, and the retry reports
``already-dispatched`` with the unresolved claim in its detail.

A ``failed`` row deliberately does not claim the key: nothing was authored,
so a retry is the honest behaviour. A failure also never blocks the rest of
the table — the remaining top group is already settled.

The return edge
---------------

``work_dispatch``/``work_dispatch_pair`` are also what lets validation find
its way home: :func:`standing_for_branch` rebuilds the standing an item was
dispatched under from the ledger, so ``bin/branch_validator.validate`` can
be handed the standing it already knows how to record as ranking evidence.
Without the pool id and the pair keys persisted here, that edge is dead on
every real invocation.

Work-type routing
-----------------

``branch_author.is_authorable(None)`` fails OPEN so that callers carrying no
work type keep working. Dispatch is fail-CLOSED in the other direction: an
``investigation``, an unknown type, and an item with no declared type at all
all route to ``human-queue`` and land in the ``work_type_refusal`` ledger.
The reason is the failure mode the design names — an investigation handed to
a backend does not fail, it produces an empty commit — and dispatch is the
caller that always knows the work type, so it has no excuse to guess.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import swiss  # noqa: E402
from bin.branch_author import (  # noqa: E402
    AUTHORABLE_WORK_TYPES,
    AuthoringError,
    NotAuthorable,
)
from bin.workorder import TournamentStanding  # noqa: E402

DEST_BRANCH_AUTHOR = "branch-author"
DEST_HUMAN_QUEUE = "human-queue"
DESTINATIONS = (DEST_BRANCH_AUTHOR, DEST_HUMAN_QUEUE)

OUTCOME_CLAIMED = "claimed"
OUTCOME_AUTHORED = "authored"
OUTCOME_ROUTED_TO_HUMAN = "routed-to-human"
OUTCOME_FAILED = "failed"
OUTCOME_ALREADY_DISPATCHED = "already-dispatched"
OUTCOME_DISCARDED_SINCE_QUEUED = "discarded-since-queued"

TERMINAL_OUTCOMES = (OUTCOME_AUTHORED, OUTCOME_ROUTED_TO_HUMAN, OUTCOME_FAILED)
PERSISTED_OUTCOMES = (OUTCOME_CLAIMED,) + TERMINAL_OUTCOMES
REPORT_ONLY_OUTCOMES = (OUTCOME_ALREADY_DISPATCHED, OUTCOME_DISCARDED_SINCE_QUEUED)

assert set(PERSISTED_OUTCOMES).isdisjoint(REPORT_ONLY_OUTCOMES), (
    "a report-only outcome describes an item this run did NOT take off the "
    "queue, so it can never be a row's state: already-dispatched names "
    "somebody else's row and discarded-since-queued names no row at all"
)

MIN_PLAYED_FOR_A_POSITION = 1

A_SKIPPED_PAIRING_ESTABLISHES_NOTHING_SO_IT_IS_NOT_A_DECIDED_MATCH = (
    "swiss records no result for a skipped pairing, so it moves no played "
    "count and awards no rank. Dispatch filters on DECIDED matches (wins + "
    "draws + losses) rather than Standing.played anyway: the two agree "
    "today, and the filter is what keeps a position nothing established out "
    "of the queue if they ever stop agreeing."
)

DEFAULT_BRANCH_PREFIX = "dispatch"


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


_WORK_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS work_dispatch (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id      INTEGER NOT NULL,
  dispatch_key   TEXT NOT NULL,
  item_id        TEXT NOT NULL DEFAULT '',
  pool_id        TEXT NOT NULL DEFAULT '',
  standing_rank  INTEGER NOT NULL DEFAULT 0,
  points         INTEGER NOT NULL DEFAULT 0,
  played         INTEGER NOT NULL DEFAULT 0,
  rounds         INTEGER NOT NULL DEFAULT 0,
  work_type      TEXT NOT NULL DEFAULT '',
  destination    TEXT NOT NULL
                 CHECK (destination IN ('branch-author', 'human-queue')),
  outcome        TEXT NOT NULL
                 CHECK (outcome IN ('claimed', 'authored', 'routed-to-human',
                                    'failed')),
  workorder_ref  TEXT NOT NULL DEFAULT '',
  branch_name    TEXT NOT NULL DEFAULT '',
  fix_branch_id  INTEGER,
  authoring_id   INTEGER,
  refusal_id     INTEGER,
  detail         TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_dispatch_claim
  ON work_dispatch(domain_id, dispatch_key) WHERE outcome <> 'failed';
CREATE INDEX IF NOT EXISTS idx_work_dispatch_domain
  ON work_dispatch(domain_id, id);
DROP TRIGGER IF EXISTS work_dispatch_immutable;
CREATE TRIGGER IF NOT EXISTS work_dispatch_only_a_claim_resolves_once
  BEFORE UPDATE ON work_dispatch
  WHEN OLD.outcome <> 'claimed'
    OR NEW.outcome = 'claimed'
    OR NEW.domain_id <> OLD.domain_id
    OR NEW.dispatch_key <> OLD.dispatch_key
    OR NEW.item_id <> OLD.item_id
    OR NEW.pool_id <> OLD.pool_id
    OR NEW.workorder_ref <> OLD.workorder_ref
    OR NEW.standing_rank <> OLD.standing_rank
    OR NEW.points <> OLD.points
    OR NEW.played <> OLD.played
    OR NEW.rounds <> OLD.rounds
    OR NEW.destination <> OLD.destination
    OR NEW.created_at <> OLD.created_at
  BEGIN
    SELECT RAISE(ABORT, 'a work_dispatch row moves from its claim to ONE terminal outcome and nowhere else; the claimed standing is immutable');
  END;
CREATE TRIGGER IF NOT EXISTS work_dispatch_no_delete
  BEFORE DELETE ON work_dispatch
  BEGIN SELECT RAISE(ABORT, 'work_dispatch rows are append-only'); END;

CREATE TABLE IF NOT EXISTS work_dispatch_pair (
  dispatch_id INTEGER NOT NULL REFERENCES work_dispatch(id),
  pair_key    TEXT    NOT NULL,
  PRIMARY KEY (dispatch_id, pair_key)
);
CREATE INDEX IF NOT EXISTS idx_work_dispatch_pair_key
  ON work_dispatch_pair(pair_key);
CREATE TRIGGER IF NOT EXISTS work_dispatch_pair_immutable
  BEFORE UPDATE ON work_dispatch_pair
  BEGIN SELECT RAISE(ABORT, 'work_dispatch_pair rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS work_dispatch_pair_no_delete
  BEFORE DELETE ON work_dispatch_pair
  BEGIN SELECT RAISE(ABORT, 'work_dispatch_pair rows are append-only'); END;
"""

_WORK_DISPATCH_ADDED_COLUMNS = (
    ("pool_id", "TEXT NOT NULL DEFAULT ''"),
    ("rounds", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_schema(conn) -> None:
    """Apply the ledger DDL, then add the columns a ledger written before the
    return edge existed predates.

    A row from before this migration carries pool_id '' and rounds 0, and
    :func:`standing_for_branch` refuses it BY NAME rather than inventing a
    pool for it: the return edge for those branches was never recorded, and
    guessing one would key a beat outcome to a pool nobody ranked in.
    """
    conn.executescript(_WORK_DISPATCH_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(work_dispatch)")}
    for column, decl in _WORK_DISPATCH_ADDED_COLUMNS:
        if column not in columns:
            conn.execute(f"ALTER TABLE work_dispatch ADD COLUMN {column} {decl}")
    conn.commit()


def init() -> None:
    """Apply the shared schema plus the work_dispatch ledger. Idempotent."""
    from bin import branch_author

    branch_author.init()
    with _connect() as conn:
        _ensure_schema(conn)


def dispatch_key(content: str) -> str:
    """The stable identity of one dispatchable item.

    sha256 over the pool content, which is the canonical JSON of the judged
    payload — the same material the pair key hashes, taken one side at a
    time. Regenerating the same work order yields the same key, so a second
    dispatch run recognises it without anybody allocating an id.
    """
    return swiss.content_digest(content)


def work_type_of(payload: Any) -> str:
    """The declared ``WorkOrder.work_type`` of a judged payload, or ''.

    '' is not a default work type; it means the payload declared none, which
    routes to a person.
    """
    if not isinstance(payload, dict):
        return ""
    order = payload.get("work_order")
    if isinstance(order, dict):
        declared = order.get("work_type")
        if isinstance(declared, str) and declared.strip():
            return declared.strip().lower()
    declared = payload.get("work_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    return ""


def destination_for(work_type: Optional[str]) -> str:
    """Where an item of this work type goes.

    Fail-closed: only the mechanically-authorable types reach a backend.
    ``None`` and '' are NOT treated as "routing not enforced" here the way
    ``branch_author.is_authorable`` treats them — dispatch always knows the
    work type it read off the payload, so an absent one is a defect in the
    item, and sending it to a backend would produce the empty commit the
    routing rule exists to prevent.
    """
    return (DEST_BRANCH_AUTHOR if work_type in AUTHORABLE_WORK_TYPES
            else DEST_HUMAN_QUEUE)


@dataclass(frozen=True)
class QueueEntry:
    """One settled item, ready to hand off."""

    key: str
    item_id: str
    rank: int
    work_type: str
    destination: str
    title: str
    standing: TournamentStanding
    payload: dict = field(default_factory=dict, repr=False)

    @property
    def points(self) -> int:
        return self.standing.points

    @property
    def played(self) -> int:
        return self.standing.played

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "item_id": self.item_id,
            "rank": self.rank,
            "points": self.points,
            "played": self.played,
            "work_type": self.work_type,
            "destination": self.destination,
            "title": self.title,
            "standing": self.standing.model_dump(),
        }


@dataclass(frozen=True)
class DispatchRecord:
    """What happened to one queue entry."""

    key: str
    item_id: str
    rank: int
    work_type: str
    destination: str
    outcome: str
    title: str = ""
    detail: str = ""
    branch_name: str = ""
    fix_branch_id: Optional[int] = None
    authoring_id: Optional[int] = None
    refusal_id: Optional[int] = None
    dispatch_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "item_id": self.item_id,
            "rank": self.rank,
            "work_type": self.work_type,
            "destination": self.destination,
            "outcome": self.outcome,
            "title": self.title,
            "detail": self.detail,
            "branch_name": self.branch_name,
            "fix_branch_id": self.fix_branch_id,
            "authoring_id": self.authoring_id,
            "refusal_id": self.refusal_id,
            "dispatch_id": self.dispatch_id,
        }


def _title_of(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("title", "label"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    order = payload.get("work_order")
    if isinstance(order, dict) and isinstance(order.get("title"), str):
        return order["title"].strip()
    return ""


def _pair_keys_for(pool: "swiss.Pool", item_id: str) -> list[str]:
    keys: list[str] = []
    for result in swiss.live_results(pool):
        if item_id in (result.item_a, result.item_b) and result.pair_key not in keys:
            keys.append(result.pair_key)
    return keys


def decided_matches(row: "swiss.Standing") -> int:
    """How many of this item's played matches DECIDED something.

    ``Standing.played`` counts every pairing the engine recorded a result
    for; a bye, a discard survivor and a skip get no result at all. Counting
    wins + draws + losses keeps dispatch honest even for a result whose
    outcome scores nothing.
    """
    return row.wins + row.draws + row.losses


def queue_from_pool(
    pool: "swiss.Pool",
    *,
    pool_id: str = "",
    min_played: int = MIN_PLAYED_FOR_A_POSITION,
    limit: Optional[int] = None,
) -> list[QueueEntry]:
    """The settled items of a pool, in standings order.

    Storage-agnostic on purpose (``swiss.Pool`` is an in-memory value), so
    the ordering rule can be exercised without a database. ``min_played``
    counts :func:`decided_matches`, never ``Standing.played`` — see
    A_SKIPPED_PAIRING_ESTABLISHES_NOTHING_SO_IT_IS_NOT_A_DECIDED_MATCH.
    """
    if min_played < MIN_PLAYED_FOR_A_POSITION:
        raise ValueError(
            f"min_played={min_played} would dispatch items with no played "
            "match; rank 0 is not a position, it is the absence of one"
        )
    rounds = swiss.rounds_total(pool)
    entries: list[QueueEntry] = []
    for row in swiss.standings(pool):
        if row.item_id in pool.discarded:
            continue
        if decided_matches(row) < min_played or row.rank < 1:
            continue
        item = pool.items[row.item_id]
        payload = item.payload if isinstance(item.payload, dict) else {}
        work_type = work_type_of(payload)
        entries.append(
            QueueEntry(
                key=dispatch_key(item.content),
                item_id=row.item_id,
                rank=row.rank,
                work_type=work_type,
                destination=destination_for(work_type),
                title=_title_of(payload),
                standing=TournamentStanding(
                    points=row.points,
                    played=row.played,
                    rank=row.rank,
                    rounds=rounds,
                    pool_id=pool_id,
                    pair_keys=_pair_keys_for(pool, row.item_id),
                ),
                payload=payload,
            )
        )
        if limit is not None and len(entries) >= limit:
            break
    return entries


def _resolve_domain(domain) -> tuple[int, str]:
    with _connect() as conn:
        if isinstance(domain, int) or (isinstance(domain, str) and domain.isdigit()):
            row = conn.execute(
                "SELECT id, name FROM domain WHERE id=?", (int(domain),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name FROM domain WHERE name=?", (str(domain),)
            ).fetchone()
    if row is None:
        raise LookupError(f"no domain {domain!r} in {_db_path()}")
    return int(row["id"]), str(row["name"])


def _config_for(conn, domain_id: int, rubric: Optional[str]):
    """The human job configuration this dispatch reads the pool through.

    ``bin.generate_cards`` falls back to an arbitrary active human config
    when the requested rubric matches none, which is right for a draw (some
    ordering is better than none) and wrong here: dispatch turns the
    resulting order into REAL branches, so an explicit --rubric that matched
    nothing must raise rather than author under a rubric nobody asked for.
    """
    from bin import generate_cards

    cfg = generate_cards._human_config_for_rubric(conn, domain_id, rubric)
    if rubric is not None and cfg["name"] != rubric:
        available = sorted({
            row["name"]
            for row in conn.execute(
                "SELECT t.name AS name FROM job_configuration c "
                "JOIN eval_template t ON t.id = c.template_id "
                "WHERE c.status='active' AND c.rater_type='human'"
            ).fetchall()
        })
        raise ValueError(
            f"rubric {rubric!r} matches no active human job configuration; "
            f"available: {available}. Refusing to author branches under "
            f"{cfg['name']!r}, which is an ordering nobody asked for"
        )
    return cfg


def _load(domain, rubric: Optional[str]):
    """(domain_id, domain_name, pool) for a domain's judged queue rows.

    The pending queue IS the tournament store; rebuilding the pool through
    bin.generate_cards keeps ONE loader in the tree rather than a second
    reader of the same payload shape that can drift from it.
    """
    domain_id, domain_name = _resolve_domain(domain)
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        cfg = _config_for(conn, domain_id, rubric)
        from bin import generate_cards

        pool = generate_cards._load_pool(conn, domain_id, cfg)
    finally:
        conn.close()
    return domain_id, domain_name, pool


def pool_id_for(domain_id: int, pool: "swiss.Pool") -> str:
    return f"domain:{domain_id}:{pool.rubric_id}:v{pool.rubric_version}"


def queue(
    domain,
    *,
    rubric: Optional[str] = None,
    min_played: int = MIN_PLAYED_FOR_A_POSITION,
    limit: Optional[int] = None,
) -> list[QueueEntry]:
    """The implementation queue for a domain: settled items, best first."""
    domain_id, _name, pool = _load(domain, rubric)
    return queue_from_pool(
        pool,
        pool_id=pool_id_for(domain_id, pool),
        min_played=min_played,
        limit=limit,
    )


def claimed_keys(domain_id: int) -> set[str]:
    """Dispatch keys this domain has already handed off (failures excluded —
    nothing was authored, so a retry is honest)."""
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT dispatch_key FROM work_dispatch "
            "WHERE domain_id=? AND outcome <> ?",
            (domain_id, OUTCOME_FAILED),
        ).fetchall()
    return {r["dispatch_key"] for r in rows}


def _pair_keys_of(conn, dispatch_id: int) -> list[str]:
    return [
        r["pair_key"]
        for r in conn.execute(
            "SELECT pair_key FROM work_dispatch_pair WHERE dispatch_id=? "
            "ORDER BY pair_key",
            (dispatch_id,),
        ).fetchall()
    ]


def dispatched(domain=None) -> list[dict]:
    """The dispatch ledger, oldest first, each row carrying the pair keys the
    item's standing was earned on."""
    with _connect() as conn:
        _ensure_schema(conn)
        if domain is None:
            rows = conn.execute(
                "SELECT * FROM work_dispatch ORDER BY id"
            ).fetchall()
        else:
            domain_id, _name = _resolve_domain(domain)
            rows = conn.execute(
                "SELECT * FROM work_dispatch WHERE domain_id=? ORDER BY id",
                (domain_id,),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["pair_keys"] = _pair_keys_of(conn, row["id"])
            out.append(record)
    return out


def standing_for_branch(fix_branch_id: int) -> Optional[dict]:
    """The standing the dispatcher recorded for the item a branch implements.

    This is the return edge's lookup: ``bin/fix_branches.py`` hands the result
    straight to ``branch_validator.validate(standing=...)``, which keys the
    beat outcome back to the judgements that produced it. ``None`` means this
    branch did not come out of a tournament, and validation runs without a
    return edge — a legitimate case (a hand-registered fix branch), not a
    degraded one.

    A ledger row written before the pool id was persisted is refused BY NAME:
    its return edge was never recorded, and inventing a pool for it would key
    a beat outcome to a ranking that never happened.
    """
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM work_dispatch WHERE fix_branch_id=? "
            "ORDER BY id DESC LIMIT 1",
            (fix_branch_id,),
        ).fetchone()
        if row is None:
            return None
        pair_keys = _pair_keys_of(conn, row["id"])
    if not row["pool_id"]:
        raise ValueError(
            f"work_dispatch row {row['id']} for fix_branch {fix_branch_id} "
            "carries no pool_id: it was written before the dispatch ledger "
            "recorded the return edge, so the pool it was ranked in is not "
            "known and no ranking evidence can honestly be keyed to it"
        )
    return {
        "points": row["points"],
        "played": row["played"],
        "rank": row["standing_rank"],
        "rounds": row["rounds"],
        "pool_id": row["pool_id"],
        "pair_keys": pair_keys,
    }


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to take an item off the queue.

    ``status`` is ``claimed`` (this run owns the item and ``dispatch_id``
    names its ledger row), ``already-dispatched`` (another row already holds
    the key), or ``discarded-since-queued`` (a judge ejected the item while
    this run was working on the items above it).
    """

    status: str
    dispatch_id: Optional[int] = None
    detail: str = ""


def _claim(
    domain_id: int,
    entry: QueueEntry,
    *,
    rubric: Optional[str],
    branch_name: str,
    workorder_ref: str,
) -> Claim:
    """Take ownership of ONE item before any backend runs.

    Everything that can invalidate the queue is re-read HERE, in the
    transaction that writes the claim, because the snapshot the run started
    from is minutes old by the time the table's tail is reached:

    * the pool is rebuilt from the judge queue as it stands NOW, so an item
      discarded since it was queued leaves without a branch;
    * the claim itself is the UNIQUE index, so a concurrent run (or a
      previous one) losing the race is reported ``already-dispatched``
      instead of raising sqlite3.IntegrityError out of the middle of the
      table.
    """
    from bin import generate_cards

    assert entry.destination in DESTINATIONS, (
        f"{entry.destination!r} is not a destination; every item goes to "
        f"exactly one of {DESTINATIONS}"
    )
    conn = _connect()
    conn.isolation_level = None
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        cfg = _config_for(conn, domain_id, rubric)
        pool = generate_cards._load_pool(conn, domain_id, cfg)
        if entry.item_id in pool.discarded:
            conn.execute("ROLLBACK")
            dropped = pool.discarded[entry.item_id]
            return Claim(
                status=OUTCOME_DISCARDED_SINCE_QUEUED,
                detail=(
                    f"{dropped.verdict} in round {dropped.round} landed after "
                    "this run read the queue; a discarded item is never "
                    "dispatched"
                ),
            )
        try:
            cur = conn.execute(
                "INSERT INTO work_dispatch(domain_id, dispatch_key, item_id, "
                "pool_id, standing_rank, points, played, rounds, work_type, "
                "destination, outcome, workorder_ref, branch_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    domain_id,
                    entry.key,
                    entry.item_id,
                    entry.standing.pool_id,
                    entry.rank,
                    entry.points,
                    entry.played,
                    entry.standing.rounds,
                    entry.work_type,
                    entry.destination,
                    OUTCOME_CLAIMED,
                    workorder_ref,
                    branch_name,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return Claim(
                status=OUTCOME_ALREADY_DISPATCHED,
                detail=_claim_holder(conn, domain_id, entry.key),
            )
        dispatch_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO work_dispatch_pair(dispatch_id, pair_key) "
            "VALUES (?, ?)",
            [(dispatch_id, key) for key in entry.standing.pair_keys],
        )
        conn.execute("COMMIT")
        return Claim(status=OUTCOME_CLAIMED, dispatch_id=dispatch_id)
    finally:
        conn.close()


def _claim_holder(conn, domain_id: int, key: str) -> str:
    """Why an item is already dispatched, in the words an operator needs.

    An UNRESOLVED claim is the interesting case: a previous run was killed
    between taking the key and finishing the beat, so the branch it names may
    exist in git with nothing else recorded about it.
    """
    row = conn.execute(
        "SELECT id, outcome, branch_name FROM work_dispatch "
        "WHERE domain_id=? AND dispatch_key=? AND outcome <> ? "
        "ORDER BY id DESC LIMIT 1",
        (domain_id, key, OUTCOME_FAILED),
    ).fetchone()
    if row is None:
        return "claimed by an earlier dispatch"
    if row["outcome"] == OUTCOME_CLAIMED:
        return (
            f"work_dispatch row {row['id']} still holds an UNRESOLVED claim on "
            f"branch {row['branch_name'] or '(none)'}: an earlier run was "
            "interrupted between claiming this item and finishing it"
        )
    return f"work_dispatch row {row['id']} already recorded {row['outcome']}"


def _resolve(
    dispatch_id: int,
    *,
    outcome: str,
    branch_name: Optional[str] = None,
    fix_branch_id: Optional[int] = None,
    authoring_id: Optional[int] = None,
    refusal_id: Optional[int] = None,
    detail: str = "",
) -> None:
    """Move a claim to its ONE terminal outcome."""
    assert outcome in TERMINAL_OUTCOMES, (
        f"{outcome!r} is not a terminal dispatch outcome; "
        f"{OUTCOME_ALREADY_DISPATCHED} and {OUTCOME_DISCARDED_SINCE_QUEUED} "
        "are reports about a row that was never taken, never a row's state"
    )
    with _connect() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE work_dispatch SET outcome=?, "
            "branch_name=COALESCE(?, branch_name), fix_branch_id=?, "
            "authoring_id=?, refusal_id=?, detail=? "
            "WHERE id=? AND outcome=?",
            (outcome, branch_name, fix_branch_id, authoring_id, refusal_id,
             detail, dispatch_id, OUTCOME_CLAIMED),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"work_dispatch row {dispatch_id} is not an open claim; a "
                "dispatch beat resolves exactly once"
            )
        conn.commit()


def branch_name_for(entry: QueueEntry, *, domain_id: int,
                    prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """Deterministic branch name, derived from the dispatch key.

    Deterministic so a second dispatch of the same item collides in git even
    if the ledger is gone; domain-scoped so the same work order text in two
    domains does not.
    """
    return f"{prefix}/d{domain_id}-{entry.key[:12]}"


def author_context_for(entry: QueueEntry, *, domain_name: str) -> dict:
    """The environment a command backend needs to author THIS item.

    Standing travels with it: a backend that can see the position also knows
    the item was ranked, not self-nominated. The work order body is not in
    here — the backend reads it from the queue, and env is not a transport
    for a document.
    """
    return {
        "WORKORDER_DOMAIN": domain_name,
        "WORKORDER_KEY": entry.key,
        "WORKORDER_ITEM_ID": entry.item_id,
        "WORKORDER_POOL_ID": entry.standing.pool_id,
        "WORKORDER_TITLE": entry.title,
        "WORKORDER_WORK_TYPE": entry.work_type,
        "WORKORDER_RANK": str(entry.rank),
        "WORKORDER_POINTS": str(entry.points),
        "WORKORDER_PLAYED": str(entry.played),
    }


def _record_of(entry: QueueEntry, *, outcome: str, destination: Optional[str] = None,
               **fields) -> DispatchRecord:
    return DispatchRecord(
        key=entry.key,
        item_id=entry.item_id,
        rank=entry.rank,
        work_type=entry.work_type,
        destination=destination or entry.destination,
        outcome=outcome,
        title=entry.title,
        **fields,
    )


def _require_repo_for(entries: list[QueueEntry], repo_path: Optional[str],
                      base_ref: str) -> None:
    """Refuse a run that cannot finish, BEFORE it claims anything.

    The old check sat inside the loop, so a table whose first items route to
    a person claimed them, authored nothing, and then aborted on the first
    authorable item — leaving a half-dispatched domain behind. It also
    suggested raising ``min_played`` or ``limit`` to dodge the problem, which
    neither flag does: ``limit`` takes the TOP of the table, where the
    authorable items are, and ``min_played`` filters on comparisons, not on
    work type.

    A repo that is not a repo, and a base ref that names no commit, are the
    same class of mistake and are refused here for the same reason: they
    cannot fail on ONE item, only on every one of them.
    """
    authorable = [e for e in entries if e.destination == DEST_BRANCH_AUTHOR]
    if not authorable:
        return
    if repo_path is None:
        ranks = ", ".join(f"rank {e.rank} ({e.work_type})" for e in authorable[:5])
        raise ValueError(
            f"{len(authorable)} of {len(entries)} queued items are authorable "
            f"[{ranks}] but dispatch_domain got no repo_path; pass one. "
            "Nothing was claimed: a run that cannot author is refused before "
            "it takes any item off the queue"
        )
    from bin import branch_author

    try:
        branch_author._git(repo_path, "rev-parse", "--verify",
                           f"{base_ref}^{{commit}}")
    except ValueError as exc:
        raise ValueError(
            f"cannot author from {base_ref!r} in {repo_path!r} ({exc}); "
            "nothing was claimed"
        ) from None


def run_exit_code(records: list[DispatchRecord]) -> int:
    """0 unless the run authored NOTHING and failed at something.

    A single failure among authored branches is not a failed run — the rest
    of the table is already settled and was dispatched. A run where every
    attempt to author failed is, and exiting 0 on it hides a broken backend
    from whatever scheduled the run.
    """
    failed = [r for r in records if r.outcome == OUTCOME_FAILED]
    if failed and not any(r.outcome == OUTCOME_AUTHORED for r in records):
        return 1
    return 0


def dispatch_domain(
    domain,
    *,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    backend: str = "fixture",
    backend_config: Optional[dict] = None,
    backend_config_for: Optional[Callable[[QueueEntry], dict]] = None,
    rubric: Optional[str] = None,
    min_played: int = MIN_PLAYED_FOR_A_POSITION,
    limit: Optional[int] = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    allow_unresolved: bool = False,
) -> list[DispatchRecord]:
    """Hand every settled item of ``domain`` to its destination, best first.

    Returns one DispatchRecord per queue entry, in standings order. The
    handoff STOPS at authoring: validation and shipping are separate beats
    with their own gates, and nothing here calls them.

    Every item is CLAIMED before its backend runs, and the claim transaction
    re-reads the pool, so the answer for one item is one of five: authored,
    routed-to-human, failed, already-dispatched (another claim holds the
    key), or discarded-since-queued (a judge ejected it while this run was
    working on the items above it).

    Refuses BEFORE the loop, having claimed nothing, when the run could not
    finish anyway: an authorable item with no repo to author into, a repo
    that is not a repo, or a base ref that names no commit.

    ``backend_config_for`` overrides ``backend_config`` per item, which is
    how a real coding-agent backend gets item-specific instructions.
    """
    from bin import branch_author

    init()
    domain_id, domain_name, pool = _load(domain, rubric)
    entries = queue_from_pool(
        pool,
        pool_id=pool_id_for(domain_id, pool),
        min_played=min_played,
        limit=limit,
    )
    _require_repo_for(entries, repo_path, base_ref)
    records: list[DispatchRecord] = []

    for entry in entries:
        branch = (
            branch_name_for(entry, domain_id=domain_id, prefix=branch_prefix)
            if entry.destination == DEST_BRANCH_AUTHOR else ""
        )
        claim = _claim(
            domain_id,
            entry,
            rubric=rubric,
            branch_name=branch,
            workorder_ref=entry.item_id,
        )
        if claim.status != OUTCOME_CLAIMED:
            records.append(
                _record_of(entry, outcome=claim.status, detail=claim.detail)
            )
            continue
        dispatch_id = claim.dispatch_id

        if entry.destination == DEST_HUMAN_QUEUE:
            refusal_id = branch_author.route_to_human(
                entry.work_type or "(undeclared)",
                workorder_ref=entry.item_id,
                detail=(
                    f"rank {entry.rank} in {entry.standing.pool_id}: "
                    "not a mechanically authorable work type"
                ),
            )
            detail = f"work_type {entry.work_type or '(undeclared)'}"
            _resolve(dispatch_id, outcome=OUTCOME_ROUTED_TO_HUMAN,
                     refusal_id=refusal_id, detail=detail)
            records.append(
                _record_of(entry, outcome=OUTCOME_ROUTED_TO_HUMAN,
                           detail=detail, refusal_id=refusal_id,
                           dispatch_id=dispatch_id)
            )
            continue

        assert entry.work_type in AUTHORABLE_WORK_TYPES, (
            f"refusing to author work_type {entry.work_type!r}: dispatch is "
            "fail-closed on work type because is_authorable(None) fails open "
            "for callers that carry none, and an investigation handed to a "
            "backend does not fail — it produces an empty commit"
        )
        config = (backend_config_for(entry) if backend_config_for is not None
                  else (backend_config or {}))
        try:
            authored = branch_author.author_branch(
                repo_path,
                base_ref=base_ref,
                branch_name=branch,
                backend=backend,
                backend_config=config,
                workorder_ref=entry.item_id,
                work_type=entry.work_type,
                allow_unresolved=allow_unresolved,
                author_context=author_context_for(entry, domain_name=domain_name),
            )
        except NotAuthorable as exc:
            _resolve(dispatch_id, outcome=OUTCOME_ROUTED_TO_HUMAN,
                     refusal_id=exc.refusal_id, detail=str(exc))
            records.append(
                _record_of(entry, outcome=OUTCOME_ROUTED_TO_HUMAN,
                           destination=DEST_HUMAN_QUEUE, detail=str(exc),
                           branch_name=branch, refusal_id=exc.refusal_id,
                           dispatch_id=dispatch_id)
            )
            continue
        except (AuthoringError, ValueError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            _resolve(dispatch_id, outcome=OUTCOME_FAILED, detail=detail)
            records.append(
                _record_of(entry, outcome=OUTCOME_FAILED, detail=detail,
                           branch_name=branch, dispatch_id=dispatch_id)
            )
            continue

        detail = f"{authored['base_sha'][:12]}..{authored['head_sha'][:12]}"
        _resolve(dispatch_id, outcome=OUTCOME_AUTHORED,
                 fix_branch_id=authored["fix_branch_id"],
                 authoring_id=authored["authoring_id"], detail=detail)
        records.append(
            _record_of(entry, outcome=OUTCOME_AUTHORED, detail=detail,
                       branch_name=branch,
                       fix_branch_id=authored["fix_branch_id"],
                       authoring_id=authored["authoring_id"],
                       dispatch_id=dispatch_id)
        )
    return records


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="dispatch.py", description=__doc__.splitlines()[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("queue", help="the settled items, in standings order")
    sp.add_argument("--domain", required=True)
    sp.add_argument("--rubric")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--min-played", type=int, default=MIN_PLAYED_FOR_A_POSITION)

    sp = sub.add_parser("run", help="dispatch the settled items")
    sp.add_argument("--domain", required=True)
    sp.add_argument("--repo")
    sp.add_argument("--base-ref", default="HEAD")
    sp.add_argument("--backend", default="fixture")
    sp.add_argument("--backend-config", help="JSON file for the backend")
    sp.add_argument("--rubric")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--min-played", type=int, default=MIN_PLAYED_FOR_A_POSITION)
    sp.add_argument("--branch-prefix", default=DEFAULT_BRANCH_PREFIX)
    sp.add_argument("--allow-unresolved", action="store_true")

    sp = sub.add_parser("log", help="the dispatch ledger for a domain")
    sp.add_argument("--domain")

    args = p.parse_args(argv)
    try:
        if args.cmd == "queue":
            _print([
                e.to_dict() for e in queue(
                    args.domain, rubric=args.rubric, limit=args.limit,
                    min_played=args.min_played,
                )
            ])
        elif args.cmd == "run":
            config = {}
            if args.backend_config:
                with open(args.backend_config) as fh:
                    config = json.load(fh)
            records = dispatch_domain(
                args.domain,
                repo_path=args.repo,
                base_ref=args.base_ref,
                backend=args.backend,
                backend_config=config,
                rubric=args.rubric,
                limit=args.limit,
                min_played=args.min_played,
                branch_prefix=args.branch_prefix,
                allow_unresolved=args.allow_unresolved,
            )
            _print([r.to_dict() for r in records])
            code = run_exit_code(records)
            if code:
                for record in records:
                    if record.outcome == OUTCOME_FAILED:
                        print(f"failed: rank {record.rank} {record.title}: "
                              f"{record.detail}", file=sys.stderr)
                print("error: nothing was authored and every attempt failed",
                      file=sys.stderr)
            return code
        elif args.cmd == "log":
            _print(dispatched(args.domain))
    except (ValueError, LookupError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
