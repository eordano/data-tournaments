#!/usr/bin/env python3
"""Campaign / finding spine over the fabric SQLite DB (wave-8 B4).

The persistence layer for the bugsweep product model
(docs/reviews/bugsweep-product-model.md): campaigns bundle a pin + objective
+ time window; findings are the intake-ledger rows that accrete state
through the pipeline (candidate → … → published, or terminal
confirmed_validated / failed_infra / no_go-with-reason); per-lens
CONFIRM/REFUTE review verdicts with the one-repair-cycle loop; and the
RED/GREEN validation ledger with intended-failure counts.

Row families (schema: bin/judgement_schema.sql, applied by ``init()``):

* Mutable catalog-style rows — campaign, finding. Plain CRUD mirroring
  bin/catalog.py. Findings are mutable because they accrete state over a
  campaign's life; their evidence links are digests into the IMMUTABLE
  evidence_ref table (finding_evidence join).

* Append-only histories — review_lens_verdict, validation_ledger. BEFORE
  UPDATE/DELETE triggers RAISE(ABORT) (approval_event precedent);
  corrections are new rows.

State machine (workflow_runs precedent): terminal states are sticky —
set_finding_state on a terminal finding is a silent no-op, so a late
projection update cannot flip a settled outcome. ``no_go`` REQUIRES a
``no_go_reason`` from the 5-class taxonomy; NO_GO is a documented terminal
deliverable, never an abandonment.

CLI is a debug aid mirroring the module functions:
  campaigns.py init
  campaigns.py create-campaign --project P --name N --kind bugsweep [...]
  campaigns.py create-finding --campaign C --slug S [...]
  campaigns.py ledger --campaign C
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

IN_FLIGHT_STATES = (
    "candidate",
    "investigating",
    "workorder_generated",
    "judged",
    "approved",
    "executing",
    "published",
)
TERMINAL_STATES = ("confirmed_validated", "failed_infra", "no_go")
STATES = IN_FLIGHT_STATES + TERMINAL_STATES

NO_GO_REASONS = (
    "already-fixed",
    "wrong-repo",
    "by-design",
    "stale-signal",
    "insufficient-evidence",
)

EVIDENCE_ROLES = ("signal", "root-cause", "dedup", "validation")
VERDICTS = ("CONFIRM", "REFUTE")
CAMPAIGN_KINDS = ("bugsweep", "perfsweep", "featuresweep", "slopsweep", "release")
DISPOSITIONS = ("ship_anyway", "needs_fix", "no_go")

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
    with _connect() as conn:
        _migrate_campaign_tables(conn)
        conn.commit()

def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row["sql"] if row else ""

def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        r["name"] == column
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )

def _migrate_campaign_tables(conn: sqlite3.Connection) -> None:
    """Idempotent upgrades for DBs created before the sweeps layer:
    ALTER-ADD for new columns; a copy-rebuild for the campaign table when
    its kind CHECK predates the sweep kinds (SQLite cannot widen a CHECK
    in place)."""
    if not _has_column(conn, "review_lens_verdict", "round_id"):
        conn.execute(
            "ALTER TABLE review_lens_verdict "
            "ADD COLUMN round_id INTEGER REFERENCES sweep_round(id)"
        )
    if not _has_column(conn, "validation_ledger", "perf_json"):
        conn.execute(
            "ALTER TABLE validation_ledger "
            "ADD COLUMN perf_json TEXT NOT NULL DEFAULT '[]'"
        )
    needs_kind_rebuild = "perfsweep" not in _table_sql(conn, "campaign")
    if not needs_kind_rebuild and not _has_column(conn, "campaign", "spec_json"):
        conn.execute("ALTER TABLE campaign ADD COLUMN spec_json TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE campaign ADD COLUMN spec_digest TEXT NOT NULL DEFAULT ''")
    if needs_kind_rebuild:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE IF EXISTS campaign_new")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE campaign_new (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id   INTEGER NOT NULL REFERENCES project(id),
                  name         TEXT NOT NULL UNIQUE,
                  kind         TEXT NOT NULL CHECK (kind IN
                                 ('bugsweep','perfsweep','featuresweep','slopsweep','release')),
                  objective    TEXT NOT NULL DEFAULT '',
                  time_window  TEXT NOT NULL DEFAULT '',
                  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
                  base_commit  TEXT NOT NULL DEFAULT '',
                  spec_json    TEXT NOT NULL DEFAULT '',
                  spec_digest  TEXT NOT NULL DEFAULT '',
                  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            cols = "id, project_id, name, kind, objective, time_window, status, base_commit, created_at"
            conn.execute(
                f"INSERT INTO campaign_new({cols}) SELECT {cols} FROM campaign"
            )
            conn.execute("DROP TABLE campaign")
            conn.execute("ALTER TABLE campaign_new RENAME TO campaign")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_campaign_project ON campaign(project_id)"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict:
    d = dict(row)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d

def create_campaign(
    *,
    project: str,
    name: str,
    kind: str,
    objective: str = "",
    time_window: str = "",
    base_commit: str = "",
    spec: Optional[dict] = None,
) -> int:
    """Create a campaign. When ``spec`` is given it is validated as a
    SweepSpec, must agree with ``kind``, and is frozen onto the row as
    canonical JSON + digest — the sweep can never drift from the spec it
    was launched with."""
    if kind not in CAMPAIGN_KINDS:
        raise ValueError(f"kind must be one of {CAMPAIGN_KINDS}, got {kind!r}")
    spec_json = ""
    spec_digest = ""
    if spec is not None:
        from bin import sweep_spec as sweep_spec_mod

        validated = sweep_spec_mod.validate_spec(spec)
        if validated.kind != kind:
            raise ValueError(
                f"campaign kind {kind!r} does not match spec.kind {validated.kind!r}"
            )
        spec_json = validated.canonical()
        spec_digest = validated.digest()
    from bin import catalog

    pid = catalog.get_project(project)["id"]
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO campaign(project_id, name, kind, objective, "
                "time_window, base_commit, spec_json, spec_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, name, kind, objective, time_window, base_commit,
                 spec_json, spec_digest),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"campaign {name!r} already exists")
        conn.commit()
        return cur.lastrowid

def get_sweep_spec(name: str):
    """The campaign's validated SweepSpec, or None for pre-spec campaigns."""
    camp = get_campaign(name)
    if not camp.get("spec_json"):
        return None
    from bin import sweep_spec as sweep_spec_mod

    return sweep_spec_mod.validate_spec(json.loads(camp["spec_json"]))

def get_campaign(name: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM campaign WHERE name=?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no campaign named {name!r}")
        return _row_to_dict(row)

def list_campaigns(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM campaign WHERE status=? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

def close_campaign(name: str) -> None:
    """Close a campaign. Fail-closed guards:

    * an OPEN round blocks the close (close the round first);
    * a sweep that exhausted ``rounds.max`` without converging is the
      human-tie-break case — every finding still outside a terminal state
      must carry a disposition (``dispose_finding``) before the campaign
      can close.
    """
    camp = get_campaign(name)
    rounds = list_rounds(name)
    if any(r["status"] == "open" for r in rounds):
        raise ValueError(f"campaign {name!r} has an open round; close it first")
    spec = get_sweep_spec(name)
    at_cap_unconverged = (
        spec is not None
        and len(rounds) >= spec.rounds.max
        and bool(rounds)
        and rounds[-1]["outcome"] == "not_converged"
    )
    if at_cap_unconverged:
        disposed = set(campaign_dispositions(name))
        undisposed = [
            f["slug"]
            for f in list_findings(name)
            if f["state"] not in TERMINAL_STATES and f["slug"] not in disposed
        ]
        if undisposed:
            raise ValueError(
                f"campaign {name!r} hit rounds.max without converging; "
                "human tie-break required — dispose_finding() each of: "
                + ", ".join(undisposed)
            )
    with _connect() as conn:
        conn.execute("UPDATE campaign SET status='closed' WHERE id=?", (camp["id"],))
        conn.commit()


def dispose_finding(
    campaign: str,
    slug: str,
    *,
    decision: str,
    rationale: str,
    no_go_reason: Optional[str] = None,
) -> int:
    """Record the human tie-break for one finding (append-only; latest row
    wins). ``rationale`` is REQUIRED — a tie-break without a why is just
    the drip-review pattern ending in a shrug. ``no_go`` additionally
    moves the finding to its terminal state (with the mandatory reason);
    ``ship_anyway``/``needs_fix`` leave the state alone — the disposition
    ledger IS their record. Ledger row and state change commit in ONE
    transaction: a failure can never leave a rationale-free terminal
    finding behind."""
    if decision not in DISPOSITIONS:
        raise ValueError(f"decision must be one of {DISPOSITIONS}, got {decision!r}")
    if not rationale.strip():
        raise ValueError("a disposition requires a non-empty rationale")
    if decision == "no_go":
        if no_go_reason is None:
            raise ValueError(
                f"decision 'no_go' requires a no_go_reason from {NO_GO_REASONS}"
            )
        if no_go_reason not in NO_GO_REASONS:
            raise ValueError(
                f"unknown no_go_reason {no_go_reason!r}; expected one of {NO_GO_REASONS}"
            )
    elif no_go_reason is not None:
        raise ValueError("no_go_reason is only valid with decision 'no_go'")
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        frow = conn.execute(
            "SELECT id, state FROM finding WHERE campaign_id=? AND slug=?",
            (cid, slug),
        ).fetchone()
        if frow is None:
            raise LookupError(f"no finding {slug!r} in campaign {campaign!r}")
        cur = conn.execute(
            "INSERT INTO finding_disposition"
            "(finding_id, decision, rationale, decided_by) VALUES (?, ?, ?, ?)",
            (frow["id"], decision, rationale, os.environ.get("DT_OPERATOR", "")),
        )
        if decision == "no_go" and frow["state"] not in TERMINAL_STATES:
            conn.execute(
                "UPDATE finding SET state='no_go', no_go_reason=?, "
                "updated_at=datetime('now') WHERE id=?",
                (no_go_reason, frow["id"]),
            )
        conn.commit()
        return cur.lastrowid


def campaign_dispositions(campaign: str) -> dict[str, dict]:
    """{slug: latest disposition row} for the campaign."""
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT f.slug, d.* FROM finding_disposition d "
            "JOIN finding f ON f.id = d.finding_id "
            "WHERE f.campaign_id=? ORDER BY d.id",
            (cid,),
        ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        latest[d.pop("slug")] = d
    return latest


def export_corpus(campaign: str, out_dir: str) -> list[str]:
    """Write each finding's signal evidence as ``<out_dir>/<slug>.md`` so
    the same frozen corpus a sweep reviewed can feed a /brackets pair
    tournament (prepared-artifacts flow) for portfolio ranking. Read-only
    over evidence; files are overwritten deterministically."""
    from bin import catalog

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for f in list_findings(campaign):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", f["slug"]) or f["slug"].startswith("."):
            raise ValueError(
                f"refusing to export finding slug {f['slug']!r}: not a safe "
                "filename (path separators / dotfiles are excluded)"
            )
        dossier = get_finding(campaign, f["slug"])
        excerpts = []
        for ev in dossier["evidence"]:
            if ev["role"] != "signal":
                continue
            payload = json.loads(catalog.get_evidence_ref(ev["evidence_digest"])["body"])
            excerpts.append(
                f"<!-- {payload.get('canonical_uri', '')} -->\n"
                + payload.get("excerpt", "")
            )
        if not excerpts:
            continue
        path = out / f"{f['slug']}.md"
        path.write_text(
            f"# {f['title'] or f['slug']}\n\n" + "\n\n---\n\n".join(excerpts),
            encoding="utf-8",
        )
        written.append(str(path))
    return written

def create_finding(
    *,
    campaign: str,
    slug: str,
    title: str = "",
    source_kind: str = "",
    root_cause: str = "",
    tracking_links: Optional[list] = None,
    dedup_notes: str = "",
) -> int:
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO finding(campaign_id, slug, title, source_kind, "
                "root_cause, tracking_links, dedup_notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    slug,
                    title,
                    source_kind,
                    root_cause,
                    json.dumps(tracking_links or []),
                    dedup_notes,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"finding {slug!r} already exists in campaign {campaign!r}")
        conn.commit()
        return cur.lastrowid

def get_finding(campaign: str, slug: str) -> dict:
    """Fetch a finding with its evidence links, lens verdicts and
    validation rows attached (the full dossier view)."""
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM finding WHERE campaign_id=? AND slug=?", (cid, slug)
        ).fetchone()
        if row is None:
            raise LookupError(f"no finding {slug!r} in campaign {campaign!r}")
        d = _row_to_dict(row, ("tracking_links",))
        d["evidence"] = [
            dict(r)
            for r in conn.execute(
                "SELECT evidence_digest, role FROM finding_evidence "
                "WHERE finding_id=? ORDER BY role, evidence_digest",
                (d["id"],),
            ).fetchall()
        ]
        d["lens_verdicts"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM review_lens_verdict WHERE finding_id=? ORDER BY id",
                (d["id"],),
            ).fetchall()
        ]
        d["validation"] = [
            _row_to_dict(r, ("harness_notes", "perf_json"))
            for r in conn.execute(
                "SELECT * FROM validation_ledger WHERE finding_id=? ORDER BY id",
                (d["id"],),
            ).fetchall()
        ]
        return d

def list_findings(campaign: str, *, state: Optional[str] = None) -> list[dict]:
    cid = get_campaign(campaign)["id"]
    q = "SELECT * FROM finding WHERE campaign_id=?"
    args: tuple = (cid,)
    if state is not None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; expected one of {STATES}")
        q += " AND state=?"
        args += (state,)
    q += " ORDER BY slug"
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
        return [_row_to_dict(r, ("tracking_links",)) for r in rows]

def set_finding_state(
    campaign: str,
    slug: str,
    state: str,
    *,
    no_go_reason: Optional[str] = None,
    root_cause: Optional[str] = None,
) -> None:
    """Move a finding through the pipeline. Validation:

    * ``state`` must belong to the taxonomy;
    * ``no_go`` REQUIRES a ``no_go_reason`` from the 5-class taxonomy;
    * ``no_go_reason`` is rejected on any other state;
    * terminal states are STICKY (workflow_runs precedent): a late update
      on a settled finding is a silent no-op.
    """
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; expected one of {STATES}")
    if state == "no_go":
        if no_go_reason is None:
            raise ValueError(
                f"state 'no_go' requires a no_go_reason from {NO_GO_REASONS}"
            )
        if no_go_reason not in NO_GO_REASONS:
            raise ValueError(
                f"unknown no_go_reason {no_go_reason!r}; expected one of {NO_GO_REASONS}"
            )
    elif no_go_reason is not None:
        raise ValueError("no_go_reason is only valid with state 'no_go'")
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, state FROM finding WHERE campaign_id=? AND slug=?",
            (cid, slug),
        ).fetchone()
        if row is None:
            raise LookupError(f"no finding {slug!r} in campaign {campaign!r}")
        if row["state"] in TERMINAL_STATES:
            return
        sets = ["state=?", "no_go_reason=?", "updated_at=datetime('now')"]
        args: list[Any] = [state, no_go_reason]
        if root_cause is not None:
            sets.append("root_cause=?")
            args.append(root_cause)
        args.append(row["id"])
        conn.execute(f"UPDATE finding SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()

def link_finding_evidence(
    campaign: str, slug: str, evidence_digest: str, *, role: str = "signal"
) -> None:
    """Link an immutable evidence_ref digest to a finding. The digest must
    already exist in evidence_ref (FK enforced — sqlite3.IntegrityError on
    a dangling digest). Re-linking the same (digest, role) is a no-op."""
    if role not in EVIDENCE_ROLES:
        raise ValueError(f"role must be one of {EVIDENCE_ROLES}, got {role!r}")
    fid = _finding_id(campaign, slug)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO finding_evidence"
            "(finding_id, evidence_digest, role) VALUES (?, ?, ?)",
            (fid, evidence_digest, role),
        )
        conn.commit()

def _finding_id(campaign: str, slug: str) -> int:
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM finding WHERE campaign_id=? AND slug=?", (cid, slug)
        ).fetchone()
    if row is None:
        raise LookupError(f"no finding {slug!r} in campaign {campaign!r}")
    return int(row["id"])

def add_lens_verdict(
    campaign: str,
    slug: str,
    *,
    lens: str,
    verdict: str,
    rationale: str = "",
    repair_of: Optional[int] = None,
) -> int:
    """Append a per-lens CONFIRM/REFUTE verdict row.

    ``repair_of`` implements the ONE-repair-cycle rule: a REFUTE may be
    answered by AT MOST ONE later verdict pointing back at it — checked
    here for a friendly error and backstopped in SQL (unique partial index
    on ``repair_of``), so concurrent second repairs cannot both commit.

    Round attachment, the repair checks, and the insert run in ONE
    ``BEGIN IMMEDIATE`` transaction, and a schema trigger refuses inserts
    into a non-open round — a verdict can never land in a round that
    closed (and stamped its convergence) underneath us.

    A campaign with a SweepSpec additionally REFUSES: verdicts outside an
    open round (spec'd sweeps batch per round, never drip), lens names not
    in the spec's panel, and repairs when
    ``repair_max_cycles_per_finding: 0``. Empty lens names are always
    refused.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    lens = lens.strip()
    if not lens:
        raise ValueError("lens must be non-empty")
    spec = get_sweep_spec(campaign)
    if spec is not None:
        panel = {l.name for l in spec.panel.lenses}
        if lens not in panel:
            raise ValueError(
                f"lens {lens!r} is not in the sweep panel "
                f"({', '.join(sorted(panel))})"
            )
        if repair_of is not None and spec.rounds.repair_max_cycles_per_finding == 0:
            raise ValueError(
                "sweep spec forbids repairs (rounds.repair_max_cycles_per_finding: 0)"
            )
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        frow = conn.execute(
            "SELECT id FROM finding WHERE campaign_id=? AND slug=?", (cid, slug)
        ).fetchone()
        if frow is None:
            raise LookupError(f"no finding {slug!r} in campaign {campaign!r}")
        fid = int(frow["id"])
        rrow = conn.execute(
            "SELECT id FROM sweep_round WHERE campaign_id=? AND status='open'",
            (cid,),
        ).fetchone()
        round_id = rrow["id"] if rrow else None
        if round_id is None and spec is not None:
            raise ValueError(
                f"campaign {campaign!r} has a sweep spec but no open round; "
                "call open_round() first — spec'd sweeps batch verdicts per round"
            )
        if repair_of is not None:
            target = conn.execute(
                "SELECT id, finding_id, verdict, repair_of "
                "FROM review_lens_verdict WHERE id=?",
                (repair_of,),
            ).fetchone()
            if target is None:
                raise LookupError(f"no review_lens_verdict with id {repair_of}")
            if target["finding_id"] != fid:
                raise ValueError(
                    f"repair_of {repair_of} belongs to a different finding"
                )
            if target["verdict"] != "REFUTE":
                raise ValueError("repair_of must reference a REFUTE verdict")
            if target["repair_of"] is not None:
                raise ValueError(
                    "one-repair-cycle rule: cannot repair a repair "
                    f"(verdict {repair_of} is itself a repair)"
                )
            existing = conn.execute(
                "SELECT id FROM review_lens_verdict WHERE repair_of=?",
                (repair_of,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    "one-repair-cycle rule: REFUTE "
                    f"{repair_of} already repaired by verdict {existing['id']}"
                )
        cur = conn.execute(
            "INSERT INTO review_lens_verdict"
            "(finding_id, lens, verdict, rationale, repair_of, round_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fid, lens, verdict, rationale, repair_of, round_id),
        )
        conn.commit()
        return cur.lastrowid

def current_open_round(campaign: str) -> Optional[dict]:
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sweep_round WHERE campaign_id=? AND status='open'",
            (cid,),
        ).fetchone()
        return _row_to_dict(row, ("summary",)) if row else None

def list_rounds(campaign: str) -> list[dict]:
    cid = get_campaign(campaign)["id"]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sweep_round WHERE campaign_id=? ORDER BY round_no",
            (cid,),
        ).fetchall()
        return [_row_to_dict(r, ("summary",)) for r in rows]

def open_round(campaign: str) -> dict:
    """Open the next review round. Refuses when a round is already open,
    when the campaign is closed, or when the spec's ``rounds.max`` is
    exhausted — the round cap is the mechanism that ends the
    11-serial-reviews pattern, so hitting it is a loud error, not a nudge."""
    camp = get_campaign(campaign)
    if camp["status"] != "active":
        raise ValueError(f"campaign {campaign!r} is {camp['status']}")
    spec = get_sweep_spec(campaign)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT status FROM sweep_round WHERE campaign_id=?", (camp["id"],)
        ).fetchall()
        if any(r["status"] == "open" for r in rows):
            raise ValueError(f"campaign {campaign!r} already has an open round")
        round_no = len(rows) + 1
        if spec is not None and round_no > spec.rounds.max:
            raise ValueError(
                f"rounds.max ({spec.rounds.max}) reached for campaign "
                f"{campaign!r}; close out with a terminal decision instead of "
                "opening another round"
            )
        cur = conn.execute(
            "INSERT INTO sweep_round(campaign_id, round_no) VALUES (?, ?)",
            (camp["id"], round_no),
        )
        conn.commit()
        rid = cur.lastrowid
    return {"id": rid, "round_no": round_no}

def _round_batch_stats(conn: sqlite3.Connection, round_id: int) -> dict:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT lens, verdict, repair_of FROM review_lens_verdict "
            "WHERE round_id=?",
            (round_id,),
        ).fetchall()
    ]
    per_lens: dict[str, dict[str, int]] = {}
    for r in rows:
        d = per_lens.setdefault(r["lens"], {"CONFIRM": 0, "REFUTE": 0, "repairs": 0})
        if r["repair_of"] is not None:
            d["repairs"] += 1
        else:
            d[r["verdict"]] += 1
    new_confirms = sum(
        1 for r in rows if r["repair_of"] is None and r["verdict"] == "CONFIRM"
    )
    return {"per_lens": per_lens, "new_confirms": new_confirms, "verdicts": len(rows)}

def close_round(campaign: str, *, dataset_run_id: str = "") -> dict:
    """Close the open round, computing its convergence outcome.

    Batching (``rounds.batching: required``): every lens named in the spec
    panel must have reported at least one verdict this round, else the
    close refuses — a half-reported round is exactly the drip-review
    failure mode rounds exist to prevent.

    Convergence criteria:
    * ``no_new_confirmed_findings`` — converged iff the round produced no
      non-repair CONFIRM verdict (nothing new was confirmed wrong);
    * ``all_findings_settled`` — converged iff every finding in the
      campaign sits in a terminal state.
    """
    camp = get_campaign(campaign)
    spec = get_sweep_spec(campaign)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rrow = conn.execute(
            "SELECT id, round_no FROM sweep_round WHERE campaign_id=? "
            "AND status='open'",
            (camp["id"],),
        ).fetchone()
        if rrow is None:
            raise ValueError(f"campaign {campaign!r} has no open round")
        rnd = {"id": rrow["id"], "round_no": rrow["round_no"]}
        stats = _round_batch_stats(conn, rnd["id"])
        if spec is not None and spec.rounds.batching == "required":
            missing = [
                lens.name
                for lens in spec.panel.lenses
                if lens.name not in stats["per_lens"]
            ]
            if missing:
                raise ValueError(
                    f"round {rnd['round_no']} incomplete: no verdict from "
                    f"lens(es) {', '.join(missing)} — batching is required"
                )
        criterion = (
            spec.rounds.convergence if spec is not None else "no_new_confirmed_findings"
        )
        if criterion == "no_new_confirmed_findings":
            converged = stats["new_confirms"] == 0
        else:
            open_findings = conn.execute(
                "SELECT COUNT(*) AS n FROM finding WHERE campaign_id=? "
                f"AND state NOT IN ({','.join('?' * len(TERMINAL_STATES))})",
                (camp["id"], *TERMINAL_STATES),
            ).fetchone()["n"]
            converged = open_findings == 0
        outcome = "converged" if converged else "not_converged"
        summary = dict(stats, criterion=criterion)
        conn.execute(
            "UPDATE sweep_round SET status='closed', outcome=?, summary=?, "
            "dataset_run_id=?, closed_at=datetime('now') WHERE id=?",
            (outcome, json.dumps(summary), dataset_run_id, rnd["id"]),
        )
        conn.commit()
    return {"round_no": rnd["round_no"], "outcome": outcome, "summary": summary}

def sweep_metrics(campaign: str) -> dict:
    """The trust numbers: rounds run / converged-at, per-lens REFUTE and
    repair rates, NO_GO breakdown, terminal-state counts. Read-only rollup
    over existing rows."""
    camp = get_campaign(campaign)
    rounds = list_rounds(campaign)
    converged_at = next(
        (r["round_no"] for r in rounds if r["outcome"] == "converged"), None
    )
    with _connect() as conn:
        lens_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT v.lens, v.verdict, v.repair_of FROM review_lens_verdict v "
                "JOIN finding f ON f.id = v.finding_id WHERE f.campaign_id=?",
                (camp["id"],),
            ).fetchall()
        ]
        no_go = {
            r["no_go_reason"]: r["n"]
            for r in conn.execute(
                "SELECT no_go_reason, COUNT(*) AS n FROM finding "
                "WHERE campaign_id=? AND state='no_go' GROUP BY no_go_reason",
                (camp["id"],),
            ).fetchall()
        }
        states = {
            r["state"]: r["n"]
            for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM finding WHERE campaign_id=? "
                "GROUP BY state",
                (camp["id"],),
            ).fetchall()
        }
    per_lens: dict[str, dict[str, int]] = {}
    for r in lens_rows:
        d = per_lens.setdefault(
            r["lens"], {"CONFIRM": 0, "REFUTE": 0, "repairs": 0}
        )
        if r["repair_of"] is not None:
            d["repairs"] += 1
        else:
            d[r["verdict"]] += 1
    for d in per_lens.values():
        top = d["CONFIRM"] + d["REFUTE"]
        d["refute_rate"] = round(d["REFUTE"] / top, 3) if top else 0.0
        d["repair_rate"] = round(d["repairs"] / d["REFUTE"], 3) if d["REFUTE"] else 0.0
    return {
        "campaign": camp["name"],
        "kind": camp["kind"],
        "rounds_run": len(rounds),
        "converged_at_round": converged_at,
        "per_lens": per_lens,
        "no_go_breakdown": no_go,
        "state_counts": states,
    }

def add_validation_row(
    campaign: str,
    slug: str,
    *,
    red_intended: int = 0,
    red_observed: int = 0,
    green_total: int = 0,
    green_passed: int = 0,
    guards: int = 0,
    harness_notes: Optional[dict] = None,
    perf: Optional[list] = None,
) -> int:
    """Append one RED/GREEN validation run. Never a boolean: intended-failure
    fractions, guard counts, and harness fixes are recorded as-is.

    ``perf`` rows generalize the quantitative RED practice: each entry is
    ``{metric, measured, budget, direction}`` (direction 'max' means
    measured <= budget passes, 'min' the reverse). Entries are validated
    for shape here; pass/fail is derived at read time, never stored."""
    fid = _finding_id(campaign, slug)
    perf_rows = []
    for entry in perf or []:
        missing = {"metric", "measured", "budget"} - set(entry)
        if missing:
            raise ValueError(f"perf entry missing keys {sorted(missing)}: {entry!r}")
        direction = entry.get("direction", "max")
        if direction not in ("max", "min"):
            raise ValueError(f"perf direction must be 'max' or 'min', got {direction!r}")
        perf_rows.append(
            {
                "metric": str(entry["metric"]),
                "measured": float(entry["measured"]),
                "budget": float(entry["budget"]),
                "direction": direction,
            }
        )
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO validation_ledger(finding_id, red_intended, "
            "red_observed, green_total, green_passed, guards, harness_notes, "
            "perf_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fid,
                int(red_intended),
                int(red_observed),
                int(green_total),
                int(green_passed),
                int(guards),
                json.dumps(harness_notes or {}),
                json.dumps(perf_rows),
            ),
        )
        conn.commit()
        return cur.lastrowid

def perf_within_budget(entry: dict) -> bool:
    if entry["direction"] == "min":
        return entry["measured"] >= entry["budget"]
    return entry["measured"] <= entry["budget"]

def _lens_summary(verdicts: list[dict]) -> str:
    """'CONFIRM ×2', 'CONFIRM ×2 + REFUTE→repaired', 'REFUTE ×2 open', '—'."""
    top = [v for v in verdicts if v["repair_of"] is None]
    repaired_ids = {v["repair_of"] for v in verdicts if v["repair_of"] is not None}
    confirms = sum(1 for v in top if v["verdict"] == "CONFIRM")
    refutes = [v for v in top if v["verdict"] == "REFUTE"]
    repaired = sum(1 for v in refutes if v["id"] in repaired_ids)
    open_ = len(refutes) - repaired
    parts = []
    if confirms:
        parts.append(f"CONFIRM ×{confirms}")
    if repaired:
        parts.append("REFUTE→repaired" if repaired == 1 else f"REFUTE ×{repaired}→repaired")
    if open_:
        parts.append(f"REFUTE ×{open_} open")
    return " + ".join(parts) if parts else "—"

def _validation_summary(rows: list[dict]) -> str:
    """'RED 2/2 GREEN 5/5' (+ ' + 2 guards') (+ ' PERF 1/2'), from the
    LATEST run; '—' if none."""
    if not rows:
        return "—"
    v = rows[-1]
    s = (
        f"RED {v['red_observed']}/{v['red_intended']} "
        f"GREEN {v['green_passed']}/{v['green_total']}"
    )
    if v["guards"]:
        s += f" + {v['guards']} guards"
    perf = json.loads(v.get("perf_json") or "[]") if isinstance(v.get("perf_json"), str) else (v.get("perf_json") or [])
    if perf:
        ok = sum(1 for e in perf if perf_within_budget(e))
        s += f" PERF {ok}/{len(perf)}"
    return s

def campaign_ledger(campaign: str) -> dict:
    """The INDEX.md-shaped rollup: one row per finding with slug, source,
    state, root-cause one-liner, lens summary and validation summary, plus
    campaign-wide counts."""
    camp = get_campaign(campaign)
    rows = []
    with _connect() as conn:
        findings = conn.execute(
            "SELECT * FROM finding WHERE campaign_id=? ORDER BY slug",
            (camp["id"],),
        ).fetchall()
        for f in findings:
            verdicts = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM review_lens_verdict WHERE finding_id=? ORDER BY id",
                    (f["id"],),
                ).fetchall()
            ]
            validation = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM validation_ledger WHERE finding_id=? ORDER BY id",
                    (f["id"],),
                ).fetchall()
            ]
            state = f["state"]
            if state == "no_go" and f["no_go_reason"]:
                state = f"no_go ({f['no_go_reason']})"
            rows.append(
                {
                    "slug": f["slug"],
                    "source": f["source_kind"],
                    "state": state,
                    "root_cause": f["root_cause"],
                    "review": _lens_summary(verdicts),
                    "validation": _validation_summary(validation),
                }
            )
    by_state: dict[str, int] = {}
    with _connect() as conn:
        for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM finding WHERE campaign_id=? "
            "GROUP BY state",
            (camp["id"],),
        ).fetchall():
            by_state[r["state"]] = r["n"]
    return {
        "campaign": camp["name"],
        "kind": camp["kind"],
        "status": camp["status"],
        "base_commit": camp["base_commit"],
        "findings": rows,
        "counts": by_state,
    }

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="campaigns.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="apply the shared schema (idempotent)")

    sp = sub.add_parser("create-campaign")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True, choices=CAMPAIGN_KINDS)
    sp.add_argument("--objective", default="")
    sp.add_argument("--time-window", default="")
    sp.add_argument("--base-commit", default="")
    sp.add_argument("--spec-file", help="JSON SweepSpec file")
    sp = sub.add_parser("get-campaign")
    sp.add_argument("--name", required=True)
    sp = sub.add_parser("list-campaigns")
    sp.add_argument("--status", default="active")
    sp = sub.add_parser("close-campaign")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("create-finding")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--slug", required=True)
    sp.add_argument("--title", default="")
    sp.add_argument("--source-kind", default="")
    sp.add_argument("--root-cause", default="")
    sp.add_argument("--dedup-notes", default="")
    sp = sub.add_parser("get-finding")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--slug", required=True)
    sp = sub.add_parser("list-findings")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--state")
    sp = sub.add_parser("set-finding-state")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--slug", required=True)
    sp.add_argument("--state", required=True)
    sp.add_argument("--no-go-reason")

    sp = sub.add_parser("ledger")
    sp.add_argument("--campaign", required=True)

    sp = sub.add_parser("open-round")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("close-round")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--dataset-run-id", default="")
    sp = sub.add_parser("rounds")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("metrics")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("get-spec")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("ingest-from-spec")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("dispose-finding")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--slug", required=True)
    sp.add_argument("--decision", required=True, choices=DISPOSITIONS)
    sp.add_argument("--rationale", required=True)
    sp.add_argument("--no-go-reason")
    sp = sub.add_parser("dispositions")
    sp.add_argument("--campaign", required=True)
    sp = sub.add_parser("export-corpus")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--out-dir", required=True)
    sp = sub.add_parser("validate-spec")
    sp.add_argument("--spec-file", required=True)
    sp = sub.add_parser("runner-pack")
    sp.add_argument("--spec-file", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--out-dir", default=".opencode")
    sp.add_argument("--campaign")
    sp = sub.add_parser("add-lens-verdict")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--slug", required=True)
    sp.add_argument("--lens", required=True)
    sp.add_argument("--verdict", required=True, choices=VERDICTS)
    sp.add_argument("--rationale", default="")
    sp.add_argument("--repair-of", type=int)

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "init":
        init()
        print(f"schema applied at {_db_path()}")
    elif cmd == "create-campaign":
        spec = None
        if args.spec_file:
            spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
        _print(
            {
                "id": create_campaign(
                    project=args.project,
                    name=args.name,
                    kind=args.kind,
                    objective=args.objective,
                    time_window=args.time_window,
                    base_commit=args.base_commit,
                    spec=spec,
                )
            }
        )
    elif cmd == "get-campaign":
        _print(get_campaign(args.name))
    elif cmd == "list-campaigns":
        _print(list_campaigns(args.status))
    elif cmd == "close-campaign":
        close_campaign(args.name)
    elif cmd == "create-finding":
        _print(
            {
                "id": create_finding(
                    campaign=args.campaign,
                    slug=args.slug,
                    title=args.title,
                    source_kind=args.source_kind,
                    root_cause=args.root_cause,
                    dedup_notes=args.dedup_notes,
                )
            }
        )
    elif cmd == "get-finding":
        _print(get_finding(args.campaign, args.slug))
    elif cmd == "list-findings":
        _print(list_findings(args.campaign, state=args.state))
    elif cmd == "set-finding-state":
        set_finding_state(
            args.campaign, args.slug, args.state, no_go_reason=args.no_go_reason
        )
    elif cmd == "ledger":
        _print(campaign_ledger(args.campaign))
    elif cmd == "open-round":
        _print(open_round(args.campaign))
    elif cmd == "close-round":
        _print(close_round(args.campaign, dataset_run_id=args.dataset_run_id))
    elif cmd == "rounds":
        _print(list_rounds(args.campaign))
    elif cmd == "metrics":
        _print(sweep_metrics(args.campaign))
    elif cmd == "get-spec":
        spec = get_sweep_spec(args.campaign)
        _print(spec.model_dump(mode="json") if spec else None)
    elif cmd == "ingest-from-spec":
        from bin import campaign_intake

        _print(campaign_intake.ingest_from_spec(args.campaign))
    elif cmd == "dispose-finding":
        _print(
            {
                "id": dispose_finding(
                    args.campaign,
                    args.slug,
                    decision=args.decision,
                    rationale=args.rationale,
                    no_go_reason=args.no_go_reason,
                )
            }
        )
    elif cmd == "dispositions":
        _print(campaign_dispositions(args.campaign))
    elif cmd == "export-corpus":
        _print(export_corpus(args.campaign, args.out_dir))
    elif cmd == "validate-spec":
        from bin import sweep_spec as sweep_spec_mod

        validated = sweep_spec_mod.validate_spec(
            json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
        )
        _print({"kind": validated.kind, "digest": validated.digest()})
    elif cmd == "runner-pack":
        from bin import runner_pack

        _print(
            {
                "written": runner_pack.generate(
                    json.loads(Path(args.spec_file).read_text(encoding="utf-8")),
                    name=args.name,
                    out_dir=args.out_dir,
                    campaign=args.campaign,
                )
            }
        )
    elif cmd == "add-lens-verdict":
        _print(
            {
                "id": add_lens_verdict(
                    args.campaign,
                    args.slug,
                    lens=args.lens,
                    verdict=args.verdict,
                    rationale=args.rationale,
                    repair_of=args.repair_of,
                )
            }
        )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
