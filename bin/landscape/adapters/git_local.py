"""git_local adapter: evidence from a local git checkout.

Everything here is system-captured git state — TIER1_SYSTEM. File excerpts
are read from the PINNED commit via ``git show <commit>:<path>`` (never the
working tree), so the excerpt always matches ``revision`` even when the
checkout is dirty.

canonical_uri scheme: ``git:<remote-or-root>#<commit>[:<path>]``.
Browsable https links (blob / commit permalinks) are derived only for
github.com remotes via ``bin.workorder.normalize_remote_url``.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt as _bounded_excerpt
from bin.landscape.adapters._text import now_iso as _now_iso
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)
from bin.workorder import RepoSnapshot, capture_repo_snapshot, normalize_remote_url

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

class GitLocalError(RuntimeError):
    """A git query needed for evidence failed (bad root, unknown commit,
    path absent at the pinned revision, …)."""

def _git(root: str, *args: str) -> str:
    """Run a git query and return stdout, raising GitLocalError on failure.

    Unlike ``bin.workorder._git`` (which returns '' for optional fields),
    evidence collection must fail loudly: a silent '' would become a
    plausible-looking empty excerpt.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitLocalError(f"git {' '.join(args)} failed in {root}: {exc}") from exc
    if proc.returncode != 0:
        raise GitLocalError(
            f"git {' '.join(args)} failed in {root}: {proc.stderr.strip()}"
        )
    return proc.stdout

def _uri_base(snap: RepoSnapshot) -> str:
    """<remote-or-root> part of the canonical uri: the normalized remote when
    one exists (stable across checkouts), else the local root."""
    return normalize_remote_url(snap.remote) or snap.remote or snap.root

def _github_base(snap: RepoSnapshot) -> str:
    """Normalized https base iff the remote lives on github.com, else ''."""
    base = normalize_remote_url(snap.remote)
    return base if base.startswith("https://github.com/") else ""

def repo_state_ref(root: str, *, why: str) -> EvidenceRef:
    """One TIER1_SYSTEM ref describing HEAD: commit, branch, remote, dirty."""
    snap = capture_repo_snapshot(root)
    if snap is None:
        raise GitLocalError(f"{root} is not inside a git repository")
    branch = _git(snap.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    excerpt = (
        f"repo={snap.remote or snap.root} branch={branch} "
        f"commit={snap.base_commit} dirty={snap.dirty}"
    )
    gh = _github_base(snap)
    link = (
        BrowsableLink(
            label=f"Commit {snap.base_commit[:12]}",
            url=f"{gh}/commit/{snap.base_commit}",
            kind="commit",
        )
        if gh and snap.base_commit
        else None
    )
    return EvidenceRef(
        source_type=SourceType.GIT_REPO,
        canonical_uri=f"git:{_uri_base(snap)}#{snap.base_commit}",
        revision=snap.base_commit,
        retrieved_at=_now_iso(),
        trust_tier=TrustTier.TIER1_SYSTEM,
        excerpt=_bounded_excerpt(excerpt),
        browsable_link=link,
        why_selected=why,
    )

def file_refs(
    root: str,
    paths: list[str],
    *,
    why: str,
    commit: str = "",
    max_chars: int = MAX_EXCERPT_CHARS,
) -> list[EvidenceRef]:
    """One ref per path, excerpt = bounded head of the file AT THE PINNED
    COMMIT (``git show <commit>:<path>``) — never the possibly-dirty tree.
    """
    snap = capture_repo_snapshot(root)
    if snap is None:
        raise GitLocalError(f"{root} is not inside a git repository")
    commit = commit or snap.base_commit
    if not commit:
        raise GitLocalError(f"no commit to pin file content to in {root}")
    max_chars = min(max_chars, MAX_EXCERPT_CHARS)
    gh = _github_base(snap)
    refs: list[EvidenceRef] = []
    for path in paths:
        content = _git(snap.root, "show", f"{commit}:{path}")
        link = (
            BrowsableLink(
                label=f"Source: {path}",
                url=f"{gh}/blob/{commit}/{path}",
                kind="source",
            )
            if gh
            else None
        )
        refs.append(
            EvidenceRef(
                source_type=SourceType.GIT_REPO,
                canonical_uri=f"git:{_uri_base(snap)}#{commit}:{path}",
                revision=commit,
                retrieved_at=_now_iso(),
                trust_tier=TrustTier.TIER1_SYSTEM,
                excerpt=_bounded_excerpt(content, max_chars),
                browsable_link=link,
                why_selected=why,
            )
        )
    return refs

def recent_commit_refs(
    root: str, *, why: str, count: int = 10
) -> list[EvidenceRef]:
    """One ref per recent commit (bounded ``count``), newest first."""
    snap = capture_repo_snapshot(root)
    if snap is None:
        raise GitLocalError(f"{root} is not inside a git repository")
    count = max(1, min(int(count), 100))
    out = _git(
        snap.root,
        "log",
        f"--max-count={count}",
        f"--format=%H{_FIELD_SEP}%an{_FIELD_SEP}%aI{_FIELD_SEP}%s{_RECORD_SEP}",
    )
    gh = _github_base(snap)
    refs: list[EvidenceRef] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, date, subject = record.split(_FIELD_SEP, 3)
        link = (
            BrowsableLink(
                label=f"Commit {sha[:12]}",
                url=f"{gh}/commit/{sha}",
                kind="commit",
            )
            if gh
            else None
        )
        refs.append(
            EvidenceRef(
                source_type=SourceType.GIT_REPO,
                canonical_uri=f"git:{_uri_base(snap)}#{sha}",
                revision=sha,
                retrieved_at=_now_iso(),
                trust_tier=TrustTier.TIER1_SYSTEM,
                excerpt=_bounded_excerpt(
                    f"commit {sha}\nauthor {author}\ndate {date}\n\n{subject}"
                ),
                browsable_link=link,
                why_selected=why,
            )
        )
    return refs

def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint.

    config: ``root`` (required), ``paths`` (optional list of repo-relative
    paths to excerpt at the pinned commit).
    limits: ``max_commits`` (default 10, 0 disables the log), ``max_chars``
    (per-file excerpt bound, clamped to MAX_EXCERPT_CHARS).
    """
    root = config.get("root", "")
    if not root:
        raise GitLocalError("git_local config requires 'root'")
    limits = limits or {}
    refs = [repo_state_ref(root, why=why)]
    paths = list(config.get("paths", []))
    if paths:
        refs.extend(
            file_refs(
                root,
                paths,
                why=why,
                max_chars=int(limits.get("max_chars", MAX_EXCERPT_CHARS)),
            )
        )
    max_commits = int(limits.get("max_commits", 10))
    if max_commits > 0:
        refs.extend(recent_commit_refs(root, why=why, count=max_commits))
    return refs
