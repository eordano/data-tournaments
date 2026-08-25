"""sentry_csv adapter: parse an ALREADY-EXPORTED Sentry weekly-issues CSV
(the aug16 bugsweep's sentry-week.csv shape) into frozen EvidenceRefs.

CSV header::

    short_id,week_events,user_count,level,substatus,lifetime_events,
    first_seen,last_seen,title,culprit,permalink

Trust rule: Sentry titles/culprits derive from external user sessions —
TIER3_EXTERNAL, always. Malformed rows (missing short_id / title / permalink)
raise SentryPayloadError; silently skipping would hide signal loss.

Digest determinism: ``retrieved_at`` is stamped from the row's ``last_seen``
(export data), never the wall clock, so re-parsing the same CSV yields
byte-identical digests.
"""
from __future__ import annotations

import csv
import html
import io
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.adapters.slack_csv import redact_text
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)


class SentryPayloadError(ValueError):
    """Malformed sentry-week CSV row. Raised loudly — silently skipping
    malformed evidence would hide data loss."""


def _require(row: dict, key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise SentryPayloadError(
            f"sentry row missing required field {key!r}"
        )
    return value


def parse_issue(
    row: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """One sentry-week.csv row -> TIER3_EXTERNAL EvidenceRef."""
    if not isinstance(row, dict):
        raise SentryPayloadError("sentry row must be a dict")
    short_id = _require(row, "short_id")
    title = _require(row, "title")
    permalink = _require(row, "permalink")

    clean = lambda s: redact_text(html.unescape(s))  # noqa: E731
    lines = [f"{short_id}: {clean(title)}"]
    culprit = (row.get("culprit") or "").strip()
    if culprit:
        lines.append(f"culprit: {clean(culprit)}")
    lines.append(
        "events: week={w} lifetime={l} users={u}".format(
            w=(row.get("week_events") or "?").strip(),
            l=(row.get("lifetime_events") or "?").strip(),
            u=(row.get("user_count") or "?").strip(),
        )
    )
    level = (row.get("level") or "").strip()
    substatus = (row.get("substatus") or "").strip()
    if level or substatus:
        lines.append(f"level={level or '?'} substatus={substatus or '?'}")
    excerpt = "\n".join(lines)

    browsable = None
    if permalink.startswith("https://"):
        browsable = BrowsableLink(
            label=f"Sentry {short_id}", url=permalink, kind="issue"
        )

    last_seen = (row.get("last_seen") or "").strip()
    return EvidenceRef(
        source_type=SourceType.API,
        canonical_uri=f"sentry:{short_id}",
        revision=last_seen,
        retrieved_at=last_seen,
        trust_tier=TrustTier.TIER3_EXTERNAL,
        excerpt=bounded_excerpt(excerpt, max_chars),
        browsable_link=browsable,
        why_selected=why,
    )


def parse_issues(
    csv_text: str, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Parse sentry-week.csv text. Empty input -> empty list; malformed rows
    raise, never skip."""
    if not (csv_text or "").strip():
        return []
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 50)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    rows = list(csv.DictReader(io.StringIO(csv_text)))[:max_items]
    return [parse_issue(r, why=why, max_chars=max_chars) for r in rows]


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint for ALREADY-EXPORTED data. config: ``csv_text``."""
    csv_text = config.get("csv_text")
    if csv_text is None:
        raise SentryPayloadError("sentry_csv config requires csv_text")
    return parse_issues(csv_text, why=why, limits=limits)
