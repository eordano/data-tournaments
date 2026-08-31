"""WorkOrder: the artifact produced by generation — a reviewable, judgeable
markdown work order instead of a bare title+body card.

Design rules (2026-08-17 user session):

* The MODEL supplies judgment: title, goal, evidence, plan, approximate
  files, self-assessed priority + rationale, work type, acceptance criteria,
  risks. ``WorkOrderDraft`` has exactly those fields, so the model cannot
  override provenance even if it tries (extra keys are ignored).
* The SYSTEM supplies provenance: domain, creation date, generation model,
  repository base commits / dirty state, source_ref, and the tournament
  ``standing`` the item earned by pairwise comparison — the only orderable
  field the implementation queue may schedule on. ``finalize_work_order``
  stamps these; links / requester / reviewers stay empty unless a human or
  an integration provides them — empty is better than hallucinated.
* Markdown is RENDERED deterministically from the structured object
  (``to_markdown``), never stored as the source of truth.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pydantic

SCHEMA_VERSION = 1

WORK_TYPES = ("bug-fix", "feature", "change-request", "refactor", "investigation")
PRIORITIES = ("P0", "P1", "P2", "P3")

class RepoSnapshot(pydantic.BaseModel):
    root: str
    remote: str = ""
    base_commit: str = ""
    dirty: bool = False

class WorkOrderLink(pydantic.BaseModel):
    """A clickable context link. System-derived or human-supplied — never
    model-invented (WorkOrderDraft has no links field)."""

    label: str
    url: str
    kind: str = "other"

    @pydantic.field_validator("url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("only https:// links are allowed")
        return v

def normalize_remote_url(remote: str) -> str:
    """Normalize a git remote to a browsable https URL ('' when impossible).

    Handles: https://host/org/repo(.git), ssh://git@host/org/repo(.git),
    git@host:org/repo(.git), and the bare scp form host:org/repo.
    """
    r = (remote or "").strip()
    if not r:
        return ""
    if r.endswith(".git"):
        r = r[: -len(".git")]
    if r.startswith("https://"):
        return r
    if r.startswith("http://"):
        return "https://" + r[len("http://"):]
    if r.startswith("ssh://"):
        rest = r[len("ssh://"):]
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        return "https://" + rest
    if r.startswith("git@"):
        r = r[len("git@"):]
    head, sep, tail = r.partition(":")
    if sep and "/" not in head and tail and not tail[0].isdigit():
        return f"https://{head}/{tail}"
    if "/" in r and "." in r.split("/", 1)[0]:
        return "https://" + r
    return ""

def derive_links(
    repos: list[RepoSnapshot], source_ref: str = ""
) -> list["WorkOrderLink"]:
    """Trustworthy links derived from system-captured git state only.

    Repository homepage for any browsable remote; commit and source-file
    permalinks only for github.com remotes (other forges use different URL
    schemes — better no link than a broken one).
    """
    links: list[WorkOrderLink] = []
    for repo in repos:
        base = normalize_remote_url(repo.remote)
        if not base:
            continue
        links.append(WorkOrderLink(label="Repository", url=base, kind="repository"))
        is_github = base.startswith("https://github.com/")
        if repo.base_commit and is_github:
            links.append(
                WorkOrderLink(
                    label=f"Base commit {repo.base_commit[:12]}",
                    url=f"{base}/commit/{repo.base_commit}",
                    kind="commit",
                )
            )
            if source_ref and repo.root:
                try:
                    rel = Path(source_ref).resolve().relative_to(
                        Path(repo.root).resolve()
                    )
                    links.append(
                        WorkOrderLink(
                            label=f"Source: {rel.name}",
                            url=f"{base}/blob/{repo.base_commit}/{rel.as_posix()}",
                            kind="source",
                        )
                    )
                except ValueError:
                    pass
        break
    return links

def _git(root: str, *args: str) -> str:
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
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""

def capture_repo_snapshot(path: str) -> Optional[RepoSnapshot]:
    """Snapshot the git repo containing ``path`` (None when not a repo).

    Captured once per generation run so every WorkOrder from that run pins
    the same base commit — the run manifest for reproducing the analysis.
    """
    toplevel = _git(str(path), "rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    return RepoSnapshot(
        root=toplevel,
        remote=_git(toplevel, "remote", "get-url", "origin"),
        base_commit=_git(toplevel, "rev-parse", "HEAD"),
        dirty=bool(_git(toplevel, "status", "--porcelain")),
    )

class TournamentStanding(pydantic.BaseModel):
    """The position an item earned by pairwise comparison, carried onto the
    work order so the implementation queue can order on it.

    System-only: ``WorkOrderDraft`` has no ``standing`` field, so a model
    cannot award itself a position. Swiss scoring
    (docs/design/priority-tournament.md): a win is 3, a draw 1, a loss 0; a
    discarded item leaves the pool instead of scoring zero, so it never
    carries a standing at all. ``pair_keys`` are the sha256 pair identities
    this item was judged under — the same keys the no-rematch rule is
    enforced with, and the join key a beat outcome is later recorded against.
    """

    points: int = 0
    played: int = 0
    rank: int = 0
    rounds: int = 0
    pool_id: str = ""
    pair_keys: list[str] = []

    @pydantic.field_validator("points", "played", "rank", "rounds")
    @classmethod
    def _non_negative(cls, v: int, info) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return v

    @pydantic.model_validator(mode="after")
    def _consistent(self) -> "TournamentStanding":
        if self.points > 3 * self.played:
            raise ValueError(
                f"points={self.points} exceeds the maximum 3 per match for "
                f"played={self.played} (a win is 3, a draw 1, a loss 0)"
            )
        if len(self.pair_keys) > self.played:
            raise ValueError(
                f"{len(self.pair_keys)} pair keys for played={self.played}; "
                "a match has at most one pair key and a bye has none"
            )
        if len(set(self.pair_keys)) != len(self.pair_keys):
            raise ValueError(
                "duplicate pair key: two items meet at most once, so a "
                "repeated pair key means a rematch was scored"
            )
        if self.rank and not self.played:
            raise ValueError(
                "a rank without a played match is a position nothing "
                "established; leave rank 0 until the item has been compared"
            )
        return self

    def summary(self) -> str:
        parts = [f"rank {self.rank}" if self.rank else "unranked"]
        parts.append(f"{self.points} pts from {self.played} played")
        if self.rounds:
            parts.append(f"{self.rounds} rounds")
        if self.pool_id:
            parts.append(f"pool {self.pool_id}")
        return " · ".join(parts)

class WorkOrderDraft(pydantic.BaseModel):
    """The model-supplied portion of a WorkOrder. No provenance fields on
    purpose: pydantic ignores unknown keys, so a model emitting ``domain`` or
    ``created_at`` cannot influence the finalized record."""

    title: str
    goal: str
    plan: str
    work_type: str = "change-request"
    priority: str = "P2"
    priority_rationale: str = ""
    evidence: str = ""
    files: list[str] = []
    acceptance_criteria: list[str] = []
    risks: list[str] = []

    @pydantic.field_validator("title")
    @classmethod
    def _bound_title(cls, v: str) -> str:
        return v.strip()[:120]

    @pydantic.field_validator("work_type")
    @classmethod
    def _known_work_type(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in WORK_TYPES else "change-request"

    @pydantic.field_validator("priority")
    @classmethod
    def _known_priority(cls, v: str) -> str:
        v = v.strip().upper()
        return v if v in PRIORITIES else "P2"

class WorkOrder(WorkOrderDraft):
    """A finalized work order: model judgment + system provenance."""

    schema_version: int = SCHEMA_VERSION
    domain: str = ""
    created_at: str = ""
    models: list[str] = []
    repos: list[RepoSnapshot] = []
    source_ref: str = ""
    links: list[WorkOrderLink] = []
    requester: str = ""
    reviewers: list[str] = []
    standing: Optional[TournamentStanding] = None

def finalize_work_order(
    draft: WorkOrderDraft,
    *,
    domain: str,
    created_at: str,
    models: list[str],
    repos: list[RepoSnapshot],
    source_ref: str = "",
    extra_links: Optional[list[WorkOrderLink]] = None,
    standing: Optional[TournamentStanding] = None,
) -> WorkOrder:
    """Stamp system provenance onto a model draft.

    Only ``WorkOrderDraft`` fields are read from the draft; provenance comes
    exclusively from the keyword arguments (i.e. from system code). Links are
    derived from the captured git state (repo / commit / source permalink);
    ``extra_links`` lets callers append human- or integration-supplied ones
    (PRs, issues, Slack, CI). ``standing`` is the tournament position the
    item earned; a work order that has not been through a tournament carries
    None rather than a fabricated zero.
    """
    return WorkOrder(
        **draft.model_dump(),
        domain=domain,
        created_at=created_at,
        models=list(models),
        repos=list(repos),
        source_ref=source_ref,
        links=derive_links(list(repos), source_ref) + list(extra_links or []),
        standing=standing,
    )

def to_markdown(wo: WorkOrder) -> str:
    """Deterministic markdown rendering (no title heading — callers display
    the title separately; repeating it here would double it in the UI).

    NO ABSOLUTE SCORE APPEARS HERE. This markdown is the body a judge is shown,
    and a judge who can see a score is comparing scores instead of items. That
    rule retired ``standing`` first; ``priority`` follows it for the identical
    reason and a worse one. Standing is at least a measurement, whereas priority
    is a self-assessed guess by a model that saw one item and could not see the
    other thirty-two -- and it rendered in the loudest colour on the page.
    Scrubbing the payload key cannot reach a score written into prose, so the
    only place this can be fixed is where the prose is composed. Both fields
    stay available to the operator views, which read the object and not this.
    """
    meta = [
        f"**Domain:** {wo.domain or '—'} · **Created:** {wo.created_at or '—'} · "
        f"**Type:** {wo.work_type}"
    ]
    if wo.links:
        meta.append(
            "**Links:** " + " · ".join(f"[{l.label}]({l.url})" for l in wo.links)
        )
    if wo.models:
        meta.append(f"**Models:** {', '.join(wo.models)}")
    for repo in wo.repos:
        commit = repo.base_commit[:12] if repo.base_commit else "unknown"
        dirty = " *(dirty working tree)*" if repo.dirty else ""
        meta.append(f"**Repo:** {repo.remote or repo.root} @ `{commit}`{dirty}")
    if wo.source_ref:
        meta.append(f"**Source:** `{wo.source_ref}`")
    if wo.files:
        meta.append("**Files (approx.):** " + ", ".join(f"`{f}`" for f in wo.files))
    if wo.requester:
        meta.append(f"**Requester:** {wo.requester}")
    if wo.reviewers:
        meta.append(f"**Reviewers:** {', '.join(wo.reviewers)}")

    sections = ["  \n".join(meta), f"## Goal\n\n{wo.goal.strip()}"]
    if wo.evidence.strip():
        sections.append(f"## Context and evidence\n\n{wo.evidence.strip()}")
    sections.append(f"## Implementation plan\n\n{wo.plan.strip()}")
    if wo.acceptance_criteria:
        bullets = "\n".join(f"- {c}" for c in wo.acceptance_criteria)
        sections.append(f"## Acceptance criteria\n\n{bullets}")
    if wo.risks:
        bullets = "\n".join(f"- {r}" for r in wo.risks)
        sections.append(f"## Risks and open questions\n\n{bullets}")
    return "\n\n".join(sections) + "\n"
