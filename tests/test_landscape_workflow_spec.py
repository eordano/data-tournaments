"""Tests for bin.landscape.workflow_spec — steps, DAG validation, digests."""
from __future__ import annotations

import pydantic
import pytest

from bin.landscape.workflow_spec import (
    APPROVAL_REQUIRED_KINDS,
    StepKind,
    WorkflowSpec,
    WorkflowStep,
)


def step(id: str, kind: StepKind = StepKind.TOOL, **kw) -> WorkflowStep:
    return WorkflowStep(id=id, kind=kind, **kw)


class TestWorkflowStep:
    def test_deploy_and_rollback_force_needs_approval(self):
        for kind in (StepKind.DEPLOY, StepKind.ROLLBACK):
            assert step("s", kind).needs_approval is True
            assert step("s", kind, needs_approval=False).needs_approval is True

    def test_approval_required_kinds_constant(self):
        assert APPROVAL_REQUIRED_KINDS == {StepKind.DEPLOY, StepKind.ROLLBACK}

    def test_other_kinds_default_no_approval(self):
        for kind in set(StepKind) - APPROVAL_REQUIRED_KINDS:
            assert step("s", kind).needs_approval is False

    def test_needs_approval_optin_preserved(self):
        assert step("s", StepKind.TOOL, needs_approval=True).needs_approval

    def test_frozen(self):
        s = step("s")
        with pytest.raises(pydantic.ValidationError):
            s.needs_approval = False
        with pytest.raises(pydantic.ValidationError):
            s.capabilities = ("network",)

    def test_empty_id_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            step("  ")

    def test_step_kind_values(self):
        assert {k.value for k in StepKind} == {
            "gather", "retrieve", "agent", "tool", "sandbox",
            "human_approval", "deploy", "rollback",
        }


class TestWorkflowSpecValidation:
    def test_unknown_dependency_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="unknown step"):
            WorkflowSpec(
                name="w",
                steps=(step("a", depends_on=("ghost",)),),
            )

    def test_duplicate_ids_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="duplicate"):
            WorkflowSpec(name="w", steps=(step("a"), step("a")))

    def test_cycle_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="cycle"):
            WorkflowSpec(
                name="w",
                steps=(
                    step("a", depends_on=("c",)),
                    step("b", depends_on=("a",)),
                    step("c", depends_on=("b",)),
                ),
            )

    def test_self_cycle_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="cycle"):
            WorkflowSpec(name="w", steps=(step("a", depends_on=("a",)),))

    def test_valid_dag_accepted(self):
        spec = WorkflowSpec(
            name="release",
            version=2,
            steps=(
                step("gather", StepKind.GATHER),
                step("plan", StepKind.AGENT, depends_on=("gather",)),
                step("verify", StepKind.SANDBOX, depends_on=("plan",),
                     sandbox_profile="linux-microvm",
                     capabilities=("fs_read", "build")),
                step("gate", StepKind.HUMAN_APPROVAL, depends_on=("verify",)),
                step("ship", StepKind.DEPLOY, depends_on=("gate", "verify")),
            ),
        )
        assert spec.steps[-1].needs_approval is True
        assert len(spec.digest) == 64

    def test_empty_name_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            WorkflowSpec(name=" ")


class TestSpecDigest:
    def test_deterministic_across_reconstruction(self):
        make = lambda: WorkflowSpec(
            name="w",
            steps=(step("a"), step("b", depends_on=("a",))),
        )
        assert make().digest == make().digest

    def test_depends_on_order_does_not_matter(self):
        s1 = WorkflowSpec(
            name="w",
            steps=(step("a"), step("b"), step("c", depends_on=("a", "b"))),
        )
        s2 = WorkflowSpec(
            name="w",
            steps=(step("a"), step("b"), step("c", depends_on=("b", "a"))),
        )
        assert s1.digest == s2.digest

    def test_content_change_changes_digest(self):
        base = WorkflowSpec(name="w", steps=(step("a"),))
        assert base.digest != WorkflowSpec(name="w", version=2, steps=(step("a"),)).digest
        assert base.digest != WorkflowSpec(name="w2", steps=(step("a"),)).digest
        assert (
            base.digest
            != WorkflowSpec(name="w", steps=(step("a", description="x"),)).digest
        )


class TestImmutabilityAndRoundTrip:
    def test_spec_mutation_raises(self):
        spec = WorkflowSpec(name="w", steps=(step("a"),))
        with pytest.raises(pydantic.ValidationError):
            spec.name = "hijacked"
        with pytest.raises(pydantic.ValidationError):
            spec.steps = ()

    def test_round_trip(self):
        spec = WorkflowSpec(
            name="release",
            steps=(step("a", StepKind.GATHER), step("d", StepKind.DEPLOY,
                                                     depends_on=("a",))),
        )
        again = WorkflowSpec.model_validate(spec.model_dump())
        assert again == spec
        assert again.digest == spec.digest
        json_again = WorkflowSpec.model_validate_json(spec.model_dump_json())
        assert json_again.digest == spec.digest

    def test_no_approval_or_authorization_state_fields(self):
        # needs_approval is a REQUIREMENT (policy), not a grant. Actual
        # approval state lives in the runtime, never in the artifact.
        forbidden = {
            "approved", "approvals", "approved_by", "approval_granted",
            "authorized", "authorization", "approval_state", "status",
        }
        assert not forbidden & set(WorkflowStep.model_fields)
        assert not forbidden & set(WorkflowSpec.model_fields)
