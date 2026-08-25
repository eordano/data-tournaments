"""Bridge between Temporal activities and the generation/judging pipeline.

Deliberately imports NO temporalio and lazy-imports the generation stack:

- Activities (bin/release_workflow/activities.py) run in a worker venv that
  has temporalio but may lack the generation deps (dspy etc.).
- The root test suite has the generation deps but not temporalio.

Putting the real logic here keeps it testable in the root suite and lets
the activity remain a thin, retry-annotated wrapper. Failures are encoded
in the returned dict (never raised past the boundary) so the workflow's
judging gate — not an activity retry storm — decides the outcome.
"""
from __future__ import annotations

from typing import Any


def run_generation(domain: str, *, limit: int | None = None) -> dict[str, Any]:
    """Run the REAL work-order generation pipeline for ``domain``.

    Returns a plain JSON-safe dict (Temporal payload friendly):
      work_order_ids  synthetic stable ids (generation does not mint ids;
                      pairs land in pending_judgement for the judge UI)
      generated/enqueued/errors/failures/aborted_reason  from GenerateResult
      summary         one human line for stage detail
      unavailable     non-empty when the generation stack cannot run here
                      (missing deps / import failure) — an honest signal,
                      never fake success
    """
    try:
        from bin import generate_cards  # lazy: dspy etc.
    except Exception as exc:  # ImportError and transitive config errors
        return {
            "work_order_ids": [],
            "generated": 0,
            "enqueued": 0,
            "errors": 0,
            "failures": {},
            "aborted_reason": "",
            "unavailable": f"generation stack unavailable: {type(exc).__name__}: {exc}",
            "summary": "generation unavailable in this worker environment",
        }
    try:
        result = generate_cards.run(domain, limit=limit, artifact="work-order")
    except Exception as exc:  # domain missing, corpus errors, ...
        return {
            "work_order_ids": [],
            "generated": 0,
            "enqueued": 0,
            "errors": 1,
            "failures": {"error": 1},
            "aborted_reason": f"{type(exc).__name__}: {exc}",
            "unavailable": "",
            "summary": f"generation failed: {type(exc).__name__}: {exc}",
        }
    ids = [f"wo-{domain}-{i + 1}" for i in range(result.cards_generated)]
    breakdown = (
        " [" + ", ".join(f"{k}={v}" for k, v in sorted(result.failures.items())) + "]"
        if result.failures
        else ""
    )
    summary = (
        f"generated {result.cards_generated} work orders, "
        f"enqueued {result.pairs_enqueued} pairs, "
        f"{result.errors} failures{breakdown}"
    )
    if result.aborted_reason:
        summary += f"; ABORTED: {result.aborted_reason}"
    return {
        "work_order_ids": ids,
        "generated": result.cards_generated,
        "enqueued": result.pairs_enqueued,
        "errors": result.errors,
        "failures": dict(result.failures),
        "aborted_reason": result.aborted_reason,
        "unavailable": "",
        "summary": summary,
    }


def gate_verdict(
    *,
    work_order_ids: list[str],
    aborted_reason: str = "",
    unavailable: str = "",
    errors: int = 0,
    generated: int = 0,
) -> tuple[bool, float, str]:
    """Batch-level judging gate: (passed, score, rationale).

    HONEST gate semantics (replaces the auto-pass-at-0.92 stub):
      - systemic abort (provider down/unauthenticated)  -> FAIL
      - generation stack unavailable in the worker      -> FAIL
      - zero work orders produced                       -> FAIL
      - otherwise pass with score = success ratio; the LLM tournament
        judging of the enqueued pairs happens in the judge UI /
        drain_llm_queue — that verdict is deliberately NOT faked here.
    """
    if aborted_reason:
        return False, 0.0, f"generation aborted systemically: {aborted_reason}"
    if unavailable:
        return False, 0.0, unavailable
    if not work_order_ids:
        return False, 0.0, "no work orders generated — nothing to release"
    total = generated + errors
    score = round(generated / total, 3) if total else 1.0
    rationale = (
        f"{generated} work orders generated ({errors} item failures); "
        "pairs enqueued for tournament judging in the judge UI"
    )
    return True, score, rationale
