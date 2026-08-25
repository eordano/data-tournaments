"""Bugsweep-corpus adapter: campaign artifacts -> frozen EvidenceRefs.

Ingests the hand-run campaign material (corpus/dcl-bugsweeps-2026-08/ shape)
so past campaigns become citable landscape evidence:

- campaign INDEX.md ledgers  -> one ref per finding row
  (``campaign://<campaign>/finding/<slug>``)
- REVIEW-RULES.md rulesets   -> one ref per numbered rule
  (``review-rule://<ruleset>/<n>``)

Trust rule: these are HUMAN-AUTHORED team artifacts — TIER2_INTERNAL
(the raw transcripts / Slack / Sentry bodies behind them are TIER3 and
enter via their own adapters; git pins are TIER1 via git_local).

Safety:
- Files are read only from within the configured corpus ``root``; paths or
  symlinks resolving OUTSIDE the root are refused (the corpus is known to
  contain absolute symlinks to a remote host's paths — e.g. the dangling
  .claude/skills/bug-campaign link).
- Excerpts pass through the shared ``redact_text`` helper (token shapes,
  ``<@U…>`` mentions) before freezing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.adapters.slack_csv import redact_text
from bin.landscape.evidence import EvidenceRef, SourceType, TrustTier

MAX_EXCERPT_CHARS = 1600


class BugsweepCorpusError(ValueError):
    """Malformed corpus config/content, or an unsafe path."""


def _safe_read(root: Path, rel: str) -> str:
    """Read ``rel`` under ``root``; refuse paths/symlinks escaping root."""
    root = root.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise BugsweepCorpusError(
            f"{rel!r} resolves outside the corpus root — refusing"
        ) from None
    if not target.is_file():
        raise BugsweepCorpusError(f"{rel!r} is not a readable file in the corpus")
    return target.read_text(encoding="utf-8", errors="replace")


# ── campaign INDEX.md ledgers ────────────────────────────────────────────

_ROW_RE = re.compile(r"^\|\s*([a-z0-9][a-z0-9-]*)\s*\|(.+)\|\s*$")


def parse_campaign_index(
    text: str,
    *,
    campaign: str,
    why: str,
    max_chars: int = MAX_EXCERPT_CHARS,
    max_items: int = 50,
) -> list[EvidenceRef]:
    """One TIER2 ref per ledger row (slug in the first table column).

    Header/divider rows are skipped structurally (the slug pattern excludes
    header text and ``---`` dividers). An EMPTY result raises so pointing
    this at the wrong file never silently passes.
    """
    refs: list[EvidenceRef] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m or m.group(1) == "slug":
            continue
        slug, rest = m.group(1), m.group(2)
        cells = [c.strip() for c in rest.split("|")]
        excerpt = redact_text(
            f"finding {slug} — " + " · ".join(c for c in cells if c)
        )
        refs.append(
            EvidenceRef(
                source_type=SourceType.DOC,
                canonical_uri=f"campaign://{campaign}/finding/{slug}",
                trust_tier=TrustTier.TIER2_INTERNAL,
                excerpt=bounded_excerpt(excerpt, max_chars),
                why_selected=why,
            )
        )
        if len(refs) >= max_items:
            break
    if not refs:
        raise BugsweepCorpusError(
            f"no ledger rows found for campaign {campaign!r} — wrong file?"
        )
    return refs


# ── REVIEW-RULES.md rulesets ─────────────────────────────────────────────

_RULE_RE = re.compile(r"^###\s+(\d+)\.\s+(.+)$")


def parse_review_rules(
    text: str,
    *,
    ruleset: str,
    why: str,
    max_chars: int = MAX_EXCERPT_CHARS,
    max_items: int = 50,
) -> list[EvidenceRef]:
    """One TIER2 ref per ``### N. <rule>`` heading, body attached."""
    refs: list[EvidenceRef] = []
    current: Optional[list] = None  # [n, title, body_lines]

    def flush() -> None:
        if current is None:
            return
        n, title, body = current
        excerpt = redact_text("\n".join([title, *body]).strip())
        refs.append(
            EvidenceRef(
                source_type=SourceType.DOC,
                canonical_uri=f"review-rule://{ruleset}/{n}",
                trust_tier=TrustTier.TIER2_INTERNAL,
                excerpt=bounded_excerpt(excerpt, max_chars),
                why_selected=why,
            )
        )

    for line in text.splitlines():
        m = _RULE_RE.match(line.strip())
        if m:
            flush()
            if len(refs) >= max_items:
                current = None
                break
            current = [m.group(1), m.group(2).strip(), []]
        elif current is not None:
            current[2].append(line)
    flush()
    if not refs:
        raise BugsweepCorpusError(
            f"no '### N.' rule headings found for ruleset {ruleset!r}"
        )
    return refs[:max_items]


# ── adapter entrypoint ───────────────────────────────────────────────────


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """config: ``root`` (corpus dir, required) plus any of:
    ``campaigns`` = {name: relative INDEX.md path}
    ``rulesets``  = {name: relative REVIEW-RULES.md path}
    """
    root_str = config.get("root", "")
    if not root_str:
        raise BugsweepCorpusError("bugsweep_corpus config requires 'root'")
    root = Path(root_str)
    if not root.is_dir():
        raise BugsweepCorpusError(f"corpus root {root_str!r} is not a directory")
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 50)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))

    refs: list[EvidenceRef] = []
    for name, rel in (config.get("campaigns") or {}).items():
        refs.extend(
            parse_campaign_index(
                _safe_read(root, rel),
                campaign=name,
                why=why,
                max_chars=max_chars,
                max_items=max_items,
            )
        )
    for name, rel in (config.get("rulesets") or {}).items():
        refs.extend(
            parse_review_rules(
                _safe_read(root, rel),
                ruleset=name,
                why=why,
                max_chars=max_chars,
                max_items=max_items,
            )
        )
    return refs
