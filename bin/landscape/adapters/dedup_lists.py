"""dedup_lists adapter: freeze the campaign's dedup-gate inputs as evidence.

The August bugsweeps gated candidates BEFORE lane assignment against three
system-queried lists: ``open-prs.tsv`` (number\\tbranch\\ttitle),
``inflight.tsv`` (the team's in-flight tracking sheet) and
``prior-campaign-slugs.txt`` (one slug per line — prior lanes are OUT).

This adapter produces ONE EvidenceRef per provided list (not per row):
``dedup:<kind>@<sha256[:12] of content>`` — the content hash makes the URI
(and digest) change exactly when the list content changes.

Trust rule: these are system-queried repository/campaign state, not human
prose — TIER1_SYSTEM.

Digest determinism: ``retrieved_at`` defaults to "" (or the optional
``retrieved_at`` config value); nothing is stamped from the wall clock.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.adapters.slack_csv import redact_text
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    EvidenceRef,
    SourceType,
    TrustTier,
)


class DedupPayloadError(ValueError):
    """dedup_lists config is missing every known list."""


# config key -> (kind label, dedup-role sentence for why_selected)
_KINDS = {
    "open_prs_tsv": (
        "open_prs",
        "dedup gate: candidates already covered by an open PR are OUT",
    ),
    "inflight_tsv": (
        "inflight",
        "dedup gate: candidates already on the team's in-flight sheet are OUT",
    ),
    "prior_slugs_text": (
        "prior_slugs",
        "dedup gate: prior campaign lanes are OUT",
    ),
}


def list_ref(
    kind: str,
    content: str,
    *,
    why: str,
    role: str,
    retrieved_at: str = "",
    max_entries: int = 10,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> EvidenceRef:
    """One whole dedup list -> one TIER1_SYSTEM EvidenceRef."""
    content = content or ""
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    rows = [ln for ln in content.splitlines() if ln.strip()]
    shown = rows[: max(1, max_entries)]
    lines = [f"{kind}: {len(rows)} rows"]
    lines.extend(
        "  " + redact_text(ln.replace("\t", " | ")) for ln in shown
    )
    if len(rows) > len(shown):
        lines.append(f"  … and {len(rows) - len(shown)} more")
    return EvidenceRef(
        source_type=SourceType.API,
        canonical_uri=f"dedup:{kind}@{sha[:12]}",
        revision=sha[:12],
        retrieved_at=retrieved_at,
        trust_tier=TrustTier.TIER1_SYSTEM,
        excerpt=bounded_excerpt("\n".join(lines), max_chars),
        browsable_link=None,
        why_selected=f"{why} — {role}",
    )


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint. config: any of ``open_prs_tsv`` /
    ``inflight_tsv`` / ``prior_slugs_text`` (raw file content strings; at
    least one required) + optional ``retrieved_at``.

    An EMPTY provided list is still one ref ("0 rows" is real system state:
    nothing in flight); only the absence of every list key is an error.
    """
    if not any(key in config for key in _KINDS):
        raise DedupPayloadError(
            "dedup_lists config requires at least one of: "
            + ", ".join(sorted(_KINDS))
        )
    limits = limits or {}
    max_entries = max(1, int(limits.get("max_items", 10)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    retrieved_at = str(config.get("retrieved_at", ""))
    refs: list[EvidenceRef] = []
    for key, (kind, role) in _KINDS.items():
        if key in config and config[key] is not None:
            refs.append(
                list_ref(
                    kind,
                    str(config[key]),
                    why=why,
                    role=role,
                    retrieved_at=retrieved_at,
                    max_entries=max_entries,
                    max_chars=max_chars,
                )
            )
    return refs
