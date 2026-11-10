"""Offline coverage for the provider-neutral Goal/Plan contracts."""

import ast
import dataclasses
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goals import (
    Goal,
    GoalPlanContractError,
    GoalStatus,
    Plan,
    PlanStep,
    PlanStepStatus,
)

GOALS_MODULE = Path(__file__).resolve().parents[1] / "apps" / "server" / "src" / "core" / "goals.py"


def make_goal(**overrides: Any) -> Goal:
    values: dict[str, Any] = {
        "objective": "Ship the weekly summary",
        "success_criteria": ("Summary drafted", "Summary reviewed"),
    }
    values.update(overrides)
    return Goal(**values)


def make_step(**overrides: Any) -> PlanStep:
    values: dict[str, Any] = {
        "objective": "Draft the summary",
        "target": "weekly-report",
        "executor_role": ExecutorRole.CONTENT_SUMMARY,
        "capability": "content.summarize",
    }
    values.update(overrides)
    return PlanStep(**values)


def make_plan(*steps: PlanStep, **overrides: Any) -> Plan:
    values: dict[str, Any] = {"goal_id": uuid4(), "steps": steps}
    values.update(overrides)
    return Plan(**values)


# --- Goal -------------------------------------------------------------------


def test_goal_defaults_and_identity() -> None:
    goal = make_goal()

    assert isinstance(goal.id, UUID)
    assert goal.status is GoalStatus.DRAFT
    assert goal.constraints == ()
    assert make_goal().id != goal.id


def test_goal_accepts_explicit_fields() -> None:
    goal_id = uuid4()
    goal = make_goal(id=goal_id, constraints=("No external sends",), status=GoalStatus.ACTIVE)

    assert goal.id == goal_id
    assert goal.constraints == ("No external sends",)
    assert goal.status is GoalStatus.ACTIVE


def test_goal_is_immutable() -> None:
    goal = make_goal()

    with pytest.raises(dataclasses.FrozenInstanceError):
        goal.objective = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("objective", ["", "   ", None, 42])
def test_goal_requires_objective(objective: object) -> None:
    with pytest.raises(GoalPlanContractError, match="goal objective is required"):
        make_goal(objective=objective)


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ((), "must not be empty"),
        (["Summary drafted"], "must be a tuple"),
        ("Summary drafted", "must be a tuple"),
        (("Summary drafted", " "), "is required"),
        (("Summary drafted", None), "is required"),
        (("Summary drafted", " summary DRAFTED "), "must not contain duplicates"),
    ],
)
def test_goal_validates_success_criteria(criteria: object, message: str) -> None:
    with pytest.raises(GoalPlanContractError, match=message):
        make_goal(success_criteria=criteria)


@pytest.mark.parametrize(
    ("constraints", "message"),
    [
        (["No sends"], "must be a tuple"),
        (("",), "is required"),
        (("No sends", "no sends"), "must not contain duplicates"),
    ],
)
def test_goal_validates_constraints(constraints: object, message: str) -> None:
    with pytest.raises(GoalPlanContractError, match=message):
        make_goal(constraints=constraints)


@pytest.mark.parametrize("status", ["active", None, PlanStepStatus.PENDING])
def test_goal_requires_known_status(status: object) -> None:
    with pytest.raises(GoalPlanContractError, match="known status"):
        make_goal(status=status)


def test_goal_requires_uuid_id() -> None:
    with pytest.raises(GoalPlanContractError, match="goal id must be a UUID"):
        make_goal(id=str(uuid4()))


# --- PlanStep ---------------------------------------------------------------


def test_step_defaults() -> None:
    step = make_step()

    assert isinstance(step.id, UUID)
    assert step.depends_on == ()
    assert step.status is PlanStepStatus.PENDING


def test_step_is_immutable() -> None:
    step = make_step()

    with pytest.raises(dataclasses.FrozenInstanceError):
        step.status = PlanStepStatus.SUCCEEDED  # type: ignore[misc]


@pytest.mark.parametrize("field_name", ["objective", "target", "capability"])
@pytest.mark.parametrize("value", ["", "  ", None])
def test_step_requires_text_fields(field_name: str, value: object) -> None:
    with pytest.raises(GoalPlanContractError, match=f"plan step {field_name} is required"):
        make_step(**{field_name: value})


@pytest.mark.parametrize("capability", ["Content.Summarize", " content.summarize", "x\n"])
def test_step_requires_canonical_capability(capability: str) -> None:
    with pytest.raises(GoalPlanContractError, match="canonical identifier"):
        make_step(capability=capability)


@pytest.mark.parametrize("role", ["content_summary", None])
def test_step_requires_known_executor_role(role: object) -> None:
    with pytest.raises(GoalPlanContractError, match="known executor role"):
        make_step(executor_role=role)


@pytest.mark.parametrize("status", ["pending", GoalStatus.ACTIVE])
def test_step_requires_known_status(status: object) -> None:
    with pytest.raises(GoalPlanContractError, match="known status"):
        make_step(status=status)


def test_step_requires_uuid_id() -> None:
    with pytest.raises(GoalPlanContractError, match="plan step id must be a UUID"):
        make_step(id="step-1")


def test_step_dependencies_must_be_tuple_of_uuids() -> None:
    with pytest.raises(GoalPlanContractError, match="must be a tuple"):
        make_step(depends_on=[uuid4()])
    with pytest.raises(GoalPlanContractError, match="dependency must be a UUID"):
        make_step(depends_on=("step-1",))


def test_step_rejects_duplicate_dependencies() -> None:
    dependency = uuid4()

    with pytest.raises(GoalPlanContractError, match="must not contain duplicates"):
        make_step(depends_on=(dependency, dependency))


def test_step_rejects_self_dependency() -> None:
    step_id = uuid4()

    with pytest.raises(GoalPlanContractError, match="must not depend on itself"):
        make_step(id=step_id, depends_on=(step_id,))


def test_step_has_no_provider_or_authorization_fields() -> None:
    names = {f.name for f in dataclasses.fields(PlanStep)}

    assert names == {
        "objective",
        "target",
        "executor_role",
        "capability",
        "depends_on",
        "status",
        "id",
    }


# --- Plan structure ---------------------------------------------------------


def test_plan_binds_goal_and_version() -> None:
    goal = make_goal()
    step = make_step()
    plan = Plan(goal_id=goal.id, steps=(step,), version=3)

    assert plan.goal_id == goal.id
    assert plan.version == 3
    assert plan.steps == (step,)
    assert plan.step(step.id) is step
    with pytest.raises(KeyError):
        plan.step(uuid4())


def test_plan_is_immutable() -> None:
    plan = make_plan(make_step())

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.version = 2  # type: ignore[misc]


def test_plan_defaults_to_version_one() -> None:
    assert make_plan(make_step()).version == 1


@pytest.mark.parametrize(
    ("version", "message"),
    [(0, "at least 1"), (-1, "at least 1"), (True, "integer"), (1.0, "integer"), ("1", "integer")],
)
def test_plan_validates_version(version: object, message: str) -> None:
    with pytest.raises(GoalPlanContractError, match=message):
        make_plan(make_step(), version=version)


def test_plan_requires_uuid_goal_id() -> None:
    with pytest.raises(GoalPlanContractError, match="plan goal id must be a UUID"):
        make_plan(make_step(), goal_id="goal")


@pytest.mark.parametrize("steps", [(), [], None])
def test_plan_requires_nonempty_step_tuple(steps: object) -> None:
    with pytest.raises(GoalPlanContractError, match="nonempty tuple of steps"):
        make_plan(steps=steps)


def test_plan_rejects_non_step_items() -> None:
    with pytest.raises(GoalPlanContractError, match="PlanStep instances"):
        make_plan(make_step(), {"objective": "x"})  # type: ignore[arg-type]


def test_plan_rejects_duplicate_step_ids() -> None:
    step_id = uuid4()

    with pytest.raises(GoalPlanContractError, match="duplicate step id"):
        make_plan(make_step(id=step_id), make_step(id=step_id, objective="Other"))


def test_plan_rejects_missing_dependency() -> None:
    missing = uuid4()

    with pytest.raises(GoalPlanContractError, match=f"unknown step {missing}"):
        make_plan(make_step(), make_step(depends_on=(missing,)))


def test_plan_rejects_two_step_cycle() -> None:
    a_id, b_id = uuid4(), uuid4()
    a = make_step(id=a_id, depends_on=(b_id,))
    b = make_step(id=b_id, depends_on=(a_id,))

    with pytest.raises(GoalPlanContractError, match="cycle") as error:
        make_plan(a, b)
    assert str(a_id) in str(error.value)
    assert str(b_id) in str(error.value)


def test_plan_rejects_long_cycle_behind_valid_root() -> None:
    root, a_id, b_id, c_id = make_step(), uuid4(), uuid4(), uuid4()
    a = make_step(id=a_id, depends_on=(root.id, c_id))
    b = make_step(id=b_id, depends_on=(a_id,))
    c = make_step(id=c_id, depends_on=(b_id,))

    with pytest.raises(GoalPlanContractError, match="cycle") as error:
        make_plan(root, a, b, c)
    assert str(root.id) not in str(error.value)


def test_plan_accepts_diamond_dag_in_any_order() -> None:
    a = make_step()
    b = make_step(depends_on=(a.id,))
    c = make_step(depends_on=(a.id,))
    d = make_step(depends_on=(b.id, c.id))

    assert make_plan(d, c, b, a).steps == (d, c, b, a)


@pytest.mark.parametrize(
    "status",
    [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED],
)
@pytest.mark.parametrize(
    "dependency_status",
    [
        PlanStepStatus.PENDING,
        PlanStepStatus.IN_PROGRESS,
        PlanStepStatus.FAILED,
        PlanStepStatus.CANCELLED,
    ],
)
def test_plan_rejects_started_step_before_dependency_succeeded(
    status: PlanStepStatus,
    dependency_status: PlanStepStatus,
) -> None:
    dependency = make_step(status=dependency_status)

    with pytest.raises(GoalPlanContractError, match="before dependency"):
        make_plan(dependency, make_step(depends_on=(dependency.id,), status=status))


def test_plan_allows_cancelled_step_with_unfinished_dependency() -> None:
    dependency = make_step(status=PlanStepStatus.FAILED)
    cancelled = make_step(depends_on=(dependency.id,), status=PlanStepStatus.CANCELLED)

    assert make_plan(dependency, cancelled).ready_steps() == ()


# --- Readiness --------------------------------------------------------------


def test_ready_steps_are_roots_initially_in_plan_order() -> None:
    a = make_step()
    b = make_step()
    c = make_step(depends_on=(a.id, b.id))
    plan = make_plan(b, c, a)

    assert plan.ready_steps() == (b, a)


def test_ready_steps_follow_succeeded_dependencies() -> None:
    a = make_step(status=PlanStepStatus.SUCCEEDED)
    b = make_step(depends_on=(a.id,))
    c = make_step(depends_on=(a.id,), status=PlanStepStatus.IN_PROGRESS)
    d = make_step(depends_on=(b.id, c.id))

    assert make_plan(a, b, c, d).ready_steps() == (b,)


def test_ready_steps_require_all_dependencies_succeeded() -> None:
    a = make_step(status=PlanStepStatus.SUCCEEDED)
    b = make_step(status=PlanStepStatus.FAILED)
    c = make_step(depends_on=(a.id, b.id))

    assert make_plan(a, b, c).ready_steps() == ()


def test_ready_steps_empty_when_everything_finished() -> None:
    a = make_step(status=PlanStepStatus.SUCCEEDED)
    b = make_step(depends_on=(a.id,), status=PlanStepStatus.SUCCEEDED)

    assert make_plan(a, b).ready_steps() == ()


def test_ready_steps_are_deterministic_and_do_not_mutate() -> None:
    a = make_step(status=PlanStepStatus.SUCCEEDED)
    b = make_step(depends_on=(a.id,))
    c = make_step(depends_on=(a.id,))
    plan = make_plan(a, c, b)
    snapshot = dataclasses.astuple(plan)

    assert plan.ready_steps() == plan.ready_steps() == (c, b)
    assert dataclasses.astuple(plan) == snapshot


# --- Side effects -----------------------------------------------------------


def test_contracts_have_no_network_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("goal/plan contracts must not open sockets")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    goal = make_goal(status=GoalStatus.ACTIVE)
    a = make_step()
    plan = Plan(goal_id=goal.id, steps=(a, make_step(depends_on=(a.id,))))

    assert plan.ready_steps() == (a,)


def test_goals_module_imports_only_pure_dependencies() -> None:
    tree = ast.parse(GOALS_MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert imported == {
        "dataclasses",
        "enum",
        "uuid",
        "apps.server.src.core.actions",
    }
