"""Tests for bin/generators/workorder_gen.py — WorkOrder-producing generator.

WorkOrderGen subclasses CardGen, so the full failure taxonomy is already
covered by test_card_gen_failure_classes.py; these tests lock the subclass
wiring (signature, item model, output field, contract) and draft validation.
"""
import dspy
import pytest

from tests.conftest import _scripted_lm

def _add_prompt(fake_langfuse, monkeypatch, text="Extract work orders."):
    fake_langfuse.add_prompt(
        "card-generator:wo", text=text, version=1, labels=["production"]
    )
    monkeypatch.setattr(
        "bin.prompts._client_factory", lambda: fake_langfuse.as_client()
    )

def test_workorder_gen_appends_contract_to_prompt(fake_langfuse, monkeypatch):
    _add_prompt(fake_langfuse, monkeypatch, text="Domain-specific brief.")
    from bin.generators.workorder_gen import WorkOrderGen

    g = WorkOrderGen(prompt_name="card-generator:wo")
    instructions = g.signature.instructions
    assert "Domain-specific brief." in instructions
    assert "work_orders" in instructions
    assert "Never invent links, requesters, reviewers, commits, or dates" in instructions

def test_workorder_gen_forward_returns_drafts(fake_langfuse, monkeypatch):
    _add_prompt(fake_langfuse, monkeypatch)
    dspy.settings.configure(lm=_scripted_lm({
        "work_orders": [
            {
                "title": "Guard divide() against b == 0",
                "goal": "Callers get a defined error instead of a crash.",
                "plan": "1. Add guard\n2. Tests",
                "work_type": "bug-fix",
                "priority": "P1",
                "priority_rationale": "common input crashes caller",
                "evidence": "math_utils.py:12 has no zero check",
                "files": ["src/math_utils.py"],
                "acceptance_criteria": ["divide(1, 0) raises ValueError"],
                "risks": [],
            }
        ],
    }))
    from bin.generators.workorder_gen import WorkOrderGen
    from bin.workorder import WorkOrderDraft

    g = WorkOrderGen(prompt_name="card-generator:wo")
    result = g(corpus_text="def divide(a, b): return a / b")
    assert isinstance(result.work_orders, list)
    assert len(result.work_orders) == 1
    draft = result.work_orders[0]
    assert isinstance(draft, WorkOrderDraft)
    assert draft.work_type == "bug-fix"
    assert draft.priority == "P1"
    assert not hasattr(draft, "domain")
    assert not hasattr(draft, "created_at")

def test_workorder_gen_invalid_item_is_parse_error(fake_langfuse, monkeypatch):
    _add_prompt(fake_langfuse, monkeypatch)
    dspy.settings.configure(lm=_scripted_lm({
        "work_orders": [{"title": "only a title, no goal or plan"}],
    }))
    from bin.generators.card_gen import CardGenParseError
    from bin.generators.workorder_gen import WorkOrderGen

    g = WorkOrderGen(prompt_name="card-generator:wo")
    with pytest.raises(CardGenParseError):
        g(corpus_text="anything")

def test_workorder_gen_empty_list_is_valid(fake_langfuse, monkeypatch):
    _add_prompt(fake_langfuse, monkeypatch)
    dspy.settings.configure(lm=_scripted_lm({"work_orders": []}))
    from bin.generators.workorder_gen import WorkOrderGen

    g = WorkOrderGen(prompt_name="card-generator:wo")
    result = g(corpus_text="perfectly fine code")
    assert result.work_orders == []
