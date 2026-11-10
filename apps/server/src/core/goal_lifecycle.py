"""Deterministic lifecycle transition policy for Goal and PlanStep snapshots.

Transitions are pure functions over the immutable Slice 1 contracts: they
return a new snapshot or raise ``GoalPlanTransitionError`` and never mutate,
persist, execute or delegate anything. Statuses remain caller-reported; a
``SUCCEEDED`` step is not evidence that work happened, and a Goal never becomes
``ACHIEVED`` because its steps did.
"""

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from uuid import UUID

from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStepStatus


class GoalPlanTransitionError(ValueError):
    """A requested Goal or PlanStep lifecycle transition is not permitted."""


GOAL_TRANSITIONS: Mapping[GoalStatus, frozenset[GoalStatus]] = MappingProxyType(
    {
        GoalStatus.DRAFT: frozenset({GoalStatus.ACTIVE, GoalStatus.ABANDONED}),
        GoalStatus.ACTIVE: frozenset({GoalStatus.ACHIEVED, GoalStatus.ABANDONED}),
        GoalStatus.ACHIEVED: frozenset(),
        GoalStatus.ABANDONED: frozenset(),
    },
)

PLAN_STEP_TRANSITIONS: Mapping[PlanStepStatus, frozenset[PlanStepStatus]] = MappingProxyType(
    {
        PlanStepStatus.PENDING: frozenset({PlanStepStatus.IN_PROGRESS, PlanStepStatus.CANCELLED}),
        PlanStepStatus.IN_PROGRESS: frozenset(
            {PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED, PlanStepStatus.CANCELLED},
        ),
        PlanStepStatus.SUCCEEDED: frozenset(),
        PlanStepStatus.FAILED: frozenset(),
        PlanStepStatus.CANCELLED: frozenset(),
    },
)

TERMINAL_GOAL_STATUSES = frozenset(
    status for status, targets in GOAL_TRANSITIONS.items() if not targets
)
TERMINAL_PLAN_STEP_STATUSES = frozenset(
    status for status, targets in PLAN_STEP_TRANSITIONS.items() if not targets
)


def transition_goal(goal: Goal, status: GoalStatus) -> Goal:
    """Return ``goal`` moved to ``status``; the same status is an idempotent no-op."""
    if not isinstance(status, GoalStatus):
        raise GoalPlanTransitionError("goal transition requires a known status")
    if status is goal.status:
        return goal
    if status not in GOAL_TRANSITIONS[goal.status]:
        raise GoalPlanTransitionError(
            f"goal transition {goal.status} -> {status} is not permitted",
        )
    return replace(goal, status=status)


def transition_plan_step(plan: Plan, step_id: UUID, status: PlanStepStatus) -> Plan:
    """Return ``plan`` with one step moved to ``status``; dependents never advance.

    The same status is an idempotent no-op that returns ``plan`` itself. Starting a
    step requires every dependency to have succeeded, and the rebuilt plan passes
    the full Slice 1 graph validation again.
    """
    if not isinstance(status, PlanStepStatus):
        raise GoalPlanTransitionError("plan step transition requires a known status")
    try:
        current = plan.step(step_id)
    except KeyError:
        raise GoalPlanTransitionError(f"plan has no step {step_id}") from None
    if status is current.status:
        return plan
    if status not in PLAN_STEP_TRANSITIONS[current.status]:
        raise GoalPlanTransitionError(
            f"plan step transition {current.status} -> {status} is not permitted",
        )
    if status is PlanStepStatus.IN_PROGRESS:
        for dependency in current.depends_on:
            if plan.step(dependency).status is not PlanStepStatus.SUCCEEDED:
                raise GoalPlanTransitionError(
                    f"plan step {step_id} cannot start before dependency "
                    f"{dependency} succeeded",
                )
    steps = tuple(
        replace(step, status=status) if step.id == step_id else step for step in plan.steps
    )
    return Plan(goal_id=plan.goal_id, steps=steps, version=plan.version)
