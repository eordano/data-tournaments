"""DSPy program: extract Cards from a piece of corpus text.

Per-item generation failures are classified into an explicit taxonomy so the
corpus loop in bin/generate_cards.py can report *why* an item was lost:

* ``CardGenTimeout``    — the LM call timed out.
* ``CardGenParseError`` — the LM answered, but the payload is malformed.
* ``CardGenTruncation`` — the LM hit its output-token limit, so the payload
  is (or may be) incomplete. A truncated finding is never silently repaired
  into a valid-looking one: dspy's JSON adapter can "fix" a payload that is
  merely missing its closing bracket, so we check the provider's
  ``finish_reason`` even when parsing nominally succeeded and fail the item
  instead.
"""
from __future__ import annotations
from typing import Optional

import dspy
import pydantic
from dspy.utils.exceptions import AdapterParseError

from bin import prompts as _prompts


class CardGenError(Exception):
    """Base class for classified per-item generation failures.

    ``failure_class`` is a short machine-readable label used in loop
    diagnostics (``failure=<class>``) and per-class counters.
    """

    failure_class = "error"


class CardGenTimeout(CardGenError):
    failure_class = "timeout"


class CardGenParseError(CardGenError):
    failure_class = "parse-error"


class CardGenTruncation(CardGenError):
    failure_class = "truncation"


#: finish_reason values that mean "output budget exhausted" across providers.
_LENGTH_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}

#: Appended to every generator prompt. The dspy signature already puts the
#: machine-parseable `cards` payload first (it is the sole output field), but
#: reasoning-heavy models can burn the whole output budget on analysis before
#: emitting it. Make the answer-first, bounded contract explicit.
OUTPUT_CONTRACT = (
    "Output contract: emit the machine-parseable `cards` payload immediately, "
    "before any prose, analysis, or explanation. Do not spend output tokens "
    "on reasoning ahead of the payload. Keep the payload bounded: at most 8 "
    "cards, each with a title \u226480 characters and a body \u2264400 "
    "characters. If nothing is card-worthy, emit an empty list. Any "
    "commentary must come after the payload, never before it."
)


def _is_timeout_error(exc: BaseException) -> bool:
    """True for stdlib TimeoutError and provider timeout types (litellm.Timeout,
    httpx.ReadTimeout, ...) without importing every provider library."""
    if isinstance(exc, TimeoutError):
        return True
    return any("timeout" in cls.__name__.lower() for cls in type(exc).__mro__)


def _finish_reason_hit_limit(lm, start_idx: int, *, last_only: bool = False) -> Optional[str]:
    """Return the offending finish_reason if any LM call made since
    ``start_idx`` exhausted the output budget, else None.

    ``last_only`` restricts the check to the most recent call — on a
    successful parse only the final (winning) attempt matters.
    """
    history = list(getattr(lm, "history", None) or [])
    entries = history[start_idx:]
    if not entries:
        return None
    if last_only:
        entries = entries[-1:]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        response = entry.get("response")
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        for choice in choices or []:
            reason = getattr(choice, "finish_reason", None)
            if reason is None and isinstance(choice, dict):
                reason = choice.get("finish_reason")
            if reason and str(reason).lower() in _LENGTH_FINISH_REASONS:
                return str(reason)
    return None


class Card(pydantic.BaseModel):
    title: str
    body: str
    source_ref: Optional[str] = None


class CardGenSig(dspy.Signature):
    """Extract one or more cards from a piece of corpus text.

    A card is a discrete observation, finding, fact, or actionable item
    worth surfacing to a user. Return an empty list when nothing in the
    corpus is worth a card.
    """

    corpus_text: str = dspy.InputField(desc="The corpus item to extract cards from.")
    cards: list[Card] = dspy.OutputField(
        desc=(
            "Zero or more Card objects. Each card needs a title (≤80 chars) "
            "and a body (≤400 chars). source_ref is optional but helpful when "
            "the corpus has natural identifiers."
        )
    )


class CardGen(dspy.Module):
    """A DSPy module wrapping a Predict whose system prompt comes from
    Langfuse Prompts at construction time.

    Subclasses may swap ``signature_cls`` / ``item_model`` / ``output_field``
    / ``contract`` to generate richer artifacts (see
    bin/generators/workorder_gen.py) while inheriting the full failure
    taxonomy: timeout, parse-error, truncation, no silent repair.
    """

    signature_cls = CardGenSig
    item_model = Card
    output_field = "cards"
    contract = OUTPUT_CONTRACT

    def __init__(self, prompt_name: str, prompt_label: str = "production"):
        super().__init__()
        instructions = _prompts.get(prompt_name, label=prompt_label)
        self.signature = self.signature_cls.with_instructions(
            instructions.rstrip() + "\n\n" + self.contract
        )
        self.predictor = dspy.Predict(self.signature)

    def forward(self, *, corpus_text: str) -> dspy.Prediction:
        lm = getattr(dspy.settings, "lm", None)
        history_start = len(getattr(lm, "history", None) or []) if lm is not None else 0
        try:
            result = self.predictor(corpus_text=corpus_text)
        except CardGenError:
            raise
        except AdapterParseError as e:
            reason = _finish_reason_hit_limit(lm, history_start)
            if reason:
                raise CardGenTruncation(
                    f"LM output hit the token limit (finish_reason={reason}); "
                    "the payload is incomplete and will not be repaired"
                ) from e
            raise CardGenParseError(
                f"LM output could not be parsed: {str(e)[:300]}"
            ) from e
        except Exception as e:
            if _is_timeout_error(e):
                raise CardGenTimeout(f"LM call timed out: {e}") from e
            raise

        # Parsing succeeded, but if the winning call exhausted its output
        # budget the payload may have been truncated mid-stream and then
        # "repaired" by the adapter's JSON fixer into a valid-looking (but
        # incomplete) finding. Incomplete means failed — never keep it.
        reason = _finish_reason_hit_limit(lm, history_start, last_only=True)
        if reason:
            raise CardGenTruncation(
                f"LM output hit the token limit (finish_reason={reason}); "
                "refusing to silently repair a possibly-incomplete payload"
            )

        raw = getattr(result, self.output_field, None) or []
        items = []
        for item in raw:
            if isinstance(item, self.item_model):
                items.append(item)
                continue
            try:
                items.append(self.item_model(**item))
            except (pydantic.ValidationError, TypeError) as e:
                # A malformed item is a parse failure for the whole finding —
                # silently dropping it would shrink the payload into a
                # valid-looking but incomplete result.
                raise CardGenParseError(
                    f"{self.item_model.__name__} item failed validation: {str(e)[:300]}"
                ) from e
        return dspy.Prediction(**{self.output_field: items})
