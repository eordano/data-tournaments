"""WorkflowSpec: a typed, judgeable plan of steps — the artifact, not the run.

A WorkflowSpec describes WHAT should happen (steps, dependencies, capability
allowlists, which steps require human approval). It carries NO runtime state:
no approvals granted, no execution status — that lives in the workflow
runtime (Temporal). Deploy and rollback steps are forced to
``needs_approval=True`` at validation time so a model emitting a spec cannot
smuggle in an unattended deploy.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

import pydantic

from bin.landscape.canonical import content_digest

class StepKind(str, Enum):
    GATHER = "gather"
    RETRIEVE = "retrieve"
    AGENT = "agent"
    TOOL = "tool"
    SANDBOX = "sandbox"
    HUMAN_APPROVAL = "human_approval"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"

APPROVAL_REQUIRED_KINDS = frozenset({StepKind.DEPLOY, StepKind.ROLLBACK})

class WorkflowStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    id: str
    kind: StepKind
    description: str = ""
    capabilities: tuple[str, ...] = ()
    sandbox_profile: Optional[str] = None
    needs_approval: bool = False
    depends_on: tuple[str, ...] = ()

    @pydantic.field_validator("id")
    @classmethod
    def _nonempty_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("step id must be non-empty")
        return v

    @pydantic.model_validator(mode="after")
    def _force_approval_for_dangerous_kinds(self) -> "WorkflowStep":
        if self.kind in APPROVAL_REQUIRED_KINDS and not self.needs_approval:
            object.__setattr__(self, "needs_approval", True)
        return self

    def _content_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "sandbox_profile": self.sandbox_profile,
            "needs_approval": self.needs_approval,
            "depends_on": sorted(self.depends_on),
        }

class WorkflowSpec(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    version: int = 1
    steps: tuple[WorkflowStep, ...] = ()

    @pydantic.field_validator("name")
    @classmethod
    def _nonempty_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("workflow name must be non-empty")
        return v

    @pydantic.model_validator(mode="after")
    def _validate_dag(self) -> "WorkflowSpec":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        known = set(ids)
        for step in self.steps:
            missing = [d for d in step.depends_on if d not in known]
            if missing:
                raise ValueError(
                    f"step {step.id!r} depends on unknown step(s): {missing}"
                )
        deps = {step.id: step.depends_on for step in self.steps}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(deps, WHITE)
        for start in deps:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = GRAY
            while stack:
                node, i = stack[-1]
                if i < len(deps[node]):
                    stack[-1] = (node, i + 1)
                    nxt = deps[node][i]
                    if color[nxt] == GRAY:
                        raise ValueError(
                            f"dependency cycle involving step {nxt!r}"
                        )
                    if color[nxt] == WHITE:
                        color[nxt] = GRAY
                        stack.append((nxt, 0))
                else:
                    color[node] = BLACK
                    stack.pop()
        return self

    def _content_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "steps": [step._content_payload() for step in self.steps],
        }

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        return content_digest(self._content_payload())
