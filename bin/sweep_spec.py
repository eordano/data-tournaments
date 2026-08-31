"""SweepSpec: the declared, versioned configuration of a review sweep.

A sweep is a campaign whose behavior is DATA, not convention
(docs/design/sweeps.md). The August bugsweeps hardcoded their process —
three adversarial lenses, one repair cycle, RED/GREEN validation, human
publish gate — in the operator's head; a SweepSpec makes each of those a
declared field so the same machinery runs performance sweeps, feature
reviews (foundry stories), and hot-or-slop quality judging.

The spec is frozen and content-addressed like every landscape artifact:
``digest()`` hashes the canonical JSON form, and bin/campaigns.py stores
both on the campaign row (``spec_json``/``spec_digest``) at creation, so a
running sweep can never drift from the spec it was launched with.

Round discipline is the load-bearing part: ``rounds.max`` caps review
rounds (the anti-11-serial-rounds rule), ``rounds.batching`` requires every
configured lens to report inside a round before it can close, and
``rounds.convergence`` names the criterion under which a closing round
counts as converged. bin/campaigns.py enforces all three.
"""
from __future__ import annotations

from typing import Literal, Optional

import pydantic

from bin.landscape.canonical import canonical_json, content_digest

SWEEP_KINDS = ("bugsweep", "perfsweep", "featuresweep", "slopsweep")

CONVERGENCE_CRITERIA = ("no_new_confirmed_findings", "all_findings_settled")

class _Frozen(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

class LensSpec(_Frozen):
    """One adversarial review lens. ``prompt_ref`` is a prompt-registry
    reference (``name`` or ``name@label``, bin/prompts.py) — lens prompts
    are versioned data, never inline strings."""

    name: str
    prompt_ref: str
    burden: Literal["refute", "confirm"] = "refute"

    @pydantic.field_validator("name", "prompt_ref")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must be non-empty")
        return v

class HumanPanelSpec(_Frozen):
    """Human judging leg: which EvalTemplate rubric feeds the wheel and
    whether a human verdict is required for quorum."""

    rubric: str = ""
    judgement_kind: Literal["pair", "single"] = "single"
    required: bool = True

class PanelSpec(_Frozen):
    lenses: tuple[LensSpec, ...]
    human: Optional[HumanPanelSpec] = None
    quorum: Literal["all_lenses", "all_lenses_and_human"] = "all_lenses"

    @pydantic.field_validator("lenses")
    @classmethod
    def _at_least_one(cls, v: tuple) -> tuple:
        if not v:
            raise ValueError("panel.lenses must name at least one lens")
        names = [lens.name for lens in v]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate lens names: {names}")
        return v

    @pydantic.model_validator(mode="after")
    def _quorum_needs_human(self) -> "PanelSpec":
        if self.quorum == "all_lenses_and_human" and self.human is None:
            raise ValueError("quorum 'all_lenses_and_human' requires panel.human")
        return self

class RoundsSpec(_Frozen):
    """Round structure. ``repair_max_cycles_per_finding`` is a binary
    toggle over the one-repair-cycle rule, whose enforced unit is
    PER-REFUTE depth: 1 keeps the rule (each REFUTE may be answered by at
    most one repair, backstopped by a unique index), 0 forbids repairs
    entirely. It does NOT cap total repairs across a finding's lifetime —
    that is bounded by ``max`` × panel size, which is the actual
    anti-drip guarantee."""

    max: int = 3
    batching: Literal["required", "none"] = "required"
    convergence: Literal["no_new_confirmed_findings", "all_findings_settled"] = (
        "no_new_confirmed_findings"
    )
    repair_max_cycles_per_finding: int = 1

    @pydantic.field_validator("max")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("rounds.max must be >= 1")
        return v

    @pydantic.field_validator("repair_max_cycles_per_finding")
    @classmethod
    def _zero_or_one(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError(
                "repair_max_cycles_per_finding must be 0 or 1 "
                "(the one-repair-cycle rule is a ceiling)"
            )
        return v

class PerfBudget(_Frozen):
    """One quantitative budget: ``measured`` vs ``budget`` under
    ``direction`` ('max': measured <= budget passes; 'min': >=)."""

    metric: str
    budget: float = pydantic.Field(allow_inf_nan=False)
    direction: Literal["max", "min"] = "max"

    @pydantic.field_validator("metric")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("metric must be non-empty")
        return v

class ValidationSpec(_Frozen):
    mode: Literal["red_green", "perf_budget", "rubric_only"] = "red_green"
    perf_budgets: tuple[PerfBudget, ...] = ()

    @pydantic.model_validator(mode="after")
    def _budgets_iff_perf(self) -> "ValidationSpec":
        if self.mode == "perf_budget" and not self.perf_budgets:
            raise ValueError("validation.mode 'perf_budget' requires perf_budgets")
        if self.mode != "perf_budget" and self.perf_budgets:
            raise ValueError("perf_budgets are only valid with mode 'perf_budget'")
        return self

class CorpusSourceSpec(_Frozen):
    """One corpus source: an intake adapter kind plus its config. The
    adapter name is validated at ingest time against the live registry
    (bin/campaign_intake.py), not here — the spec stays importable without
    the adapter stack."""

    adapter: str
    config: dict = pydantic.Field(default_factory=dict)

    @pydantic.field_validator("adapter")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("adapter must be non-empty")
        return v

class IntakeSpec(_Frozen):
    max_candidates: int = 30
    rationale_required: bool = True

    @pydantic.field_validator("max_candidates")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("intake.max_candidates must be >= 1")
        return v

class PublishSpec(_Frozen):
    gate: Literal["human", "none"] = "human"
    granularity: Literal["branch-per-finding", "pr-per-finding", "report-only"] = (
        "branch-per-finding"
    )


class RunnerSpec(_Frozen):
    """How the sweep's agent loop is enacted. Absent runner == manual
    (humans/ad-hoc agents drive the CLI). ``driver`` names the agent
    runtime; ``parallel`` caps concurrent lens workers per round;
    ``model`` optionally pins the runtime's model. The runner NEVER owns
    convergence — the round guards do; a driver is just hands."""

    driver: Literal["opencode", "claude-workflow"]
    model: str = ""
    parallel: int = 4

    @pydantic.field_validator("parallel")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("runner.parallel must be >= 1")
        return v

class SweepSpec(_Frozen):
    kind: Literal["bugsweep", "perfsweep", "featuresweep", "slopsweep"]
    corpus: tuple[CorpusSourceSpec, ...] = ()
    intake: IntakeSpec = IntakeSpec()
    panel: PanelSpec
    rounds: RoundsSpec = RoundsSpec()
    validation: ValidationSpec = ValidationSpec()
    publish: PublishSpec = PublishSpec()
    runner: Optional[RunnerSpec] = None

    @pydantic.model_validator(mode="after")
    def _kind_validation_fit(self) -> "SweepSpec":
        if self.kind == "perfsweep" and self.validation.mode != "perf_budget":
            raise ValueError("perfsweep requires validation.mode 'perf_budget'")
        if self.kind == "slopsweep" and self.validation.mode == "red_green":
            raise ValueError(
                "slopsweep judges artifacts, not patches — validation.mode "
                "must be 'rubric_only' (or 'perf_budget')"
            )
        return self

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

def validate_spec(payload: dict) -> SweepSpec:
    """Parse+validate a raw spec dict. Raises pydantic.ValidationError with
    field paths on any violation — a malformed spec must fail at campaign
    creation, never at round three."""
    return SweepSpec.model_validate(payload)
