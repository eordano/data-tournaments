"""DSPy program: extract WorkOrder drafts from a piece of corpus text.

Subclasses CardGen to inherit the full failure taxonomy (timeout /
parse-error / truncation, no silent repair) while producing
``WorkOrderDraft`` objects — the model-supplied portion of a work order.
Provenance (domain, date, models, base commits) is stamped later by
``bin.workorder.finalize_work_order``; the draft schema has no such fields,
so the model cannot invent them.
"""
from __future__ import annotations

import dspy

from bin.generators.card_gen import CardGen
from bin.workorder import WorkOrderDraft

#: Appended to every generator prompt (see card_gen.OUTPUT_CONTRACT for the
#: rationale — answer-first, bounded, payload before any prose).
WORK_ORDER_CONTRACT = (
    "Output contract: emit the machine-parseable `work_orders` payload "
    "immediately, before any prose, analysis, or explanation. Keep the "
    "payload bounded: at most 3 work orders per corpus item; only findings "
    "worth a real engineering work order. Each work order needs: title "
    "(\u2264120 chars, specific), goal (what outcome and why it matters), "
    "plan (numbered implementation steps), work_type (bug-fix | feature | "
    "change-request | refactor | investigation), priority (P0..P3) with "
    "priority_rationale, evidence (concrete code locations / behaviors from "
    "the corpus item), files (approximate paths to modify), "
    "acceptance_criteria, and risks. If nothing warrants a work order, emit "
    "an empty list. Never invent links, requesters, reviewers, commits, or "
    "dates — those are supplied by the system. Any commentary must come "
    "after the payload, never before it."
)


class WorkOrderGenSig(dspy.Signature):
    """Extract zero or more engineering work orders from a corpus item.

    A work order is a self-contained, actionable unit of engineering work:
    a goal worth pursuing, evidence grounding it, and a concrete
    implementation plan. Return an empty list when the corpus item warrants
    no work.
    """

    corpus_text: str = dspy.InputField(desc="The corpus item to analyze.")
    work_orders: list[WorkOrderDraft] = dspy.OutputField(
        desc=(
            "Zero to three WorkOrderDraft objects. Prefer one well-grounded "
            "work order over several thin ones."
        )
    )


class WorkOrderGen(CardGen):
    """WorkOrder-producing generator with CardGen's hardened failure paths."""

    signature_cls = WorkOrderGenSig
    item_model = WorkOrderDraft
    output_field = "work_orders"
    contract = WORK_ORDER_CONTRACT
