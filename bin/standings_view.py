#!/usr/bin/env python3
"""standings_view.py -- the points table, computed ONCE, in Python.

There is exactly one implementation of Swiss scoring in this tree and it is
:mod:`bin.swiss`. This module is the only thing that turns judgement-fabric
score rows into a points table, and it publishes the answer as a materialised
read model in the fabric DB (table ``standings_view``, one JSON document per
scope) so a UI can paint the page with a single SELECT. Nothing downstream
adds points, ranks items, or decides what a discard removes; a second
implementation of any of that is a bug, not a convenience.

Identity is the pair, not the match row. Two score rows that show the same two
texts under the same rubric version are the SAME comparison however many
tournament match rows produced them, so they are folded to the latest one
before anything is scored -- ``bin/judgement.py`` keys the queue the same way
(``snapshot_pair`` + :func:`bin.swiss.pair_key`), and this module reuses those
functions rather than restating them.

Discards are per side. ``discard-a`` ejects A and leaves B in the pool with
NOTHING recorded about it, so the survivor is reported in the table with zero
matches played rather than a loss -- the same treatment a bye gets. The
document says so per item, because "0 played" and "played and lost" are
different facts and a table that renders them identically lies.

A verdict this engine does not score -- a retired vocabulary, or ``skip`` --
is never quietly folded in as a nil-point played match. It is counted, named,
and published in ``unscored_verdicts`` so the page can say out loud how many
judgements are sitting outside the table and why.

CLI:
  standings_view.py refresh                      -- materialise every scope
  standings_view.py show [--domain D] [--rater-type T]
  standings_view.py scopes                       -- what is materialised
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import swiss  # noqa: E402  (needs _REPO_ROOT on sys.path)
from bin.judgement import snapshot_pair  # noqa: E402  (one snapshot rule)

VIEW_TABLE = "standings_view"

EVERY_RATER = ""
EVERY_DOMAIN = ""
HUMAN = "human"

RATER_SCOPES = (HUMAN, EVERY_RATER)

ONLY_A_PERSONS_VERDICT_ORDERS_THE_QUEUE_BY_DEFAULT = (
    "the human scope is what the standings page reads. A model verdict may be "
    "materialised too, under the EVERY_RATER scope, but it is a deliberate "
    "widening: defaulting to it would quietly make the ordering a model's "
    "opinion, which is the thing the tournament exists to prevent."
)

A_VERDICT_THE_ENGINE_DOES_NOT_SCORE_IS_COUNTED_AND_NAMED_NEVER_FOLDED_IN = (
    "feeding an unknown verdict to the engine with a default outcome would "
    "record a played match worth no points, so a fully judged pool would "
    "present as settled with everything on zero. Retired vocabulary and skip "
    "are excluded from the pool and reported in unscored_verdicts instead."
)

A_SURVIVOR_OF_A_DISCARD_HAS_PLAYED_NOTHING_AND_MUST_NOT_READ_AS_A_LOSS = (
    "a discarded pairing produces no result for the item that stayed, so it "
    "sits at zero points with zero matches played. That is not the same "
    "position as an item that played and lost, and awaiting_first_result vs "
    "lost_honestly is how the read model keeps them apart."
)

VIEW_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {VIEW_TABLE} (
  rater_type          TEXT    NOT NULL,
  domain              TEXT    NOT NULL,
  document            TEXT    NOT NULL,
  computed_at         TEXT    NOT NULL,
  source_verdict_rows INTEGER NOT NULL,
  PRIMARY KEY (rater_type, domain)
);
"""

PAIR_RUBRIC_SQL = """
SELECT name, MAX(version) AS version
FROM eval_template
WHERE is_draft = 0
  AND COALESCE(json_extract(output_definition, '$.judgement_kind'), 'pair') = 'pair'
GROUP BY name
ORDER BY name
"""

SOURCE_VERDICT_ROWS_SQL = (
    "SELECT COUNT(*) AS rows FROM score WHERE name='judgement.verdict'"
)

VERDICT_ROW_SQL = """
SELECT s.rating_id                              AS rating_id,
       s.value                                  AS verdict,
       t.name                                   AS rubric,
       s.rubric_version                         AS rubric_version,
       s.pending_id                             AS pending_id,
       s.tournament_db_path                     AS tournament_db_path,
       s.created_at                             AS created_at,
       s.id                                     AS score_id,
       p.trace_payload                          AS trace_payload,
       p.content_a                              AS content_a,
       p.content_b                              AS content_b,
       d.name                                   AS domain_name,
       json_extract(s.metadata, '$.rater.type') AS rater_type
FROM score s
JOIN eval_template t ON t.id = s.template_id
LEFT JOIN pending_judgement p ON p.id = s.pending_id
LEFT JOIN domain d ON d.id = p.domain_id
WHERE s.name = 'judgement.verdict'
ORDER BY s.created_at ASC, s.id ASC
"""


def db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_view_table(conn: sqlite3.Connection) -> None:
    conn.executescript(VIEW_SCHEMA)


def pair_rubrics(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every pair-shaped rubric on disk, at its newest non-draft version.

    Read from the fabric rather than from a list, so nothing has to be kept
    in step by hand when a rubric is added, renamed, or revised. A template
    with no ``judgement_kind`` predates the field and defaults to pair --
    the same default :func:`bin.judgement.normalize_output_definition`
    applies.
    """
    return [(row["name"], row["version"]) for row in conn.execute(PAIR_RUBRIC_SQL)]


def source_verdict_rows(conn: sqlite3.Connection) -> int:
    """How many verdict score rows exist right now.

    Stored beside every materialised document so a reader can tell a table
    that is merely old from a table that is behind the corpus: score rows are
    append-only, so a higher count means judgements landed after this
    document was computed.
    """
    row = conn.execute(SOURCE_VERDICT_ROWS_SQL).fetchone()
    return int(row["rows"] or 0)


def _superseded_rating_ids(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT previous_rating_id FROM judgement_revision"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row["previous_rating_id"] for row in rows}


def _parse_json(text) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _payload_round(payload: dict) -> Optional[int]:
    value = payload.get("round")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    label = payload.get("label")
    if isinstance(label, str) and label.startswith("R"):
        head = label[1:].split("-", 1)[0]
        if head.isdigit():
            return int(head)
    return None


def _card(payload: dict, card_key: str, ref_key: str) -> dict:
    card = payload.get(card_key)
    if isinstance(card, dict):
        return {
            "title": card.get("title"),
            "source_ref": card.get("source_ref") or card.get("ref"),
        }
    ref = payload.get(ref_key)
    return {"title": ref if isinstance(ref, str) else None, "source_ref": ref
            if isinstance(ref, str) else None}


def item_key(content: str) -> str:
    """The id one judged text carries everywhere.

    The SAME digest ``bin/judgement.py`` hashes into a pair key, so an item's
    identity in the table and its identity in the queue cannot drift apart.
    """
    return swiss.content_digest(content)[:16]


def _scoreable_rows(conn: sqlite3.Connection, *, rater_type: str,
                    domain: str) -> tuple[list[dict], dict[str, int], int]:
    """Effective verdict rows for a scope, oldest first.

    Returns ``(rows, unscored_verdicts, unpairable)``: the rows the engine can
    score, a count per verdict the engine cannot score, and how many rows
    carried no snapshot of the two texts (single-artifact judgements, byes)
    and are therefore not a pairing at all.
    """
    superseded = _superseded_rating_ids(conn)
    scored = swiss.known_verdicts()
    rows: list[dict] = []
    unscored: dict[str, int] = {}
    unpairable = 0
    for raw in conn.execute(VERDICT_ROW_SQL):
        if raw["rating_id"] in superseded:
            continue
        row_rater = raw["rater_type"] or ""
        if rater_type != EVERY_RATER and row_rater != rater_type:
            continue
        pool_name = raw["domain_name"] or Path(raw["tournament_db_path"] or "").stem
        if domain != EVERY_DOMAIN and (raw["domain_name"] or "") != domain:
            continue
        payload = _parse_json(raw["trace_payload"])
        content_a, content_b = raw["content_a"], raw["content_b"]
        if content_a is None or content_b is None:
            content_a, content_b = snapshot_pair(payload)
        if content_a is None or content_b is None:
            unpairable += 1
            continue
        if raw["verdict"] not in scored:
            unscored[raw["verdict"]] = unscored.get(raw["verdict"], 0) + 1
            continue
        rows.append({
            "rating_id": raw["rating_id"],
            "verdict": raw["verdict"],
            "rubric": raw["rubric"],
            "rubric_version": raw["rubric_version"],
            "created_at": raw["created_at"],
            "round": _payload_round(payload),
            "content_a": content_a,
            "content_b": content_b,
            "card_a": _card(payload, "card_a", "input_a"),
            "card_b": _card(payload, "card_b", "input_b"),
            "pool": pool_name,
        })
    return rows, unscored, unpairable


def _fold_to_pairs(rows: Iterable[dict], rubric: str) -> list[dict]:
    """One row per pair key, the latest verdict winning.

    The design makes the pair the unit: two match rows that put the same two
    texts in front of a judge under the same rubric version are one
    comparison, and counting both would score an item twice for a single
    decision.
    """
    latest: dict[str, dict] = {}
    for row in rows:
        key = swiss.pair_key(row["content_a"], row["content_b"], rubric,
                             row["rubric_version"])
        latest[key] = dict(row, pair_key=key)
    return sorted(latest.values(), key=lambda r: (r["created_at"], r["rating_id"]))


def _table_for(rubric: str, current_version: int, rows: list[dict]) -> dict:
    folded = _fold_to_pairs(rows, rubric)
    pool = swiss.Pool(rubric_id=rubric, rubric_version=current_version)
    display: dict[str, dict] = {}
    sides: list[tuple[dict, str, str]] = []
    for row in folded:
        pair_ids = []
        for side in ("a", "b"):
            content = row[f"content_{side}"]
            item = swiss.Item(id=item_key(content), content=content)
            swiss.add_item(pool, item)
            display[item.id] = dict(row[f"card_{side}"], pool=row["pool"])
            pair_ids.append(item.id)
        sides.append((row, pair_ids[0], pair_ids[1]))

    highest_round = None
    for row, item_a, item_b in sides:
        swiss.record(
            pool,
            round=row["round"] or 0,
            item_a=item_a,
            item_b=item_b,
            verdict=row["verdict"],
            rubric_id=rubric,
            rubric_version=row["rubric_version"],
        )
        if row["round"] is not None:
            highest_round = row["round"] if highest_round is None else max(
                highest_round, row["round"])

    live = swiss.live_results(pool)
    entries = [_entry(standing, display) for standing in swiss.standings(pool)]
    leader = max((e["points"] for e in entries if e["played"]), default=0)
    for entry in entries:
        entry["top_group"] = leader > 0 and entry["points"] == leader and entry["played"] > 0
    return {
        "rubric": rubric,
        "rubric_version": current_version,
        "round": highest_round,
        "matches": len(live),
        "stale_matches": len(pool.results) - len(live),
        "top_group_points": leader if leader > 0 else None,
        "standings": entries,
        "discards": [_discard(entry, display) for entry in swiss.discards(pool)],
    }


def _entry(standing: swiss.Standing, display: dict[str, dict]) -> dict:
    card = display.get(standing.item_id, {})
    return {
        "item_key": standing.item_id,
        "title": card.get("title") or standing.item_id,
        "source_ref": card.get("source_ref"),
        "pool": card.get("pool"),
        "rank": standing.rank,
        "points": standing.points,
        "played": standing.played,
        "wins": standing.wins,
        "draws": standing.draws,
        "losses": standing.losses,
        "byes": standing.byes,
        "lost_honestly": standing.points == 0 and standing.played > 0,
        "awaiting_first_result": standing.played == 0,
        "top_group": False,
    }


def _discard(entry: swiss.Discard, display: dict[str, dict]) -> dict:
    card = display.get(entry.item_id, {})
    survivor = display.get(entry.opponent or "", {})
    return {
        "item_key": entry.item_id,
        "title": card.get("title") or entry.item_id,
        "source_ref": card.get("source_ref"),
        "pool": card.get("pool"),
        "verdict": entry.verdict,
        "side": entry.side,
        "round": entry.round,
        "survivor_key": entry.opponent,
        "survivor_title": survivor.get("title"),
    }


def build_document(conn: sqlite3.Connection, *, rater_type: str,
                   domain: str) -> dict:
    """The whole read model for one scope, ready to render as-is."""
    rows, unscored, unpairable = _scoreable_rows(
        conn, rater_type=rater_type, domain=domain)
    tables = []
    for rubric, version in pair_rubrics(conn):
        scoped = [row for row in rows if row["rubric"] == rubric]
        if not scoped:
            continue
        tables.append(_table_for(rubric, version, scoped))
    return {
        "scope": {
            "rater_type": rater_type,
            "domain": domain,
        },
        "tables": tables,
        "totals": {
            "rubrics": len(tables),
            "items": sum(len(t["standings"]) for t in tables),
            "matches": sum(t["matches"] for t in tables),
            "discarded": sum(len(t["discards"]) for t in tables),
        },
        "unscored_verdicts": [
            {"verdict": verdict, "count": count}
            for verdict, count in sorted(unscored.items())
        ],
        "unpairable_rows": unpairable,
    }


def materialise(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Recompute and store every scope. Returns one summary per scope written.

    Scopes whose domain no longer exists are dropped rather than left to age:
    the view is derived, and a table for a deleted domain is not history, it
    is a stale answer to a question nobody can ask.
    """
    if conn is None:
        with connect() as own:
            return materialise(own)
    ensure_view_table(conn)
    domains = [EVERY_DOMAIN] + [
        row["name"] for row in conn.execute("SELECT name FROM domain ORDER BY name")
    ]
    rows_now = source_verdict_rows(conn)
    computed_at = _now()
    written: list[dict] = []
    for rater_type in RATER_SCOPES:
        for domain in domains:
            document = build_document(conn, rater_type=rater_type, domain=domain)
            document["computed_at"] = computed_at
            document["source_verdict_rows"] = rows_now
            conn.execute(
                f"INSERT INTO {VIEW_TABLE}"
                "(rater_type, domain, document, computed_at, source_verdict_rows) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(rater_type, domain) DO UPDATE SET "
                "  document=excluded.document, "
                "  computed_at=excluded.computed_at, "
                "  source_verdict_rows=excluded.source_verdict_rows",
                (rater_type, domain, json.dumps(document, ensure_ascii=False),
                 computed_at, rows_now),
            )
            written.append({
                "rater_type": rater_type,
                "domain": domain,
                "items": document["totals"]["items"],
                "matches": document["totals"]["matches"],
                "discarded": document["totals"]["discarded"],
            })
    placeholders = ",".join("?" for _ in domains)
    conn.execute(
        f"DELETE FROM {VIEW_TABLE} WHERE domain NOT IN ({placeholders})", domains
    )
    conn.commit()
    return written


def read_document(conn: sqlite3.Connection, *, rater_type: str = HUMAN,
                  domain: str = EVERY_DOMAIN) -> Optional[dict]:
    try:
        row = conn.execute(
            f"SELECT document FROM {VIEW_TABLE} WHERE rater_type=? AND domain=?",
            (rater_type, domain),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row["document"]) if row else None


def _cmd_refresh(args) -> int:
    with connect() as conn:
        written = materialise(conn)
    if args.json:
        print(json.dumps(written, indent=2))
    else:
        for scope in written:
            rater = scope["rater_type"] or "every rater"
            domain = scope["domain"] or "every domain"
            print(f"{rater:>12} / {domain:<24} "
                  f"{scope['items']:>4} items  {scope['matches']:>4} matches  "
                  f"{scope['discarded']:>3} discarded")
    return 0


def _cmd_show(args) -> int:
    with connect() as conn:
        document = read_document(conn, rater_type=args.rater_type,
                                 domain=args.domain)
    if document is None:
        print(
            f"no materialised standings for rater_type={args.rater_type!r} "
            f"domain={args.domain!r}; run: standings_view.py refresh",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


def _cmd_scopes(args) -> int:
    with connect() as conn:
        try:
            rows = conn.execute(
                f"SELECT rater_type, domain, computed_at, source_verdict_rows "
                f"FROM {VIEW_TABLE} ORDER BY rater_type, domain"
            ).fetchall()
        except sqlite3.OperationalError:
            print("standings_view has never been materialised", file=sys.stderr)
            return 1
        behind = source_verdict_rows(conn)
    for row in rows:
        state = "current" if row["source_verdict_rows"] == behind else (
            f"behind by {behind - row['source_verdict_rows']} verdict row(s)")
        print(f"{row['rater_type'] or '*':>12} / {row['domain'] or '*':<24} "
              f"{row['computed_at']}  {state}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    refresh = sub.add_parser("refresh", help="recompute and store every scope")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=_cmd_refresh)

    show = sub.add_parser("show", help="print one materialised document")
    show.add_argument("--domain", default=EVERY_DOMAIN)
    show.add_argument("--rater-type", dest="rater_type", default=HUMAN)
    show.set_defaults(func=_cmd_show)

    scopes = sub.add_parser("scopes", help="what is materialised, and how stale")
    scopes.set_defaults(func=_cmd_scopes)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
