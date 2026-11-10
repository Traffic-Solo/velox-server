"""Offline coverage for the Goal and PlanStep lifecycle transition policy."""

import ast
import itertools
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goal_lifecycle import (
    GOAL_TRANSITIONS,
    PLAN_STEP_TRANSITIONS,
    TERMINAL_GOAL_STATUSES,
    TERMINAL_PLAN_STEP_STATUSES,
    GoalPlanTransitionError,
    transition_goal,
    transition_plan_step,
)
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep, PlanStepStatus

LIFECYCLE_MODULE = (
    Path(__file__).resolve().parents[1] / "apps" / "server" / "src" / "core" / "goal_lifecycle.py"
)

ALLOWED_GOAL = {
    (GoalStatus.DRAFT, GoalStatus.ACTIVE),
    (GoalStatus.DRAFT, GoalStatus.ABANDONED),
    (GoalStatus.ACTIVE, GoalStatus.ACHIEVED),
    (GoalStatus.ACTIVE, GoalStatus.ABANDONED),
}
ALLOWED_STEP = {
    (PlanStepStatus.PENDING, PlanStepStatus.IN_PROGRESS),
    (PlanStepStatus.PENDING, PlanStepStatus.CANCELLED),
    (PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED),
    (PlanStepStatus.IN_PROGRESS, PlanStepStatus.FAILED),
    (PlanStepStatus.IN_PROGRESS, PlanStepStatus.CANCELLED),
}
GOAL_PAIRS = list(itertools.product(GoalStatus, GoalStatus))
STEP_PAIRS = list(itertools.product(PlanStepStatus, PlanStepStatus))


def make_goal(status: GoalStatus = GoalStatus.DRAFT) -> Goal:
    return Goal(objective="Ship the weekly summary", success_criteria=("Reviewed",), status=status)


def make_step(**overrides: Any) -> PlanStep:
    values: dict[str, Any] = {
        "objective": "Draft the summary",
        "target": "weekly-report",
        "executor_role": ExecutorRole.CONTENT_SUMMARY,
        "capability": "content.summarize",
    }
    values.update(overrides)
    return PlanStep(**values)


def make_plan(*steps: PlanStep) -> Plan:
    return Plan(goal_id=uuid4(), steps=steps, version=2)


def test_transition_matrices_match_slice_2_policy() -> None:
    assert {(a, b) for a, targets in GOAL_TRANSITIONS.items() for b in targets} == ALLOWED_GOAL
    assert {
        (a, b) for a, targets in PLAN_STEP_TRANSITIONS.items() for b in targets
    } == ALLOWED_STEP
    assert set(GOAL_TRANSITIONS) == set(GoalStatus)
    assert set(PLAN_STEP_TRANSITIONS) == set(PlanStepStatus)
    assert {GoalStatus.ACHIEVED, GoalStatus.ABANDONED} == TERMINAL_GOAL_STATUSES
    assert {
        PlanStepStatus.SUCCEEDED,
        PlanStepStatus.FAILED,
        PlanStepStatus.CANCELLED,
    } == TERMINAL_PLAN_STEP_STATUSES


def test_transition_matrices_are_read_only() -> None:
    with pytest.raises(TypeError):
        GOAL_TRANSITIONS[GoalStatus.ACHIEVED] = frozenset({GoalStatus.ACTIVE})  # type: ignore[index]


# --- Goal -------------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), GOAL_PAIRS)
def test_goal_transition_matrix(current: GoalStatus, target: GoalStatus) -> None:
    goal = make_goal(current)

    if current is target:
        assert transition_goal(goal, target) is goal
    elif (current, target) in ALLOWED_GOAL:
        moved = transition_goal(goal, target)
        assert moved.status is target
        assert moved.id == goal.id
        assert moved.success_criteria == goal.success_criteria
        assert goal.status is current
    else:
        with pytest.raises(GoalPlanTransitionError, match="not permitted"):
            transition_goal(goal, target)


@pytest.mark.parametrize("status", ["active", None, PlanStepStatus.PENDING])
def test_goal_transition_requires_known_status(status: object) -> None:
    with pytest.raises(GoalPlanTransitionError, match="known status"):
        transition_goal(make_goal(), status)  # type: ignore[arg-type]


# --- PlanStep ---------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), STEP_PAIRS)
def test_step_transition_matrix(current: PlanStepStatus, target: PlanStepStatus) -> None:
    step = make_step(status=current)
    plan = make_plan(step)

    if current is target:
        assert transition_plan_step(plan, step.id, target) is plan
    elif (current, target) in ALLOWED_STEP:
        moved = transition_plan_step(plan, step.id, target)
        assert moved.step(step.id).status is target
        assert moved.version == plan.version
        assert moved.goal_id == plan.goal_id
        assert plan.step(step.id).status is current
    else:
        with pytest.raises(GoalPlanTransitionError, match="not permitted"):
            transition_plan_step(plan, step.id, target)


@pytest.mark.parametrize(
    "dependency_status",
    [
        PlanStepStatus.PENDING,
        PlanStepStatus.IN_PROGRESS,
        PlanStepStatus.FAILED,
        PlanStepStatus.CANCELLED,
    ],
)
def test_step_cannot_start_before_every_dependency_succeeded(
    dependency_status: PlanStepStatus,
) -> None:
    done = make_step(status=PlanStepStatus.SUCCEEDED)
    other = make_step(status=dependency_status)
    dependent = make_step(depends_on=(done.id, other.id))

    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        transition_plan_step(
            make_plan(done, other, dependent),
            dependent.id,
            PlanStepStatus.IN_PROGRESS,
        )


def test_step_starts_once_dependencies_succeeded_and_dependents_never_advance() -> None:
    first = make_step(status=PlanStepStatus.IN_PROGRESS)
    second = make_step(depends_on=(first.id,))
    third = make_step(depends_on=(second.id,))
    plan = make_plan(first, second, third)

    plan = transition_plan_step(plan, first.id, PlanStepStatus.SUCCEEDED)
    assert plan.step(second.id).status is PlanStepStatus.PENDING
    assert plan.ready_steps() == (plan.step(second.id),)

    plan = transition_plan_step(plan, second.id, PlanStepStatus.IN_PROGRESS)
    assert plan.step(second.id).status is PlanStepStatus.IN_PROGRESS
    assert plan.step(third.id).status is PlanStepStatus.PENDING
    assert [step.id for step in plan.steps] == [first.id, second.id, third.id]


def test_pending_step_with_unfinished_dependency_can_be_cancelled() -> None:
    dependency = make_step(status=PlanStepStatus.FAILED)
    dependent = make_step(depends_on=(dependency.id,))

    moved = transition_plan_step(
        make_plan(dependency, dependent),
        dependent.id,
        PlanStepStatus.CANCELLED,
    )

    assert moved.step(dependent.id).status is PlanStepStatus.CANCELLED


def test_step_transition_rejects_unknown_step() -> None:
    with pytest.raises(GoalPlanTransitionError, match="has no step"):
        transition_plan_step(make_plan(make_step()), uuid4(), PlanStepStatus.IN_PROGRESS)


@pytest.mark.parametrize("status", ["in_progress", None, GoalStatus.ACTIVE])
def test_step_transition_requires_known_status(status: object) -> None:
    step = make_step()

    with pytest.raises(GoalPlanTransitionError, match="known status"):
        transition_plan_step(make_plan(step), step.id, status)  # type: ignore[arg-type]


def test_all_steps_succeeding_does_not_touch_the_goal() -> None:
    goal = make_goal(GoalStatus.ACTIVE)
    step = make_step(status=PlanStepStatus.IN_PROGRESS)
    plan = Plan(goal_id=goal.id, steps=(step,))

    finished = transition_plan_step(plan, step.id, PlanStepStatus.SUCCEEDED)

    assert finished.ready_steps() == ()
    assert goal.status is GoalStatus.ACTIVE


def test_lifecycle_module_imports_only_pure_dependencies() -> None:
    tree = ast.parse(LIFECYCLE_MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert imported == {
        "collections.abc",
        "dataclasses",
        "types",
        "uuid",
        "apps.server.src.core.goals",
    }
