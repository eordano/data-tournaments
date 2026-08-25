"""Tests for deriving generator/card-level feedback from human pair verdicts.

Pair verdicts train the judge directly, but they also project to per-card
quality labels for generator-prompt optimization.

The rule of projection:

| verdict              | card A          | card B          | reason            |
|----------------------|-----------------|-----------------|-------------------|
| a-clearly-better     | positive  w=1.0 | weak     w=0.4  | strong preference |
| a-marginally-better  | positive  w=0.6 | neutral  w=0.0  | weak preference   |
| b-clearly-better     | weak     w=0.4  | positive w=1.0  | mirror            |
| b-marginally-better  | neutral  w=0.0  | positive w=0.6  | mirror            |
| tie-both-strong      | positive w=1.0  | positive w=1.0  | both good         |
| tie-both-weak        | negative w=1.0  | negative w=1.0  | both bad          |
| incoherent           | negative w=0.7  | negative w=0.7  | pair-level neg    |
| skip                 | (no feedback)                                       |

Reason tags from the rationale/metadata may attach per-card or per-pair,
but the projection itself is verdict-only.
"""
from __future__ import annotations
import pytest

from bin.feedback import derive_card_feedback


def _card(title: str, body: str = "...", source_ref: str = "x") -> dict:
    return {"title": title, "body": body, "source_ref": source_ref}


def _judgement(verdict: str, *, card_a=None, card_b=None, rationale: str = "", reason_tags=None) -> dict:
    return {
        "verdict": verdict,
        "rationale": rationale,
        "reason_tags": reason_tags or [],
        "card_a": card_a or _card("A"),
        "card_b": card_b or _card("B"),
    }


def test_clearly_better_marks_winner_positive_and_loser_weak():
    feedback = derive_card_feedback(_judgement("a-clearly-better"))
    a, b = feedback["card_a"], feedback["card_b"]
    assert a["quality"] == "positive" and a["weight"] == 1.0
    assert b["quality"] == "weak" and 0 < b["weight"] < 1.0


def test_b_clearly_better_mirrors_a():
    feedback = derive_card_feedback(_judgement("b-clearly-better"))
    assert feedback["card_a"]["quality"] == "weak"
    assert feedback["card_b"]["quality"] == "positive"
    assert feedback["card_b"]["weight"] == 1.0


def test_marginal_preference_is_softer_than_clear():
    clear = derive_card_feedback(_judgement("a-clearly-better"))
    marginal = derive_card_feedback(_judgement("a-marginally-better"))
    assert marginal["card_a"]["weight"] < clear["card_a"]["weight"]
    # Marginal loser is neutral, not weak-negative
    assert marginal["card_b"]["quality"] == "neutral"


def test_tie_both_strong_marks_both_positive():
    feedback = derive_card_feedback(_judgement("tie-both-strong"))
    assert feedback["card_a"]["quality"] == "positive"
    assert feedback["card_b"]["quality"] == "positive"
    assert feedback["card_a"]["weight"] == 1.0
    assert feedback["card_b"]["weight"] == 1.0


def test_tie_both_weak_marks_both_negative():
    feedback = derive_card_feedback(_judgement("tie-both-weak"))
    assert feedback["card_a"]["quality"] == "negative"
    assert feedback["card_b"]["quality"] == "negative"


def test_incoherent_marks_both_negative_with_lower_weight():
    feedback = derive_card_feedback(_judgement("incoherent"))
    # Without side-specific tags, we mark both negative with reduced weight.
    assert feedback["card_a"]["quality"] == "negative"
    assert feedback["card_b"]["quality"] == "negative"
    assert feedback["card_a"]["weight"] < 1.0


def test_skip_produces_no_training_signal():
    feedback = derive_card_feedback(_judgement("skip"))
    # Skip is unknown; emit feedback rows with quality=unknown and weight=0
    # so consumers can filter them out cleanly.
    assert feedback["card_a"]["quality"] == "unknown"
    assert feedback["card_a"]["weight"] == 0.0
    assert feedback["card_b"]["quality"] == "unknown"
    assert feedback["card_b"]["weight"] == 0.0


def test_reason_tags_attach_to_targeted_side():
    j = _judgement(
        "incoherent",
        reason_tags=[
            {"side": "a", "tag": "malformed"},
            {"side": "b", "tag": "too vague"},
        ],
    )
    feedback = derive_card_feedback(j)
    assert "malformed" in feedback["card_a"]["reason_tags"]
    assert "too vague" in feedback["card_b"]["reason_tags"]
    assert "malformed" not in feedback["card_b"]["reason_tags"]


def test_reason_tags_with_pair_side_attach_to_both():
    j = _judgement(
        "tie-both-weak",
        reason_tags=[{"side": "pair", "tag": "duplicate"}],
    )
    feedback = derive_card_feedback(j)
    assert "duplicate" in feedback["card_a"]["reason_tags"]
    assert "duplicate" in feedback["card_b"]["reason_tags"]


def test_feedback_includes_card_provenance():
    j = _judgement(
        "a-clearly-better",
        card_a=_card("guard shortcuts while typing", source_ref="git:2274501"),
        card_b=_card("vague update", source_ref="git:abc1234"),
    )
    feedback = derive_card_feedback(j)
    assert feedback["card_a"]["title"] == "guard shortcuts while typing"
    assert feedback["card_a"]["source_ref"] == "git:2274501"
    assert feedback["card_b"]["title"] == "vague update"


def test_unknown_verdict_raises():
    with pytest.raises(ValueError, match="unknown verdict"):
        derive_card_feedback(_judgement("nonsense-verdict"))
