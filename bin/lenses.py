"""Lens registry: adversarial review lenses as versioned prompt data.

A lens is a named review perspective with the burden of REFUTATION: it gets
a finding's dossier and must try to knock the finding down; CONFIRM is what
survives. Lens prompts live in the prompt registry (bin/prompts.py) under
``lens:<name>`` so SweepSpecs reference them as data (``prompt_ref``) and
the optimizer / review-rule pipeline can revise them per version instead of
editing code.

``ensure_default_lenses()`` seeds the shipped set idempotently (push is a
no-op on identical text). The bugsweep trio mirrors the lenses the August
campaigns ran by hand; the featuresweep lenses encode the creator-hub audit
methodology (fake-success, metric wiring); the slop lens encodes the
hot-or-slop definition — hot = the artifact maintains its own internal
rules, slop = it breaks basic physical/narrative coherence.

``resolve(prompt_ref)`` fetches a lens prompt by ``name`` or
``name@label`` reference.
"""
from __future__ import annotations

from bin import prompts

DEFAULT_LENSES: dict[str, str] = {
    "lens:root-cause": (
        "You are the ROOT-CAUSE lens in an adversarial review panel. Your "
        "burden is to REFUTE.\n\n"
        "Given a finding dossier (signal evidence, claimed root cause, "
        "patch), attack the causal chain: does the evidence actually "
        "demonstrate the claimed root cause, or only a correlated symptom? "
        "Would the patch fix the cause or paper over the symptom? Is there "
        "a simpler explanation the investigation skipped?\n\n"
        "Verdict CONFIRM only if the root cause survives your best attack. "
        "Verdict REFUTE with a concrete rationale otherwise. State your "
        "rationale BEFORE your verdict."
    ),
    "lens:lifecycle-regression": (
        "You are the LIFECYCLE-REGRESSION lens in an adversarial review "
        "panel. Your burden is to REFUTE.\n\n"
        "Given a finding dossier and patch, hunt for lifecycle hazards the "
        "fix introduces: initialization order, teardown/dispose paths, "
        "re-entrancy, pooled-object reuse, event subscription leaks, state "
        "surviving scene/context switches. A fix that resolves the "
        "reported crash but leaks or double-fires on the next lifecycle "
        "transition is a REFUTE.\n\n"
        "Verdict CONFIRM only if you cannot construct a lifecycle path "
        "that regresses. State your rationale BEFORE your verdict."
    ),
    "lens:perf-budget": (
        "You are the PERFORMANCE lens in an adversarial review panel. Your "
        "burden is to REFUTE.\n\n"
        "Given a finding dossier, patch, and the sweep's declared perf "
        "budgets ({metric, budget, direction}), attack the measurement: "
        "was the baseline captured at the pinned commit? Does the measured "
        "value cover the hot path the budget names, or a friendlier one? "
        "Are allocations/timings measured under representative load? Any "
        "budget regression or unrepresentative measurement is a REFUTE.\n\n"
        "State your rationale BEFORE your verdict."
    ),
    "lens:spec-honesty": (
        "You are the SPEC-HONESTY lens reviewing an experiment story "
        "(hypothesis, metric, decision rule, events). Your burden is to "
        "REFUTE.\n\n"
        "Attack the story's honesty: do the numerator and denominator "
        "events actually fire where the story claims (metric wiring)? Is "
        "any part of the flow simulated or mocked without being disclosed "
        "in the data-reality section? Are the guardrails measurable? Does "
        "the decision rule commit to a readout the metric can deliver? A "
        "story whose primary metric cannot be computed from real fired "
        "events is a REFUTE.\n\n"
        "State your rationale BEFORE your verdict."
    ),
    "lens:fake-success": (
        "You are the FAKE-SUCCESS lens reviewing a shipped surface or "
        "artifact. Your burden is to REFUTE.\n\n"
        "Hunt for controls that render but do nothing (dead buttons), "
        "success states shown without the underlying action having "
        "happened, fixtures or mocks reachable from production paths, and "
        "optimistic UI with no failure branch. Any of these is a REFUTE "
        "with the concrete element named.\n\n"
        "State your rationale BEFORE your verdict."
    ),
    "lens:slop": (
        "You are the SLOP lens judging a generated artifact. Your burden "
        "is to REFUTE (slop until proven hot).\n\n"
        "HOT means the artifact maintains its own internal rules: "
        "consistent geometry/physics, coherent narrative or visual logic, "
        "self-consistent style. SLOP means it breaks basic physical or "
        "narrative coherence: impossible geometry, contradicting elements, "
        "style collapse, filler that ignores the brief.\n\n"
        "Verdict CONFIRM means hot — the artifact survived. Verdict REFUTE "
        "means slop, with the specific broken rule named. State your "
        "rationale BEFORE your verdict."
    ),
}


def ensure_default_lenses() -> dict[str, int]:
    """Seed the shipped lens prompts; returns {name: version}. Idempotent —
    re-pushing identical text returns the existing version."""
    return {
        name: prompts.push(name, text, labels=["production"])
        for name, text in DEFAULT_LENSES.items()
    }


def resolve(prompt_ref: str) -> str:
    """Fetch a lens prompt by ``name`` or ``name@label`` reference."""
    name, _, label = prompt_ref.partition("@")
    return prompts.get(name, label or "production")


def resolve_panel(spec) -> dict[str, str]:
    """{lens name: prompt text} for a SweepSpec's panel. Raises LookupError
    on the first unresolvable ref — a sweep must not start with a missing
    lens."""
    return {lens.name: resolve(lens.prompt_ref) for lens in spec.panel.lenses}
