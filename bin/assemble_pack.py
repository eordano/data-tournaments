#!/usr/bin/env python3
"""Pack-assembly pipeline: project -> evidence -> snapshot -> role packs.

Reads a project and its active sources from the catalog, dispatches each
source to the adapter registry (git sources -> git_local; sources whose kind
has no usable adapter are SKIPPED with an explicit note — never silently),
assembles an immutable LandscapeSnapshot, then persists everything through
bin.catalog in citation order: evidence refs (with their source_id) ->
snapshot row -> snapshot_evidence links -> one ContextPack per requested
role (build_pack is the only sanctioned projection).

Idempotent by construction: all artifacts are content-addressed and the
catalog inserts are duplicate-tolerant, so re-assembling identical content
reuses the same digests without duplicating rows.

CLI (mirrors bin/domains.py argparse conventions; machine line follows the
DRAFT_JSON convention of domain_builder_cli.py):

    python3 bin/assemble_pack.py --project NAME --objective TEXT \\
        [--roles creator,judge] [--limit-files N] [--max-commits N]

Prints a human summary, then a final ``PACK_JSON: {...}`` line.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import catalog  # noqa: E402
from bin.landscape import (  # noqa: E402
    ContextPack,
    EvidenceRef,
    LandscapeSnapshot,
    Role,
    build_pack,
)
from bin.landscape.adapters import assemble_snapshot, get_adapter  # noqa: E402
from bin.workorder import capture_repo_snapshot  # noqa: E402

_KIND_TO_ADAPTER = {
    "git": "git_local",
    "git_local": "git_local",
    "github": "github_api",
    "github_api": "github_api",
}

def _frozen_refs_for_source(source: dict) -> list[EvidenceRef]:
    """Rebuild EvidenceRef models from a source's frozen evidence_ref rows.

    Wave-9 L2: intake freezes evidence (immutable, digest-addressed) but the
    catalog source row often keeps no live config. Assembly recovers those
    rows instead of skipping the source. Rows whose body fails to parse are
    ignored (never guessed at) — a partial recovery is honest, an invented
    ref is not.
    """
    refs: list[EvidenceRef] = []
    for row in catalog.list_evidence_refs_for_source(source["id"]):
        try:
            refs.append(EvidenceRef.model_validate(json.loads(row["body"])))
        except (ValueError, TypeError, KeyError):
            continue
    return refs

DEFAULT_ROLES: tuple[Role, ...] = (Role.CREATOR, Role.JUDGE, Role.EXECUTOR)

@dataclass(frozen=True)
class SkippedSource:
    """A source assembly could not collect from, and why."""

    name: str
    kind: str
    reason: str

@dataclass(frozen=True)
class AssembleResult:
    """Typed result of one assembly run — everything citable by digest."""

    project: str
    objective: str
    snapshot_digest: str
    pack_digests: dict[str, str]
    evidence_counts: dict[str, int]
    skipped_sources: tuple[SkippedSource, ...] = ()
    collected_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "objective": self.objective,
            "snapshot_digest": self.snapshot_digest,
            "pack_digests": dict(self.pack_digests),
            "evidence_counts": dict(self.evidence_counts),
            "skipped_sources": [
                {"name": s.name, "kind": s.kind, "reason": s.reason}
                for s in self.skipped_sources
            ],
            "collected_sources": list(self.collected_sources),
        }

class AssembleError(RuntimeError):
    """Assembly failed in a way the caller must handle (empty evidence,
    unusable git source, ...). Never papered over with partial output."""

def _parse_roles(spec: Sequence[str | Role]) -> tuple[Role, ...]:
    roles: list[Role] = []
    for item in spec:
        role = Role(item)
        if role not in roles:
            roles.append(role)
    if not roles:
        raise AssembleError("at least one role is required")
    return tuple(roles)

def _git_config(source: dict, limits: dict) -> dict:
    """Adapter config for a git source: config wins, locator is the
    fallback root; --limit-files bounds the excerpted paths."""
    config = dict(source.get("config") or {})
    config.setdefault("root", source.get("locator", ""))
    max_files = limits.get("max_files")
    if max_files is not None:
        config["paths"] = list(config.get("paths", []))[: max(0, int(max_files))]
    return config

def assemble(
    project_name: str,
    *,
    objective: str,
    roles: Sequence[str | Role] = DEFAULT_ROLES,
    limits: Optional[dict] = None,
) -> AssembleResult:
    """Collect evidence for ``project_name``, snapshot it, persist role packs.

    Returns an AssembleResult carrying every digest needed for citation.
    Raises AssembleError when no evidence could be collected (an empty pack
    is a configuration error, not a valid downstream input).
    """
    objective = (objective or "").strip()
    if not objective:
        raise AssembleError("objective must be non-empty")
    role_tuple = _parse_roles(roles)
    limits = dict(limits or {})
    adapter_limits = {k: v for k, v in limits.items() if k != "max_files"}

    project = catalog.get_project(project_name)
    sources = catalog.list_sources(project_name, status="active")

    collected: list[EvidenceRef] = []
    per_source: list[tuple[int, list[EvidenceRef]]] = []
    skipped: list[SkippedSource] = []
    collected_names: list[str] = []
    repos = []

    for source in sources:
        adapter_kind = _KIND_TO_ADAPTER.get(source["kind"])
        if adapter_kind is None:
            frozen = _frozen_refs_for_source(source)
            if frozen:
                per_source.append((source["id"], frozen))
                collected.extend(frozen)
                collected_names.append(source["name"])
                continue
            skipped.append(
                SkippedSource(
                    name=source["name"],
                    kind=source["kind"],
                    reason=f"no adapter registered for source kind {source['kind']!r}",
                )
            )
            continue
        adapter = get_adapter(adapter_kind)
        if adapter_kind == "git_local":
            config = _git_config(source, limits)
        else:
            config = dict(source.get("config") or {})
        refs = adapter.collect(config, why=objective, limits=adapter_limits or None)
        if not refs:
            frozen = _frozen_refs_for_source(source)
            if frozen:
                per_source.append((source["id"], frozen))
                collected.extend(frozen)
                collected_names.append(source["name"])
                continue
            skipped.append(
                SkippedSource(
                    name=source["name"],
                    kind=source["kind"],
                    reason="adapter returned no evidence for this source config",
                )
            )
            continue
        per_source.append((source["id"], refs))
        collected.extend(refs)
        collected_names.append(source["name"])
        if adapter_kind == "git_local":
            repo = capture_repo_snapshot(config["root"])
            if repo is not None:
                repos.append(repo)

    if not collected:
        raise AssembleError(
            f"no evidence collected for project {project_name!r}: "
            f"{len(skipped)} source(s) skipped "
            f"({', '.join(s.name for s in skipped) or 'none configured'})"
        )

    snapshot: LandscapeSnapshot = assemble_snapshot(
        project_name, collected, repos
    )

    for source_id, refs in per_source:
        for ref in refs:
            catalog.insert_evidence_ref(ref, source_id=source_id)
    snapshot_digest = catalog.insert_landscape_snapshot(
        snapshot, project_id=project["id"]
    )
    for ref in snapshot.evidence:
        catalog.link_snapshot_evidence(snapshot_digest, ref.digest)

    pack_digests: dict[str, str] = {}
    for role in role_tuple:
        pack: ContextPack = build_pack(snapshot, role)
        pack_digests[role.value] = catalog.insert_context_pack(pack)

    tier_counts = Counter(ref.trust_tier.value for ref in snapshot.evidence)
    return AssembleResult(
        project=project_name,
        objective=objective,
        snapshot_digest=snapshot_digest,
        pack_digests=pack_digests,
        evidence_counts=dict(sorted(tier_counts.items())),
        skipped_sources=tuple(skipped),
        collected_sources=tuple(collected_names),
    )

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="assemble_pack.py", description=__doc__.splitlines()[0]
    )
    p.add_argument("--project", required=True, help="catalog project name")
    p.add_argument("--objective", required=True, help="why this pack exists")
    p.add_argument(
        "--roles",
        default=",".join(r.value for r in DEFAULT_ROLES),
        help="comma-separated roles (creator,judge,executor)",
    )
    p.add_argument(
        "--limit-files",
        type=int,
        default=None,
        help="max excerpted file paths per git source",
    )
    p.add_argument(
        "--max-commits",
        type=int,
        default=None,
        help="max recent commits per git source (0 disables the log)",
    )
    args = p.parse_args(argv)

    limits: dict = {}
    if args.limit_files is not None:
        limits["max_files"] = args.limit_files
    if args.max_commits is not None:
        limits["max_commits"] = args.max_commits

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    try:
        result = assemble(
            args.project,
            objective=args.objective,
            roles=roles,
            limits=limits or None,
        )
    except (AssembleError, LookupError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"project:  {result.project}")
    print(f"objective: {result.objective}")
    print(f"snapshot: {result.snapshot_digest}")
    for role, digest in result.pack_digests.items():
        print(f"pack[{role}]: {digest}")
    counts = ", ".join(f"{k}={v}" for k, v in result.evidence_counts.items())
    print(f"evidence: {counts or 'none'}")
    if result.skipped_sources:
        for s in result.skipped_sources:
            print(f"skipped:  {s.name} (kind={s.kind}) — {s.reason}")
    print("PACK_JSON: " + json.dumps(result.to_dict()))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
