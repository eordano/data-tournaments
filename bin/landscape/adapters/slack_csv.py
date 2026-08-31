"""slack_csv adapter: parse an ALREADY-EXPORTED Slack #bug-reporting CSV
(the August bugsweeps' slack-bugs.csv shape) into frozen EvidenceRefs.

CSV header: ``ts,date,replies,text`` — one workflow submission per row. The
``text`` column usually carries the bug-report template (TITLE / DESCRIPTION /
STR / SYSTEM / REPRODUCTION INDEX / REPORTER ...) but free-form prose is
tolerated: unparsed text is excerpted as-is (after redaction).

Trust rule: Slack reports are human-authored external prose — TIER3_EXTERNAL,
always. Excerpts are REDACTED before freezing: ``<@U…>`` mentions become
``<@user>`` and token-like strings (xox?-, sk-, ghp\\_, long hex, long base64)
become ``[REDACTED]``. ``redact_text`` is the shared redaction helper for all
signal adapters (sentry_csv / github_autoclosed import it).

Digest determinism: ``retrieved_at`` is stamped from the row's ``date``
column (export data), never the wall clock, so re-parsing the same CSV yields
byte-identical digests.
"""
from __future__ import annotations

import csv
import html
import io
import re
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    EvidenceRef,
    SourceType,
    TrustTier,
)

class SlackPayloadError(ValueError):
    """Malformed slack-bugs CSV row. Raised loudly — silently skipping
    malformed evidence would hide data loss."""

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]{2,}>")
_TOKEN_RES = (
    re.compile(r"xox[a-z]-[A-Za-z0-9-]{4,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"),
)

def redact_text(text: str) -> str:
    """Redact user mentions and token-like strings from excerpt text.

    ``<@U…>``/``<@W…>`` mentions -> ``<@user>``; anything matching a
    token-like pattern -> ``[REDACTED]``. Applied AFTER html-unescaping and
    BEFORE excerpting, so redacted forms are what gets frozen and digested.
    """
    text = _MENTION_RE.sub("<@user>", text)
    for pat in _TOKEN_RES:
        text = pat.sub("[REDACTED]", text)
    return text

_TEMPLATE_FIELDS = (
    "TITLE",
    "DESCRIPTION",
    "STR",
    "STEPS TO REPRODUCE",
    "EXPECTED",
    "CURRENT",
    "SYSTEM",
    "REPRODUCTION INDEX",
    "REPORTER",
)
_FIELD_RE = re.compile(
    r"(?im)^\s*[*_]{0,2}("
    + "|".join(re.escape(f) for f in _TEMPLATE_FIELDS)
    + r")[*_]{0,2}\s*:\s*"
)

def parse_template(text: str) -> dict[str, str]:
    """Extract bug-template fields from a Slack report's text.

    Returns {} for free-form text — callers must tolerate both shapes.
    """
    parts = _FIELD_RE.split(text)
    if len(parts) < 3:
        return {}
    fields: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].upper()
        value = parts[i + 1].strip()
        if label and label not in fields:
            fields[label] = value
    return fields

def parse_report(
    row: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """One slack-bugs.csv row -> TIER3_EXTERNAL EvidenceRef."""
    if not isinstance(row, dict):
        raise SlackPayloadError("slack row must be a dict")
    ts = (row.get("ts") or "").strip()
    text = row.get("text")
    if not ts:
        raise SlackPayloadError("slack row missing required field 'ts'")
    if text is None or not str(text).strip():
        raise SlackPayloadError("slack row missing required field 'text'")

    clean = redact_text(html.unescape(str(text)))
    fields = parse_template(clean)
    date = (row.get("date") or "").strip()
    replies = str(row.get("replies") or "0").strip()

    lines = [f"slack #bug-reporting ts={ts} date={date} replies={replies}"]
    if fields:
        for label in _TEMPLATE_FIELDS:
            if label in fields and fields[label]:
                lines.append(f"{label}: {fields[label]}")
    else:
        lines.append(clean.strip())
    excerpt = "\n".join(lines)

    return EvidenceRef(
        source_type=SourceType.CHAT,
        canonical_uri=f"slack:{ts}",
        revision=ts,
        retrieved_at=date,
        trust_tier=TrustTier.TIER3_EXTERNAL,
        excerpt=bounded_excerpt(excerpt, max_chars),
        browsable_link=None,
        why_selected=why,
    )

def parse_reports(
    csv_text: str, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Parse slack-bugs.csv text. Empty input -> empty list; malformed rows
    raise, never skip."""
    if not (csv_text or "").strip():
        return []
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 50)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    rows = list(csv.DictReader(io.StringIO(csv_text)))[:max_items]
    return [parse_report(r, why=why, max_chars=max_chars) for r in rows]

def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint for ALREADY-EXPORTED data. config: ``csv_text``."""
    csv_text = config.get("csv_text")
    if csv_text is None:
        raise SlackPayloadError("slack_csv config requires csv_text")
    return parse_reports(csv_text, why=why, limits=limits)
