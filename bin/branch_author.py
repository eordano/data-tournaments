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
  in its env. Nonzero exit or an EMPTY diff raises AuthoringError — an
  authoring failure is a failure, never an empty commit.

Authoring invariants:

* Every branch is authored INDEPENDENTLY from ONE immutable base SHA in
  its own detached worktree (``git worktree add --detach``); the branch is
  created inside the worktree and the worktree is ALWAYS removed in a
  ``finally``. Candidates are NEVER merged (register_branch additionally
  rejects merge commits in base..head).
* Refuses to author onto an EXISTING branch name.
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

BACKENDS = ("fixture", "command")


class AuthoringError(RuntimeError):
    """The backend failed to produce a usable patch (nonzero exit, empty
    diff, bad config, ...). No branch is registered, no row is written."""


# ── Paths / connection (bin/campaigns.py conventions) ─────────────────────


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
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> None:
    """Apply the shared schema file. Idempotent (all DDL is IF NOT EXISTS)."""
    from bin import catalog

    catalog.init()


# ── Git plumbing (hermetic; bin/fix_branches.py conventions) ──────────────


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


def _branch_exists(repo_path: str, branch_name: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return proc.returncode == 0


# ── Backends ───────────────────────────────────────────────────────────────


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
        for relpath in sorted(self.files):  # deterministic order
            target = worktree / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.files[relpath])

    def provenance(self) -> dict:
        # Config SUMMARY: label + file list, never full content or secrets.
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
        # argv + timeout only — env values (which could carry secrets) are
        # NEVER recorded.
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


# ── Authoring ──────────────────────────────────────────────────────────────


def author_branch(
    repo_path: str,
    *,
    base_ref: str,
    branch_name: str,
    backend: str,
    backend_config: dict,
    workorder_ref: Optional[str] = None,
    finding: Optional[int] = None,
    allow_unresolved: bool = False,
) -> dict:
    """Author ONE branch from ``base_ref`` via ``backend``:

    1. resolve base_ref -> immutable base SHA; REFUSE an existing branch;
       fail-closed lineage (wave-11 W2): a provided ``workorder_ref`` must
       resolve to a pending_judgement id, finding slug, or domain name in
       this DB (ValueError otherwise; ``allow_unresolved=True`` stamps it
       'unresolved-ref:<ref>' instead), and a provided ``finding`` must
       exist;
    2. detached worktree at the base SHA, ``git checkout -b`` inside it;
    3. backend produces changes; empty diff -> AuthoringError (no empty
       commits); commit with a provenance message;
    4. register via bin.fix_branches.register_branch (SHA-bound spine row)
       and write ONE append-only branch_authoring provenance row;
    5. the worktree is ALWAYS removed in ``finally``; a failed authoring
       also deletes the just-created branch ref (nothing half-authored
       survives).

    Returns {fix_branch_id, authoring_id, base_sha, head_sha,
    patch_digest, backend}.
    """
    from bin import fix_branches

    repo_path = str(Path(repo_path).resolve())
    impl = _make_backend(backend, backend_config)

    # Lineage FIRST — fail closed before any git mutation, so a dangling
    # ref never leaves a half-authored branch behind. The resolved (and
    # possibly 'unresolved-ref:'-stamped) value is used everywhere below.
    with _connect() as conn:
        stored_ref = fix_branches._resolve_lineage(
            conn,
            workorder_ref=workorder_ref,
            finding=finding,
            allow_unresolved=allow_unresolved,
        )

    if _branch_exists(repo_path, branch_name):
        raise AuthoringError(
            f"branch {branch_name!r} already exists in {repo_path} — "
            "authoring never reuses branch names; pick a fresh one"
        )
    base_sha = _git(repo_path, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

    tmp_parent = tempfile.mkdtemp(prefix="branch-author-")
    worktree = Path(tmp_parent) / "wt"
    committed = False
    branch_created = False
    try:
        _git(repo_path, "worktree", "add", "--detach", str(worktree), base_sha)
        _git(str(worktree), "checkout", "-b", branch_name)
        branch_created = True

        impl.apply(worktree, {
            "workorder_ref": stored_ref,
            "base_sha": base_sha,
            "branch_name": branch_name,
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
            pass  # worktree may never have been created
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
    finding: Optional[int] = None,
    allow_unresolved: bool = False,
) -> list[dict]:
    """Author N candidate branches INDEPENDENTLY from the SAME immutable
    base SHA (resolved ONCE up front). Each candidate is
    ``{branch_name, backend, backend_config}``. Candidates are never
    merged — register_branch rejects merge commits in base..head, and this
    function asserts every result shares the exact same base_sha.
    """
    if not candidates:
        raise AuthoringError("author_candidates requires at least one candidate")
    repo_path = str(Path(repo_path).resolve())
    # Pin the base ONCE: every candidate is authored from this exact SHA
    # even if base_ref moves mid-run.
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
                finding=finding,
                allow_unresolved=allow_unresolved,
            )
        )
    assert all(r["base_sha"] == base_sha for r in results), (
        "candidate authored from a different base — invariant violated"
    )
    return results


# ── Queries ────────────────────────────────────────────────────────────────


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


# ── CLI (debug aid; the real entry points are the importable functions) ───


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
             "workorder_ref?, finding?}",
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
             "backend_config}, ...], workorder_ref?, finding?}",
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
                    finding=cfg.get("finding"),
                    allow_unresolved=args.allow_unresolved,
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
                    finding=cfg.get("finding"),
                    allow_unresolved=args.allow_unresolved,
                )
            )
    except (AuthoringError, ValueError, LookupError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
