#!/usr/bin/env python3
"""Isolated per-branch validator (wave-9 B2; harness trust widened wave-11 B).

Validates ONE fix branch at ONE exact SHA in an ISOLATED detached git
worktree — never the main worktree, never a merged/aggregate tree:

1. ``git worktree add --detach <scratch>/<dir> <head_sha>`` at the
   branch's CURRENT registered head.
2. Runs the red / green / guard commands INSIDE the worktree. Output
   convention (explicit, parseable):
     red_cmd   prints ``RED <observed>/<intended>``   (intended-failure tests)
     green_cmd prints ``GREEN <passed>/<total>``
     guard_cmd prints ``GUARD <passed>/<total>``       (optional)
   passed = every present leg reports full counts (observed==intended,
   passed==total) with nonzero denominators.
3. Captures combined stdout+stderr of all legs into one log, stores it
   content-addressed via bin.catalog.cas_write (sha256 digest →
   $DATA_TOURNAMENTS_HOME/cas/sha256/...), and records the digest in
   fix_branch_validation.log_digest.
4. Writes the fix_branch_validation row with tested_sha = the worktree's
   EXACT checked-out SHA (re-read via ``git rev-parse HEAD`` in the
   worktree, not trusted from the DB), which also flips fix_branch.status
   to validated/failed.
5. ALWAYS removes the worktree (``git worktree remove --force``) in a
   finally block. Never merges anything.
6. When the work order came out of a priority tournament (a ``standing``
   is passed), appends the outcome as ranking evidence keyed to the
   item's pair_keys — the return edge of
   docs/design/priority-tournament.md. Inert by construction: it is a
   labelled example for bin/optimize.py, and promotes nothing.

   An unavailable return edge DEGRADES, it never aborts the validation.
   A byed item legitimately holds no pair key (a bye is not a result and
   awards none), so its beat outcome is recorded with
   join_status='no-pair-keys' and a join_detail saying why — honest and
   machine-readable — instead of the whole validation refusing to run.
   What is still REFUSED is a claim of ranking evidence that is not real:
   a pair key that is not a sha256 pair identity or that names no judged
   pair in this database, a standing with no pool, or a branch with no
   work order to key the outcome to.
"""
from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import NamedTuple, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import fix_branches  # noqa: E402
from bin.fix_branches import _connect, _git, _git_env  # noqa: E402
from bin.workorder import TournamentStanding  # noqa: E402

_LINE_RE = {
    "RED": re.compile(r"^RED\s+(\d+)/(\d+)\s*$", re.MULTILINE),
    "GREEN": re.compile(r"^GREEN\s+(\d+)/(\d+)\s*$", re.MULTILINE),
    "GUARD": re.compile(r"^GUARD\s+(\d+)/(\d+)\s*$", re.MULTILINE),
}

DEFAULT_PROTECTED_GLOBS = [
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain.toml",
    "build.rs",
    ".cargo/config",
    ".cargo/config.toml",
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "package-lock.json",
    "yarn.lock",
]

MAX_PROTECTED_PATHS = 200

_TEST_NAME_RE = re.compile(r"--test[\s=]+['\"]?([A-Za-z0-9_.-]+)")

def _normalize_rel(token: str) -> Optional[str]:
    """Normalize a command token to a worktree-relative path, or None when
    it is not one (absolute paths, bare program names resolved via PATH,
    paths escaping the tree)."""
    if not token or token.startswith("-"):
        return None
    if token.startswith("/") or token.startswith("~"):
        return None
    norm = posixpath.normpath(token)
    if norm.startswith("..") or norm in (".", ""):
        return None
    return norm

def _exists_at(repo_path: str, sha: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", f"{sha}:{path}"],
        capture_output=True,
        env=_git_env(),
    )
    return proc.returncode == 0

def _default_protected(
    repo_path: str, base_sha: str, cmds: list[str]
) -> list[str]:
    """The worktree-relative script paths referenced by the red/green/guard
    commands: every shell token that normalizes to a relative path AND
    exists in the BASE tree ('./guard.sh', 'sh scripts/check.sh', ...).
    Tokens that are PATH programs ('true', 'python3') or absolute paths are
    not the branch's to tamper with and are skipped."""
    protected: list[str] = []
    for cmd in cmds:
        if not cmd:
            continue
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        for token in tokens:
            rel = _normalize_rel(token)
            if rel and rel not in protected and _exists_at(repo_path, base_sha, rel):
                protected.append(rel)
    return protected

def _show_at_base(repo_path: str, base_sha: str, path: str) -> str:
    """The file's content AT BASE (git show base:path), decoded leniently.
    Empty string when unreadable — discovery is best-effort per file."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "show", f"{base_sha}:{path}"],
        capture_output=True,
        env=_git_env(),
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")

def _base_tree_paths(repo_path: str, base_sha: str) -> list[str]:
    """All paths in the BASE tree (git ls-tree -r base --name-only) — the
    only tree glob patterns are ever matched against. The candidate's head
    never influences discovery."""
    out = _git(repo_path, "ls-tree", "-r", base_sha, "--name-only")
    return out.splitlines()

def _content_path_tokens(content: str):
    """Candidate worktree-relative path tokens inside a script's content:
    tokenize on whitespace/quotes/'=' (which also strips '--flag=path'
    prefixes), keep tokens containing '/' or a file extension, normalize.
    Yields normalized relative paths (existence is checked by the caller)."""
    for token in re.split(r"[\s'\"=;|&()<>`]+", content):
        if not token:
            continue
        if "/" not in token and not posixpath.splitext(token)[1]:
            continue
        rel = _normalize_rel(token)
        if rel is not None:
            yield rel

def _discover_protected(
    repo_path: str,
    base_sha: str,
    cmds: list[str],
    explicit: Optional[list[str]],
) -> tuple[list[str], dict[str, str]]:
    """The widened protected set (wave-11 B) and WHY each path is in it.

    Everything is BASE-enumerated — the candidate's head never influences
    discovery. Sources (first match wins):
      'script'        — worktree-relative script paths in the cmd tokens.
      'transitive'    — real base paths named INSIDE those scripts (one
                        level: 'python3 inner_test.py' in red.sh protects
                        inner_test.py).
      'manifest-glob' — DEFAULT_PROTECTED_GLOBS files existing at base,
                        plus tests/<name>.rs / **/tests/<name>.rs matches
                        for every '--test <name>' a script mentions
                        (fnmatch against git ls-tree of BASE).
      'explicit'      — caller-supplied protected_paths.

    Raises ValueError when the widened set exceeds MAX_PROTECTED_PATHS —
    an honest refusal, never a silent truncation."""
    sources: dict[str, str] = {}

    scripts = _default_protected(repo_path, base_sha, cmds)
    for path in scripts:
        sources[path] = "script"

    script_contents = {s: _show_at_base(repo_path, base_sha, s) for s in scripts}
    for content in script_contents.values():
        for rel in _content_path_tokens(content):
            if rel not in sources and _exists_at(repo_path, base_sha, rel):
                sources[rel] = "transitive"

    for name in DEFAULT_PROTECTED_GLOBS:
        if name not in sources and _exists_at(repo_path, base_sha, name):
            sources[name] = "manifest-glob"

    test_names: list[str] = []
    for content in script_contents.values():
        test_names.extend(_TEST_NAME_RE.findall(content))
    if test_names:
        tree = _base_tree_paths(repo_path, base_sha)
        for name in test_names:
            for pattern in (f"tests/{name}.rs", f"*/tests/{name}.rs"):
                for path in tree:
                    if path not in sources and fnmatch.fnmatch(path, pattern):
                        sources[path] = "manifest-glob"

    for extra in explicit or []:
        rel = _normalize_rel(extra)
        if rel is None:
            raise ValueError(
                f"protected path {extra!r} is not worktree-relative"
            )
        if rel not in sources:
            sources[rel] = "explicit"

    if len(sources) > MAX_PROTECTED_PATHS:
        raise ValueError(
            f"protected set has {len(sources)} paths, exceeding the cap of "
            f"{MAX_PROTECTED_PATHS}; refusing to validate rather than "
            "silently truncate harness protection"
        )
    return sorted(sources), sources

def _tampered_paths(
    repo_path: str, base_sha: str, head_sha: str, protected: list[str]
) -> list[str]:
    """Protected paths touched anywhere in base..head (git diff --name-only
    intersected with the protected set)."""
    out = _git(repo_path, "diff", "--name-only", f"{base_sha}..{head_sha}")
    changed = set(out.splitlines())
    return sorted(p for p in protected if p in changed)

def _harness_digest(repo_path: str, base_sha: str, protected: list[str]) -> str:
    """sha256 over the protected files' contents AT BASE (git show
    base:path), path-labelled and order-independent (sorted)."""
    h = hashlib.sha256()
    for path in sorted(protected):
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "show", f"{base_sha}:{path}"],
            capture_output=True,
            env=_git_env(),
        )
        if proc.returncode != 0:
            raise ValueError(
                f"cannot read {path!r} at base {base_sha[:12]}: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )
        h.update(path.encode("utf-8") + b"\0")
        h.update(proc.stdout)
        h.update(b"\0")
    return h.hexdigest()

def _run_leg(cmd: str, cwd: Path) -> tuple[str, int]:
    """Run one leg via the shell in the worktree; return (combined output,
    exit code). Output is captured, never streamed."""
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    out = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    return out, proc.returncode

def _parse_counts(kind: str, output: str) -> Optional[tuple[int, int]]:
    """Parse the LAST 'KIND <a>/<b>' line from a leg's output, or None."""
    matches = _LINE_RE[kind].findall(output)
    if not matches:
        return None
    a, b = matches[-1]
    return int(a), int(b)

def _store_log(text: str) -> str:
    """Content-address the combined run log; return the sha256 digest."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    from bin import catalog

    catalog.cas_write(digest, text)
    return digest

_RANKING_EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS ranking_evidence (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  pool_id       TEXT    NOT NULL,
  workorder_ref TEXT    NOT NULL,
  fix_branch_id INTEGER NOT NULL REFERENCES fix_branch(id),
  validation_id INTEGER NOT NULL REFERENCES fix_branch_validation(id),
  tested_sha    TEXT    NOT NULL,
  outcome       TEXT    NOT NULL CHECK (outcome IN ('passed','failed','refused')),
  join_status   TEXT    NOT NULL DEFAULT 'joined'
                CHECK (join_status IN ('joined','no-pair-keys')),
  join_detail   TEXT    NOT NULL DEFAULT '',
  points        INTEGER NOT NULL,
  played        INTEGER NOT NULL,
  rank          INTEGER NOT NULL,
  rounds        INTEGER NOT NULL,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ranking_evidence_pool
  ON ranking_evidence(pool_id);
CREATE TRIGGER IF NOT EXISTS ranking_evidence_immutable
  BEFORE UPDATE ON ranking_evidence
  BEGIN SELECT RAISE(ABORT, 'ranking_evidence rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ranking_evidence_no_delete
  BEFORE DELETE ON ranking_evidence
  BEGIN SELECT RAISE(ABORT, 'ranking_evidence rows are append-only'); END;

CREATE TABLE IF NOT EXISTS ranking_evidence_pair (
  evidence_id INTEGER NOT NULL REFERENCES ranking_evidence(id),
  pair_key    TEXT    NOT NULL,
  PRIMARY KEY (evidence_id, pair_key)
);
CREATE INDEX IF NOT EXISTS idx_ranking_evidence_pair_key
  ON ranking_evidence_pair(pair_key);
CREATE TRIGGER IF NOT EXISTS ranking_evidence_pair_immutable
  BEFORE UPDATE ON ranking_evidence_pair
  BEGIN SELECT RAISE(ABORT, 'ranking_evidence_pair rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ranking_evidence_pair_no_delete
  BEFORE DELETE ON ranking_evidence_pair
  BEGIN SELECT RAISE(ABORT, 'ranking_evidence_pair rows are append-only'); END;
"""

JOIN_JOINED = "joined"
JOIN_NO_PAIR_KEYS = "no-pair-keys"
JOIN_STATUSES = (JOIN_JOINED, JOIN_NO_PAIR_KEYS)

_PAIR_KEY_RE = re.compile(r"[0-9a-f]{64}")

A_PAIR_KEY_IS_MATCHED_WHOLE_BECAUSE_A_TRAILING_NEWLINE_IS_NOT_A_DIGEST = (
    "re.match with a '$' anchor accepts a trailing newline, so "
    "'<64 hex>\\n' passed the shape check and landed in a table whose "
    "UPDATE and DELETE triggers RAISE(ABORT) -- permanently, and joining "
    "against nothing. fullmatch is the whole check."
)

_PAIR_KEY_SOURCES = (
    ("pending_judgement", "SELECT 1 FROM pending_judgement WHERE pair_key=?"),
    ("score", "SELECT 1 FROM score WHERE pair_key=?"),
    ("work_dispatch_pair",
     "SELECT 1 FROM work_dispatch_pair WHERE pair_key=?"),
)

SHAPE_IS_NOT_EXISTENCE_A_WELL_FORMED_KEY_STILL_HAS_TO_NAME_A_JUDGED_PAIR = (
    "any 64 hex characters look exactly like a pair key, so a shape check "
    "alone records evidence 'joined' to a comparison nobody ever made. The "
    "key must appear where judged pairs are recorded: on the pending row or "
    "score row of the judgement itself, or in the dispatch ledger's "
    "work_dispatch_pair, which is where the pairs behind a dispatched "
    "item's standing are written at claim time."
)

_MIGRATIONS = (
    ("join_status",
     "ALTER TABLE ranking_evidence ADD COLUMN join_status TEXT NOT NULL "
     "DEFAULT 'joined' CHECK (join_status IN ('joined','no-pair-keys'))"),
    ("join_detail",
     "ALTER TABLE ranking_evidence ADD COLUMN join_detail TEXT NOT NULL "
     "DEFAULT ''"),
)

def _ensure_schema(conn) -> None:
    """Apply the ranking-evidence DDL, then add any column a DB created by
    an older revision predates. Rows written before join_status existed
    carried pair keys by construction (an empty set aborted the whole
    validation back then), so 'joined' is the honest backfill default."""
    conn.executescript(_RANKING_EVIDENCE_DDL)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ranking_evidence)")
    }
    for column, ddl in _MIGRATIONS:
        if column not in columns:
            conn.execute(ddl)

def _coerce_standing(standing) -> TournamentStanding:
    if isinstance(standing, TournamentStanding):
        return standing
    return TournamentStanding(**dict(standing))

class ReturnEdge(NamedTuple):
    """How a beat outcome will key back to the judgements that ranked the
    item: the standing it entered implementation with, the work order it
    belongs to, and whether the join is available at all."""

    standing: TournamentStanding
    workorder_ref: str
    join_status: str
    join_detail: str

    @property
    def joinable(self) -> bool:
        return self.join_status == JOIN_JOINED

def _unrecorded_pair_keys(conn, keys) -> list[str]:
    """The keys that appear nowhere a judged pair is recorded."""
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = []
    for key in keys:
        found = False
        for table, sql in _PAIR_KEY_SOURCES:
            if table in tables and conn.execute(sql, (key,)).fetchone():
                found = True
                break
        if not found:
            missing.append(key)
    return missing

def _check_pair_keys(standing: TournamentStanding, conn=None) -> None:
    """REFUSE pair keys that are not real.

    A pair key is ``sha256(item_a || item_b || rubric_id || rubric_version)``
    over a comparison somebody actually made, so a key is refused when it is
    not a 64-char lowercase hex digest AND when it is one that names no
    judged pair in this database. Both are claims of evidence rather than
    evidence. Holding NO pair key is a different thing entirely and is not
    checked here — that degrades, see join_status.
    """
    bad = [key for key in standing.pair_keys if not _PAIR_KEY_RE.fullmatch(key)]
    if bad:
        raise ValueError(
            f"pair key {bad[0]!r} is not a sha256 pair identity; ranking "
            "evidence claiming a judged pair that cannot exist is refused "
            "(an item holding no pair key at all degrades instead of "
            f"refusing — see join_status). "
            f"{A_PAIR_KEY_IS_MATCHED_WHOLE_BECAUSE_A_TRAILING_NEWLINE_IS_NOT_A_DIGEST}"
        )
    if conn is None:
        with _connect() as own:
            missing = _unrecorded_pair_keys(own, standing.pair_keys)
    else:
        missing = _unrecorded_pair_keys(conn, standing.pair_keys)
    if missing:
        raise ValueError(
            f"pair key {missing[0]!r} names no judged pair in this database; "
            "ranking evidence is refused rather than recorded as 'joined'. "
            f"{SHAPE_IS_NOT_EXISTENCE_A_WELL_FORMED_KEY_STILL_HAS_TO_NAME_A_JUDGED_PAIR}"
        )

def _join_state(standing: TournamentStanding) -> tuple[str, str]:
    """The join status of an outcome recorded against this standing, and
    the honest reason when there is nothing to join to."""
    if standing.pair_keys:
        return JOIN_JOINED, ""
    if standing.played:
        return JOIN_NO_PAIR_KEYS, (
            f"standing played {standing.played} match(es) and holds no pair "
            "key: a bye is not a result and awards none, so this outcome "
            "cannot be joined back to any judgement"
        )
    return JOIN_NO_PAIR_KEYS, (
        "standing played no match: the item reached implementation without "
        "a pairwise comparison, so this outcome cannot be joined back to "
        "any judgement"
    )

def _check_return_edge(branch: dict, standing) -> ReturnEdge:
    """Resolve the join back to the tournament BEFORE anything runs.

    An UNAVAILABLE join degrades: a standing with no pair keys yields
    join_status='no-pair-keys', and validation proceeds — a byed item is a
    perfectly valid item, and losing its whole validation over a missing
    evidence row was the bug this replaced.

    A join that is WRONG still refuses, because a beat outcome keyed to a
    pair nobody judged is worse than no evidence: a pair key that is
    malformed OR that names no judged pair in this database, a standing
    with no pool to scope the rubric it grades, or a branch with no work
    order to name the item.
    """
    parsed = _coerce_standing(standing)
    if not parsed.pool_id:
        raise ValueError(
            "ranking evidence needs standing.pool_id: the return edge is "
            "scoped to the pool whose rubric it grades"
        )
    _check_pair_keys(parsed)
    workorder_ref = (branch.get("workorder_ref") or "").strip()
    if not workorder_ref:
        raise ValueError(
            f"fix_branch {branch['id']} has no workorder_ref; a tournament "
            "item's beat outcome must be joinable back to its work order"
        )
    status, detail = _join_state(parsed)
    return ReturnEdge(parsed, workorder_ref, status, detail)

def record_ranking_evidence(
    fix_branch_id: int,
    *,
    validation_id: int,
    tested_sha: str,
    outcome: str,
    standing,
    workorder_ref: str,
) -> int:
    """Append ONE beat outcome as ranking evidence, keyed to the pairs.

    The row carries the standing the item entered implementation with; one
    ranking_evidence_pair row per pair_key joins it back to the judgements
    that produced that standing. Append-only, and inert: nothing here
    promotes a rubric — bin/optimize.py's gate is unchanged and reads this
    only as labelled examples.

    join_status is DERIVED from the standing, never asserted by the
    caller: 'joined' when the item holds pair keys, 'no-pair-keys' when it
    does not, with join_detail carrying the reason in words. A byed item
    still gets its row — the outcome is real, only the join is missing,
    and saying so is what makes the gap machine-readable instead of
    invisible.
    """
    parsed = _coerce_standing(standing)
    if outcome not in ("passed", "failed", "refused"):
        raise ValueError(
            f"outcome {outcome!r} is not one of passed/failed/refused"
        )
    join_status, join_detail = _join_state(parsed)
    with _connect() as conn:
        _ensure_schema(conn)
        _check_pair_keys(parsed, conn)
        cur = conn.execute(
            "INSERT INTO ranking_evidence(pool_id, workorder_ref, "
            "fix_branch_id, validation_id, tested_sha, outcome, join_status, "
            "join_detail, points, played, rank, rounds) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                parsed.pool_id,
                workorder_ref,
                fix_branch_id,
                validation_id,
                tested_sha,
                outcome,
                join_status,
                join_detail,
                parsed.points,
                parsed.played,
                parsed.rank,
                parsed.rounds,
            ),
        )
        evidence_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO ranking_evidence_pair(evidence_id, pair_key) "
            "VALUES (?, ?)",
            [(evidence_id, key) for key in parsed.pair_keys],
        )
        conn.commit()
    return evidence_id

def _evidence_dict(row: dict, pair_keys: list[str]) -> dict:
    """One labelled example in the shape bin/optimize.py consumes: the
    pair keys to join on, the verdict, the standing that verdict grades,
    and the provenance (work order, branch, validation, tested SHA) to
    join through. ``join_status``/``join_detail`` say in the row itself
    whether the pair-key join exists — an example carrying no keys is
    still a real outcome, just one no judgement can be blamed for."""
    return {
        "workorder_ref": row["workorder_ref"],
        "standing": {
            "points": row["points"],
            "played": row["played"],
            "rank": row["rank"],
            "rounds": row["rounds"],
            "pool_id": row["pool_id"],
            "pair_keys": pair_keys,
        },
        "outcome": row["outcome"],
        "pair_keys": pair_keys,
        "pool_id": row["pool_id"],
        "join_status": row["join_status"],
        "join_detail": row["join_detail"],
        "joinable": row["join_status"] == JOIN_JOINED,
        "evidence_id": row["id"],
        "validation_id": row["validation_id"],
        "fix_branch_id": row["fix_branch_id"],
        "tested_sha": row["tested_sha"],
        "created_at": row["created_at"],
    }

def ranking_evidence(evidence_id: int) -> Optional[dict]:
    """ONE recorded beat outcome as a labelled example, or None. This is
    what ``validate`` hands back inline so a consumer never has to re-key
    the outcome to the tournament by hand."""
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM ranking_evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if row is None:
            return None
        pairs = [
            r["pair_key"]
            for r in conn.execute(
                "SELECT pair_key FROM ranking_evidence_pair "
                "WHERE evidence_id=? ORDER BY pair_key",
                (evidence_id,),
            ).fetchall()
        ]
    return _evidence_dict(dict(row), pairs)

def ranking_evidence_for_pool(pool_id: str) -> list[dict]:
    """The (item, standing, beat outcome) triples recorded for one pool,
    ordered by rank then work order. Each triple carries the pair_keys the
    item's standing was earned on, so a rubric revision can find exactly
    which judgements a failed beat calls into question — and a
    join_status saying so when there are none to find."""
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM ranking_evidence WHERE pool_id=? "
            "ORDER BY rank, workorder_ref, id",
            (pool_id,),
        ).fetchall()
        pairs: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT p.evidence_id, p.pair_key FROM ranking_evidence_pair p "
            "JOIN ranking_evidence e ON e.id = p.evidence_id "
            "WHERE e.pool_id=? ORDER BY p.pair_key",
            (pool_id,),
        ).fetchall():
            pairs.setdefault(row["evidence_id"], []).append(row["pair_key"])
    return [
        _evidence_dict(dict(row), pairs.get(row["id"], [])) for row in rows
    ]

def evidence_for_pair(pair_key: str) -> list[dict]:
    """Every beat outcome recorded against one pair key — the lookup a
    judgement uses to ask what happened to the items it compared."""
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT e.* FROM ranking_evidence e "
            "JOIN ranking_evidence_pair p ON p.evidence_id = e.id "
            "WHERE p.pair_key=? ORDER BY e.id",
            (pair_key,),
        ).fetchall()
    return [dict(r) for r in rows]

def validate(
    fix_branch_id: int,
    *,
    red_cmd: str,
    green_cmd: str,
    guard_cmd: Optional[str] = None,
    scratch_dir: Optional[str] = None,
    protected_paths: Optional[list[str]] = None,
    expected: Optional[dict[str, tuple[int, int]]] = None,
    standing=None,
) -> dict:
    """Validate a fix branch at its CURRENT head in an isolated worktree.

    Returns {'validation_id', 'tested_sha', 'passed', 'status',
    'log_digest', 'harness_digest', 'protected_paths', 'protected_source',
    counts...}. The fix_branch row flips to validated/failed; the worktree
    is ALWAYS removed.

    HARNESS TRUST (wave-10 V2, widened wave-11 B): the validation harness
    is pinned to the branch's registered BASE — a candidate branch cannot
    grade itself. The protected set is BASE-enumerated (the candidate's
    head never influences discovery) and unions four sources, recorded in
    'protected_source' ({path: source}) so evidence packages can show WHY
    each path is protected:
      'script'        — worktree-relative script paths in the cmd tokens
                        that exist at base.
      'transitive'    — one level deeper: real base paths named INSIDE
                        those scripts ('python3 inner_test.py' protects
                        inner_test.py).
      'manifest-glob' — DEFAULT_PROTECTED_GLOBS harness manifests existing
                        at base (Cargo.toml, conftest.py, pyproject.toml,
                        ...), plus tests/<name>.rs / **/tests/<name>.rs
                        base matches for every '--test <name>' a script
                        mentions.
      'explicit'      — caller-supplied ``protected_paths``.
    The widened set is capped at MAX_PROTECTED_PATHS (200): exceeding it
    raises ValueError — an honest refusal, never a silent truncation.

    The whole widened set feeds the same three mechanisms:
    * If base..head touches ANY protected path, validation is REFUSED
      before any candidate code runs: a passed=0 row is written whose log
      starts with 'HARNESS-TAMPERED: <files>' and the branch fails.
    * Otherwise the protected files are materialized FROM BASE into the
      worktree (git checkout base_sha -- ...) so even tricks that don't
      show in the diff cannot swap the harness at run time, and
      harness_digest = sha256 over their base contents is recorded.

    EXPECTED-COUNT PINNING (wave-11 B): ``expected`` optionally pins the
    exact counters per leg, e.g. ``{'red': (2, 2), 'green': (5, 5),
    'guard': (3, 3)}`` (lowercase keys; any subset of legs). When a pinned
    leg's parsed counters differ, the leg FAILS and the log carries a
    'COUNTER-MISMATCH' line — a tampered inner test source that changes
    totals is caught even when percentages look right. This is a generic
    line-convention check; full cargo-JSON test-report parsing is out of
    scope for this validator.

    THE RETURN EDGE (docs/design/priority-tournament.md): when ``standing``
    is given — a TournamentStanding, or a dict of one — the outcome is also
    appended as ranking evidence keyed to the item's pair_keys, so a
    ranking that sent a doomed item to the top is a labelled example rather
    than a lost beat. The returned dict gains 'ranking_evidence_id',
    'ranking_evidence' (the whole labelled example: pair keys, verdict,
    standing and provenance) and 'ranking_evidence_join'.

    An UNAVAILABLE join degrades and never aborts: an item with no pair
    keys — a bye awards none — is still validated, and its evidence row
    carries join_status='no-pair-keys' plus a join_detail saying why no
    judgement can be joined to it. A join that is WRONG still raises
    ValueError before anything runs: a pair key that is not a sha256 pair
    identity or that names no judged pair in this database, a standing with
    no pool_id, or a branch with no workorder_ref. The evidence is inert either way: no rubric is promoted
    by it, and the promotion gate in bin/optimize.py is untouched.
    """
    branch = fix_branches.get_branch(fix_branch_id)
    repo_path = branch["repo_path"]
    head_sha = branch["head_sha"]
    base_sha = branch["base_sha"]
    edge = None
    if standing is not None:
        edge = _check_return_edge(branch, standing)

    protected, protected_source = _discover_protected(
        repo_path, base_sha, [red_cmd, green_cmd, guard_cmd or ""],
        protected_paths,
    )

    tampered = _tampered_paths(repo_path, base_sha, head_sha, protected)
    if tampered:
        log_text = (
            f"HARNESS-TAMPERED: {', '.join(tampered)}\n"
            f"base_sha={base_sha}\nhead_sha={head_sha}\n"
            f"protected={sorted(protected)}\n"
            "validation REFUSED before execution — the branch modifies "
            "protected harness files; no candidate code was run.\n"
        )
        log_digest = _store_log(log_text)
        validation_id = fix_branches.record_validation(
            fix_branch_id,
            head_sha,
            passed=False,
            red_cmd=red_cmd,
            green_cmd=green_cmd,
            log_digest=log_digest,
        )
        evidence_id = None
        if edge is not None:
            evidence_id = record_ranking_evidence(
                fix_branch_id,
                validation_id=validation_id,
                tested_sha=head_sha,
                outcome="refused",
                standing=edge.standing,
                workorder_ref=edge.workorder_ref,
            )
        return {
            "validation_id": validation_id,
            "ranking_evidence_id": evidence_id,
            "ranking_evidence": (
                ranking_evidence(evidence_id) if evidence_id else None
            ),
            "ranking_evidence_join": edge.join_status if edge else None,
            "tested_sha": head_sha,
            "passed": False,
            "status": "failed",
            "refused": "harness-tampered",
            "tampered_paths": tampered,
            "protected_paths": sorted(protected),
            "protected_source": dict(protected_source),
            "harness_digest": None,
            "log_digest": log_digest,
            "red": None,
            "green": None,
            "guard": None,
        }

    harness_digest = _harness_digest(repo_path, base_sha, protected)

    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(
        prefix="branch-validator-"))
    scratch.mkdir(parents=True, exist_ok=True)
    worktree = scratch / f"wt-{fix_branch_id}-{uuid.uuid4().hex[:8]}"

    _git(repo_path, "worktree", "add", "--detach", str(worktree), head_sha)
    try:
        tested_sha = _git(str(worktree), "rev-parse", "HEAD")

        if protected:
            _git(str(worktree), "checkout", base_sha, "--", *protected)

        log_parts: list[str] = [
            f"HARNESS-DIGEST: sha256:{harness_digest} "
            f"(base {base_sha[:12]}, protected {sorted(protected)})"
        ]
        legs: dict[str, Optional[tuple[int, int]]] = {}
        counter_ok: dict[str, bool] = {}
        for kind, cmd in (("RED", red_cmd), ("GREEN", green_cmd),
                          ("GUARD", guard_cmd)):
            if cmd is None:
                continue
            out, rc = _run_leg(cmd, worktree)
            log_parts.append(
                f"===== {kind} leg: {cmd!r} (exit {rc}) =====\n{out}"
            )
            counts = _parse_counts(kind, out)
            legs[kind] = counts
            pinned = (expected or {}).get(kind.lower())
            if pinned is not None and counts != tuple(pinned):
                counter_ok[kind] = False
                log_parts.append(
                    f"COUNTER-MISMATCH: {kind} expected "
                    f"{pinned[0]}/{pinned[1]}, got "
                    f"{'none' if counts is None else f'{counts[0]}/{counts[1]}'}"
                    " — leg fails despite any full-count appearance."
                )
            else:
                counter_ok[kind] = True

        def _full(counts: Optional[tuple[int, int]]) -> bool:
            return counts is not None and counts[1] > 0 and counts[0] == counts[1]

        def _leg_ok(kind: str) -> bool:
            return _full(legs.get(kind)) and counter_ok.get(kind, True)

        passed = _leg_ok("RED") and _leg_ok("GREEN") and (
            guard_cmd is None or _leg_ok("GUARD")
        )

        log_text = "\n".join(log_parts)
        log_digest = _store_log(log_text)

        red = legs.get("RED") or (None, None)
        green = legs.get("GREEN") or (None, None)
        guard = legs.get("GUARD") or (None, None)
        validation_id = fix_branches.record_validation(
            fix_branch_id,
            tested_sha,
            passed=passed,
            red_cmd=red_cmd,
            red_observed=red[0],
            red_intended=red[1],
            green_cmd=green_cmd,
            green_passed=green[0],
            green_total=green[1],
            guard_passed=guard[0],
            guard_total=guard[1],
            log_digest=log_digest,
        )
        evidence_id = None
        if edge is not None:
            evidence_id = record_ranking_evidence(
                fix_branch_id,
                validation_id=validation_id,
                tested_sha=tested_sha,
                outcome="passed" if passed else "failed",
                standing=edge.standing,
                workorder_ref=edge.workorder_ref,
            )
        return {
            "validation_id": validation_id,
            "ranking_evidence_id": evidence_id,
            "ranking_evidence": (
                ranking_evidence(evidence_id) if evidence_id else None
            ),
            "ranking_evidence_join": edge.join_status if edge else None,
            "tested_sha": tested_sha,
            "passed": passed,
            "status": "validated" if passed else "failed",
            "log_digest": log_digest,
            "harness_digest": harness_digest,
            "protected_paths": sorted(protected),
            "protected_source": dict(protected_source),
            "red": legs.get("RED"),
            "green": legs.get("GREEN"),
            "guard": legs.get("GUARD"),
        }
    finally:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force",
             str(worktree)],
            capture_output=True,
            env=_git_env(),
        )
