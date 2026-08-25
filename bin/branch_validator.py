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
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import fix_branches  # noqa: E402
from bin.fix_branches import _git, _git_env  # noqa: E402

_LINE_RE = {
    "RED": re.compile(r"^RED\s+(\d+)/(\d+)\s*$", re.MULTILINE),
    "GREEN": re.compile(r"^GREEN\s+(\d+)/(\d+)\s*$", re.MULTILINE),
    "GUARD": re.compile(r"^GUARD\s+(\d+)/(\d+)\s*$", re.MULTILINE),
}

# Well-known harness-defining files (wave-11 B): protected whenever they
# exist at BASE. These resolve what the harness scripts actually run — a
# branch editing Cargo.toml/conftest.py/... can redirect 'cargo test' or
# 'pytest' to doctored code without ever touching the scripts themselves.
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

# Hard cap on the widened protected set. Exceeding it REFUSES validation
# (honest error) rather than silently truncating protection.
MAX_PROTECTED_PATHS = 200

# 'cargo test --test <name>' style indirection inside a protected script:
# the named target lives in a tests/ tree, not in the command tokens.
_TEST_NAME_RE = re.compile(r"--test[\s=]+['\"]?([A-Za-z0-9_.-]+)")


# ── Harness trust (wave-10 V2): the validation scripts are pinned to BASE ──


def _normalize_rel(token: str) -> Optional[str]:
    """Normalize a command token to a worktree-relative path, or None when
    it is not one (absolute paths, bare program names resolved via PATH,
    paths escaping the tree)."""
    if not token or token.startswith("-"):
        return None
    if token.startswith("/") or token.startswith("~"):
        return None  # not worktree-relative
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

    # One level of transitive scanning: paths the scripts themselves name.
    script_contents = {s: _show_at_base(repo_path, base_sha, s) for s in scripts}
    for content in script_contents.values():
        for rel in _content_path_tokens(content):
            if rel not in sources and _exists_at(repo_path, base_sha, rel):
                sources[rel] = "transitive"

    # Well-known harness-defining manifests existing at base.
    for name in DEFAULT_PROTECTED_GLOBS:
        if name not in sources and _exists_at(repo_path, base_sha, name):
            sources[name] = "manifest-glob"

    # 'cargo test --test <name>' indirection: protect the tests/ trees the
    # scripts reference, matched against the BASE tree listing only.
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


def validate(
    fix_branch_id: int,
    *,
    red_cmd: str,
    green_cmd: str,
    guard_cmd: Optional[str] = None,
    scratch_dir: Optional[str] = None,
    protected_paths: Optional[list[str]] = None,
    expected: Optional[dict[str, tuple[int, int]]] = None,
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
    """
    branch = fix_branches.get_branch(fix_branch_id)
    repo_path = branch["repo_path"]
    head_sha = branch["head_sha"]
    base_sha = branch["base_sha"]

    protected, protected_source = _discover_protected(
        repo_path, base_sha, [red_cmd, green_cmd, guard_cmd or ""],
        protected_paths,
    )

    # REFUSE BEFORE RUNNING ANYTHING: a branch that touches the harness
    # never gets its code executed.
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
        return {
            "validation_id": validation_id,
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
        # The SHA actually checked out — the binding recorded in the row.
        tested_sha = _git(str(worktree), "rev-parse", "HEAD")

        # Materialize the harness FROM BASE inside the worktree: even a
        # branch whose tampering doesn't show in the diff runs the BASE
        # scripts, never its own copies.
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
        return {
            "validation_id": validation_id,
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
