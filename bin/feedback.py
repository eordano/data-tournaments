"""Derive per-card feedback from human pair verdicts.

Pair verdicts train the judge directly (pairwise preference); they also
project to card-level quality labels that feed generator-prompt optimization.

The projection is intentionally softer for generator feedback than for judge
feedback — losing a pair against a strong card does not necessarily mean the
losing card was bad.

Verdict projection table (see tests/test_feedback.py for the canonical spec):

    a-wins-big  → A: positive 1.0, B: weak     0.4
    a-wins      → A: positive 0.6, B: neutral  0.0
    b-wins-big  → A: weak     0.4, B: positive 1.0
    b-wins      → A: neutral  0.0, B: positive 0.6
    tie         → both neutral 0.0  (an order that does not matter says
                  nothing about either card's quality)
    discard-a   → A: negative 1.0, B: unknown  0.0
    discard-b   → A: unknown  0.0, B: negative 1.0
    skip        → both unknown  0.0  (no training signal)

A discard is per-side, so it labels ONLY the side it ejects: the survivor of
a discarded pairing is `unknown`, never `weak`, because nothing was
established about it. Same rule the swiss engine applies when it declines to
record a result for it.

reason_tags on the judgement may be side-specific:
    [{"side": "a"|"b"|"pair", "tag": "..."}]
A `"pair"` tag attaches to both sides.
"""
from __future__ import annotations
from typing import TypedDict

class CardFeedback(TypedDict):
    quality: str
    weight: float
    reason_tags: list[str]
    title: str
    body: str
    source_ref: str

class PairFeedback(TypedDict):
    card_a: CardFeedback
    card_b: CardFeedback

_PROJECTION: dict[str, tuple[tuple[str, float], tuple[str, float]]] = {
    "a-wins-big": (("positive", 1.0), ("weak",     0.4)),
    "a-wins":     (("positive", 0.6), ("neutral",  0.0)),
    "b-wins-big": (("weak",     0.4), ("positive", 1.0)),
    "b-wins":     (("neutral",  0.0), ("positive", 0.6)),
    "tie":        (("neutral",  0.0), ("neutral",  0.0)),
    "discard-a":  (("negative", 1.0), ("unknown",  0.0)),
    "discard-b":  (("unknown",  0.0), ("negative", 1.0)),
    "skip":       (("unknown",  0.0), ("unknown",  0.0)),
}

A_DISCARD_LABELS_ONLY_THE_SIDE_IT_EJECTS_THE_SURVIVOR_STAYS_UNKNOWN = (
    "projecting a discard onto both cards is the generator-feedback form of "
    "the defect the per-side vocabulary exists to fix: a malformed A would "
    "teach the generator that a perfectly good B was bad too."
)
assert _PROJECTION["discard-a"][1][0] == "unknown", (
    A_DISCARD_LABELS_ONLY_THE_SIDE_IT_EJECTS_THE_SURVIVOR_STAYS_UNKNOWN
)
assert _PROJECTION["discard-b"][0][0] == "unknown", (
    A_DISCARD_LABELS_ONLY_THE_SIDE_IT_EJECTS_THE_SURVIVOR_STAYS_UNKNOWN
)

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
