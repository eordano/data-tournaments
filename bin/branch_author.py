#!/usr/bin/env python3
"""WorkOrder -> branch authoring bridge (wave-10 V1).

Turns a workorder into one or more REAL fix branches, honestly labelled
by the backend that produced them:

* FixtureBackend  — deterministic file content from config (the showcase's
  FIXTURE_AUTOFIX_BACKEND made first-class): backend_config =
  ``{'files': {relpath: new_content, ...}, 'label': str}``.
* CommandBackend  — an arbitrary configured command (a real coding agent
  later): backend_config = ``{'argv': [...], 'timeout_s': int}``. The
  command runs in the worktree with WORKORDER_REF / BASE_SHA / BRANCH_NAME
  in its env, plus any validated ``author_context`` the caller carries for
  THIS item (bin/dispatch.py passes the item's identity and standing).
  Nonzero exit or an EMPTY diff raises AuthoringError — an authoring
  failure is a failure, never an empty commit.

Authoring invariants:

* Every branch is authored INDEPENDENTLY from ONE immutable base SHA in
  its own throwaway worktree; the branch ref and the worktree are created
  by ONE ``git worktree add -b``, and the worktree is ALWAYS removed in a
  ``finally``. Candidates are NEVER merged (register_branch additionally
  rejects merge commits in base..head).
* Refuses to author onto an EXISTING branch name.
* Routes by work type (docs/design/priority-tournament.md, "The handoff"):
  only AUTHORABLE_WORK_TYPES (bug-fix / feature / change-request /
  refactor) reach a backend. An ``investigation`` — or any unknown type —
  raises NotAuthorable BEFORE any git or DB mutation and is recorded in
  work_type_refusal so the item can be handed to a person. NotAuthorable is
  not an AuthoringError: a routing decision is not a failed beat.
* After the commit the branch is registered via
  bin.fix_branches.register_branch (SHA-bound spine row) and ONE
  append-only branch_authoring row binds the provenance (backend kind +
  config summary — never secrets) to the exact base/head SHAs and patch
  digest.

CLI is a debug aid mirroring the module functions:
  branch_author.py author --repo R --config CONFIG.json
  branch_author.py author-candidates --repo R --config CONFIG.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin.workorder import WORK_TYPES  # noqa: E402

BACKENDS = ("fixture", "command")

AUTHORABLE_WORK_TYPES = ("bug-fix", "feature", "change-request", "refactor")
HUMAN_WORK_TYPES = ("investigation",)

class AuthoringError(RuntimeError):
    """The backend failed to produce a usable patch (nonzero exit, empty
    diff, bad config, ...). No branch is registered, no row is written."""

class NotAuthorable(RuntimeError):
    """The work order's work_type is not mechanically authorable, so no
    backend was ever asked to try.

    Deliberately NOT an AuthoringError: an investigation is a routing
    decision, not a failed beat. Nothing is registered, nothing is
    validated, and the item keeps the standing it earned — it is handed to
    a person. ``work_type`` and ``workorder_ref`` are attributes so callers
    can route without parsing the message.
    """

    def __init__(self, work_type: str, workorder_ref: str = "", refusal_id=None):
        self.work_type = work_type
        self.workorder_ref = workorder_ref
        self.refusal_id = refusal_id
        known = work_type in WORK_TYPES
        detail = (
            f"work_type {work_type!r} is a human task"
            if known
            else f"work_type {work_type!r} is not one of {WORK_TYPES}"
        )
        super().__init__(
            f"refusing to author {workorder_ref or '(no workorder)'}: {detail}; "
            f"authorable types are {AUTHORABLE_WORK_TYPES}. Route it to a "
            "person — this is not an authoring failure."
        )

def is_authorable(work_type: Optional[str]) -> bool:
    """True when a backend may be asked for a patch. ``None`` means the
    caller did not carry a work type and routing is not being enforced."""
    return work_type is None or work_type in AUTHORABLE_WORK_TYPES

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

_WORK_TYPE_REFUSAL_DDL = """
CREATE TABLE IF NOT EXISTS work_type_refusal (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  workorder_ref TEXT,
  work_type     TEXT NOT NULL,
  repo_path     TEXT NOT NULL DEFAULT '',
  branch_name   TEXT NOT NULL DEFAULT '',
  disposition   TEXT NOT NULL DEFAULT 'route-to-human'
                CHECK (disposition IN ('route-to-human')),
  detail        TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_work_type_refusal_ref
  ON work_type_refusal(workorder_ref);
CREATE TRIGGER IF NOT EXISTS work_type_refusal_immutable
  BEFORE UPDATE ON work_type_refusal
  BEGIN SELECT RAISE(ABORT, 'work_type_refusal rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS work_type_refusal_no_delete
  BEFORE DELETE ON work_type_refusal
  BEGIN SELECT RAISE(ABORT, 'work_type_refusal rows are append-only'); END;
"""

def init() -> None:
    """Apply the shared schema file plus the work_type_refusal ledger.
    Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()
    with _connect() as conn:
        conn.executescript(_WORK_TYPE_REFUSAL_DDL)
        conn.commit()

def _record_refusal(
    work_type: str,
    *,
    workorder_ref: str = "",
    repo_path: str = "",
    branch_name: str = "",
    detail: str = "",
) -> int:
    """Append the routing decision so a refused item is visible as work for
    a person, not as a missing branch."""
    with _connect() as conn:
        conn.executescript(_WORK_TYPE_REFUSAL_DDL)
        cur = conn.execute(
            "INSERT INTO work_type_refusal(workorder_ref, work_type, "
            "repo_path, branch_name, disposition, detail) "
            "VALUES (?, ?, ?, ?, 'route-to-human', ?)",
            (workorder_ref or None, work_type, repo_path, branch_name, detail),
        )
        conn.commit()
        return cur.lastrowid

def route_to_human(
    work_type: str,
    *,
    workorder_ref: str = "",
    repo_path: str = "",
    branch_name: str = "",
    detail: str = "",
) -> int:
    """Record that this item is a person's task, without asking a backend.

    The public counterpart of the refusal ``author_branch`` raises. A caller
    that decides the destination BEFORE authoring (bin/dispatch.py, which
    reads WorkOrder.work_type off the queue) still lands the item in the one
    human ledger, so `refusals()` remains the whole answer to "what is
    waiting for a person" no matter which side made the call.
    """
    return _record_refusal(
        str(work_type),
        workorder_ref=workorder_ref,
        repo_path=repo_path,
        branch_name=branch_name,
        detail=detail or "routed to a human by the caller",
    )

def refusals(workorder_ref: Optional[str] = None) -> list[dict]:
    """Work orders refused by work-type routing, newest last."""
    with _connect() as conn:
        conn.executescript(_WORK_TYPE_REFUSAL_DDL)
        if workorder_ref is None:
            rows = conn.execute(
                "SELECT * FROM work_type_refusal ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM work_type_refusal WHERE workorder_ref=? ORDER BY id",
                (workorder_ref,),
            ).fetchall()
    return [dict(r) for r in rows]

def _route_or_refuse(
    work_type: Optional[str],
    *,
    workorder_ref: Optional[str],
    repo_path: str = "",
    branch_name: str = "",
) -> None:
    """Raise NotAuthorable (recording it first) unless a backend may try."""
    if is_authorable(work_type):
        return
    detail = (
        "human task" if work_type in WORK_TYPES else "unknown work type"
    )
    refusal_id = _record_refusal(
        str(work_type),
        workorder_ref=workorder_ref or "",
        repo_path=repo_path,
        branch_name=branch_name,
        detail=detail,
    )
    raise NotAuthorable(str(work_type), workorder_ref or "", refusal_id=refusal_id)

def _git_env() -> dict:
    """Hermetic git: no user/system config; a fixed committer identity so
    authoring works on machines without git config."""
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="branch-author",
        GIT_AUTHOR_EMAIL="branch-author@data-tournaments.invalid",
        GIT_COMMITTER_NAME="branch-author",
        GIT_COMMITTER_EMAIL="branch-author@data-tournaments.invalid",
    )
    return env

def _git(repo_path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()

_ALREADY_EXISTS = (
    "branch {branch!r} already exists in {repo} — authoring never reuses "
    "branch names; pick a fresh one"
)

THE_REF_IS_TAKEN_BY_ONE_GIT_CALL_SO_A_CONCURRENT_DISPATCHER_CANNOT_WIN_THE_GAP = (
    "'git worktree add -b' creates the branch and checks it out in a single "
    "atomic ref update. The _branch_exists probe before it exists for the "
    "readable message only: on its own it is a check-then-act race, and two "
    "dispatchers authoring the same item both passed it before either "
    "created anything."
)

def _branch_exists(repo_path: str, branch_name: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return proc.returncode == 0

RESERVED_AUTHOR_CONTEXT_KEYS = ("WORKORDER_REF", "BASE_SHA", "BRANCH_NAME")

AUTHOR_CONTEXT_ALLOWLIST = (
    "WORKORDER_DOMAIN",
    "WORKORDER_KEY",
    "WORKORDER_ITEM_ID",
    "WORKORDER_POOL_ID",
    "WORKORDER_TITLE",
    "WORKORDER_WORK_TYPE",
    "WORKORDER_RANK",
    "WORKORDER_POINTS",
    "WORKORDER_PLAYED",
)

MAX_AUTHOR_CONTEXT_ENTRIES = len(AUTHOR_CONTEXT_ALLOWLIST)
MAX_AUTHOR_CONTEXT_VALUE_CHARS = 4096

AN_AUTHOR_CONTEXT_DENYLIST_HANDS_THE_BACKEND_ITS_OWN_EXECUTION_ENVIRONMENT = (
    "a name-shape check accepts PATH, LD_PRELOAD, GIT_CONFIG_GLOBAL, "
    "GIT_SSH_COMMAND and every other UPPER_SNAKE name, so a caller could "
    "overwrite the hermetic git environment the backend runs under and "
    "redirect a bare argv[0] to a program of its choosing. The allowlist is "
    "the whole set of variables an ITEM is allowed to describe itself with; "
    "anything else is the harness's, not the item's."
)

def _validated_author_context(context: Optional[dict]) -> dict:
    """Per-item environment a backend may read, checked before it is exported.

    An ALLOWLIST, not a name-shape check: only the variables that describe
    the dispatched item may be set, values are stringified, capped, and
    rejected if they carry control characters (a newline in an exported
    variable is how one entry becomes two in anything that re-parses the
    environment). The contents are NEVER written to branch_authoring —
    provenance records the backend's config summary, and an item's context is
    not part of a config summary.
    """
    if not context:
        return {}
    if not isinstance(context, dict):
        raise AuthoringError(
            f"author_context must be a dict of environment entries, got "
            f"{type(context).__name__}"
        )
    if len(context) > MAX_AUTHOR_CONTEXT_ENTRIES:
        raise AuthoringError(
            f"author_context carries {len(context)} entries; at most "
            f"{MAX_AUTHOR_CONTEXT_ENTRIES} are allowed"
        )
    out: dict[str, str] = {}
    for key, value in context.items():
        name = str(key)
        if name in RESERVED_AUTHOR_CONTEXT_KEYS:
            raise AuthoringError(
                f"author_context key {name!r} is set by the backend itself; "
                f"reserved: {RESERVED_AUTHOR_CONTEXT_KEYS}"
            )
        if name not in AUTHOR_CONTEXT_ALLOWLIST:
            raise AuthoringError(
                f"author_context key {name!r} is not one of "
                f"{AUTHOR_CONTEXT_ALLOWLIST}. "
                f"{AN_AUTHOR_CONTEXT_DENYLIST_HANDS_THE_BACKEND_ITS_OWN_EXECUTION_ENVIRONMENT}"
            )
        text = "" if value is None else str(value)
        if len(text) > MAX_AUTHOR_CONTEXT_VALUE_CHARS:
            raise AuthoringError(
                f"author_context value for {name!r} is {len(text)} chars, "
                f"over the {MAX_AUTHOR_CONTEXT_VALUE_CHARS} cap"
            )
        bad = [c for c in text if c == "\x7f" or ord(c) < 0x20]
        if bad:
            raise AuthoringError(
                f"author_context value for {name!r} carries the control "
                f"character {bad[0]!r}; an exported variable is one line"
            )
        out[name] = text
    return out

class FixtureBackend:
    """Deterministic patch content from config: writes ``files``
    (relpath -> new content) into the worktree. This is how the showcase
    authors branches A and B (FIXTURE_AUTOFIX_BACKEND made first-class)."""

    name = "fixture"

    def __init__(self, backend_config: dict):
        files = backend_config.get("files")
        if not isinstance(files, dict) or not files:
            raise AuthoringError(
                "fixture backend requires backend_config['files'] = "
                "{relpath: new_content, ...} (non-empty dict)"
            )
        for relpath in files:
            p = Path(relpath)
            if p.is_absolute() or ".." in p.parts:
                raise AuthoringError(
                    f"fixture backend: relpath {relpath!r} escapes the worktree"
                )
        self.files = files
        self.label = str(backend_config.get("label", ""))

    def apply(self, worktree: Path, ctx: dict) -> None:
        for relpath in sorted(self.files):
            target = worktree / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.files[relpath])

    def provenance(self) -> dict:
        return {
            "backend": self.name,
            "label": self.label,
            "files": sorted(self.files),
        }

class CommandBackend:
    """Arbitrary configured command (real coding agents later): runs
    ``argv`` in the worktree with WORKORDER_REF / BASE_SHA / BRANCH_NAME in
    its env. Nonzero exit -> AuthoringError (honest failure)."""

    name = "command"

    def __init__(self, backend_config: dict):
        argv = backend_config.get("argv")
        if not isinstance(argv, list) or not argv:
            raise AuthoringError(
                "command backend requires backend_config['argv'] = [...] "
                "(non-empty list)"
            )
        self.argv = [str(a) for a in argv]
        self.timeout_s = int(backend_config.get("timeout_s", 300))

    def apply(self, worktree: Path, ctx: dict) -> None:
        env = _git_env()
        env.update(_validated_author_context(ctx.get("author_context")))
        env["WORKORDER_REF"] = str(ctx.get("workorder_ref") or "")
        env["BASE_SHA"] = ctx["base_sha"]
        env["BRANCH_NAME"] = ctx["branch_name"]
        try:
            proc = subprocess.run(
                self.argv,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise AuthoringError(
                f"command backend timed out after {self.timeout_s}s: {self.argv}"
            )
        except OSError as exc:
            raise AuthoringError(f"command backend failed to start {self.argv}: {exc}")
        if proc.returncode != 0:
            raise AuthoringError(
                f"command backend exited {proc.returncode}: {self.argv}\n"
                f"{(proc.stderr or proc.stdout).strip()}"
            )

    def provenance(self) -> dict:
        return {
            "backend": self.name,
            "argv": self.argv,
            "timeout_s": self.timeout_s,
        }

def _make_backend(backend: str, backend_config: dict):
    if backend == "fixture":
        return FixtureBackend(backend_config or {})
    if backend == "command":
        return CommandBackend(backend_config or {})
    raise AuthoringError(f"unknown backend {backend!r}; expected one of {BACKENDS}")

def author_branch(
    repo_path: str,
    *,
    base_ref: str,
    branch_name: str,
    backend: str,
    backend_config: dict,
    workorder_ref: Optional[str] = None,
    work_type: Optional[str] = None,
    finding: Optional[int] = None,
    allow_unresolved: bool = False,
    author_context: Optional[dict] = None,
) -> dict:
    """Author ONE branch from ``base_ref`` via ``backend``:

    0. route by ``work_type``: a non-authorable type (``investigation``, or
       any type outside AUTHORABLE_WORK_TYPES) raises NotAuthorable before
       any git or DB mutation, after recording a work_type_refusal row.
       ``work_type=None`` means the caller carries no work type and routing
       is not enforced;
    1. resolve base_ref -> immutable base SHA; REFUSE an existing branch;
       fail-closed lineage (wave-11 W2): a provided ``workorder_ref`` must
       resolve to a pending_judgement id, finding slug, or domain name in
       this DB (ValueError otherwise; ``allow_unresolved=True`` stamps it
       'unresolved-ref:<ref>' instead), and a provided ``finding`` must
       exist;
    2. ONE ``git worktree add -b`` creates the branch at the base SHA and
       checks it out in a throwaway worktree (atomic ref creation — see
       THE_REF_IS_TAKEN_BY_ONE_GIT_CALL_SO_A_CONCURRENT_DISPATCHER_CANNOT_WIN_THE_GAP);
    3. backend produces changes; empty diff -> AuthoringError (no empty
       commits); commit with a provenance message;
    4. register via bin.fix_branches.register_branch (SHA-bound spine row)
       and write ONE append-only branch_authoring provenance row;
    5. the worktree is ALWAYS removed in ``finally``; a failed authoring
       also deletes the just-created branch ref (nothing half-authored
       survives).

    ``author_context`` is per-item environment for the command backend (the
    caller's item identity and standing); it is validated, never recorded in
    provenance, and may not shadow WORKORDER_REF / BASE_SHA / BRANCH_NAME.

    Returns {fix_branch_id, authoring_id, base_sha, head_sha,
    patch_digest, backend}.
    """
    from bin import fix_branches

    repo_path = str(Path(repo_path).resolve())
    _route_or_refuse(
        work_type,
        workorder_ref=workorder_ref,
        repo_path=repo_path,
        branch_name=branch_name,
    )
    impl = _make_backend(backend, backend_config)
    _validated_author_context(author_context)

    with _connect() as conn:
        stored_ref = fix_branches._resolve_lineage(
            conn,
            workorder_ref=workorder_ref,
            finding=finding,
            allow_unresolved=allow_unresolved,
        )

    if _branch_exists(repo_path, branch_name):
        raise AuthoringError(_ALREADY_EXISTS.format(
            branch=branch_name, repo=repo_path))
    base_sha = _git(repo_path, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

    tmp_parent = tempfile.mkdtemp(prefix="branch-author-")
    worktree = Path(tmp_parent) / "wt"
    committed = False
    branch_created = False
    try:
        try:
            _git(repo_path, "worktree", "add", "-b", branch_name,
                 str(worktree), base_sha)
        except ValueError as exc:
            if "already exists" in str(exc):
                raise AuthoringError(_ALREADY_EXISTS.format(
                    branch=branch_name, repo=repo_path)) from None
            raise
        branch_created = True

        impl.apply(worktree, {
            "workorder_ref": stored_ref,
            "base_sha": base_sha,
            "branch_name": branch_name,
            "author_context": author_context,
        })

        _git(str(worktree), "add", "-A")
        staged = _git(str(worktree), "status", "--porcelain")
        if not staged:
            raise AuthoringError(
                f"backend {backend!r} produced an EMPTY diff for "
                f"{branch_name!r} — refusing to create an empty commit"
            )
        _git(
            str(worktree), "commit", "-m",
            f"authored by {backend} for {stored_ref or '(no workorder)'}",
        )
        head_sha = _git(str(worktree), "rev-parse", "HEAD")
        committed = True
    finally:
        try:
            _git(repo_path, "worktree", "remove", "--force", str(worktree))
        except ValueError:
            pass
        shutil.rmtree(tmp_parent, ignore_errors=True)
        if branch_created and not committed:
            try:
                _git(repo_path, "branch", "-D", branch_name)
            except ValueError:
                pass

    fix_branch_id = fix_branches.register_branch(
        repo_path,
        branch_name,
        base=base_sha,
        finding=finding,
        workorder_ref=workorder_ref,
        allow_unresolved=allow_unresolved,
    )
    patch_digest = fix_branches._patch_digest(repo_path, base_sha, head_sha)

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO branch_authoring(fix_branch_id, backend, "
            "workorder_ref, base_sha, head_sha, patch_digest, provenance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fix_branch_id,
                backend,
                stored_ref,
                base_sha,
                head_sha,
                patch_digest,
                json.dumps(impl.provenance()),
            ),
        )
        conn.commit()
        authoring_id = cur.lastrowid

    return {
        "fix_branch_id": fix_branch_id,
        "authoring_id": authoring_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "patch_digest": patch_digest,
        "backend": backend,
    }

def author_candidates(
    repo_path: str,
    *,
    base_ref: str,
    candidates: list[dict],
    workorder_ref: Optional[str] = None,
    work_type: Optional[str] = None,
    finding: Optional[int] = None,
    allow_unresolved: bool = False,
    author_context: Optional[dict] = None,
) -> list[dict]:
    """Author N candidate branches INDEPENDENTLY from the SAME immutable
    base SHA (resolved ONCE up front). Each candidate is
    ``{branch_name, backend, backend_config}``. Candidates are never
    merged — register_branch rejects merge commits in base..head, and this
    function asserts every result shares the exact same base_sha.

    Work-type routing happens ONCE for the whole batch: a non-authorable
    work order never reaches a backend, so it produces one refusal row
    rather than N of them.
    """
    if not candidates:
        raise AuthoringError("author_candidates requires at least one candidate")
    repo_path = str(Path(repo_path).resolve())
    _route_or_refuse(
        work_type, workorder_ref=workorder_ref, repo_path=repo_path
    )
    base_sha = _git(repo_path, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    results = []
    for cand in candidates:
        results.append(
            author_branch(
                repo_path,
                base_ref=base_sha,
                branch_name=cand["branch_name"],
                backend=cand["backend"],
                backend_config=cand.get("backend_config") or {},
                workorder_ref=workorder_ref,
                work_type=work_type,
                finding=finding,
                allow_unresolved=allow_unresolved,
                author_context=author_context,
            )
        )
    assert all(r["base_sha"] == base_sha for r in results), (
        "candidate authored from a different base — invariant violated"
    )
    return results

def get_authoring(fix_branch_id: int) -> list[dict]:
    """All branch_authoring rows for a fix_branch (provenance decoded)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM branch_authoring WHERE fix_branch_id=? ORDER BY id",
            (fix_branch_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("provenance"):
            d["provenance"] = json.loads(d["provenance"])
        out.append(d)
    return out

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))

def _load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="branch_author.py", description=__doc__.splitlines()[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("author", help="author ONE branch from a JSON config")
    sp.add_argument("--repo", required=True)
    sp.add_argument(
        "--config", required=True,
        help="JSON file: {base_ref, branch_name, backend, backend_config, "
             "workorder_ref?, work_type?, finding?}",
    )
    sp.add_argument(
        "--allow-unresolved", action="store_true",
        help="accept a workorder_ref that does not resolve in this DB; "
             "it is stamped 'unresolved-ref:<ref>' (exploratory use)",
    )

    sp = sub.add_parser(
        "author-candidates",
        help="author N candidates from the SAME base (JSON config)",
    )
    sp.add_argument("--repo", required=True)
    sp.add_argument(
        "--config", required=True,
        help="JSON file: {base_ref, candidates: [{branch_name, backend, "
             "backend_config}, ...], workorder_ref?, work_type?, finding?}",
    )
    sp.add_argument(
        "--allow-unresolved", action="store_true",
        help="accept a workorder_ref that does not resolve in this DB; "
             "it is stamped 'unresolved-ref:<ref>' (exploratory use)",
    )

    args = p.parse_args(argv)
    init()
    try:
        if args.cmd == "author":
            cfg = _load_config(args.config)
            _print(
                author_branch(
                    args.repo,
                    base_ref=cfg["base_ref"],
                    branch_name=cfg["branch_name"],
                    backend=cfg["backend"],
                    backend_config=cfg.get("backend_config") or {},
                    workorder_ref=cfg.get("workorder_ref"),
                    work_type=cfg.get("work_type"),
                    finding=cfg.get("finding"),
                    allow_unresolved=args.allow_unresolved,
                    author_context=cfg.get("author_context"),
                )
            )
        elif args.cmd == "author-candidates":
            cfg = _load_config(args.config)
            _print(
                author_candidates(
                    args.repo,
                    base_ref=cfg["base_ref"],
                    candidates=cfg["candidates"],
                    workorder_ref=cfg.get("workorder_ref"),
                    work_type=cfg.get("work_type"),
                    finding=cfg.get("finding"),
                    allow_unresolved=args.allow_unresolved,
                    author_context=cfg.get("author_context"),
                )
            )
    except NotAuthorable as exc:
        print(f"routed-to-human: {exc}", file=sys.stderr)
        return 3
    except (AuthoringError, ValueError, LookupError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
