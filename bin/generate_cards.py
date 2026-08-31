"""Generate cards from a domain's corpus and enqueue pairs for the judge wheel.

CLI::

    nix run .#generate-cards -- --domain memory-extraction --limit 50
    nix run .#generate-cards -- --domain memory-extraction --advance-round

Output is line-oriented for Phoenix Port-tail consumption.

--advance-round draws the next Swiss round and stops at the campaign's round
cap (--rounds, default ceil(log2 N)) instead of drawing a round nobody can
judge: past the cap every pairing is a rematch. It exits 0 on a finished pool,
3 on one that ran out of comparisons, and 4 on a draw that seated no match --
three states an operator polling the CLI has to be able to tell apart.

Every round decision -- which round is open, what the standings are, whether
anything is still outstanding, and the write itself -- runs inside one
BEGIN IMMEDIATE transaction, so two operators advancing the same domain at the
same moment cannot both draw the same round from the same standings.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import random
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Iterable, Iterator, Optional

import dspy

from bin import domains as _domains
from bin import llm_config as _llm_config
from bin import swiss
from bin.corpus import iter_filesystem_paths, split_globs
from bin.env_loader import load_dotenv as _load_dotenv
from bin.generators.card_gen import CardGen, CardGenError
from bin.generators.workorder_gen import WorkOrderGen
from bin.workorder import capture_repo_snapshot, finalize_work_order, to_markdown

_load_dotenv()

def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"

_ROUND_LOCK_WAIT_SECONDS = 30.0

THE_WHOLE_ROUND_DECISION_IS_ONE_TRANSACTION_OR_TWO_OPERATORS_DRAW_THE_SAME_ROUND = (
    "reading which round is open, loading the pool, counting what is still "
    "outstanding and writing the next round are four steps that MUST see one "
    "database. Read unlocked they are advisory: two operators both find round "
    "N judged out and both draw round N+1 from the same standings, and a "
    "person is handed every comparison twice. The last verdict of the open "
    "round landing between the pool read and the outstanding count is the "
    "same bug wearing one operator: the guard passes and the round is drawn "
    "from standings that are already stale. BEGIN IMMEDIATE takes the write "
    "lock on the first statement of the decision and holds it through the "
    "write."
)

_ROUND_STORAGE_DDL = (
    "CREATE TABLE IF NOT EXISTS domain_campaign ("
    "  domain_id  INTEGER PRIMARY KEY,"
    "  rounds_cap INTEGER,"
    "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_one_row_per_domain_match_id "
    "ON pending_judgement(domain_id, match_id)",
)

A_DOMAIN_MATCH_ID_IS_ALLOCATED_UNDER_THE_WRITE_LOCK_AND_BACKED_BY_A_UNIQUE_INDEX = (
    "match_id is MAX+1 over the domain's own rows, which two draws reading at "
    "once both resolve to the same number. The allocation runs inside the "
    "round decision's BEGIN IMMEDIATE, and idx_pending_one_row_per_domain_"
    "match_id is the catch-net that turns a collision into a failed insert "
    "instead of two queue rows wearing one match id. Rows with no domain_id "
    "(the tournament path, one row per active config) are untouched: SQLite "
    "treats their NULLs as distinct."
)

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=_ROUND_LOCK_WAIT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn

def _duplicate_domain_match_ids(conn: sqlite3.Connection) -> list[tuple]:
    return [
        (row["domain_id"], row["match_id"], row["rows"])
        for row in conn.execute(
            "SELECT domain_id, match_id, COUNT(*) AS rows FROM pending_judgement "
            "WHERE domain_id IS NOT NULL GROUP BY domain_id, match_id "
            "HAVING COUNT(*) > 1 ORDER BY domain_id, match_id"
        ).fetchall()
    ]

def _ensure_round_storage(conn: sqlite3.Connection) -> None:
    """Create the campaign dial's table and the match id uniqueness index.

    Both are idempotent. The index refuses to be created on a database that
    already holds a collision -- exactly what the unlocked allocation this
    replaces could write -- so that case is reported with the colliding rows
    named instead of surfacing as a bare SQLite error.
    """
    for ddl in _ROUND_STORAGE_DDL:
        try:
            conn.execute(ddl)
        except sqlite3.IntegrityError:
            collisions = ", ".join(
                f"domain {d} match_id {m} ({n} rows)"
                for d, m, n in _duplicate_domain_match_ids(conn)
            )
            raise RuntimeError(
                "this queue already holds pending rows sharing one domain "
                f"match id, so it cannot be made unique: {collisions}. They "
                "were written by two draws racing; cancel the duplicates "
                "before advancing. "
                f"{A_DOMAIN_MATCH_ID_IS_ALLOCATED_UNDER_THE_WRITE_LOCK_AND_BACKED_BY_A_UNIQUE_INDEX}"
            ) from None

@contextmanager
def _round_decision(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Hold the write lock across one whole read-check-write round decision."""
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_round_storage(conn)
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

def _require_round_decision(conn: sqlite3.Connection) -> None:
    assert conn.in_transaction, (
        THE_WHOLE_ROUND_DECISION_IS_ONE_TRANSACTION_OR_TWO_OPERATORS_DRAW_THE_SAME_ROUND
    )

def _next_match_id(conn: sqlite3.Connection, domain_id: int) -> int:
    _require_round_decision(conn)
    return int(conn.execute(
        "SELECT COALESCE(MAX(match_id), -1) + 1 FROM pending_judgement "
        "WHERE domain_id=?",
        (domain_id,),
    ).fetchone()[0])

def _payload_of(raw) -> Optional[dict]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

CardGen = CardGen  # noqa: F811
WorkOrderGen = WorkOrderGen  # noqa: F811

@dataclass
class GenerateResult:
    cards_generated: int
    pairs_enqueued: int
    errors: int
    singles_enqueued: int = 0
    failures: dict = field(default_factory=dict)
    aborted_reason: str = ""

_SYSTEMIC_ERROR_MARKERS = (
    "AuthenticationError",
    "All connection attempts failed",
    "Cannot connect to host",
    "Connection refused",
)
_SYSTEMIC_ABORT_AFTER = 3

def _build_generation_lm() -> dspy.LM:
    """Build a bounded LM for interactive corpus fan-out.

    Generation runs once per corpus item, so the optimizer's deliberately
    generous retry/output defaults can make one bad request look like a hung
    Domains job. Keep this role short and fail fast; the item loop already
    records an error and continues with the remaining corpus.
    """
    from bin.optimize import _build_role_lm

    cfg = _llm_config.generator_config()
    return _build_role_lm(
        cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout,
        num_retries=cfg.num_retries,
    )

def _iter_corpus(spec: _domains.DomainSpec) -> Iterator[dict]:
    """Yield {text, source_ref} dicts from the domain's corpus_source."""
    src = spec.corpus_source
    kind = src["kind"]
    if kind == "inline":
        for i, item in enumerate(src.get("items", [])):
            text = item.get("text") or item.get("body") or json.dumps(item)
            ref = item.get("source_ref") or item.get("ref") or f"inline:{i}"
            yield {"text": text, "source_ref": ref}
    elif kind == "filesystem":
        for path in iter_filesystem_paths(src):
            try:
                yield {"text": path.read_text(encoding="utf-8"), "source_ref": str(path)}
            except (OSError, UnicodeError):
                continue
    elif kind == "sqlite":
        conn = sqlite3.connect(f"file:{src['path']}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(src["query"]):
                d = {k: row[k] for k in row.keys()}
                text = d.get("text") or d.get("content") or d.get("body") or ""
                ref = d.get("source_ref") or d.get("ref") or str(d.get("id") or "?")
                yield {"text": text, "source_ref": ref}
        finally:
            conn.close()
    else:  # pragma: no cover — _validate_corpus_source guards this
        raise ValueError(f"unknown corpus_source kind: {kind!r}")

def _enqueue_pairs(domain_id: int, items: list[dict], rng: random.Random,
                   rounds: Optional[int] = None) -> int:
    """Draw one round over freshly generated artifacts and write its pending
    rows with domain-unique match ids.

    ``rounds`` sets the campaign's round cap, recorded against the domain in
    ``domain_campaign``, so a campaign that wants a coarser ordering says so
    once and every later :func:`advance_round` honours it without the flag
    being re-passed. Left unset, the cap stays ``swiss.rounds_total`` and
    grows with late arrivals.

    ``items`` are payload dicts (a card's ``model_dump()`` or a work order's
    display payload). The draw is the Swiss engine's round one: a seeded
    shuffle with no standing input, neighbours paired, the odd item out taking
    the bye. Repeated generation runs continue after the domain's highest
    match id and join the round already open, entering the pool at zero
    points; every LATER round comes from :func:`advance_round`, which draws
    from standings instead.

    Domain pairs are reviewed by HUMANS (the review bar — user-devs stand at
    the end of the loop), so exactly ONE pending row is written per pair,
    against a single human-rater config. The pre-fix behaviour inserted one
    row per ACTIVE config (3-model LLM panel + human = 4 identical rows per
    pair) whenever the domain's rubric matched no eval_template name — the
    LLM rows were never drained by any domain flow and inflated the queue
    (wave-9 L6).
    """
    conn = _connect()
    try:
        with _round_decision(conn):
            cfg = _human_config_for_rubric(conn, domain_id)
            pool = swiss.new_pool(
                (swiss.item_from_payload(item) for item in items),
                rubric_id=cfg["name"],
                rubric_version=int(cfg["template_version"]),
                seed=rng.randrange(2 ** 31),
            )
            _resolve_round_dial(conn, domain_id, pool, rounds)
            number = max(1, _current_round(conn, domain_id))
            return _write_round(conn, cfg, domain_id, pool,
                                swiss.pair_round(pool, 1), number)
    finally:
        conn.close()

def _round_of(payload: dict) -> int:
    """The round a pending row belongs to. Rows written before rounds were
    modelled carry only their ``R<n>-<slot>`` label."""
    number = payload.get("round")
    if isinstance(number, int):
        return number
    label = str(payload.get("label") or "")
    if label.startswith("R") and "-" in label:
        head = label[1:label.index("-")]
        if head.isdigit():
            return int(head)
    return 1

def _current_round(conn: sqlite3.Connection, domain_id: int) -> int:
    """Highest round enqueued for this domain, or 0 when none is."""
    rounds = [
        _round_of(payload)
        for payload in (
            _payload_of(row["trace_payload"])
            for row in conn.execute(
                "SELECT trace_payload FROM pending_judgement WHERE domain_id=?",
                (domain_id,),
            ).fetchall()
        )
        if payload is not None
    ]
    return max(rounds, default=0)

def _write_round(conn: sqlite3.Connection, cfg: sqlite3.Row, domain_id: int,
                 pool: "swiss.Pool", drawn: "swiss.Round", number: int) -> int:
    """Persist one drawn round as pending rows; return the pairs enqueued.

    The payload keeps ``card_a``/``card_b`` for the judge surface and adds the
    pairing bookkeeping the next round needs: the round number, the pair key,
    both item ids, the rubric it was drawn under, and the round's bye. The bye
    rides along because no pending row shows it and the pool would otherwise
    lose the item between rounds. Points are deliberately absent — the judging
    surface never sees a standing, and neither the campaign's round cap nor
    any other piece of tournament state rides in what a person is shown.

    Runs inside the caller's round decision: the match ids it allocates are
    only unique while that write lock is held.
    """
    _require_round_decision(conn)
    slot = _next_match_id(conn, domain_id)
    byes = [
        {"item_id": item_id, "card": pool.items[item_id].payload}
        for item_id in drawn.byes
    ]
    enqueued = 0
    for match in drawn.matches:
        body = {
            "label": f"R{number}-{slot + 1}",
            "round": number,
            "pair_key": match.pair_key,
            "rubric": pool.rubric_id,
            "rubric_version": pool.rubric_version,
            "item_a": match.item_a,
            "item_b": match.item_b,
            "card_a": pool.items[match.item_a].payload,
            "card_b": pool.items[match.item_b].payload,
            "byes": byes,
        }
        conn.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, domain_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (cfg["id"], f"domain:{domain_id}", slot, json.dumps(body), domain_id),
        )
        enqueued += 1
        slot += 1
    return enqueued

def _verdict_for_pending(conn: sqlite3.Connection, row: sqlite3.Row):
    """The effective verdict of a resolved pending row, with the rubric it was
    judged under: ``(verdict, rubric_name, rubric_version)`` or None."""
    if row["status"] != "done" or not row["rating_id"]:
        return None
    score = conn.execute(
        "SELECT s.value AS value, s.rubric_version AS rubric_version, "
        "       t.name AS rubric_name FROM score s "
        "JOIN eval_template t ON t.id = s.template_id "
        "WHERE s.pending_id=? AND s.rating_id=? AND s.name LIKE 'judgement.%verdict' "
        "ORDER BY s.id LIMIT 1",
        (row["id"], row["rating_id"]),
    ).fetchone()
    if score is None:
        return None
    return score["value"], score["rubric_name"], int(score["rubric_version"])

def _load_pool(conn: sqlite3.Connection, domain_id: int, cfg: sqlite3.Row):
    """Rebuild the Swiss pool for a domain from its pending rows and scores.

    The pending queue IS the store: items come from the payloads, results from
    the score rows joined through each row's effective rating, byes from the
    ``byes`` the draw recorded. Verdicts from another rubric's vocabulary are
    replayed as skips — inert, never fatal — and verdicts judged under a
    superseded rubric version go stale on their own, because the pair key they
    carry no longer matches the pool's.
    """
    pool = swiss.new_pool([], rubric_id=cfg["name"],
                          rubric_version=int(cfg["template_version"]))
    rows = conn.execute(
        "SELECT id, match_id, trace_payload, status, rating_id "
        "FROM pending_judgement WHERE domain_id=? ORDER BY match_id",
        (domain_id,),
    ).fetchall()
    decided = []
    for row in rows:
        payload = _payload_of(row["trace_payload"])
        if payload is None or "card_a" not in payload:
            continue
        number = _round_of(payload)
        item_a = _register(pool, payload.get("item_a"), payload["card_a"])
        item_b = _register(pool, payload.get("item_b"), payload.get("card_b"))
        for bye in payload.get("byes") or []:
            bye_id = _register(pool, bye.get("item_id"), bye.get("card"))
            if bye_id is not None and (number, bye_id) not in swiss.byes(pool):
                swiss.no_result(pool, round=number, item_id=bye_id,
                                cause=swiss.NO_RESULT_CAUSE_BYE)
        if item_a is None or item_b is None:
            continue
        verdict = _verdict_for_pending(conn, row)
        if verdict is not None:
            decided.append((number, item_a, item_b, verdict))
    for number, item_a, item_b, (value, rubric_name, rubric_version) in decided:
        swiss.record(pool, round=number, item_a=item_a, item_b=item_b,
                     verdict=value, rubric_id=rubric_name,
                     rubric_version=rubric_version,
                     default_outcome=swiss.OUTCOME_SKIP)
    return pool

def _register(pool: "swiss.Pool", item_id, card) -> Optional[str]:
    if not isinstance(card, dict):
        return None
    item = swiss.item_from_payload(card)
    if isinstance(item_id, str) and item_id:
        item = swiss.Item(id=item_id, content=item.content, payload=card)
    swiss.add_item(pool, item)
    return item.id

ROUND_DRAWN = "drawn"
ROUND_COMPLETE = "complete"
ROUND_EXHAUSTED = "exhausted"
ROUND_STUCK = "stuck"

TERMINAL_ROUND_STATUSES = frozenset(
    {ROUND_COMPLETE, ROUND_EXHAUSTED, ROUND_STUCK}
)

ROUND_STATUS_SETTLED = {
    ROUND_DRAWN: False,
    ROUND_COMPLETE: True,
    ROUND_EXHAUSTED: True,
    ROUND_STUCK: False,
}

ROUND_STATUS_EXIT_CODE = {
    ROUND_DRAWN: 0,
    ROUND_COMPLETE: 0,
    ROUND_EXHAUSTED: 3,
    ROUND_STUCK: 4,
}

A_STUCK_DRAW_IS_NOT_A_SETTLED_POOL_AND_DOES_NOT_SHARE_AN_EXIT_CODE_WITH_A_DRY_ONE = (
    "the three terminal statuses used to share one dict that hardcoded "
    "settled=True and one exit code of 3. 'stuck' means the draw seated no "
    "match while unjudged pairs remain -- a pairing failure that wants a "
    "person, and the opposite of settled -- while 'exhausted' means every "
    "legal comparison has been made and the standings are final. Reporting "
    "them with the same two values makes a benign end and a broken one "
    "indistinguishable to whoever is polling."
)
assert ROUND_STATUS_SETTLED[ROUND_STUCK] is False, (
    A_STUCK_DRAW_IS_NOT_A_SETTLED_POOL_AND_DOES_NOT_SHARE_AN_EXIT_CODE_WITH_A_DRY_ONE
)
assert ROUND_STATUS_EXIT_CODE[ROUND_EXHAUSTED] != ROUND_STATUS_EXIT_CODE[ROUND_STUCK], (
    A_STUCK_DRAW_IS_NOT_A_SETTLED_POOL_AND_DOES_NOT_SHARE_AN_EXIT_CODE_WITH_A_DRY_ONE
)
assert set(ROUND_STATUS_SETTLED) == set(ROUND_STATUS_EXIT_CODE) == (
    TERMINAL_ROUND_STATUSES | {ROUND_DRAWN}
), "every round status must declare whether it is settled and how it exits"

THE_ROUND_CAP_IS_CAMPAIGN_STATE_AND_NEVER_RIDES_IN_WHAT_A_PERSON_IS_SHOWN = (
    "the cap used to be smuggled into every match's judge display payload and "
    "recovered by scanning those payloads newest-first. That payload is the "
    "judging surface -- tournament state does not belong in it, and a dial "
    "with no home was only as sticky as the last row a draw happened to "
    "write. It lives in domain_campaign now, one row per domain, written the "
    "moment --rounds is passed."
)

PAYLOAD_SMUGGLED_ROUND_CAPS_ARE_ABANDONED_NOT_MIGRATED = (
    "a campaign whose cap only ever existed inside its judge payloads is back "
    "on the default cap. Nothing is read out of those payloads and nothing is "
    "backfilled from them; the operator is told once, by domain, and passes "
    "--rounds again to set the dial in its real home."
)

def _campaign_round_cap(conn: sqlite3.Connection, domain_id: int) -> Optional[int]:
    """This campaign's round cap, or None for the default.

    None means ``swiss.rounds_total`` and is NEVER fed back through
    :func:`swiss.rounds_cap` as an override: the default of a pool with fewer
    than two entrants is zero, and zero is not a legal override.
    """
    row = conn.execute(
        "SELECT rounds_cap FROM domain_campaign WHERE domain_id=?",
        (domain_id,),
    ).fetchone()
    if row is None:
        _abandon_payload_smuggled_round_cap(conn, domain_id)
        return None
    cap = row["rounds_cap"]
    return None if cap is None else int(cap)

def _set_campaign_round_cap(conn: sqlite3.Connection, domain_id: int,
                            cap: int) -> None:
    _require_round_decision(conn)
    conn.execute(
        "INSERT INTO domain_campaign(domain_id, rounds_cap) VALUES (?, ?) "
        "ON CONFLICT(domain_id) DO UPDATE SET rounds_cap=excluded.rounds_cap, "
        "updated_at=datetime('now')",
        (domain_id, cap),
    )

def _resolve_round_dial(conn: sqlite3.Connection, domain_id: int,
                        pool: "swiss.Pool",
                        rounds: Optional[int]) -> Optional[int]:
    """The cap override in force for this call, persisting an explicit one.

    An explicit ``--rounds`` is validated before it is written and recorded
    the moment it is passed — including on the call that finds the campaign
    already at that cap, which draws no round and would otherwise leave the
    dial nowhere for the next flagless poll to find.
    """
    if rounds is None:
        return _campaign_round_cap(conn, domain_id)
    dial = swiss.rounds_cap(pool, rounds)
    _set_campaign_round_cap(conn, domain_id, dial)
    return dial

def _abandon_payload_smuggled_round_cap(conn: sqlite3.Connection,
                                        domain_id: int) -> None:
    """Announce, once, that a pre-``domain_campaign`` cap is not honoured."""
    legacy = None
    for row in conn.execute(
        "SELECT trace_payload FROM pending_judgement WHERE domain_id=? "
        "ORDER BY match_id DESC",
        (domain_id,),
    ).fetchall():
        payload = _payload_of(row["trace_payload"])
        cap = None if payload is None else payload.get("rounds_cap")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
            legacy = cap
            break
    if legacy is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO domain_campaign(domain_id, rounds_cap) "
        "VALUES (?, NULL)",
        (domain_id,),
    )
    print(
        f"[advance] ABANDONED ROUND CAP: domain {domain_id} carried "
        f"rounds_cap={legacy} inside its judge display payloads. "
        f"{THE_ROUND_CAP_IS_CAMPAIGN_STATE_AND_NEVER_RIDES_IN_WHAT_A_PERSON_IS_SHOWN} "
        f"{PAYLOAD_SMUGGLED_ROUND_CAPS_ARE_ABANDONED_NOT_MIGRATED} "
        f"Pass --rounds {legacy} once to keep that ordering.",
        flush=True, file=sys.stderr,
    )

A_DISCARDED_ITEM_IS_WITHDRAWN_ON_THE_DECISION_CONNECTION_OR_IT_DEADLOCKS = (
    "swiss.cancel_pending opens its own connection, which would block against "
    "the write lock the round decision is holding. Withdrawing a discarded "
    "item's outstanding rows is part of the decision -- the outstanding count "
    "that gates the draw has to see it -- so it runs on the decision's own "
    "connection."
)

def _payload_item_ids(payload: dict) -> set[str]:
    """The items a queue row SHOWS. A round's bye list rides in every match
    payload of that round and names nobody the row is asking about, so it is
    deliberately not read here."""
    ids: set[str] = set()
    for key in ("item_a", "item_b", "item"):
        value = payload.get(key)
        if isinstance(value, str):
            ids.add(value)
    for key in ("card_a", "card_b", "card"):
        card = payload.get(key)
        if isinstance(card, dict):
            ids.add(swiss.item_from_payload(card).id)
    return ids

def _cancel_pending_showing(conn: sqlite3.Connection, domain_id: int,
                            item_ids: Iterable[str], *, reason: str) -> int:
    """Cancel this domain's outstanding rows that show any of these items."""
    wanted = set(item_ids)
    if not wanted:
        return 0
    _require_round_decision(conn)
    cancelled = 0
    for row in conn.execute(
        "SELECT id, trace_payload FROM pending_judgement "
        "WHERE domain_id=? AND status='pending'",
        (domain_id,),
    ).fetchall():
        payload = _payload_of(row["trace_payload"])
        if payload is None or not (_payload_item_ids(payload) & wanted):
            continue
        conn.execute(
            "UPDATE pending_judgement SET status='cancelled', error_message=?, "
            "completed_at=datetime('now') WHERE id=? AND status='pending'",
            (reason, row["id"]),
        )
        cancelled += 1
    return cancelled

def _round_report(status: str, reason: str, *, pool: "swiss.Pool",
                  dial: Optional[int], rounds_played: int,
                  round_drawn: Optional[int] = None, pairs_enqueued: int = 0,
                  byes: Iterable[str] = (),
                  discarded: Iterable[str] = ()) -> dict:
    """The ONE shape :func:`advance_round` answers in, drawn or terminal.

    ``round_drawn`` is the round this call put on the queue and is None when
    it put none there — the old shape used ``round`` for the round drawn on
    one path and the last round played on the other, which read the same and
    meant different things. ``rounds_played`` is always the rounds already
    judged, and ``last_round`` always says whether the cap allows another
    after the round this report is about.
    """
    reached = rounds_played if round_drawn is None else round_drawn
    return {
        "status": status,
        "reason": reason,
        "rounds_played": rounds_played,
        "round_drawn": round_drawn,
        "rounds_cap": swiss.rounds_cap(pool, dial),
        "rounds_full": swiss.rounds_total(pool),
        "pairs_enqueued": pairs_enqueued,
        "byes": list(byes),
        "discarded": list(discarded),
        "settled": ROUND_STATUS_SETTLED[status],
        "last_round": swiss.is_settled(pool, reached, override=dial),
    }

def advance_round(domain_id: int, *, rubric: Optional[str] = None,
                  rounds: Optional[int] = None) -> dict:
    """Draw the next round for a domain from the standings so far.

    Every read this decision makes and the write it ends in run inside one
    ``BEGIN IMMEDIATE`` transaction, so a second operator advancing the same
    domain waits and then finds the round already drawn rather than drawing it
    again from the same standings.

    Answers in one shape whatever happens — see :func:`_round_report` — with a
    ``status``. ``drawn`` is a new round on the queue; the three terminal
    statuses say why there is not one, and they are the difference between a
    tournament that finished and a tournament that is stuck:

    - ``complete`` — every round the campaign asked for has been played. The
      ordering is settled at that resolution.
    - ``exhausted`` — under the cap, but every legal comparison has already
      been made. The pool cannot be resolved further without a rubric bump or
      new items.
    - ``stuck`` — under the cap with unjudged pairs left, yet the draw seated
      no match. That is a pairing failure and wants a person.

    Past the cap the round is NOT drawn: every pairing would be a rematch, the
    no-rematch rule would strand the whole pool as byes, and an all-bye round
    on the queue is indistinguishable from a jammed tournament. ``rounds``
    overrides the cap for this campaign — stopping short costs resolution, not
    validity, so a shorter cap is a setting and never an error.

    Refuses loudly while the open round still has judgements outstanding: the
    next draw depends on how the current one lands, and pairing early would
    hand the judge comparisons the standings do not yet justify. Discarded
    items are gone from the draw and their outstanding queue rows go with
    them; stale matches (judged under a superseded rubric version) neither
    score nor block a rematch.
    """
    conn = _connect()
    try:
        with _round_decision(conn):
            cfg = _human_config_for_rubric(conn, domain_id, rubric)
            number = _current_round(conn, domain_id)
            if number < 1:
                raise RuntimeError(
                    f"domain {domain_id} has no enqueued round to advance "
                    "from — generate artifacts first"
                )
            pool = _load_pool(conn, domain_id, cfg)
            dropped = [d.item_id for d in swiss.discards(pool)]
            _cancel_pending_showing(conn, domain_id, dropped,
                                    reason="discarded from pool")
            outstanding = conn.execute(
                "SELECT COUNT(*) FROM pending_judgement "
                "WHERE domain_id=? AND status='pending'",
                (domain_id,),
            ).fetchone()[0]
            if outstanding:
                raise RuntimeError(
                    f"refusing to advance domain {domain_id} past round "
                    f"{number}: {outstanding} judgement(s) still pending — the "
                    "next round is drawn from standings, which are not settled "
                    "until this round is"
                )
            dial = _resolve_round_dial(conn, domain_id, pool, rounds)
            cap = swiss.rounds_cap(pool, dial)
            full = swiss.rounds_total(pool)
            report = partial(_round_report, pool=pool, dial=dial,
                             rounds_played=number, discarded=dropped)
            if swiss.is_settled(pool, number, override=dial):
                if full == 0:
                    return report(ROUND_COMPLETE, (
                        f"domain {domain_id} has fewer than two entrants: "
                        "there is nothing to compare and no ordering to "
                        "resolve, so no round is drawn"
                    ))
                resolution = (
                    "a full ordering" if cap >= full
                    else f"a coarser ordering than the {full} rounds a full "
                         "one takes"
                )
                return report(ROUND_COMPLETE, (
                    f"domain {domain_id} finished: {number} of {cap} round(s) "
                    f"played and judged, standings are settled — {resolution}"
                ))
            remaining = swiss.unplayed_pairs(pool)
            if not remaining:
                return report(ROUND_EXHAUSTED, (
                    f"domain {domain_id} is out of comparisons at round "
                    f"{number} of {cap}: all {len(swiss.active_ids(pool))} "
                    "surviving item(s) have met each other, so the cap of "
                    f"{cap} cannot be reached (this pool seats at most "
                    f"{swiss.max_rounds(pool)} round(s)) — standings are "
                    "final, not stalled"
                ))
            drawn = swiss.pair_round(pool, number + 1)
            if not drawn.matches:
                return report(ROUND_STUCK, (
                    f"domain {domain_id} drew no match for round {number + 1} "
                    f"though {len(remaining)} unjudged pair(s) remain: the "
                    "whole pool would sit the round out as byes, which is a "
                    "pairing failure and not a finished tournament"
                ))
            enqueued = _write_round(conn, cfg, domain_id, pool, drawn,
                                    number + 1)
            return report(
                ROUND_DRAWN,
                (f"domain {domain_id} drew round {number + 1} of {cap}: "
                 f"{enqueued} pair(s) and {len(drawn.byes)} bye(s) on the "
                 "queue"),
                round_drawn=number + 1, pairs_enqueued=enqueued,
                byes=drawn.byes,
            )
    finally:
        conn.close()

def _human_config_for_rubric(conn: sqlite3.Connection, domain_id: int,
                             rubric: Optional[str] = None) -> sqlite3.Row:
    """Resolve the ONE human job_configuration a domain's pendings bind to.

    Prefer a human config on the HIGHEST version of the template that matches
    the rubric (explicit override or the domain's own); otherwise the first
    active human config. A rubric revision therefore takes effect on the next
    draw, and the matches judged under the version it replaced go stale.
    Exactly ONE config — one pending row per pair/artifact (the wave-9 L6
    invariant: domain judgements are reviewed by HUMANS, never fanned out
    across the LLM panel).
    """
    cfgs = conn.execute(
        "SELECT c.id AS id, c.rater_type AS rater_type, t.name AS name, "
        "       t.version AS template_version, t.id AS template_id "
        "FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE c.status='active' AND c.rater_type='human'"
    ).fetchall()
    if not cfgs:
        raise RuntimeError(
            "no active human job_configuration exists — run judgement init "
            "before generating domain pairs"
        )
    if rubric is None:
        spec = conn.execute(
            "SELECT rubric FROM domain WHERE id=?", (domain_id,)
        ).fetchone()
        rubric = spec["rubric"] if spec else None
    matched = sorted(
        (c for c in cfgs if rubric is not None and c["name"] == rubric),
        key=lambda c: int(c["template_version"]),
        reverse=True,
    )
    return (matched or cfgs)[0]

def enqueue_singles(domain_id: int, items: list[dict],
                    rubric: Optional[str] = None) -> int:
    """Write ONE pending row per generated artifact for single judgement.

    Mirrors ``_enqueue_pairs`` but never pretends to be a pair: the
    trace_payload is ``{"label": ..., "card": {...}}`` (no card_a/card_b —
    a single judgement is NEVER represented by duplicating one artifact
    into both pair slots). match_id stays domain-unique so repeated runs
    never collide, and every artifact is enqueued (no bye). The L6
    invariant holds: exactly ONE pending row per artifact, bound to a
    single human config (``rubric`` overrides the domain's own when the
    caller passed --rubric). The allocation runs under the same write lock a
    round draw takes, so two runs cannot hand one match id to two artifacts.
    """
    conn = _connect()
    enqueued = 0
    try:
        with _round_decision(conn):
            cfg = _human_config_for_rubric(conn, domain_id, rubric)
            slot = _next_match_id(conn, domain_id)
            for item in items:
                payload = json.dumps({
                    "label": f"S1-{slot + 1}",
                    "card": item,
                })
                conn.execute(
                    "INSERT INTO pending_judgement(config_id, "
                    "tournament_db_path, match_id, trace_payload, domain_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cfg["id"], f"domain:{domain_id}", slot, payload, domain_id),
                )
                enqueued += 1
                slot += 1
    finally:
        conn.close()
    return enqueued

def _resolve_judgement_kind(spec, judgement_kind: Optional[str],
                            rubric: Optional[str]) -> str:
    """Pick 'pair' or 'single' for this run.

    Explicit --judgement-kind wins. Otherwise consult the (normalized)
    output_definition of the explicit --rubric, or the domain's active
    template: a single-kind template selects the singles path. Missing
    templates fall back to 'pair' (legacy behavior).
    """
    if judgement_kind is not None:
        if judgement_kind not in ("pair", "single"):
            raise ValueError(f"unknown judgement kind: {judgement_kind!r}")
        return judgement_kind
    from bin.judgement import normalize_output_definition
    conn = sqlite3.connect(str(_db_path()))
    try:
        row = conn.execute(
            "SELECT output_definition FROM eval_template "
            "WHERE name=? AND is_draft=0 ORDER BY version DESC LIMIT 1",
            (rubric or spec.rubric,),
        ).fetchone()
    except sqlite3.OperationalError:
        return "pair"
    finally:
        conn.close()
    if row is None:
        return "pair"
    return normalize_output_definition(json.loads(row[0]))["judgement_kind"]

def _is_systemic(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}"
    return any(marker in text for marker in _SYSTEMIC_ERROR_MARKERS)

def _work_order_payload(wo, cited_evidence: Optional[list[str]] = None) -> dict:
    """Display/judging payload for a finalized WorkOrder.

    ``title``/``body``/``source_ref`` keep the legacy card shape so the
    existing judge UI and LLM-judge prompt path render work orders without
    changes (body carries the canonical markdown). The full structured
    object rides alongside for richer consumers. ``cited_evidence`` is a
    list of catalog EvidenceRef digests stamped by the PIPELINE (never the
    model) — the judge view resolves them into tier-badged citation chips.
    """
    payload = {
        "kind": "work-order",
        "title": wo.title,
        "body": to_markdown(wo),
        "source_ref": wo.source_ref,
        "work_order": wo.model_dump(),
    }
    if cited_evidence:
        payload["cited_evidence"] = list(cited_evidence)
    return payload

class _EvidenceStamper:
    """Pipeline-side citation stamping for work-order generation.

    For git-backed filesystem corpora: each corpus item becomes a
    TIER1_SYSTEM EvidenceRef pinned to the captured base commit
    (git_local.file_refs), persisted through bin.catalog into the shared
    fabric DB under project ``catalog_project`` (corpus_source override,
    default: the domain name) / source ``corpus``. The returned digests are
    stamped into the payload's ``cited_evidence`` — the model NEVER supplies
    them (WorkOrderDraft has no such field by design).

    Fail-open by design: citation stamping must never break generation.
    Any failure (non-git corpus, catalog unavailable) disables the stamper
    for the rest of the run and logs once.
    """

    def __init__(self, spec, snap):
        self._spec = spec
        self._snap = snap
        self._enabled = snap is not None
        self._source_id: Optional[int] = None
        self._warned = False

    def _disable(self, why: str) -> None:
        if not self._warned:
            print(f"[generate] evidence stamping off: {why}", flush=True)
            self._warned = True
        self._enabled = False

    def _ensure_source(self) -> Optional[int]:
        if self._source_id is not None:
            return self._source_id
        from bin import catalog as _catalog

        project = self._spec.corpus_source.get("catalog_project") or self._spec.name
        _catalog.init()
        try:
            _catalog.get_project(project)
        except LookupError:
            _catalog.create_project(
                name=project,
                description=f"auto-created by generation for domain {self._spec.name}",
            )
        try:
            src = _catalog.get_source(project, "corpus")
        except LookupError:
            _catalog.create_source(
                project=project,
                name="corpus",
                kind="git",
                locator=self._snap.root,
                trust_tier=1,
            )
            src = _catalog.get_source(project, "corpus")
        self._source_id = int(src["id"])
        return self._source_id

    def cite(self, item_path: str) -> list[str]:
        """Return EvidenceRef digests for one corpus item (or [])."""
        if not self._enabled:
            return []
        try:
            from bin import catalog as _catalog
            from bin.landscape.adapters import git_local

            root = Path(self._snap.root).resolve()
            rel = str(Path(item_path).resolve().relative_to(root))
            refs = git_local.file_refs(
                str(root),
                [rel],
                why=f"work-order source for domain {self._spec.name}",
                commit=self._snap.base_commit,
            )
            source_id = self._ensure_source()
            return [
                _catalog.insert_evidence_ref(ref, source_id=source_id)
                for ref in refs
            ]
        except Exception as exc:
            self._disable(f"{type(exc).__name__}: {exc}")
            return []

def run(domain_name: str, *, limit: Optional[int] = None, seed: int = 0,
        artifact: Optional[str] = None, judgement_kind: Optional[str] = None,
        rubric: Optional[str] = None,
        rounds: Optional[int] = None) -> GenerateResult:
    spec = _domains.get_domain(domain_name)
    if limit is None:
        limit = _llm_config.generator_max_items()
    if artifact is None:
        artifact = spec.corpus_source.get("artifact", "card")
    if artifact not in ("card", "work-order"):
        raise ValueError(f"unknown artifact kind: {artifact!r}")
    kind = _resolve_judgement_kind(spec, judgement_kind, rubric)
    cfg = _llm_config.generator_config()
    print(
        f"[generate] domain={spec.name} corpus_kind={spec.corpus_source['kind']} "
        f"artifact={artifact} judgement_kind={kind} item_budget={limit}",
        flush=True,
    )
    print(
        f"[generate] config max_tokens={cfg.max_tokens} "
        f"timeout={cfg.timeout:g}s retries={cfg.num_retries}",
        flush=True,
    )

    if getattr(dspy.settings, "lm", None) is None:
        dspy.settings.configure(lm=_build_generation_lm())

    rng = random.Random(seed or hash(spec.name) & 0xFFFFFFFF)

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    active_lm_model = getattr(getattr(dspy.settings, "lm", None), "model", None)
    run_models = [m for m in [active_lm_model or cfg.model or ""] if m]
    repos = []
    if artifact == "work-order" and spec.corpus_source.get("kind") == "filesystem":
        snap = capture_repo_snapshot(spec.corpus_source.get("root", ""))
        if snap is not None:
            repos = [snap]
            dirty = " (dirty working tree)" if snap.dirty else ""
            print(
                f"[generate] repo {snap.remote or snap.root} "
                f"@ {snap.base_commit[:12]}{dirty}",
                flush=True,
            )

    if artifact == "work-order":
        gen = WorkOrderGen(prompt_name=spec.generator_prompt)
        output_field = "work_orders"
        stamper = _EvidenceStamper(spec, repos[0] if repos else None)
    else:
        gen = CardGen(prompt_name=spec.generator_prompt)
        output_field = "cards"
        stamper = None

    payloads: list[dict] = []
    errors = 0
    failures: dict[str, int] = {}
    aborted_reason = ""
    consecutive_systemic = 0
    last_systemic = ""
    seen = 0

    explore = (
        spec.corpus_source.get("kind") == "filesystem"
        and _llm_config.generator_explore()
    )
    if explore:
        from bin.generators import explorer as _explorer
        corpus_root = spec.corpus_source.get("root", "")
        globs = split_globs(spec.corpus_source.get("glob", "*"))
        target = _llm_config.generator_target_cards()
        print(
            f"[generate] exploring {corpus_root} globs={globs or ['*']} "
            f"target={target}",
            flush=True,
        )
        try:
            explored, dropped = _explorer.explore(
                instructions=gen.signature.instructions,
                root=corpus_root,
                globs=globs,
                goal=spec.description,
                target_cards=target,
                artifact=artifact,
                files=_explorer.inventory(
                    iter_filesystem_paths(spec.corpus_source),
                    pathlib.Path(corpus_root).resolve(),
                ),
            )
            for artifact_item, ref in explored:
                if artifact == "work-order":
                    wo = finalize_work_order(
                        artifact_item,
                        domain=spec.name,
                        created_at=created_at,
                        models=run_models,
                        repos=repos,
                        source_ref=ref,
                    )
                    cited = stamper.cite(ref) if stamper else []
                    payloads.append(_work_order_payload(wo, cited_evidence=cited))
                else:
                    payloads.append(
                        artifact_item.model_copy(
                            update={"source_ref": ref}
                        ).model_dump()
                    )
            seen = 1
            if not payloads and not dropped:
                print(
                    "[generate] the explorer finished without filing anything: "
                    "check that the globs match source files, or raise "
                    "GENERATOR_TARGET_CARDS",
                    flush=True, file=sys.stderr,
                )
            if dropped:
                errors += dropped
                failures["unverified-ref"] = dropped
                print(
                    f"[generate] dropped {dropped} card(s) whose source_ref did "
                    "not resolve under the corpus root",
                    flush=True, file=sys.stderr,
                )
        except CardGenError as e:
            errors += 1
            failures[e.failure_class] = failures.get(e.failure_class, 0) + 1
            aborted_reason = str(e)
            print(
                f"[generate] explore failed failure={e.failure_class}: {e}",
                flush=True, file=sys.stderr,
            )

    for item in ([] if explore else _iter_corpus(spec)):
        if seen >= limit:
            print(
                f"[generate] item budget reached ({limit}); run again to continue "
                "or raise GENERATOR_MAX_ITEMS / pass --limit",
                flush=True,
            )
            break
        seen += 1
        print(f"[generate] item {seen}: {item['source_ref']}", flush=True)
        try:
            result = gen(corpus_text=item["text"])
            produced = getattr(result, output_field, None) or []
            consecutive_systemic = 0
            for artifact_item in produced:
                if artifact == "work-order":
                    wo = finalize_work_order(
                        artifact_item,
                        domain=spec.name,
                        created_at=created_at,
                        models=run_models,
                        repos=repos,
                        source_ref=item["source_ref"],
                    )
                    cited = stamper.cite(item["source_ref"]) if stamper else []
                    payloads.append(_work_order_payload(wo, cited_evidence=cited))
                else:
                    c = artifact_item.model_copy(
                        update={"source_ref": item["source_ref"]}
                    )
                    payloads.append(c.model_dump())
        except CardGenError as e:
            errors += 1
            consecutive_systemic = 0
            failures[e.failure_class] = failures.get(e.failure_class, 0) + 1
            print(
                f"[generate] item failed failure={e.failure_class} "
                f"on {item.get('source_ref')}: {type(e).__name__}: {e}",
                flush=True, file=sys.stderr,
            )
        except Exception as e:
            errors += 1
            failures["error"] = failures.get("error", 0) + 1
            print(
                f"[generate] item failed failure=error "
                f"on {item.get('source_ref')}: {type(e).__name__}: {e}",
                flush=True, file=sys.stderr,
            )
            if _is_systemic(e):
                consecutive_systemic += 1
                last_systemic = f"{type(e).__name__}: {e}"
                if consecutive_systemic >= _SYSTEMIC_ABORT_AFTER:
                    aborted_reason = (
                        f"provider unavailable after {consecutive_systemic} "
                        f"consecutive failures: {last_systemic}"
                    )
                    print(
                        f"[generate] ABORTED: {aborted_reason}\n"
                        "[generate] check OPENROUTER_API_KEY / LLM_BASE_URL "
                        "(repo .env), then rerun",
                        flush=True, file=sys.stderr,
                    )
                    break
            else:
                consecutive_systemic = 0

    breakdown = (
        " [" + ", ".join(f"{k}={v}" for k, v in sorted(failures.items())) + "]"
        if failures else ""
    )
    noun = "work orders" if artifact == "work-order" else "cards"
    print(f"[generate] generated {len(payloads)} {noun} from {seen} corpus items "
          f"({errors} errors{breakdown})", flush=True)

    pairs = 0
    singles = 0
    if kind == "single":
        singles = enqueue_singles(spec.id, payloads, rubric=rubric)
        print(f"[generate] enqueued {singles} single(s) for the judge axis",
              flush=True)
    else:
        pairs = _enqueue_pairs(spec.id, payloads, rng, rounds=rounds)
        print(f"[generate] enqueued {pairs} pair(s) for the judge wheel",
              flush=True)

    return GenerateResult(
        cards_generated=len(payloads),
        pairs_enqueued=pairs,
        singles_enqueued=singles,
        errors=errors,
        failures=failures,
        aborted_reason=aborted_reason,
    )

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--domain", required=True)
    p.add_argument(
        "--advance-round",
        action="store_true",
        help=(
            "Draw the next Swiss round for the domain from the standings so "
            "far instead of generating: refuses while the open round still "
            "has judgements outstanding, and stops at the round cap instead "
            "of drawing an all-bye round past it. Exits 0 when the pool is "
            "finished, 3 when it is out of comparisons, 4 when the draw is "
            "stuck."
        ),
    )
    p.add_argument(
        "--rounds",
        type=int,
        default=None,
        help=(
            "Round cap for this campaign (default: ceil(log2 N), a full "
            "ordering). A shorter cap is a legitimate stop: it yields a "
            "coarser ordering, never an error. Recorded against the domain "
            "the moment it is passed, so later --advance-round calls honour "
            "it unasked."
        ),
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--artifact",
        choices=("card", "work-order"),
        default=None,
        help=(
            "Artifact kind to generate. Default: the domain's "
            "corpus_source['artifact'], falling back to 'card'."
        ),
    )
    p.add_argument(
        "--judgement-kind",
        choices=("pair", "single"),
        default=None,
        help=(
            "How generated artifacts are enqueued: 'pair' brackets them for "
            "the comparison wheel, 'single' writes one pending per artifact "
            "for absolute judgement. Default: the judgement_kind of the "
            "domain's active template (or --rubric), falling back to 'pair'."
        ),
    )
    p.add_argument(
        "--rubric",
        default=None,
        help=(
            "Explicit eval_template name to bind pendings to (overrides the "
            "domain's own rubric for config matching and kind detection)."
        ),
    )
    args = p.parse_args()
    if args.advance_round:
        spec = _domains.get_domain(args.domain)
        drawn = advance_round(spec.id, rubric=args.rubric, rounds=args.rounds)
        if drawn["status"] == ROUND_DRAWN:
            print(
                f"[advance] round {drawn['round_drawn']}/{drawn['rounds_cap']}: "
                f"{drawn['pairs_enqueued']} pair(s), {len(drawn['byes'])} bye, "
                f"{len(drawn['discarded'])} discarded",
                flush=True,
            )
        else:
            print(
                f"[advance] {drawn['status'].upper()} after round "
                f"{drawn['rounds_played']}/{drawn['rounds_cap']}: "
                f"{drawn['reason']}",
                flush=True,
            )
        exit_code = ROUND_STATUS_EXIT_CODE[drawn["status"]]
        if exit_code:
            sys.exit(exit_code)
        return
    result = run(args.domain, limit=args.limit, seed=args.seed,
                 artifact=args.artifact, judgement_kind=args.judgement_kind,
                 rubric=args.rubric, rounds=args.rounds)
    if result.aborted_reason:
        sys.exit(2)

if __name__ == "__main__":
    main()
