"""Provider-neutral Goal/Plan contracts: immutable snapshots with graph-safe steps.

A ``Goal`` states *what outcome* is wanted and how success will be judged. A
``Plan`` is one versioned proposal of steps toward one goal. A ``PlanStep``
names an objective, target, executor Role and canonical capability, exactly
like a ``TaskDelegationRequest``; it never names a provider, credential or
approval, and holding a plan authorizes nothing.

These are pure value objects. They do not persist, plan, delegate or execute,
and statuses are caller-reported snapshots: a ``SUCCEEDED`` step or an
``ACHIEVED`` goal is not evidence that work happened or criteria were verified.

This is separate from the event ``Planner`` (``ProcessedEvent -> list[Action]``)
and from task delegation.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from apps.server.src.core.actions import ExecutorRole


class GoalPlanContractError(ValueError):
    """A Goal, Plan or PlanStep snapshot is structurally invalid."""


class GoalStatus(StrEnum):
    """Explicit goal lifecycle status as reported by the caller."""

    DRAFT = "draft"
    ACTIVE = "active"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"


class PlanStepStatus(StrEnum):
    """Execution progress of one step as reported by the caller."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Statuses that claim the step has started; its dependencies must have succeeded.
_STARTED_STEP_STATUSES = frozenset(
    {PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED},
)


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise GoalPlanContractError(f"{name} is required")


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise GoalPlanContractError(f"{name} must be a UUID")


def _require_text_tuple(value: object, name: str, *, nonempty: bool) -> None:
    if not isinstance(value, tuple):
        raise GoalPlanContractError(f"{name} must be a tuple")
    if nonempty and not value:
        raise GoalPlanContractError(f"{name} must not be empty")
    for item in value:
        _require_text(item, f"each of {name}")
    normalized = [item.strip().casefold() for item in value]
    if len(set(normalized)) != len(normalized):
        raise GoalPlanContractError(f"{name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class Goal:
    """Immutable goal snapshot: objective, success criteria and lifecycle status."""

    objective: str
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    status: GoalStatus = GoalStatus.DRAFT
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_uuid(self.id, "goal id")
        _require_text(self.objective, "goal objective")
        _require_text_tuple(self.success_criteria, "goal success criteria", nonempty=True)
        _require_text_tuple(self.constraints, "goal constraints", nonempty=False)
        if not isinstance(self.status, GoalStatus):
            raise GoalPlanContractError("goal requires a known status")


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Immutable planned step: what to do and who should do it, never which provider."""

    objective: str
    target: str
    executor_role: ExecutorRole
    capability: str
    depends_on: tuple[UUID, ...] = ()
    status: PlanStepStatus = PlanStepStatus.PENDING
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_uuid(self.id, "plan step id")
        _require_text(self.objective, "plan step objective")
        _require_text(self.target, "plan step target")
        if not isinstance(self.executor_role, ExecutorRole):
            raise GoalPlanContractError("plan step requires a known executor role")
        _require_text(self.capability, "plan step capability")
        if self.capability != self.capability.strip().casefold():
            raise GoalPlanContractError("plan step capability must be a canonical identifier")
        if not isinstance(self.depends_on, tuple):
            raise GoalPlanContractError("plan step dependencies must be a tuple")
        for dependency in self.depends_on:
            _require_uuid(dependency, "plan step dependency")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise GoalPlanContractError("plan step dependencies must not contain duplicates")
        if self.id in self.depends_on:
            raise GoalPlanContractError("plan step must not depend on itself")
        if not isinstance(self.status, PlanStepStatus):
            raise GoalPlanContractError("plan step requires a known status")


@dataclass(frozen=True, slots=True)
class Plan:
    """Versioned, acyclic step graph bound to exactly one goal."""

    goal_id: UUID
    steps: tuple[PlanStep, ...]
    version: int = 1

    def __post_init__(self) -> None:
        _require_uuid(self.goal_id, "plan goal id")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise GoalPlanContractError("plan version must be an integer")
        if self.version < 1:
            raise GoalPlanContractError("plan version must be at least 1")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise GoalPlanContractError("plan requires a nonempty tuple of steps")
        for step in self.steps:
            if not isinstance(step, PlanStep):
                raise GoalPlanContractError("plan steps must be PlanStep instances")
        _validate_graph(self.steps)

    def step(self, step_id: UUID) -> PlanStep:
        """Return the step with ``step_id``; raise ``KeyError`` when absent."""
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def ready_steps(self) -> tuple[PlanStep, ...]:
        """Pending steps whose dependencies have all succeeded, in plan order."""
        succeeded = {step.id for step in self.steps if step.status is PlanStepStatus.SUCCEEDED}
        return tuple(
            step
            for step in self.steps
            if step.status is PlanStepStatus.PENDING
            and all(dependency in succeeded for dependency in step.depends_on)
        )


def _validate_graph(steps: tuple[PlanStep, ...]) -> None:
    by_id: dict[UUID, PlanStep] = {}
    for step in steps:
        if step.id in by_id:
            raise GoalPlanContractError(f"plan contains duplicate step id {step.id}")
        by_id[step.id] = step

    for step in steps:
        missing = [dependency for dependency in step.depends_on if dependency not in by_id]
        if missing:
            raise GoalPlanContractError(
                f"plan step {step.id} depends on unknown step {missing[0]}",
            )

    # Kahn's algorithm in plan order: anything left unvisited lies on or behind a cycle.
    remaining = {step.id: len(step.depends_on) for step in steps}
    dependents: dict[UUID, list[UUID]] = {step.id: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            dependents[dependency].append(step.id)
    queue = [step.id for step in steps if remaining[step.id] == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for dependent in dependents[current]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                queue.append(dependent)
    if visited != len(steps):
        cyclic = [str(step.id) for step in steps if remaining[step.id] > 0]
        raise GoalPlanContractError(f"plan dependencies form a cycle among {', '.join(cyclic)}")

    for step in steps:
        if step.status not in _STARTED_STEP_STATUSES:
            continue
        for dependency in step.depends_on:
            if by_id[dependency].status is not PlanStepStatus.SUCCEEDED:
                raise GoalPlanContractError(
                    f"plan step {step.id} is {step.status} before dependency "
                    f"{dependency} succeeded",
                )
