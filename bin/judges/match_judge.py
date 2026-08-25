"""DSPy program for the card-prioritizer judge.

Replaces the hand-rolled httpx + tool-calling code path. Same input contract
(a card-shaped trace_payload), same output contract (verdict, confidence,
rationale).
"""
from __future__ import annotations
from typing import Optional

import dspy

from bin import prompts as _prompts


class JudgeCardSig(dspy.Signature):
    """Pick the card more worth surfacing to a human user.

    Reason FIRST in `rationale`, THEN commit to a `verdict`.
    """

    card_a_title = dspy.InputField(desc="Title of card A")
    card_a_body = dspy.InputField(desc="Body / details of card A")
    card_a_source_ref = dspy.InputField(
        desc="Authoritative source path or reference for card A; may be empty"
    )
    card_b_title = dspy.InputField(desc="Title of card B")
    card_b_body = dspy.InputField(desc="Body / details of card B")
    card_b_source_ref = dspy.InputField(
        desc="Authoritative source path or reference for card B; may be empty"
    )

    rationale = dspy.OutputField(desc="1–3 sentences. Reason about specificity, novelty, actionability, and impact.")
    confidence = dspy.OutputField(desc="One of: low, mid, high.")
    verdict = dspy.OutputField(
        desc=(
            "Exactly one of: a-clearly-better, a-marginally-better, "
            "tie-both-strong, tie-both-weak, b-marginally-better, "
            "b-clearly-better, incoherent, skip."
        )
    )


VERDICT_ENUM = {
    "a-clearly-better", "a-marginally-better",
    "tie-both-strong", "tie-both-weak",
    "b-marginally-better", "b-clearly-better",
    "incoherent", "skip",
}
CONFIDENCE_ENUM = {"low", "mid", "high"}


class MatchJudge(dspy.Module):
    """Judge wrapping a `dspy.ChainOfThought` whose system prompt comes from
    Langfuse Prompts.

    Construction reads `judge-instructions:<label>` (default `production`)
    and bakes it into the signature's instructions.
    """

    def __init__(self, prompt_name: str = "judge-instructions",
                 prompt_label: str = "production",
                 instructions: Optional[str] = None):
        super().__init__()
        self.signature = JudgeCardSig.with_instructions(
            instructions
            if instructions is not None
            else _prompts.get(prompt_name, label=prompt_label)
        )
        self.predictor = dspy.Predict(self.signature)

    def forward(
        self,
        *,
        card_a_title,
        card_a_body,
        card_b_title,
        card_b_body,
        card_a_source_ref="",
        card_b_source_ref="",
    ):
        result = self.predictor(
            card_a_title=card_a_title,
            card_a_body=card_a_body,
            card_a_source_ref=card_a_source_ref or "",
            card_b_title=card_b_title,
            card_b_body=card_b_body,
            card_b_source_ref=card_b_source_ref or "",
        )
        verdict = (result.verdict or "").strip()
        confidence = (result.confidence or "").strip().lower()
        rationale = result.rationale or ""

        if verdict not in VERDICT_ENUM:
            raise ValueError(
                f"verdict {verdict!r} not in rubric enum {sorted(VERDICT_ENUM)}"
            )
        if confidence not in CONFIDENCE_ENUM:
            confidence = "mid"

        return dspy.Prediction(
            verdict=verdict, confidence=confidence, rationale=rationale,
        )
