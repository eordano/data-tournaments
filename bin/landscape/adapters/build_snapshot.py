"""assemble_snapshot: wire collected EvidenceRefs + RepoSnapshots into a
frozen, content-addressed LandscapeSnapshot.

Convenience only — no new contract logic. Evidence ordering / dedup is done
by LandscapeSnapshot's own validator; repos are re-wrapped into
FrozenRepoSnapshot so mutable RepoSnapshots from bin.workorder can be passed
directly.
"""
from __future__ import annotations

from typing import Iterable, Optional

from bin.landscape.adapters._text import now_iso
from bin.landscape.evidence import EvidenceRef
from bin.landscape.snapshot import FrozenRepoSnapshot, LandscapeSnapshot
from bin.workorder import RepoSnapshot


def assemble_snapshot(
    project: str,
    collected: list[EvidenceRef],
    repos: Iterable[RepoSnapshot] = (),
    *,
    created_at: Optional[str] = None,
) -> LandscapeSnapshot:
    """Build a LandscapeSnapshot from adapter output.

    ``created_at`` defaults to now (UTC ISO-8601); pass an explicit value for
    reproducible digests across runs.
    """
    frozen_repos = tuple(
        repo
        if isinstance(repo, FrozenRepoSnapshot)
        else FrozenRepoSnapshot(**repo.model_dump())
        for repo in repos
    )
    return LandscapeSnapshot(
        project=project,
        created_at=now_iso() if created_at is None else created_at,
        evidence=tuple(collected),
        repos=frozen_repos,
    )
