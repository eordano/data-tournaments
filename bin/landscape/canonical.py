"""Canonical JSON serialization + content digests.

One canonical form for all landscape artifacts: JSON with sorted keys and no
whitespace variance, hashed with SHA-256. Two payloads with identical content
therefore always produce identical digests, independent of dict insertion
order or formatting.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, no ASCII escaping
    variance (unicode passes through as-is)."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_digest(payload: Any) -> str:
    """SHA-256 hex digest over the canonical JSON form of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
