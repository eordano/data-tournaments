"""Small shared helpers for evidence adapters (no contract logic here)."""
from __future__ import annotations

from datetime import datetime, timezone

from bin.landscape.evidence import MAX_EXCERPT_CHARS

TRUNCATION_NOTE = "\n… [truncated]"


def now_iso() -> str:
    """ISO-8601 UTC timestamp for ``retrieved_at`` stamping."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bounded_excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Truncate to ``limit`` with an explicit note. EvidenceRef's validator
    would clip silently at MAX_EXCERPT_CHARS; the note keeps truncation
    visible to consumers."""
    limit = min(limit, MAX_EXCERPT_CHARS)
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_NOTE)] + TRUNCATION_NOTE
