"""Offline coverage for durable Goal/Plan persistence and controlled transitions."""

import ast
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goal_lifecycle import GoalPlanTransitionError
from apps.server.src.core.goal_plan_repository import (
    GoalPlanConflictError,
    GoalPlanCorruptedError,
    GoalPlanDelegationExistsError,
    GoalPlanNotFoundError,
    GoalPlanRepository,
    GoalPlanRepositoryError,
    InMemoryGoalPlanRepository,
    SqliteGoalPlanRepository,
    StepDelegation,
    StepDelegationStatus,
    StepHandoff,
    StoredGoal,
    StoredPlan,
)
from apps.server.src.core.goals import (
    Goal,
    GoalPlanContractError,
    GoalStatus,
    Plan,
    PlanStep,
    PlanStepStatus,
)
from apps.server.src.integrations.software_engineering_state import (
    SqliteSoftwareEngineeringRunRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_MODULE = REPO_ROOT / "apps" / "server" / "src" / "core" / "goal_plan_repository.py"
SE_ROLE = ExecutorRole.SOFTWARE_ENGINEERING.value
SE_CAPABILITY = "code.implement"


def make_goal(**overrides: Any) -> Goal:
    values: dict[str, Any] = {
        "objective": "Ship the weekly summary",
        "success_criteria": ("Summary drafted", "Summary reviewed"),
        "constraints": ("No external sends",),
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


def make_chain(goal_id: UUID, version: int = 1) -> Plan:
    """Three steps stored out of topological order: review -> draft -> context."""
    context = make_step(
        objective="Collect context",
        executor_role=ExecutorRole.CONTEXT_PREPARATION,
        capability="context.prepare",
    )
    draft = make_step(depends_on=(context.id,))
    review = make_step(
        objective="Review the summary",
        target="weekly-report-review",
        executor_role=ExecutorRole.CONTENT_REVIEW,
        capability="content.review",
        depends_on=(draft.id, context.id),
    )
    return Plan(goal_id=goal_id, steps=(review, draft, context), version=version)


def raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


@pytest.fixture(params=["memory", "sqlite"])
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> GoalPlanRepository:
    if request.param == "memory":
        return InMemoryGoalPlanRepository()
    return SqliteGoalPlanRepository(tmp_path / "state" / "goals.sqlite3")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "goals.sqlite3"


def active_goal(repo: GoalPlanRepository) -> StoredGoal:
    """Steps may only start under an active Goal (Sprint 6 Slice 3)."""
    goal = repo.create_goal(make_goal())
    return repo.transition_goal(goal.goal.id, GoalStatus.ACTIVE, expected_revision=1)


def seeded(repo: GoalPlanRepository) -> tuple[StoredGoal, StoredPlan]:
    goal = active_goal(repo)
    return goal, repo.create_plan(make_chain(goal.goal.id))


# --- Persistence (both implementations) ---------------------------------------


def test_goal_round_trip(repository: GoalPlanRepository) -> None:
    goal = make_goal()

    stored = repository.create_goal(goal)

    assert stored == StoredGoal(goal=goal, revision=1)
    assert repository.get_goal(goal.id) == stored


def test_missing_records_read_as_none(repository: GoalPlanRepository) -> None:
    assert repository.get_goal(uuid4()) is None
    assert repository.get_plan(uuid4(), 1) is None
    assert repository.get_latest_plan(uuid4()) is None


def test_multiple_goals_are_independent(repository: GoalPlanRepository) -> None:
    first = repository.create_goal(make_goal())
    second = repository.create_goal(make_goal(objective="Prepare the quarterly review"))
    repository.transition_goal(first.goal.id, GoalStatus.ACTIVE, expected_revision=1)

    assert repository.get_goal(second.goal.id) == second
    stored_first = repository.get_goal(first.goal.id)
    assert stored_first is not None
    assert stored_first.goal.status is GoalStatus.ACTIVE


def test_plan_round_trip_preserves_every_field_and_order(repository: GoalPlanRepository) -> None:
    goal = repository.create_goal(make_goal())
    plan = make_chain(goal.goal.id)

    stored = repository.create_plan(plan)

    assert stored == StoredPlan(plan=plan, revision=1)
    assert repository.get_plan(goal.goal.id, 1) == stored
    assert repository.get_latest_plan(goal.goal.id) == stored


def test_plan_versions_are_retained_and_latest_is_highest(
    repository: GoalPlanRepository,
) -> None:
    goal = repository.create_goal(make_goal())
    plans = [repository.create_plan(make_chain(goal.goal.id, version)) for version in (1, 2, 3)]

    assert [repository.get_plan(goal.goal.id, v) for v in (1, 2, 3)] == plans
    assert repository.get_latest_plan(goal.goal.id) == plans[2]
    assert repository.get_plan(goal.goal.id, 4) is None


def test_plans_of_different_goals_do_not_interfere(repository: GoalPlanRepository) -> None:
    first, first_plan = seeded(repository)
    second, second_plan = seeded(repository)

    assert repository.get_latest_plan(first.goal.id) == first_plan
    assert repository.get_latest_plan(second.goal.id) == second_plan


# --- Integrity (both implementations) -----------------------------------------


def test_duplicate_goal_identity_is_a_conflict(repository: GoalPlanRepository) -> None:
    goal = make_goal()
    repository.create_goal(goal)

    with pytest.raises(GoalPlanConflictError, match="already exists"):
        repository.create_goal(make_goal(id=goal.id, objective="Something else"))
    assert repository.get_goal(goal.id) == StoredGoal(goal=goal, revision=1)


@pytest.mark.parametrize("status", [GoalStatus.ACTIVE, GoalStatus.ACHIEVED, GoalStatus.ABANDONED])
def test_new_goal_must_be_draft(repository: GoalPlanRepository, status: GoalStatus) -> None:
    goal = make_goal(status=status)

    with pytest.raises(GoalPlanTransitionError, match="start as draft"):
        repository.create_goal(goal)
    assert repository.get_goal(goal.id) is None


def test_plan_requires_existing_goal(repository: GoalPlanRepository) -> None:
    plan = make_chain(uuid4())

    with pytest.raises(GoalPlanNotFoundError, match="does not exist"):
        repository.create_plan(plan)
    assert repository.get_latest_plan(plan.goal_id) is None


def test_duplicate_plan_version_is_a_conflict(repository: GoalPlanRepository) -> None:
    goal, original = seeded(repository)

    with pytest.raises(GoalPlanConflictError, match="already exists"):
        repository.create_plan(make_chain(goal.goal.id, 1))
    assert repository.get_plan(goal.goal.id, 1) == original


@pytest.mark.parametrize(("existing", "attempted"), [(0, 2), (1, 3), (2, 5)])
def test_plan_versions_must_progress_by_one(
    repository: GoalPlanRepository,
    existing: int,
    attempted: int,
) -> None:
    goal = repository.create_goal(make_goal())
    for version in range(1, existing + 1):
        repository.create_plan(make_chain(goal.goal.id, version))

    with pytest.raises(GoalPlanConflictError, match=f"must be {existing + 1}"):
        repository.create_plan(make_chain(goal.goal.id, attempted))
    latest = repository.get_latest_plan(goal.goal.id)
    assert (latest.plan.version if latest else 0) == existing


@pytest.mark.parametrize(
    "status",
    [
        PlanStepStatus.IN_PROGRESS,
        PlanStepStatus.SUCCEEDED,
        PlanStepStatus.FAILED,
        PlanStepStatus.CANCELLED,
    ],
)
def test_new_plan_steps_must_be_pending(
    repository: GoalPlanRepository,
    status: PlanStepStatus,
) -> None:
    goal = repository.create_goal(make_goal())
    plan = Plan(goal_id=goal.goal.id, steps=(make_step(status=status),))

    with pytest.raises(GoalPlanTransitionError, match="every step pending"):
        repository.create_plan(plan)
    assert repository.get_latest_plan(goal.goal.id) is None


def test_invalid_dependency_graph_never_reaches_storage() -> None:
    a_id, b_id = uuid4(), uuid4()

    with pytest.raises(GoalPlanContractError, match="cycle"):
        Plan(
            goal_id=uuid4(),
            steps=(make_step(id=a_id, depends_on=(b_id,)), make_step(id=b_id, depends_on=(a_id,))),
        )


@pytest.mark.parametrize("value", [object(), {"objective": "x"}, None])
def test_create_operations_require_contract_snapshots(
    repository: GoalPlanRepository,
    value: object,
) -> None:
    with pytest.raises(GoalPlanContractError, match="must be a Goal"):
        repository.create_goal(value)  # type: ignore[arg-type]
    with pytest.raises(GoalPlanContractError, match="must be a Plan"):
        repository.create_plan(value)  # type: ignore[arg-type]


def test_operations_validate_identifiers_and_revisions(repository: GoalPlanRepository) -> None:
    goal, stored = seeded(repository)
    step_id = stored.plan.steps[2].id

    with pytest.raises(GoalPlanContractError, match="goal id must be a UUID"):
        repository.get_goal(str(goal.goal.id))  # type: ignore[arg-type]
    for version in (0, -1, True, 1.0):
        with pytest.raises(GoalPlanContractError, match="version must be a positive integer"):
            repository.get_plan(goal.goal.id, version)  # type: ignore[arg-type]
    for revision in (0, True, "1", None):
        with pytest.raises(GoalPlanContractError, match="expected revision"):
            repository.transition_goal(
                goal.goal.id,
                GoalStatus.ACTIVE,
                expected_revision=revision,  # type: ignore[arg-type]
            )
        with pytest.raises(GoalPlanContractError, match="expected revision"):
            repository.transition_step(
                goal.goal.id,
                1,
                step_id,
                PlanStepStatus.IN_PROGRESS,
                expected_revision=revision,  # type: ignore[arg-type]
            )
    with pytest.raises(GoalPlanContractError, match="plan step id must be a UUID"):
        repository.transition_step(
            goal.goal.id,
            1,
            str(step_id),  # type: ignore[arg-type]
            PlanStepStatus.IN_PROGRESS,
            expected_revision=1,
        )


# --- Goal transitions (both implementations) ----------------------------------

GOAL_PATHS = [
    [GoalStatus.ACTIVE],
    [GoalStatus.ABANDONED],
    [GoalStatus.ACTIVE, GoalStatus.ACHIEVED],
    [GoalStatus.ACTIVE, GoalStatus.ABANDONED],
]


@pytest.mark.parametrize("path", GOAL_PATHS)
def test_every_allowed_goal_transition_increments_revision(
    repository: GoalPlanRepository,
    path: list[GoalStatus],
) -> None:
    stored = repository.create_goal(make_goal())

    for status in path:
        moved = repository.transition_goal(
            stored.goal.id,
            status,
            expected_revision=stored.revision,
        )
        assert moved.goal.status is status
        assert moved.revision == stored.revision + 1
        assert moved.goal.objective == stored.goal.objective
        stored = moved

    assert repository.get_goal(stored.goal.id) == stored


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        ([], GoalStatus.ACHIEVED),
        ([GoalStatus.ACTIVE], GoalStatus.DRAFT),
        ([GoalStatus.ABANDONED], GoalStatus.DRAFT),
        ([GoalStatus.ABANDONED], GoalStatus.ACTIVE),
        ([GoalStatus.ABANDONED], GoalStatus.ACHIEVED),
        ([GoalStatus.ACTIVE, GoalStatus.ACHIEVED], GoalStatus.DRAFT),
        ([GoalStatus.ACTIVE, GoalStatus.ACHIEVED], GoalStatus.ACTIVE),
        ([GoalStatus.ACTIVE, GoalStatus.ACHIEVED], GoalStatus.ABANDONED),
        ([GoalStatus.ACTIVE, GoalStatus.ABANDONED], GoalStatus.DRAFT),
        ([GoalStatus.ACTIVE, GoalStatus.ABANDONED], GoalStatus.ACTIVE),
        ([GoalStatus.ACTIVE, GoalStatus.ABANDONED], GoalStatus.ACHIEVED),
    ],
)
def test_every_prohibited_goal_transition_leaves_state_unchanged(
    repository: GoalPlanRepository,
    path: list[GoalStatus],
    forbidden: GoalStatus,
) -> None:
    stored = repository.create_goal(make_goal())
    for status in path:
        stored = repository.transition_goal(
            stored.goal.id,
            status,
            expected_revision=stored.revision,
        )

    with pytest.raises(GoalPlanTransitionError, match="not permitted"):
        repository.transition_goal(stored.goal.id, forbidden, expected_revision=stored.revision)
    assert repository.get_goal(stored.goal.id) == stored


@pytest.mark.parametrize(
    "path",
    [[], [GoalStatus.ACTIVE], [GoalStatus.ACTIVE, GoalStatus.ACHIEVED]],
)
def test_same_goal_status_is_an_idempotent_no_op(
    repository: GoalPlanRepository,
    path: list[GoalStatus],
) -> None:
    stored = repository.create_goal(make_goal())
    for status in path:
        stored = repository.transition_goal(
            stored.goal.id,
            status,
            expected_revision=stored.revision,
        )

    again = repository.transition_goal(
        stored.goal.id,
        stored.goal.status,
        expected_revision=stored.revision,
    )

    assert again == stored
    assert repository.get_goal(stored.goal.id) == stored


def test_goal_transition_rejects_missing_goal_and_stale_revision(
    repository: GoalPlanRepository,
) -> None:
    with pytest.raises(GoalPlanNotFoundError):
        repository.transition_goal(uuid4(), GoalStatus.ACTIVE, expected_revision=1)

    stored = repository.create_goal(make_goal())
    active = repository.transition_goal(stored.goal.id, GoalStatus.ACTIVE, expected_revision=1)

    with pytest.raises(GoalPlanConflictError, match="stale goal revision"):
        repository.transition_goal(stored.goal.id, GoalStatus.ABANDONED, expected_revision=1)
    with pytest.raises(GoalPlanConflictError, match="stale goal revision"):
        repository.transition_goal(stored.goal.id, GoalStatus.ACTIVE, expected_revision=1)
    assert repository.get_goal(stored.goal.id) == active


# --- Step transitions (both implementations) ----------------------------------

STEP_PATHS = [
    [PlanStepStatus.IN_PROGRESS],
    [PlanStepStatus.CANCELLED],
    [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED],
    [PlanStepStatus.IN_PROGRESS, PlanStepStatus.FAILED],
    [PlanStepStatus.IN_PROGRESS, PlanStepStatus.CANCELLED],
]


def single_step_plan(repo: GoalPlanRepository) -> StoredPlan:
    goal = active_goal(repo)
    return repo.create_plan(Plan(goal_id=goal.goal.id, steps=(make_step(),)))


def move(
    repo: GoalPlanRepository,
    stored: StoredPlan,
    step_id: UUID,
    status: PlanStepStatus,
) -> StoredPlan:
    return repo.transition_step(
        stored.plan.goal_id,
        stored.plan.version,
        step_id,
        status,
        expected_revision=stored.revision,
    )


@pytest.mark.parametrize("path", STEP_PATHS)
def test_every_allowed_step_transition_bumps_revision_not_version(
    repository: GoalPlanRepository,
    path: list[PlanStepStatus],
) -> None:
    stored = single_step_plan(repository)
    step_id = stored.plan.steps[0].id

    for status in path:
        moved = move(repository, stored, step_id, status)
        assert moved.plan.step(step_id).status is status
        assert moved.revision == stored.revision + 1
        assert moved.plan.version == 1
        stored = moved

    assert repository.get_plan(stored.plan.goal_id, 1) == stored
    assert repository.get_latest_plan(stored.plan.goal_id) == stored


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        ([], PlanStepStatus.SUCCEEDED),
        ([], PlanStepStatus.FAILED),
        ([PlanStepStatus.IN_PROGRESS], PlanStepStatus.PENDING),
        *[
            (path, target)
            for path in (
                [PlanStepStatus.CANCELLED],
                [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED],
                [PlanStepStatus.IN_PROGRESS, PlanStepStatus.FAILED],
                [PlanStepStatus.IN_PROGRESS, PlanStepStatus.CANCELLED],
            )
            for target in PlanStepStatus
            if target is not path[-1]
        ],
    ],
)
def test_every_prohibited_step_transition_leaves_state_unchanged(
    repository: GoalPlanRepository,
    path: list[PlanStepStatus],
    forbidden: PlanStepStatus,
) -> None:
    stored = single_step_plan(repository)
    step_id = stored.plan.steps[0].id
    for status in path:
        stored = move(repository, stored, step_id, status)

    with pytest.raises(GoalPlanTransitionError, match="not permitted"):
        move(repository, stored, step_id, forbidden)
    assert repository.get_plan(stored.plan.goal_id, 1) == stored


@pytest.mark.parametrize("path", [[], [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED]])
def test_same_step_status_is_an_idempotent_no_op(
    repository: GoalPlanRepository,
    path: list[PlanStepStatus],
) -> None:
    stored = single_step_plan(repository)
    step_id = stored.plan.steps[0].id
    for status in path:
        stored = move(repository, stored, step_id, status)

    again = move(repository, stored, step_id, stored.plan.step(step_id).status)

    assert again == stored
    assert repository.get_plan(stored.plan.goal_id, 1) == stored


def test_step_dependencies_are_enforced_and_dependents_never_advance(
    repository: GoalPlanRepository,
) -> None:
    _, stored = seeded(repository)
    review, draft, context = stored.plan.steps

    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        move(repository, stored, draft.id, PlanStepStatus.IN_PROGRESS)
    assert repository.get_plan(stored.plan.goal_id, 1) == stored

    stored = move(repository, stored, context.id, PlanStepStatus.IN_PROGRESS)
    stored = move(repository, stored, context.id, PlanStepStatus.SUCCEEDED)
    assert stored.plan.step(draft.id).status is PlanStepStatus.PENDING
    assert stored.plan.ready_steps() == (stored.plan.step(draft.id),)

    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        move(repository, stored, review.id, PlanStepStatus.IN_PROGRESS)

    stored = move(repository, stored, draft.id, PlanStepStatus.IN_PROGRESS)
    stored = move(repository, stored, draft.id, PlanStepStatus.FAILED)
    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        move(repository, stored, review.id, PlanStepStatus.IN_PROGRESS)
    stored = move(repository, stored, review.id, PlanStepStatus.CANCELLED)

    assert [step.id for step in stored.plan.steps] == [review.id, draft.id, context.id]
    assert stored.revision == 6
    assert repository.get_latest_plan(stored.plan.goal_id) == stored


def test_succeeded_steps_never_achieve_the_goal(repository: GoalPlanRepository) -> None:
    goal, stored = seeded(repository)
    for step in reversed(stored.plan.steps):
        stored = move(repository, stored, step.id, PlanStepStatus.IN_PROGRESS)
        stored = move(repository, stored, step.id, PlanStepStatus.SUCCEEDED)

    assert stored.plan.ready_steps() == ()
    assert repository.get_goal(goal.goal.id) == goal
    assert goal.goal.status is GoalStatus.ACTIVE


def test_step_transition_rejects_unknown_targets(repository: GoalPlanRepository) -> None:
    goal, stored = seeded(repository)
    context_id = stored.plan.steps[2].id

    with pytest.raises(GoalPlanNotFoundError, match="plan step"):
        move(repository, stored, uuid4(), PlanStepStatus.IN_PROGRESS)
    with pytest.raises(GoalPlanNotFoundError, match="plan version 2"):
        repository.transition_step(
            goal.goal.id, 2, context_id, PlanStepStatus.IN_PROGRESS, expected_revision=1
        )
    with pytest.raises(GoalPlanNotFoundError):
        repository.transition_step(
            uuid4(), 1, context_id, PlanStepStatus.IN_PROGRESS, expected_revision=1
        )
    assert repository.get_plan(goal.goal.id, 1) == stored


def test_superseded_plan_version_is_frozen(repository: GoalPlanRepository) -> None:
    goal, first = seeded(repository)
    _, draft, context = first.plan.steps
    first = move(repository, first, context.id, PlanStepStatus.IN_PROGRESS)
    first = move(repository, first, context.id, PlanStepStatus.SUCCEEDED)
    second = repository.create_plan(make_chain(goal.goal.id, 2))

    with pytest.raises(GoalPlanConflictError, match="superseded"):
        move(repository, first, draft.id, PlanStepStatus.IN_PROGRESS)
    assert repository.get_plan(goal.goal.id, 1) == first
    assert repository.get_latest_plan(goal.goal.id) == second
    assert second.plan.step(second.plan.steps[2].id).status is PlanStepStatus.PENDING


def assert_no_superseded_step_in_progress(repo: GoalPlanRepository, goal_id: UUID) -> None:
    latest = repo.get_latest_plan(goal_id)
    assert latest is not None
    for version in range(1, latest.plan.version):
        stored = repo.get_plan(goal_id, version)
        assert stored is not None
        assert all(step.status is not PlanStepStatus.IN_PROGRESS for step in stored.plan.steps)


@pytest.mark.parametrize(
    "terminal",
    [PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED, PlanStepStatus.CANCELLED],
)
def test_in_progress_step_blocks_supersession_until_terminal(
    repository: GoalPlanRepository,
    terminal: PlanStepStatus,
) -> None:
    goal, first = seeded(repository)
    context_id = first.plan.steps[2].id
    first = move(repository, first, context_id, PlanStepStatus.IN_PROGRESS)
    replacement = make_chain(goal.goal.id, 2)

    with pytest.raises(GoalPlanConflictError, match="cannot be superseded") as error:
        repository.create_plan(replacement)
    assert str(context_id) in str(error.value)
    assert repository.get_latest_plan(goal.goal.id) == first
    assert repository.get_plan(goal.goal.id, 2) is None

    first = move(repository, first, context_id, terminal)
    second = repository.create_plan(replacement)

    assert second == StoredPlan(plan=replacement, revision=1)
    assert repository.get_plan(goal.goal.id, 1) == first
    assert first.plan.step(context_id).status is terminal
    assert_no_superseded_step_in_progress(repository, goal.goal.id)


def test_every_in_progress_step_must_finish_before_supersession(
    repository: GoalPlanRepository,
) -> None:
    goal = active_goal(repository)
    a, b, c = make_step(), make_step(objective="Second root"), make_step(objective="Third root")
    stored = repository.create_plan(Plan(goal_id=goal.goal.id, steps=(a, b, c)))
    stored = move(repository, stored, a.id, PlanStepStatus.IN_PROGRESS)
    stored = move(repository, stored, b.id, PlanStepStatus.IN_PROGRESS)
    stored = move(repository, stored, a.id, PlanStepStatus.FAILED)
    replacement = make_chain(goal.goal.id, 2)

    with pytest.raises(GoalPlanConflictError, match=str(b.id)) as error:
        repository.create_plan(replacement)
    assert str(a.id) not in str(error.value)
    assert repository.get_latest_plan(goal.goal.id) == stored

    stored = move(repository, stored, b.id, PlanStepStatus.CANCELLED)
    assert stored.plan.step(c.id).status is PlanStepStatus.PENDING
    assert repository.create_plan(replacement).plan == replacement
    assert repository.get_plan(goal.goal.id, 1) == stored


def test_rejected_supersession_writes_nothing(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal, first = seeded(repo)
    first = move(repo, first, first.plan.steps[2].id, PlanStepStatus.IN_PROGRESS)
    with raw(db_path) as connection:
        before = connection.execute("SELECT * FROM goal_plan_steps ORDER BY position").fetchall()

    with pytest.raises(GoalPlanConflictError, match="cannot be superseded"):
        repo.create_plan(make_chain(goal.goal.id, 2))

    with raw(db_path) as connection:
        plans = connection.execute("SELECT version, revision FROM goal_plans").fetchall()
        after = connection.execute("SELECT * FROM goal_plan_steps ORDER BY position").fetchall()
    assert [tuple(row) for row in plans] == [(1, 2)]
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert SqliteGoalPlanRepository(db_path).get_latest_plan(goal.goal.id) == first


def test_stale_plan_revision_prevents_lost_update(repository: GoalPlanRepository) -> None:
    goal = active_goal(repository)
    a, b = make_step(), make_step(objective="Second root")
    snapshot = repository.create_plan(Plan(goal_id=goal.goal.id, steps=(a, b)))

    winner = move(repository, snapshot, a.id, PlanStepStatus.IN_PROGRESS)
    with pytest.raises(GoalPlanConflictError, match="stale plan revision"):
        move(repository, snapshot, b.id, PlanStepStatus.CANCELLED)

    assert repository.get_latest_plan(goal.goal.id) == winner
    assert winner.plan.step(b.id).status is PlanStepStatus.PENDING


def test_repository_role_exposes_no_raw_status_replacement() -> None:
    public = {name for name in dir(GoalPlanRepository) if not name.startswith("_")}

    assert public == {
        "create_goal",
        "get_goal",
        "transition_goal",
        "create_plan",
        "get_plan",
        "get_latest_plan",
        "transition_step",
        "get_step_delegation",
        "get_step_delegation_for_action",
        "delegate_step",
        "resolve_step_approval",
    }
    for implementation in (InMemoryGoalPlanRepository, SqliteGoalPlanRepository):
        methods = {
            name
            for name in dir(implementation)
            if not name.startswith("_") and callable(getattr(implementation, name))
        }
        assert methods == public


# --- SQLite durability and restart --------------------------------------------


def test_sqlite_state_survives_repository_reopen(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal, stored = seeded(repo)
    context_id = stored.plan.steps[2].id
    stored = move(repo, stored, context_id, PlanStepStatus.IN_PROGRESS)

    reopened = SqliteGoalPlanRepository(db_path)

    assert reopened.get_goal(goal.goal.id) == goal
    assert reopened.get_latest_plan(goal.goal.id) == stored
    assert move(reopened, stored, context_id, PlanStepStatus.SUCCEEDED).revision == 3


def run_python(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


WRITER = """
import sys
from pathlib import Path
from uuid import UUID
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goal_plan_repository import SqliteGoalPlanRepository
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep

repo = SqliteGoalPlanRepository(Path(sys.argv[1]))
goal = Goal(id=UUID(sys.argv[2]), objective="Child goal", success_criteria=("Done",))
repo.create_goal(goal)
repo.transition_goal(goal.id, GoalStatus.ACTIVE, expected_revision=1)
repo.create_plan(Plan(goal_id=goal.id, steps=(PlanStep(
    id=UUID(sys.argv[3]), objective="Child step", target="child",
    executor_role=ExecutorRole.CONTENT_REVIEW, capability="content.review",
),)))
"""


def test_sqlite_state_written_by_another_process_is_reconstructed(db_path: Path) -> None:
    goal_id, step_id = uuid4(), uuid4()

    result = run_python(WRITER, str(db_path), str(goal_id), str(step_id))

    assert result.returncode == 0, result.stderr
    repo = SqliteGoalPlanRepository(db_path)
    goal = repo.get_goal(goal_id)
    plan = repo.get_latest_plan(goal_id)
    assert goal is not None
    assert goal.goal.status is GoalStatus.ACTIVE
    assert goal.revision == 2
    assert plan is not None
    assert plan.plan.steps[0].id == step_id
    assert plan.plan.steps[0].executor_role is ExecutorRole.CONTENT_REVIEW


READER = """
import sys
from pathlib import Path
from uuid import UUID
from apps.server.src.core.goal_plan_repository import SqliteGoalPlanRepository

repo = SqliteGoalPlanRepository(Path(sys.argv[1]))
stored = repo.get_latest_plan(UUID(sys.argv[2]))
print(stored.revision, [(str(step.id), step.status.value) for step in stored.plan.steps])
"""


def test_sqlite_state_is_readable_from_a_new_process(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    context_id = stored.plan.steps[2].id
    stored = move(repo, stored, context_id, PlanStepStatus.IN_PROGRESS)

    result = run_python(READER, str(db_path), str(stored.plan.goal_id))

    assert result.returncode == 0, result.stderr
    expected = [(str(step.id), step.status.value) for step in stored.plan.steps]
    assert result.stdout.strip() == f"2 {expected}"


CRASHER = """
import os
import sys
from pathlib import Path
from uuid import UUID
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goal_plan_repository import SqliteGoalPlanRepository
from apps.server.src.core.goals import Plan, PlanStep

repo = SqliteGoalPlanRepository(Path(sys.argv[1]))
connect = repo._connect

def crashing_connect():
    connection = connect()
    connection.set_trace_callback(
        lambda sql: os._exit(17) if "INSERT INTO goal_plan_steps" in sql else None
    )
    return connection

repo._connect = crashing_connect
repo.create_plan(Plan(goal_id=UUID(sys.argv[2]), steps=(PlanStep(
    objective="Never stored", target="crash",
    executor_role=ExecutorRole.CONTENT_REVIEW, capability="content.review",
),)))
"""


def test_process_killed_mid_plan_write_leaves_no_partial_plan(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal = repo.create_goal(make_goal())

    result = run_python(CRASHER, str(db_path), str(goal.goal.id))

    assert result.returncode == 17, result.stderr
    assert repo.get_latest_plan(goal.goal.id) is None
    with raw(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_plans").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM goal_plan_steps").fetchone()[0] == 0
    assert repo.create_plan(make_chain(goal.goal.id)).revision == 1


# --- SQLite rollback and atomicity --------------------------------------------


def install_trigger(path: Path, sql: str) -> None:
    with raw(path) as connection:
        connection.execute(sql)


def drop_trigger(path: Path, name: str) -> None:
    with raw(path) as connection:
        connection.execute(f"DROP TRIGGER {name}")


def test_failed_step_insert_rolls_back_whole_plan(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal = repo.create_goal(make_goal())
    plan = make_chain(goal.goal.id)
    install_trigger(
        db_path,
        """
        CREATE TRIGGER test_fail_last_step BEFORE INSERT ON goal_plan_steps
        WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """,
    )

    with pytest.raises(GoalPlanRepositoryError, match="plan could not be persisted"):
        repo.create_plan(plan)

    assert repo.get_latest_plan(goal.goal.id) is None
    with raw(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_plans").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM goal_plan_steps").fetchone()[0] == 0
    drop_trigger(db_path, "test_fail_last_step")
    assert repo.create_plan(plan) == StoredPlan(plan=plan, revision=1)


def test_failed_revision_bump_rolls_back_step_status(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    context_id = stored.plan.steps[2].id
    install_trigger(
        db_path,
        """
        CREATE TRIGGER test_fail_revision BEFORE UPDATE OF revision ON goal_plans
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """,
    )

    with pytest.raises(GoalPlanRepositoryError, match="transition could not be persisted"):
        move(repo, stored, context_id, PlanStepStatus.IN_PROGRESS)

    assert repo.get_plan(stored.plan.goal_id, 1) == stored
    drop_trigger(db_path, "test_fail_revision")
    assert move(repo, stored, context_id, PlanStepStatus.IN_PROGRESS).revision == 2


def test_failed_goal_update_does_not_advance_state(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    stored = repo.create_goal(make_goal())
    install_trigger(
        db_path,
        """
        CREATE TRIGGER test_fail_goal BEFORE UPDATE OF status ON goals
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """,
    )

    with pytest.raises(GoalPlanRepositoryError, match="goal transition could not be persisted"):
        repo.transition_goal(stored.goal.id, GoalStatus.ACTIVE, expected_revision=1)

    assert repo.get_goal(stored.goal.id) == stored


def test_storage_rejects_out_of_band_definition_edits_and_deletes(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal, first = seeded(repo)
    repo.create_plan(make_chain(goal.goal.id, 2))
    statements = [
        "UPDATE goals SET objective = 'changed'",
        "UPDATE goals SET success_criteria = '[]'",
        "DELETE FROM goals",
        "UPDATE goal_plans SET version = 9",
        "DELETE FROM goal_plans",
        "UPDATE goal_plan_steps SET capability = 'other.capability'",
        "UPDATE goal_plan_steps SET depends_on = '[]'",
        "UPDATE goal_plan_steps SET position = position + 10",
        "DELETE FROM goal_plan_steps",
        "UPDATE goal_plan_steps SET status = 'cancelled' WHERE version = 1",
        "UPDATE goal_plans SET revision = 5 WHERE version = 1",
    ]

    with raw(db_path) as connection:
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

    assert repo.get_plan(goal.goal.id, 1) == first


# --- SQLite concurrency -------------------------------------------------------


def race(workers: Sequence[Callable[[], object]]) -> tuple[list[object], list[BaseException]]:
    barrier = threading.Barrier(len(workers))
    results: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(worker: Callable[[], object]) -> None:
        barrier.wait()
        try:
            outcome = worker()
        except BaseException as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def create_plan_in(path: Path, plan: Plan) -> StoredPlan:
    return SqliteGoalPlanRepository(path).create_plan(plan)


def start_step_in(path: Path, snapshot: StoredPlan, step_id: UUID) -> StoredPlan:
    return move(SqliteGoalPlanRepository(path), snapshot, step_id, PlanStepStatus.IN_PROGRESS)


def transition_goal_in(path: Path, goal_id: UUID, target: GoalStatus) -> StoredGoal:
    return SqliteGoalPlanRepository(path).transition_goal(goal_id, target, expected_revision=1)


def test_concurrent_plan_version_writers_yield_exactly_one_winner(db_path: Path) -> None:
    goal, _ = seeded(SqliteGoalPlanRepository(db_path))
    candidates = [make_chain(goal.goal.id, 2) for _ in range(8)]

    results, errors = race([partial(create_plan_in, db_path, plan) for plan in candidates])

    assert len(results) == 1
    assert len(errors) == 7
    assert all(isinstance(error, GoalPlanConflictError) for error in errors)
    latest = SqliteGoalPlanRepository(db_path).get_latest_plan(goal.goal.id)
    assert latest == results[0]


def test_concurrent_step_transitions_on_one_revision_yield_one_winner(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal = active_goal(repo)
    steps = tuple(make_step(objective=f"Root {index}") for index in range(6))
    snapshot = repo.create_plan(Plan(goal_id=goal.goal.id, steps=steps))

    results, errors = race([partial(start_step_in, db_path, snapshot, step.id) for step in steps])

    assert len(results) == 1
    assert len(errors) == 5
    assert all(isinstance(error, GoalPlanConflictError) for error in errors)
    latest = repo.get_latest_plan(goal.goal.id)
    assert latest == results[0]
    assert latest is not None
    assert latest.revision == 2
    started = [s for s in latest.plan.steps if s.status is PlanStepStatus.IN_PROGRESS]
    assert len(started) == 1


def test_concurrent_goal_transitions_yield_one_winner(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    stored = repo.create_goal(make_goal())
    targets = [GoalStatus.ACTIVE, GoalStatus.ABANDONED] * 4

    results, errors = race(
        [partial(transition_goal_in, db_path, stored.goal.id, target) for target in targets],
    )

    assert len(results) == 1
    assert len(errors) == 7
    assert all(isinstance(error, GoalPlanConflictError) for error in errors)
    winner = repo.get_goal(stored.goal.id)
    assert winner == results[0]
    assert winner is not None
    assert winner.revision == 2


@pytest.mark.parametrize("attempt", range(10))
def test_step_start_racing_supersession_never_strands_a_step(
    repository: GoalPlanRepository,
    attempt: int,
) -> None:
    goal, first = seeded(repository)
    context_id = first.plan.steps[2].id

    results, errors = race(
        [
            partial(move, repository, first, context_id, PlanStepStatus.IN_PROGRESS),
            partial(repository.create_plan, make_chain(goal.goal.id, 2)),
        ],
    )

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], GoalPlanConflictError)
    assert_no_superseded_step_in_progress(repository, goal.goal.id)
    latest = repository.get_latest_plan(goal.goal.id)
    assert latest == results[0]


@pytest.mark.parametrize("attempt", range(10))
def test_step_finish_racing_supersession_always_lets_the_step_finish(
    repository: GoalPlanRepository,
    attempt: int,
) -> None:
    goal, first = seeded(repository)
    context_id = first.plan.steps[2].id
    first = move(repository, first, context_id, PlanStepStatus.IN_PROGRESS)
    replacement = make_chain(goal.goal.id, 2)

    results, errors = race(
        [
            partial(move, repository, first, context_id, PlanStepStatus.SUCCEEDED),
            partial(repository.create_plan, replacement),
        ],
    )

    finished = repository.get_plan(goal.goal.id, 1)
    assert finished is not None
    assert finished.plan.step(context_id).status is PlanStepStatus.SUCCEEDED
    assert finished in results
    assert all(isinstance(error, GoalPlanConflictError) for error in errors)
    if errors:
        assert repository.get_latest_plan(goal.goal.id) == finished
    else:
        assert repository.get_latest_plan(goal.goal.id) == StoredPlan(
            plan=replacement,
            revision=1,
        )
    assert_no_superseded_step_in_progress(repository, goal.goal.id)


def test_reader_sees_committed_snapshot_while_writer_holds_lock(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    blocker = raw(db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE goal_plan_steps SET status = 'in_progress' WHERE position = 2")

        assert repo.get_plan(stored.plan.goal_id, 1) == stored
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert repo.get_plan(stored.plan.goal_id, 1) == stored


# --- SQLite corruption detection ----------------------------------------------


def tamper(path: Path, *statements: str, drop: tuple[str, ...] = ()) -> None:
    with raw(path) as connection:
        for name in drop:
            connection.execute(f"DROP TRIGGER {name}")
        for statement in statements:
            connection.execute(statement)


@pytest.mark.parametrize(
    ("statement", "drop"),
    [
        ("UPDATE goals SET status = 'ACTIVE'", ()),
        ("UPDATE goals SET status = 'done'", ()),
        ("UPDATE goals SET objective = '   '", ("goals_definition_immutable",)),
        ("UPDATE goals SET objective = x'3432'", ("goals_definition_immutable",)),
        ("UPDATE goals SET success_criteria = '[]'", ("goals_definition_immutable",)),
        ("UPDATE goals SET success_criteria = 'not json'", ("goals_definition_immutable",)),
        ("UPDATE goals SET success_criteria = '{\"a\": 1}'", ("goals_definition_immutable",)),
        ("UPDATE goals SET constraints = '[1]'", ("goals_definition_immutable",)),
        ("UPDATE goals SET revision = 'one'", ()),
    ],
)
def test_corrupted_goal_is_rejected_not_normalized(
    db_path: Path,
    statement: str,
    drop: tuple[str, ...],
) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    stored = repo.create_goal(make_goal())
    tamper(db_path, statement, drop=drop)

    with pytest.raises(GoalPlanCorruptedError, match="persisted goal is invalid"):
        repo.get_goal(stored.goal.id)
    with pytest.raises(GoalPlanCorruptedError):
        repo.transition_goal(stored.goal.id, GoalStatus.ACTIVE, expected_revision=1)


STEPS_IMMUTABLE = ("goal_plan_steps_definition_immutable",)


@pytest.mark.parametrize(
    ("statement", "drop"),
    [
        ("UPDATE goal_plan_steps SET status = 'done' WHERE position = 0", ()),
        ("UPDATE goal_plan_steps SET status = 'PENDING' WHERE position = 0", ()),
        ("UPDATE goal_plan_steps SET status = 'succeeded' WHERE position = 0", ()),
        ("UPDATE goal_plans SET revision = 'x'", ()),
        ("UPDATE goal_plan_steps SET executor_role = 'gmail' WHERE position = 0", STEPS_IMMUTABLE),
        ("UPDATE goal_plan_steps SET capability = 'Content.Review'", STEPS_IMMUTABLE),
        ("UPDATE goal_plan_steps SET depends_on = '[\"x\"]' WHERE position = 0", STEPS_IMMUTABLE),
        ("UPDATE goal_plan_steps SET step_id = upper(step_id) WHERE position = 2", STEPS_IMMUTABLE),
        ("UPDATE goal_plan_steps SET position = 7 WHERE position = 2", STEPS_IMMUTABLE),
        (
            "UPDATE goal_plan_steps SET depends_on = '[\"00000000-0000-4000-8000-000000000000\"]'"
            " WHERE position = 2",
            STEPS_IMMUTABLE,
        ),
    ],
)
def test_corrupted_plan_is_rejected_not_normalized(
    db_path: Path,
    statement: str,
    drop: tuple[str, ...],
) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    tamper(db_path, statement, drop=drop)

    with pytest.raises(GoalPlanCorruptedError, match="persisted plan is invalid"):
        repo.get_plan(stored.plan.goal_id, 1)
    with pytest.raises(GoalPlanCorruptedError):
        repo.get_latest_plan(stored.plan.goal_id)
    with pytest.raises(GoalPlanCorruptedError):
        move(repo, stored, stored.plan.steps[2].id, PlanStepStatus.IN_PROGRESS)


def test_plan_reconstruction_reruns_graph_validation(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    review, draft, _ = stored.plan.steps
    tamper(
        db_path,
        f"UPDATE goal_plan_steps SET depends_on = '[\"{review.id}\"]' WHERE step_id = '{draft.id}'",
        drop=STEPS_IMMUTABLE,
    )

    with pytest.raises(GoalPlanCorruptedError, match="persisted plan is invalid"):
        repo.get_plan(stored.plan.goal_id, 1)


def test_plan_with_missing_steps_is_corrupted(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal = repo.create_goal(make_goal())
    with raw(db_path) as connection:
        connection.execute(
            "INSERT INTO goal_plans (goal_id, version, revision) VALUES (?, 1, 1)",
            (str(goal.goal.id),),
        )

    with pytest.raises(GoalPlanCorruptedError, match="persisted plan is invalid"):
        repo.get_latest_plan(goal.goal.id)


# --- Schema initialization and compatibility -----------------------------------


def schema_versions(path: Path) -> dict[str, int]:
    with raw(path) as connection:
        rows = connection.execute("SELECT name, version FROM velox_schema").fetchall()
    return {str(row["name"]): int(row["version"]) for row in rows}


def test_fresh_database_initialization(db_path: Path) -> None:
    SqliteGoalPlanRepository(db_path)

    assert schema_versions(db_path) == {"goal_plans": 2}
    with raw(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"velox_schema", "goals", "goal_plans", "goal_plan_steps"} <= tables


def test_reinitialization_is_idempotent(db_path: Path) -> None:
    goal, stored = seeded(SqliteGoalPlanRepository(db_path))

    for _ in range(3):
        repo = SqliteGoalPlanRepository(db_path)

    assert repo.get_goal(goal.goal.id) == goal
    assert repo.get_latest_plan(goal.goal.id) == stored
    assert schema_versions(db_path) == {"goal_plans": 2}


def test_relative_path_is_rejected() -> None:
    with pytest.raises(GoalPlanRepositoryError, match="must be absolute"):
        SqliteGoalPlanRepository(Path("relative.sqlite3"))


def test_unsupported_schema_version_is_rejected(db_path: Path) -> None:
    SqliteGoalPlanRepository(db_path)
    with raw(db_path) as connection:
        connection.execute("UPDATE velox_schema SET version = 3 WHERE name = 'goal_plans'")

    with pytest.raises(GoalPlanRepositoryError, match="unsupported"):
        SqliteGoalPlanRepository(db_path)


def test_incomplete_schema_is_rejected(db_path: Path) -> None:
    SqliteGoalPlanRepository(db_path)
    drop_trigger(db_path, "goal_plans_superseded_read_only")

    with pytest.raises(GoalPlanCorruptedError, match="incomplete"):
        SqliteGoalPlanRepository(db_path)


def test_foreign_tables_with_the_same_name_are_not_adopted(db_path: Path) -> None:
    with raw(db_path) as connection:
        connection.execute("CREATE TABLE goals (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO goals (title) VALUES ('unrelated')")

    with pytest.raises(GoalPlanRepositoryError, match="could not be initialized"):
        SqliteGoalPlanRepository(db_path)

    with raw(db_path) as connection:
        assert connection.execute("SELECT title FROM goals").fetchall()[0][0] == "unrelated"
        schema = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'velox_schema'",
        ).fetchone()
    assert schema is None


def register_se_action(path: Path) -> UUID:
    action_id = uuid4()
    SqliteSoftwareEngineeringRunRepository(path).register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=SE_ROLE,
        capability=SE_CAPABILITY,
        delegation_status="awaiting_approval",
        objective="Implement the slice",
    )
    return action_id


@pytest.mark.parametrize("goal_first", [True, False])
def test_goal_plan_and_se_repositories_share_one_database(
    db_path: Path,
    goal_first: bool,
) -> None:
    if goal_first:
        goal, stored = seeded(SqliteGoalPlanRepository(db_path))
        action_id = register_se_action(db_path)
    else:
        action_id = register_se_action(db_path)
        goal, stored = seeded(SqliteGoalPlanRepository(db_path))

    assert schema_versions(db_path) == {"goal_plans": 2, "software_engineering_runs": 3}
    for _ in range(2):
        se = SqliteSoftwareEngineeringRunRepository(db_path)
        goals = SqliteGoalPlanRepository(db_path)
    se_state = se.get(action_id)
    assert se_state is not None
    assert se_state.pending_objective == "Implement the slice"
    assert goals.get_goal(goal.goal.id) == goal
    assert goals.get_latest_plan(goal.goal.id) == stored


def write_legacy_se_v1(path: Path, action_id: UUID) -> None:
    with raw(path) as connection:
        connection.executescript(
            """
            CREATE TABLE velox_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL);
            CREATE TABLE software_engineering_runs (
                action_id TEXT PRIMARY KEY,
                executor_role TEXT NOT NULL,
                capability TEXT NOT NULL,
                target TEXT NOT NULL,
                delegation_status TEXT,
                execution_status TEXT,
                execution_finished_at TEXT,
                external_execution_performed INTEGER,
                disposition TEXT,
                disposition_succeeded INTEGER,
                worktree_present INTEGER,
                branch_present INTEGER,
                canonical_unchanged INTEGER,
                promotion_commit_sha TEXT,
                pull_request_number INTEGER,
                pull_request_url TEXT,
                promotion_base_branch TEXT,
                promotion_head_branch TEXT,
                promotion_finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO velox_schema(name, version) VALUES ('software_engineering_runs', 1);
            """
        )
        connection.execute(
            """
            INSERT INTO software_engineering_runs (
                action_id, executor_role, capability, target, delegation_status,
                execution_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'velox-server', 'awaiting_approval', 'succeeded', ?, ?)
            """,
            (str(action_id), SE_ROLE, SE_CAPABILITY, "2026-11-10T00:00:00+00:00",
             "2026-11-10T00:00:00+00:00"),
        )


def test_existing_legacy_se_database_gains_goal_tables_without_migrating_se(
    db_path: Path,
) -> None:
    action_id = uuid4()
    write_legacy_se_v1(db_path, action_id)

    goal, stored = seeded(SqliteGoalPlanRepository(db_path))

    assert schema_versions(db_path) == {"goal_plans": 2, "software_engineering_runs": 1}
    se_state = SqliteSoftwareEngineeringRunRepository(db_path).get(action_id)
    assert se_state is not None
    assert se_state.execution_status == "succeeded"
    assert schema_versions(db_path) == {"goal_plans": 2, "software_engineering_runs": 3}
    goals = SqliteGoalPlanRepository(db_path)
    assert goals.get_goal(goal.goal.id) == goal
    assert goals.get_latest_plan(goal.goal.id) == stored


BOTH_READER = """
import sys
from pathlib import Path
from uuid import UUID
from apps.server.src.core.goal_plan_repository import SqliteGoalPlanRepository
from apps.server.src.integrations.software_engineering_state import (
    SqliteSoftwareEngineeringRunRepository,
)

path = Path(sys.argv[1])
goals = SqliteGoalPlanRepository(path)
se = SqliteSoftwareEngineeringRunRepository(path)
print(goals.get_latest_plan(UUID(sys.argv[2])).revision, se.get(UUID(sys.argv[3])).target)
"""


def test_shared_database_reopens_in_a_new_process(db_path: Path) -> None:
    action_id = register_se_action(db_path)
    _, stored = seeded(SqliteGoalPlanRepository(db_path))

    result = run_python(BOTH_READER, str(db_path), str(stored.plan.goal_id), str(action_id))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 velox-server"


# --- Isolation ------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("goal/plan persistence must not open sockets")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


@pytest.mark.usefixtures("no_network")
def test_full_lifecycle_needs_no_network_credentials_or_workers(
    repository: GoalPlanRepository,
) -> None:
    _, stored = seeded(repository)
    for step in reversed(stored.plan.steps):
        stored = move(repository, stored, step.id, PlanStepStatus.IN_PROGRESS)
        stored = move(repository, stored, step.id, PlanStepStatus.SUCCEEDED)

    assert stored.revision == 7


def test_repository_module_imports_only_storage_and_core_contracts() -> None:
    tree = ast.parse(REPOSITORY_MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if node.module == "apps.server.src.core":
                    imported.add(f"{node.module}.{alias.name}")
                else:
                    imported.add(node.module)

    assert imported == {
        "json",
        "sqlite3",
        "threading",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "enum",
        "pathlib",
        "typing",
        "uuid",
        "apps.server.src.core.goal_lifecycle",
        "apps.server.src.core.actions",
        "apps.server.src.core.goals",
    }


# --- Step delegation boundary (Sprint 6 Slice 3) -------------------------------


def recording_handoff(
    calls: list[PlanStep],
    status: StepDelegationStatus = StepDelegationStatus.QUEUED,
) -> Callable[[PlanStep], StepHandoff]:
    def handoff(step: PlanStep) -> StepHandoff:
        calls.append(step)
        return StepHandoff(action_id=uuid4(), status=status)

    return handoff


def delegate(
    repo: GoalPlanRepository,
    stored: StoredPlan,
    step_id: UUID,
    handoff: Callable[[PlanStep], StepHandoff],
    *,
    expected_revision: int | None = None,
) -> tuple[StoredPlan, StepDelegation]:
    return repo.delegate_step(
        stored.plan.goal_id,
        stored.plan.version,
        step_id,
        expected_revision=stored.revision if expected_revision is None else expected_revision,
        requested_by="owner",
        account_principal=None,
        account_identifier=None,
        handoff=handoff,
    )


@pytest.mark.parametrize(
    ("status", "started"),
    [
        (StepDelegationStatus.QUEUED, True),
        (StepDelegationStatus.AWAITING_APPROVAL, False),
        (StepDelegationStatus.DENIED, False),
        (StepDelegationStatus.ROUTE_REJECTED, False),
    ],
)
def test_delegate_step_records_once_and_starts_only_queued_work(
    repository: GoalPlanRepository,
    status: StepDelegationStatus,
    started: bool,
) -> None:
    _, stored = seeded(repository)
    context = stored.plan.steps[2]
    calls: list[PlanStep] = []

    updated, delegation = delegate(repository, stored, context.id, recording_handoff(calls, status))

    assert calls == [context]
    assert delegation.status is status
    assert updated.revision == stored.revision + 1
    expected = PlanStepStatus.IN_PROGRESS if started else PlanStepStatus.PENDING
    assert updated.plan.step(context.id).status is expected
    assert repository.get_plan(stored.plan.goal_id, 1) == updated
    assert repository.get_step_delegation(stored.plan.goal_id, 1, context.id) == delegation
    assert repository.get_step_delegation_for_action(delegation.action_id) == delegation
    with pytest.raises(GoalPlanDelegationExistsError) as error:
        delegate(repository, updated, context.id, recording_handoff(calls))
    assert error.value.delegation == delegation
    assert len(calls) == 1


def test_handoff_never_runs_when_a_precondition_fails(repository: GoalPlanRepository) -> None:
    goal, stored = seeded(repository)
    review, draft, context = stored.plan.steps
    calls: list[PlanStep] = []
    handoff = recording_handoff(calls)

    with pytest.raises(GoalPlanConflictError, match="stale"):
        delegate(repository, stored, context.id, handoff, expected_revision=9)
    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        delegate(repository, stored, draft.id, handoff)
    with pytest.raises(GoalPlanNotFoundError):
        delegate(repository, stored, uuid4(), handoff)
    repository.transition_goal(goal.goal.id, GoalStatus.ABANDONED, expected_revision=2)
    with pytest.raises(GoalPlanTransitionError, match="goal is active"):
        delegate(repository, stored, context.id, handoff)

    assert calls == []
    assert repository.get_plan(goal.goal.id, 1) == stored
    assert repository.get_step_delegation(goal.goal.id, 1, review.id) is None


def test_failing_or_invalid_handoff_records_nothing(repository: GoalPlanRepository) -> None:
    _, stored = seeded(repository)
    context_id = stored.plan.steps[2].id

    def explode(step: PlanStep) -> StepHandoff:
        raise RuntimeError("delegator down")

    with pytest.raises(RuntimeError, match="delegator down"):
        delegate(repository, stored, context_id, explode)
    with pytest.raises(GoalPlanContractError, match="StepHandoff"):
        delegate(repository, stored, context_id, lambda step: "queued")  # type: ignore[arg-type,return-value]

    assert repository.get_plan(stored.plan.goal_id, 1) == stored
    assert repository.get_step_delegation(stored.plan.goal_id, 1, context_id) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested_by": ""},
        {"requested_by": " owner"},
        {"account_principal": "someone", "account_identifier": None},
        {"account_identifier": "  "},
    ],
)
def test_delegate_step_requires_explicit_requester_and_account(
    repository: GoalPlanRepository,
    kwargs: dict[str, Any],
) -> None:
    _, stored = seeded(repository)
    values: dict[str, Any] = {
        "expected_revision": stored.revision,
        "requested_by": "owner",
        "account_principal": None,
        "account_identifier": None,
        "handoff": recording_handoff([]),
        **kwargs,
    }

    with pytest.raises(GoalPlanContractError):
        repository.delegate_step(stored.plan.goal_id, 1, stored.plan.steps[2].id, **values)
    assert repository.get_plan(stored.plan.goal_id, 1) == stored


def test_step_approval_starts_or_rejects_exactly_once(repository: GoalPlanRepository) -> None:
    _, stored = seeded(repository)
    context_id = stored.plan.steps[2].id
    held, delegation = delegate(
        repository,
        stored,
        context_id,
        recording_handoff([], StepDelegationStatus.AWAITING_APPROVAL),
    )

    approved, record = repository.resolve_step_approval(delegation.action_id, approved=True)

    assert record.status is StepDelegationStatus.APPROVED
    assert approved.revision == held.revision + 1
    assert approved.plan.step(context_id).status is PlanStepStatus.IN_PROGRESS
    with pytest.raises(GoalPlanConflictError, match="not awaiting approval"):
        repository.resolve_step_approval(delegation.action_id, approved=False)
    with pytest.raises(GoalPlanNotFoundError):
        repository.resolve_step_approval(uuid4(), approved=True)


def test_rejection_is_recorded_even_after_supersession(repository: GoalPlanRepository) -> None:
    goal, stored = seeded(repository)
    _, delegation = delegate(
        repository,
        stored,
        stored.plan.steps[2].id,
        recording_handoff([], StepDelegationStatus.AWAITING_APPROVAL),
    )
    repository.create_plan(make_chain(goal.goal.id, 2))

    with pytest.raises(GoalPlanConflictError, match="superseded"):
        repository.resolve_step_approval(delegation.action_id, approved=True)
    _, rejected = repository.resolve_step_approval(delegation.action_id, approved=False)

    assert rejected.status is StepDelegationStatus.APPROVAL_REJECTED
    first = repository.get_plan(goal.goal.id, 1)
    assert first is not None
    assert first.plan.steps[2].status is PlanStepStatus.PENDING


def test_v1_database_upgrades_to_v2_with_data_intact(db_path: Path) -> None:
    goal, stored = seeded(SqliteGoalPlanRepository(db_path))
    with raw(db_path) as connection:
        connection.execute("DROP TRIGGER goal_plan_step_delegations_definition_immutable")
        connection.execute("DROP TRIGGER goal_plan_step_delegations_retained")
        connection.execute("DROP TABLE goal_plan_step_delegations")
        connection.execute("UPDATE velox_schema SET version = 1 WHERE name = 'goal_plans'")

    repo = SqliteGoalPlanRepository(db_path)

    assert schema_versions(db_path) == {"goal_plans": 2}
    assert repo.get_goal(goal.goal.id) == goal
    assert repo.get_latest_plan(goal.goal.id) == stored
    _, delegation = delegate(repo, stored, stored.plan.steps[2].id, recording_handoff([]))
    assert repo.get_step_delegation_for_action(delegation.action_id) == delegation


def test_v1_database_missing_v1_objects_is_not_upgraded(db_path: Path) -> None:
    SqliteGoalPlanRepository(db_path)
    with raw(db_path) as connection:
        connection.execute("DROP TRIGGER goal_plan_step_delegations_definition_immutable")
        connection.execute("DROP TRIGGER goal_plan_step_delegations_retained")
        connection.execute("DROP TABLE goal_plan_step_delegations")
        connection.execute("DROP TRIGGER goals_retained")
        connection.execute("UPDATE velox_schema SET version = 1 WHERE name = 'goal_plans'")

    with pytest.raises(GoalPlanCorruptedError, match="incomplete"):
        SqliteGoalPlanRepository(db_path)
    assert schema_versions(db_path) == {"goal_plans": 1}


def test_delegation_records_are_immutable_and_retained(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    _, stored = seeded(repo)
    delegate(repo, stored, stored.plan.steps[2].id, recording_handoff([]))

    with raw(db_path) as connection:
        for statement in (
            "UPDATE goal_plan_step_delegations SET action_id = 'x'",
            "UPDATE goal_plan_step_delegations SET requested_by = 'mallory'",
            "UPDATE goal_plan_step_delegations SET account_identifier = 'other'",
            "DELETE FROM goal_plan_step_delegations",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
