"""DSPy program: draft a new domain (name + generator + judge prompts) from
a one-line description plus a peek at a few corpus samples.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import dspy

from bin import prompts as _prompts

@dataclass
class DomainDraft:
    domain_name: str
    generator_prompt: str
    judge_prompt: str

class DomainBuilderSig(dspy.Signature):
    """Draft a new card-prioritization domain.

    Given a description of what the user wants to extract, plus metadata
    about the corpus, output a domain name (short, hyphenated) and the
    two prompts (generator and judge) that will run the domain.
    """

    description: str = dspy.InputField(desc="One-line description of the domain.")
    corpus_kind: str = dspy.InputField(desc="One of: sqlite, filesystem, inline.")
    corpus_samples: list[dict] = dspy.InputField(
        desc="A few representative corpus items so the LLM can see the actual data shape."
    )

    domain_name: str = dspy.OutputField(
        desc="Short, lowercase, hyphen-separated. ≤64 chars."
    )
    generator_prompt: str = dspy.OutputField(
        desc="System prompt for the per-corpus-item card generator."
    )
    judge_prompt: str = dspy.OutputField(
        desc="System prompt for the pair-comparison judge."
    )

def _normalize_name(s: str) -> str:
    """lowercase, alphanumeric+hyphen, collapse runs, ≤64 chars."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]

class DomainBuilder(dspy.Module):
    def __init__(self, prompt_name: str = "domain-builder",
                 prompt_label: str = "production"):
        super().__init__()
        try:
            instructions = _prompts.get(prompt_name, label=prompt_label)
        except LookupError:
            from bin.judgement import SEED_DOMAIN_BUILDER_INSTRUCTIONS
            instructions = SEED_DOMAIN_BUILDER_INSTRUCTIONS
        self.signature = DomainBuilderSig.with_instructions(
            instructions
        )
        self.predictor = dspy.Predict(self.signature)

    def draft(self, *, description: str, corpus_kind: str,
              corpus_samples: list[dict]) -> DomainDraft:
        result = self.predictor(
            description=description,
            corpus_kind=corpus_kind,
            corpus_samples=corpus_samples,
        )
        return DomainDraft(
            domain_name=_normalize_name(result.domain_name or "unnamed"),
            generator_prompt=(result.generator_prompt or "").strip(),
            judge_prompt=(result.judge_prompt or "").strip(),
        )
