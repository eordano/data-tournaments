"""foundry_stories adapter: catalyrst story.md files as review evidence.

A "story" is the catalyrst sites experiment artifact
(``packages/features/src/stories/<surface>/<slug>/story.md``): YAML
frontmatter carrying the hypothesis (statement + because), the metric
(primary / numerator / denominator / guardrails), the decision rule and the
experiment block, followed by prose sections (events table, wiring,
data-reality caveats). ``spec.stories.tsx`` beside it is generated FROM the
story — the story.md is the source of truth and the only thing this
adapter reads.

Each story becomes one EvidenceRef whose excerpt is a normalized digest of
the frontmatter (id, status, hypothesis, metric, decision) plus the opening
prose — enough for a featuresweep lens or a human judge to review the
experiment design without the full file. Stories are team-authored:
TIER2_INTERNAL.

Digest determinism: ``revision`` is a short hash of the file's bytes and
``retrieved_at`` is left empty, so re-collecting an unchanged tree yields
byte-identical EvidenceRefs (mtimes never leak into digests).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import yaml

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.evidence import EvidenceRef, SourceType, TrustTier


class FoundryStoryError(ValueError):
    """Malformed story.md. Raised loudly — a silently skipped story would
    hide a hole in review coverage."""


def _split_frontmatter(text: str, path: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise FoundryStoryError(f"{path}: no YAML frontmatter")
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        raise FoundryStoryError(f"{path}: unterminated frontmatter")
    try:
        meta = yaml.safe_load(parts[0][3:]) or {}
    except yaml.YAMLError as e:
        raise FoundryStoryError(f"{path}: bad frontmatter YAML: {e}") from e
    if not isinstance(meta, dict):
        raise FoundryStoryError(f"{path}: frontmatter is not a mapping")
    body = parts[1] if len(parts) == 2 else "\n---".join(parts[1:])
    return meta, body.lstrip("-").lstrip()


def _story_summary(meta: dict, body: str) -> str:
    head = str(meta.get("id") or "untitled story")
    if meta.get("status"):
        head += f" [{meta['status']}]"
    lines = [head]
    if meta.get("owner"):
        lines.append(f"owner: {meta['owner']}")
    hyp = meta.get("hypothesis") or {}
    if isinstance(hyp, dict):
        if hyp.get("statement"):
            lines.append(f"hypothesis: {' '.join(str(hyp['statement']).split())}")
        if hyp.get("because"):
            lines.append(f"because: {' '.join(str(hyp['because']).split())}")
    metric = meta.get("metric") or {}
    if isinstance(metric, dict) and metric.get("primary"):
        guard = ", ".join(metric.get("guardrails") or [])
        lines.append(
            f"metric: {metric.get('primary')} = "
            f"{metric.get('numerator', '?')} / {metric.get('denominator', '?')}"
            + (f" (guardrails: {guard})" if guard else "")
        )
    decision = meta.get("decision") or {}
    if isinstance(decision, dict) and decision.get("rule"):
        lines.append(f"decision: {' '.join(str(decision['rule']).split())}")
    prose = body.strip()
    if prose:
        lines.append("")
        lines.append(prose)
    return "\n".join(lines)


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint.

    config: ``root`` (required — the stories tree, e.g.
    ``.../packages/features/src/stories``), ``surfaces`` (optional list of
    first-level directory names to include, e.g. ``["foundry",
    "landings"]``; default all).
    limits: ``max_stories`` (default 200), ``max_chars`` (per-story excerpt
    bound).
    """
    root = Path(config.get("root", ""))
    if not config.get("root"):
        raise FoundryStoryError("foundry_stories config requires 'root'")
    if not root.is_dir():
        raise FoundryStoryError(f"stories root {root} is not a directory")
    surfaces = config.get("surfaces")
    if surfaces is not None and (
        isinstance(surfaces, str)
        or not all(isinstance(s, str) for s in surfaces)
    ):
        raise FoundryStoryError(
            f"'surfaces' must be a list of directory names, got {surfaces!r}"
        )
    limits = limits or {}
    max_stories = int(limits.get("max_stories", 200))
    max_chars = int(limits.get("max_chars", 2000))

    story_paths = sorted(root.glob("*/*/story.md"))
    if surfaces:
        available = {p.parent.parent.name for p in story_paths}
        unknown = sorted(set(surfaces) - available)
        if unknown:
            raise FoundryStoryError(
                f"surface(s) {', '.join(unknown)} match no story directory "
                f"under {root} (have: {', '.join(sorted(available))})"
            )
        wanted = set(surfaces)
        story_paths = [p for p in story_paths if p.parent.parent.name in wanted]
    refs: list[EvidenceRef] = []
    for path in story_paths[:max_stories]:
        text = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text, str(path))
        rel = path.relative_to(root)
        story_id = str(meta.get("id") or rel.parent)
        refs.append(
            EvidenceRef(
                source_type=SourceType.DOC,
                canonical_uri=f"story:{rel.parent}#{story_id}",
                revision=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
                trust_tier=TrustTier.TIER2_INTERNAL,
                excerpt=bounded_excerpt(_story_summary(meta, body), max_chars),
                why_selected=why,
            )
        )
    return refs
