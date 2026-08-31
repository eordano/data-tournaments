"""Tests for bin/release_workflow/generation_bridge.py — the temporalio-free
bridge activities call for REAL generation + an honest judging gate
(replaces the auto-pass-at-0.92 stub semantics)."""
from __future__ import annotations

import pytest

from bin.release_workflow import generation_bridge as gb

@pytest.fixture
def bridge_fabric(fake_langfuse, monkeypatch, tmp_data_home):
    """Mirror of test_generate_cards.fresh_fabric_with_domain: fake Langfuse
    wired into bin.prompts so prompt pushes/fetches never touch a network."""
    monkeypatch.setattr(
        "bin.prompts._client_factory", lambda: fake_langfuse.as_client()
    )
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib
    import judgement

    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home

def test_run_generation_unknown_domain_reports_error(bridge_fabric):
    out = gb.run_generation("no-such-domain")
    assert out["work_order_ids"] == []
    assert out["aborted_reason"]
    assert "no-such-domain" in out["aborted_reason"] or out["summary"]
    assert out["unavailable"] == ""

def test_run_generation_real_pipeline(bridge_fabric, monkeypatch):
    """Drive the ACTUAL generate_cards.run through the bridge with a
    drafting stub generator — proves the wiring end-to-end in-process."""
    from bin import domains, prompts
    from bin.workorder import WorkOrderDraft
    import bin.generate_cards as gc

    prompts.push("card-generator:bridge-domain", "x", labels=["production"])
    prompts.push("judge-instructions:bridge-domain", "y", labels=["production"])
    domains.create_domain(
        name="bridge-domain", description="",
        corpus_source={"kind": "inline", "items": [{"text": "a"}, {"text": "b"}]},
    )

    class DraftingGen:
        signature = type("Sig", (), {"instructions": "draft work orders"})()

        def __init__(self, **_kw):
            pass

        def __call__(self, **_kw):
            return type("P", (), {"work_orders": [
                WorkOrderDraft(title="t", goal="g", plan="p")
            ]})()

    monkeypatch.setattr(gc, "WorkOrderGen", DraftingGen)
    out = gb.run_generation("bridge-domain")
    assert out["generated"] == 2
    assert out["work_order_ids"] == ["wo-bridge-domain-1", "wo-bridge-domain-2"]
    assert out["enqueued"] >= 1
    assert out["unavailable"] == "" and out["aborted_reason"] == ""
    assert "generated 2 work orders" in out["summary"]

def test_gate_fails_on_systemic_abort():
    passed, score, why = gb.gate_verdict(
        work_order_ids=["a"], aborted_reason="AuthenticationError: no key"
    )
    assert passed is False and score == 0.0
    assert "AuthenticationError" in why

def test_gate_fails_when_generation_unavailable():
    passed, score, why = gb.gate_verdict(
        work_order_ids=[], unavailable="generation stack unavailable: ImportError"
    )
    assert passed is False
    assert "unavailable" in why

def test_gate_fails_on_zero_workorders():
    passed, score, why = gb.gate_verdict(work_order_ids=[], generated=0)
    assert passed is False
    assert "no work orders" in why

def test_gate_passes_with_success_ratio_score():
    passed, score, why = gb.gate_verdict(
        work_order_ids=["a", "b", "c"], generated=3, errors=1
    )
    assert passed is True
    assert score == 0.75
    assert "tournament judging" in why

def test_gate_never_reports_the_stub_constant():
    """The old stub always returned 0.92 — the honest gate must derive its
    score from the batch, so a full success is 1.0, not 0.92."""
    passed, score, _ = gb.gate_verdict(work_order_ids=["a"], generated=1, errors=0)
    assert passed is True and score == 1.0
