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
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ── State taxonomy (docs/reviews/bugsweep-product-model.md §1.3) ─────────

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
CAMPAIGN_KINDS = ("bugsweep", "release")


# ── Paths / connection (bin/catalog.py conventions) ──────────────────────


def _data_home() -> Path:
    return Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))


def _db_path() -> Path:
    return _data_home() / "judgements.db"


class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block also CLOSES on exit
    (see bin/catalog.py for the fd-exhaustion rationale)."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)  # commit / rollback
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # ADR 0001 §2 concurrency hygiene: wait instead of failing SQLITE_BUSY.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> None:
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()


def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict:
    d = dict(row)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d


# ── Campaign CRUD ─────────────────────────────────────────────────────────


def create_campaign(
    *,
    project: str,
    name: str,
    kind: str,
    objective: str = "",
    time_window: str = "",
    base_commit: str = "",
) -> int:
    if kind not in CAMPAIGN_KINDS:
        raise ValueError(f"kind must be one of {CAMPAIGN_KINDS}, got {kind!r}")
    from bin import catalog

    pid = catalog.get_project(project)["id"]
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO campaign(project_id, name, kind, objective, "
                "time_window, base_commit) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, name, kind, objective, time_window, base_commit),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"campaign {name!r} already exists")
        conn.commit()
        return cur.lastrowid


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
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE campaign SET status='closed' WHERE name=?", (name,)
        )
        if cur.rowcount == 0:
            raise LookupError(f"no campaign named {name!r}")
        conn.commit()


# ── Finding CRUD ──────────────────────────────────────────────────────────


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
            _row_to_dict(r, ("harness_notes",))
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
            return  # sticky terminal state
        sets = ["state=?", "no_go_reason=?", "updated_at=datetime('now')"]
        args: list[Any] = [state, no_go_reason]
        if root_cause is not None:
            sets.append("root_cause=?")
            args.append(root_cause)
        args.append(row["id"])
        conn.execute(f"UPDATE finding SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()


# ── Evidence links ────────────────────────────────────────────────────────


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


# ── Review lens verdicts (append-only; one repair cycle) ─────────────────


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

    ``repair_of`` implements the ONE-repair-cycle rule from the campaigns:
    a REFUTE may be answered by AT MOST ONE later verdict pointing back at
    it. Enforced here (not in SQL):

    * the target row must exist and belong to the same finding;
    * the target must be a REFUTE (you don't repair a CONFIRM);
    * the target must not itself be a repair (no repair-of-repair chains);
    * the target must not already have a repair (second repair raises).
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    fid = _finding_id(campaign, slug)
    with _connect() as conn:
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
            "(finding_id, lens, verdict, rationale, repair_of) "
            "VALUES (?, ?, ?, ?, ?)",
            (fid, lens, verdict, rationale, repair_of),
        )
        conn.commit()
        return cur.lastrowid


# ── Validation ledger (append-only) ──────────────────────────────────────


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
) -> int:
    """Append one RED/GREEN validation run. Never a boolean: intended-failure
    fractions, guard counts, and harness fixes are recorded as-is."""
    fid = _finding_id(campaign, slug)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO validation_ledger(finding_id, red_intended, "
            "red_observed, green_total, green_passed, guards, harness_notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fid,
                int(red_intended),
                int(red_observed),
                int(green_total),
                int(green_passed),
                int(guards),
                json.dumps(harness_notes or {}),
            ),
        )
        conn.commit()
        return cur.lastrowid


# ── Campaign ledger rollup (INDEX.md shape) ──────────────────────────────


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
    """'RED 2/2 GREEN 5/5' (+ ' + 2 guards'), from the LATEST run; '—' if none."""
    if not rows:
        return "—"
    v = rows[-1]
    s = (
        f"RED {v['red_observed']}/{v['red_intended']} "
        f"GREEN {v['green_passed']}/{v['green_total']}"
    )
    if v["guards"]:
        s += f" + {v['guards']} guards"
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


# ── CLI (debug aid; the real entry points are the importable functions) ──


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

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "init":
        init()
        print(f"schema applied at {_db_path()}")
    elif cmd == "create-campaign":
        _print(
            {
                "id": create_campaign(
                    project=args.project,
                    name=args.name,
                    kind=args.kind,
                    objective=args.objective,
                    time_window=args.time_window,
                    base_commit=args.base_commit,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
