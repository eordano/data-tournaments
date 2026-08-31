"""Tests for deriving generator/card-level feedback from human pair verdicts.

Pair verdicts train the judge directly, but they also project to per-card
quality labels for generator-prompt optimization.

The rule of projection:

| verdict     | card A          | card B          | reason                 |
|-------------|-----------------|-----------------|------------------------|
| a-wins-big  | positive w=1.0  | weak     w=0.4  | strong preference      |
| a-wins      | positive w=0.6  | neutral  w=0.0  | weak preference        |
| b-wins-big  | weak     w=0.4  | positive w=1.0  | mirror                 |
| b-wins      | neutral  w=0.0  | positive w=0.6  | mirror                 |
| tie         | neutral  w=0.0  | neutral  w=0.0  | says nothing about     |
|             |                 |                 | either card's quality  |
| discard-a   | negative w=1.0  | unknown  w=0.0  | per side, A only       |
| discard-b   | unknown  w=0.0  | negative w=1.0  | per side, B only       |
| skip        | (no feedback)                                            |

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

def test_a_big_win_marks_winner_positive_and_loser_weak():
    feedback = derive_card_feedback(_judgement("a-wins-big"))
    a, b = feedback["card_a"], feedback["card_b"]
    assert a["quality"] == "positive" and a["weight"] == 1.0
    assert b["quality"] == "weak" and 0 < b["weight"] < 1.0

def test_b_wins_big_mirrors_a():
    feedback = derive_card_feedback(_judgement("b-wins-big"))
    assert feedback["card_a"]["quality"] == "weak"
    assert feedback["card_b"]["quality"] == "positive"
    assert feedback["card_b"]["weight"] == 1.0

def test_a_plain_win_is_softer_than_a_big_one():
    big = derive_card_feedback(_judgement("a-wins-big"))
    plain = derive_card_feedback(_judgement("a-wins"))
    assert plain["card_a"]["weight"] < big["card_a"]["weight"]
    assert plain["card_b"]["quality"] == "neutral"

def test_a_tie_says_nothing_about_either_card():
    """The retired vocabulary had a both-strong tie and a both-weak tie. One
    tie replaced them, and it carries no quality claim in either direction."""
    feedback = derive_card_feedback(_judgement("tie"))
    assert feedback["card_a"]["quality"] == "neutral"
    assert feedback["card_b"]["quality"] == "neutral"
    assert feedback["card_a"]["weight"] == feedback["card_b"]["weight"] == 0.0

def test_a_discard_labels_only_the_side_it_ejects():
    """The generator-feedback form of the per-side rule: a malformed A must
    never teach the generator that the B beside it was bad."""
    from bin.feedback import (
        A_DISCARD_LABELS_ONLY_THE_SIDE_IT_EJECTS_THE_SURVIVOR_STAYS_UNKNOWN,
    )

    a_gone = derive_card_feedback(_judgement("discard-a"))
    assert a_gone["card_a"]["quality"] == "negative"
    assert a_gone["card_a"]["weight"] == 1.0
    assert a_gone["card_b"] == {
        **a_gone["card_b"], "quality": "unknown", "weight": 0.0,
    }, A_DISCARD_LABELS_ONLY_THE_SIDE_IT_EJECTS_THE_SURVIVOR_STAYS_UNKNOWN

    b_gone = derive_card_feedback(_judgement("discard-b"))
    assert b_gone["card_b"]["quality"] == "negative"
    assert b_gone["card_a"]["quality"] == "unknown"

def test_the_retired_vocabulary_produces_no_feedback_at_all():
    for retired in ("a-clearly-better", "tie-both-strong", "tie-both-weak",
                    "incoherent", "neither-good"):
        with pytest.raises(ValueError, match="unknown verdict"):
            derive_card_feedback(_judgement(retired))

def test_skip_produces_no_training_signal():
    feedback = derive_card_feedback(_judgement("skip"))
    assert feedback["card_a"]["quality"] == "unknown"
    assert feedback["card_a"]["weight"] == 0.0
    assert feedback["card_b"]["quality"] == "unknown"
    assert feedback["card_b"]["weight"] == 0.0

def test_reason_tags_attach_to_targeted_side():
    j = _judgement(
        "discard-a",
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
        "tie",
        reason_tags=[{"side": "pair", "tag": "duplicate"}],
    )
    feedback = derive_card_feedback(j)
    assert "duplicate" in feedback["card_a"]["reason_tags"]
    assert "duplicate" in feedback["card_b"]["reason_tags"]

def test_feedback_includes_card_provenance():
    j = _judgement(
        "a-wins-big",
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
