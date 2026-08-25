"""Derive per-card feedback from human pair verdicts.

Pair verdicts train the judge directly (pairwise preference); they also
project to card-level quality labels that feed generator-prompt optimization.

The projection is intentionally softer for generator feedback than for judge
feedback — losing a pair against a strong card does not necessarily mean the
losing card was bad.

Verdict projection table (see tests/test_feedback.py for the canonical spec):

    a-clearly-better     → A: positive 1.0, B: weak 0.4
    a-marginally-better  → A: positive 0.6, B: neutral 0.0
    b-clearly-better     → A: weak     0.4, B: positive 1.0
    b-marginally-better  → A: neutral  0.0, B: positive 0.6
    tie-both-strong      → both positive 1.0
    tie-both-weak        → both negative 1.0
    incoherent           → both negative 0.7  (pair-level negative)
    skip                 → both unknown  0.0  (no training signal)

reason_tags on the judgement may be side-specific:
    [{"side": "a"|"b"|"pair", "tag": "..."}]
A `"pair"` tag attaches to both sides.
"""
from __future__ import annotations
from typing import TypedDict


class CardFeedback(TypedDict):
    quality: str  # "positive" | "neutral" | "weak" | "negative" | "unknown"
    weight: float
    reason_tags: list[str]
    title: str
    body: str
    source_ref: str


class PairFeedback(TypedDict):
    card_a: CardFeedback
    card_b: CardFeedback


_PROJECTION: dict[str, tuple[tuple[str, float], tuple[str, float]]] = {
    "a-clearly-better":    (("positive", 1.0), ("weak",     0.4)),
    "a-marginally-better": (("positive", 0.6), ("neutral",  0.0)),
    "b-clearly-better":    (("weak",     0.4), ("positive", 1.0)),
    "b-marginally-better": (("neutral",  0.0), ("positive", 0.6)),
    "tie-both-strong":     (("positive", 1.0), ("positive", 1.0)),
    "tie-both-weak":       (("negative", 1.0), ("negative", 1.0)),
    "incoherent":          (("negative", 0.7), ("negative", 0.7)),
    "skip":                (("unknown",  0.0), ("unknown",  0.0)),
}


def derive_card_feedback(judgement: dict) -> PairFeedback:
    """Project a pair verdict into per-card quality labels."""
    verdict = judgement.get("verdict", "")
    if verdict not in _PROJECTION:
        raise ValueError(f"unknown verdict: {verdict!r}")

    (a_qual, a_w), (b_qual, b_w) = _PROJECTION[verdict]
    a_tags, b_tags = _split_tags(judgement.get("reason_tags") or [])

    return {
        "card_a": _build_card_feedback(judgement.get("card_a") or {}, a_qual, a_w, a_tags),
        "card_b": _build_card_feedback(judgement.get("card_b") or {}, b_qual, b_w, b_tags),
    }


def _build_card_feedback(card: dict, quality: str, weight: float, tags: list[str]) -> CardFeedback:
    return {
        "quality": quality,
        "weight": weight,
        "reason_tags": tags,
        "title": card.get("title", ""),
        "body": card.get("body", ""),
        "source_ref": card.get("source_ref", ""),
    }


def _split_tags(reason_tags: list[dict]) -> tuple[list[str], list[str]]:
    a, b = [], []
    for entry in reason_tags:
        side = entry.get("side")
        tag = entry.get("tag")
        if not tag:
            continue
        if side == "a":
            a.append(tag)
        elif side == "b":
            b.append(tag)
        elif side == "pair":
            a.append(tag)
            b.append(tag)
    return a, b


# ─────────────────────────────────────────────────────────────────────────
# DB-aggregation layer
#
# list_card_feedback_for_domain reads the fabric DB, finds scored pairs for a
# given domain, projects each pair via derive_card_feedback(), and emits a
# flat list of per-card feedback rows for generator-prompt optimization.

def list_card_feedback_for_domain(domain_name: str, *, min_weight: float = 0.0) -> list[dict]:
    import json
    import sqlite3
    from pathlib import Path
    import os

    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    db_path = home / "judgements.db"
    if not db_path.exists():
        return []

    db = sqlite3.connect(db_path)

    sql = """
    SELECT p.trace_payload, s.value AS verdict, s.metadata
    FROM pending_judgement p
    JOIN domain d ON d.id = p.domain_id
    JOIN score s ON s.pending_id = p.id AND s.name = 'judgement.verdict'
    WHERE d.name = ?
    ORDER BY p.id, s.created_at
    """
    out = []
    for raw_payload, verdict, raw_meta in db.execute(sql, (domain_name,)):
        try:
            payload = json.loads(raw_payload or "{}")
            meta = json.loads(raw_meta or "{}")
        except (TypeError, ValueError):
            continue

        judgement = {
            "verdict": verdict,
            "card_a": payload.get("card_a") or {},
            "card_b": payload.get("card_b") or {},
            "rationale": meta.get("rationale", ""),
            "reason_tags": meta.get("reason_tags") or [],
        }
        try:
            feedback = derive_card_feedback(judgement)
        except ValueError:
            continue
        for side in ("card_a", "card_b"):
            row = dict(feedback[side])
            row["domain"] = domain_name
            row["side"] = side
            if row["weight"] >= min_weight:
                out.append(row)
    return out
