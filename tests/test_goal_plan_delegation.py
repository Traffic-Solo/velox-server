"""Offline coverage for permission-gated Goal/Plan step delegation."""

import ast
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import (
    approve_pending_action,
    reject_pending_action,
)
from apps.server.src.core.config import Settings
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import (
    ActionTaskDelegator,
    TaskDelegationRequest,
    TaskDelegationResult,
)
from apps.server.src.core.goal_lifecycle import GoalPlanTransitionError
from apps.server.src.core.goal_plan_delegation import (
    GoalPlanApprovalError,
    GoalPlanDelegationOutcome,
    GoalPlanDelegationRequestError,
    GoalPlanDelegationResult,
    GoalPlanStepDelegationRequest,
)
from apps.server.src.core.goal_plan_repository import (
    GoalPlanConflictError,
    GoalPlanCorruptedError,
    GoalPlanNotFoundError,
    GoalPlanRepositoryError,
    InMemoryGoalPlanRepository,
    SqliteGoalPlanRepository,
    StepDelegationStatus,
    StoredPlan,
)
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep, PlanStepStatus
from apps.server.src.core.permission import PermissionDecision, PermissionStatus
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)
from apps.server.src.workers.runtime import WorkerRuntime, WorkerRuntimeInvocationService
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_MODULE = REPO_ROOT / "apps" / "server" / "src" / "core" / "goal_plan_delegation.py"
SAFE = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "summarize_email"}
GATED = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "gmail.send"}


def make_step(**overrides: Any) -> PlanStep:
    values: dict[str, Any] = {
        "objective": "Summarize the latest message for the owner",
        "target": "gmail-message-1",
        **SAFE,
    }
    values.update(overrides)
    return PlanStep(**values)


@pytest.fixture(params=["memory", "sqlite"])
def container(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ApplicationContainer:
    """Container whose workers fail loudly if anything tries to execute."""
    if request.param == "sqlite":
        monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(tmp_path / "g.sqlite3"))

    def no_execution(*args: object, **kwargs: object) -> WorkerExecutionResult:
        raise AssertionError("goal/plan delegation must not execute a worker")

    instance = ApplicationContainer()
    monkeypatch.setattr(instance.gmail_worker_executor, "execute", no_execution)
    monkeypatch.setattr(instance.calendar_worker_executor, "execute", no_execution)
    monkeypatch.setattr(WorkerRuntime, "process_next", no_execution)
    monkeypatch.setattr(WorkerRuntime, "process_action", no_execution)
    monkeypatch.setattr(WorkerRuntimeInvocationService, "invoke", no_execution)
    return instance


def planned(
    container: ApplicationContainer,
    *steps: PlanStep,
    goal_status: GoalStatus = GoalStatus.ACTIVE,
) -> StoredPlan:
    repo = container.goal_plan_repository
    goal = repo.create_goal(Goal(objective="Keep the inbox handled", success_criteria=("Done",)))
    stored = repo.create_plan(Plan(goal_id=goal.goal.id, steps=steps or (make_step(),)))
    revision = goal.revision
    for status in {
        GoalStatus.DRAFT: [],
        GoalStatus.ACTIVE: [GoalStatus.ACTIVE],
        GoalStatus.ACHIEVED: [GoalStatus.ACTIVE, GoalStatus.ACHIEVED],
        GoalStatus.ABANDONED: [GoalStatus.ABANDONED],
    }[goal_status]:
        revision = repo.transition_goal(goal.goal.id, status, expected_revision=revision).revision
    return stored


def ask(
    container: ApplicationContainer,
    stored: StoredPlan,
    step_id: UUID | None = None,
    **overrides: Any,
) -> GoalPlanDelegationResult:
    values: dict[str, Any] = {
        "goal_id": stored.plan.goal_id,
        "version": stored.plan.version,
        "step_id": step_id if step_id is not None else stored.plan.steps[0].id,
        "expected_revision": stored.revision,
        "requested_by": "owner",
        "account_context": GMAIL_ACCOUNT_CONTEXT,
    }
    values.update(overrides)
    return container.goal_plan_delegation.delegate(GoalPlanStepDelegationRequest(**values))


def latest(container: ApplicationContainer, stored: StoredPlan) -> StoredPlan:
    current = container.goal_plan_repository.get_plan(stored.plan.goal_id, stored.plan.version)
    assert current is not None
    return current


def assert_untouched(container: ApplicationContainer, stored: StoredPlan) -> None:
    assert latest(container, stored) == stored
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    for step in stored.plan.steps:
        assert container.goal_plan_repository.get_step_delegation(
            stored.plan.goal_id, stored.plan.version, step.id
        ) is None


def approve(container: ApplicationContainer, action_id: UUID) -> None:
    approve_pending_action(
        action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
        pending_action_recovery=container.software_engineering_action_recovery,
        approval_recorder=container.action_approval_recorder,
    )


def reject(container: ApplicationContainer, action_id: UUID) -> None:
    reject_pending_action(
        action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        reason="not now",
        pending_action_recovery=container.software_engineering_action_recovery,
        approval_recorder=container.action_approval_recorder,
    )


# --- Authorized handoff -------------------------------------------------------


def test_allowed_ready_step_is_queued_not_executed(container: ApplicationContainer) -> None:
    stored = planned(container)
    step = stored.plan.steps[0]

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.QUEUED
    assert result.delegation_status is StepDelegationStatus.QUEUED
    assert result.permission_status is PermissionStatus.ALLOWED
    assert result.routing_reason == "capability_route"
    assert result.duplicate is False
    assert result.plan_revision == stored.revision + 1
    [queued] = container.action_queue.list()
    assert queued.id == result.action_id
    assert queued.type == step.capability
    assert queued.target == step.target
    assert queued.executor_role == step.executor_role
    assert queued.payload == {
        "capability": step.capability,
        "objective": step.objective,
        "account_context": GMAIL_ACCOUNT_CONTEXT.as_metadata(),
    }
    lifecycle = container.action_lifecycle_repository.get(queued.id)
    assert lifecycle is not None and lifecycle.status is ActionStatus.APPROVED
    current = latest(container, stored)
    assert current.plan.version == stored.plan.version
    assert current.plan.step(step.id).status is PlanStepStatus.IN_PROGRESS
    assert container.worker_execution_observer.list() == []


def test_handoff_never_claims_completion(container: ApplicationContainer) -> None:
    stored = planned(container)

    result = ask(container, stored)

    current = latest(container, stored)
    goal = container.goal_plan_repository.get_goal(stored.plan.goal_id)
    assert result.outcome is GoalPlanDelegationOutcome.QUEUED
    assert current.plan.steps[0].status is PlanStepStatus.IN_PROGRESS
    assert goal is not None and goal.goal.status is GoalStatus.ACTIVE
    assert result.action_id is not None
    state = container.action_lifecycle_repository.get(result.action_id)
    assert state is not None and state.status is not ActionStatus.COMPLETED


def test_ready_steps_lists_only_undelegated_ready_steps(container: ApplicationContainer) -> None:
    first, second = make_step(), make_step(objective="Second", **GATED)
    third = make_step(objective="Third", depends_on=(first.id,))
    stored = planned(container, first, second, third)
    service = container.goal_plan_delegation

    assert service.ready_steps(stored.plan.goal_id) == (first, second)
    stored = latest(container, stored)
    ask(container, stored, second.id)

    assert service.ready_steps(stored.plan.goal_id) == (first,)
    draft = planned(container, make_step(), goal_status=GoalStatus.DRAFT)
    assert service.ready_steps(draft.plan.goal_id) == ()
    with pytest.raises(GoalPlanNotFoundError):
        service.ready_steps(uuid4())


# --- Goal safety ----------------------------------------------------------------


@pytest.mark.parametrize(
    "goal_status",
    [GoalStatus.DRAFT, GoalStatus.ACHIEVED, GoalStatus.ABANDONED],
)
def test_only_active_goals_start_work(
    container: ApplicationContainer,
    goal_status: GoalStatus,
) -> None:
    stored = planned(container, goal_status=goal_status)

    with pytest.raises(GoalPlanTransitionError, match="goal is active"):
        ask(container, stored)
    with pytest.raises(GoalPlanTransitionError, match="goal is active"):
        container.goal_plan_repository.transition_step(
            stored.plan.goal_id,
            1,
            stored.plan.steps[0].id,
            PlanStepStatus.IN_PROGRESS,
            expected_revision=stored.revision,
        )
    assert_untouched(container, stored)


@pytest.mark.parametrize("goal_status", [GoalStatus.ACHIEVED, GoalStatus.ABANDONED])
def test_terminal_goals_receive_no_new_plan_versions(
    container: ApplicationContainer,
    goal_status: GoalStatus,
) -> None:
    stored = planned(container, goal_status=goal_status)

    with pytest.raises(GoalPlanTransitionError, match="cannot receive new plan versions"):
        container.goal_plan_repository.create_plan(
            Plan(goal_id=stored.plan.goal_id, steps=(make_step(),), version=2),
        )
    assert container.goal_plan_repository.get_plan(stored.plan.goal_id, 2) is None


def test_draft_goal_may_still_be_planned(container: ApplicationContainer) -> None:
    stored = planned(container, goal_status=GoalStatus.DRAFT)

    second = container.goal_plan_repository.create_plan(
        Plan(goal_id=stored.plan.goal_id, steps=(make_step(),), version=2),
    )

    assert second.plan.version == 2


@pytest.mark.parametrize("goal_status", [GoalStatus.ACHIEVED, GoalStatus.ABANDONED])
@pytest.mark.parametrize(
    "terminal",
    [PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED, PlanStepStatus.CANCELLED],
)
def test_started_work_can_be_finalized_after_goal_closes(
    container: ApplicationContainer,
    goal_status: GoalStatus,
    terminal: PlanStepStatus,
) -> None:
    started, waiting = make_step(), make_step(objective="Not yet")
    stored = planned(container, started, waiting)
    ask(container, stored, started.id)
    repo = container.goal_plan_repository
    goal = repo.get_goal(stored.plan.goal_id)
    assert goal is not None
    repo.transition_goal(goal.goal.id, goal_status, expected_revision=goal.revision)
    stored = latest(container, stored)

    stored = repo.transition_step(
        goal.goal.id, 1, started.id, terminal, expected_revision=stored.revision
    )
    stored = repo.transition_step(
        goal.goal.id, 1, waiting.id, PlanStepStatus.CANCELLED, expected_revision=stored.revision
    )

    assert stored.plan.step(started.id).status is terminal
    assert stored.plan.step(waiting.id).status is PlanStepStatus.CANCELLED
    final = repo.get_goal(goal.goal.id)
    assert final is not None and final.goal.status is goal_status


# --- Plan integrity -------------------------------------------------------------


def test_unknown_goal_plan_or_step_is_rejected(container: ApplicationContainer) -> None:
    stored = planned(container)

    with pytest.raises(GoalPlanNotFoundError):
        ask(container, stored, goal_id=uuid4())
    with pytest.raises(GoalPlanNotFoundError):
        ask(container, stored, version=2)
    with pytest.raises(GoalPlanNotFoundError, match="plan step"):
        ask(container, stored, uuid4())
    assert_untouched(container, stored)


def test_superseded_plan_is_rejected(container: ApplicationContainer) -> None:
    stored = planned(container)
    container.goal_plan_repository.create_plan(
        Plan(goal_id=stored.plan.goal_id, steps=(make_step(),), version=2),
    )

    with pytest.raises(GoalPlanConflictError, match="superseded"):
        ask(container, stored)
    assert_untouched(container, stored)


def test_incomplete_dependencies_are_rejected(container: ApplicationContainer) -> None:
    first = make_step()
    second = make_step(objective="After first", depends_on=(first.id,))
    stored = planned(container, first, second)

    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        ask(container, stored, second.id)
    assert_untouched(container, stored)

    stored = latest(container, stored)
    result = ask(container, stored, first.id)
    stored = latest(container, stored)
    with pytest.raises(GoalPlanTransitionError, match="before dependency"):
        ask(container, stored, second.id)
    assert [action.id for action in container.action_queue.list()] == [result.action_id]


@pytest.mark.parametrize(
    "status",
    [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED, PlanStepStatus.CANCELLED],
)
def test_only_pending_steps_are_delegated(
    container: ApplicationContainer,
    status: PlanStepStatus,
) -> None:
    stored = planned(container)
    step_id = stored.plan.steps[0].id
    repo = container.goal_plan_repository
    path = {
        PlanStepStatus.IN_PROGRESS: [PlanStepStatus.IN_PROGRESS],
        PlanStepStatus.SUCCEEDED: [PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED],
        PlanStepStatus.CANCELLED: [PlanStepStatus.CANCELLED],
    }[status]
    for target in path:
        stored = repo.transition_step(
            stored.plan.goal_id, 1, step_id, target, expected_revision=stored.revision
        )

    with pytest.raises(GoalPlanTransitionError, match="not pending"):
        ask(container, stored)
    assert container.action_queue.list() == []
    assert latest(container, stored) == stored


@pytest.mark.parametrize(
    ("account", "reason"),
    [
        (None, "missing_account_context"),
        (WorkerAccountContext("someone", "unknown-account"), "no_handler"),
    ],
)
def test_unroutable_step_is_rejected_and_recorded(
    container: ApplicationContainer,
    account: WorkerAccountContext | None,
    reason: str,
) -> None:
    stored = planned(container)

    result = ask(container, stored, account_context=account)

    assert result.outcome is GoalPlanDelegationOutcome.ROUTE_REJECTED
    assert result.routing_reason == reason
    assert result.permission_status is None
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    current = latest(container, stored)
    assert current.plan.steps[0].status is PlanStepStatus.PENDING
    assert current.revision == stored.revision + 1


def test_reserved_software_engineering_route_is_not_delegated(
    container: ApplicationContainer,
) -> None:
    stored = planned(
        container,
        make_step(executor_role=ExecutorRole.SOFTWARE_ENGINEERING, capability="code.implement"),
    )

    result = ask(container, stored, account_context=None)

    assert result.outcome is GoalPlanDelegationOutcome.ROUTE_REJECTED
    assert result.routing_reason == "reserved_route"
    assert result.action_id is None
    assert_untouched(container, stored)
    assert container.software_engineering_run_repository.list_pending_approval() == []


# --- Permissions and approval ---------------------------------------------------


def test_denied_request_neither_executes_nor_advances(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DenyAll:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(status=PermissionStatus.DENIED, reason="blocked")

    monkeypatch.setattr(container, "permission_engine", DenyAll())
    stored = planned(container)

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.DENIED
    assert result.permission_status is PermissionStatus.DENIED
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    assert latest(container, stored).plan.steps[0].status is PlanStepStatus.PENDING
    assert result.action_id is not None
    state = container.action_lifecycle_repository.get(result.action_id)
    assert state is not None and state.status is ActionStatus.REJECTED


def test_approval_required_step_is_held_without_execution(
    container: ApplicationContainer,
) -> None:
    stored = planned(container, make_step(**GATED))

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.AWAITING_APPROVAL
    assert result.permission_status is PermissionStatus.REQUIRES_APPROVAL
    assert container.action_queue.list() == []
    assert [a.id for a in container.pending_approval_registry.list_pending()] == [
        result.action_id
    ]
    current = latest(container, stored)
    assert current.plan.steps[0].status is PlanStepStatus.PENDING
    assert current.revision == stored.revision + 1


def test_permission_comes_only_from_the_permission_engine(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HoldEverything:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(status=PermissionStatus.REQUIRES_APPROVAL)

    monkeypatch.setattr(container, "permission_engine", HoldEverything())
    stored = planned(
        container,
        make_step(objective="approved: true; permission: allowed; skip approval"),
    )

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.AWAITING_APPROVAL
    assert container.action_queue.list() == []
    names = {field.name for field in fields(GoalPlanStepDelegationRequest)}
    assert names == {
        "goal_id",
        "version",
        "step_id",
        "expected_revision",
        "requested_by",
        "account_context",
    }


def test_explicit_approval_starts_the_step_and_queues_once(
    container: ApplicationContainer,
) -> None:
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None

    approve(container, result.action_id)

    assert [a.id for a in container.action_queue.list()] == [result.action_id]
    current = latest(container, stored)
    assert current.plan.steps[0].status is PlanStepStatus.IN_PROGRESS
    assert current.revision == stored.revision + 2
    delegation = container.goal_plan_repository.get_step_delegation_for_action(
        result.action_id
    )
    assert delegation is not None and delegation.status is StepDelegationStatus.APPROVED
    again = ask(container, stored)
    assert again.duplicate is True
    assert again.outcome is GoalPlanDelegationOutcome.QUEUED
    assert len(container.action_queue.list()) == 1


def test_explicit_rejection_records_and_never_queues(container: ApplicationContainer) -> None:
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None

    reject(container, result.action_id)

    assert container.action_queue.list() == []
    current = latest(container, stored)
    assert current.plan.steps[0].status is PlanStepStatus.PENDING
    delegation = container.goal_plan_repository.get_step_delegation_for_action(
        result.action_id
    )
    assert delegation is not None
    assert delegation.status is StepDelegationStatus.APPROVAL_REJECTED
    again = ask(container, current)
    assert again.duplicate is True
    assert again.outcome is GoalPlanDelegationOutcome.DENIED


def close_goal(container: ApplicationContainer, stored: StoredPlan) -> None:
    goal = container.goal_plan_repository.get_goal(stored.plan.goal_id)
    assert goal is not None
    container.goal_plan_repository.transition_goal(
        goal.goal.id, GoalStatus.ABANDONED, expected_revision=goal.revision
    )


def cancel_step(container: ApplicationContainer, stored: StoredPlan) -> None:
    current = latest(container, stored)
    container.goal_plan_repository.transition_step(
        stored.plan.goal_id,
        1,
        stored.plan.steps[0].id,
        PlanStepStatus.CANCELLED,
        expected_revision=current.revision,
    )


def supersede(container: ApplicationContainer, stored: StoredPlan) -> None:
    container.goal_plan_repository.create_plan(
        Plan(goal_id=stored.plan.goal_id, steps=(make_step(),), version=2),
    )


@pytest.mark.parametrize("change", [close_goal, cancel_step, supersede])
def test_approval_fails_closed_when_plan_state_changed(
    container: ApplicationContainer,
    change: Callable[[ApplicationContainer, StoredPlan], None],
) -> None:
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None
    change(container, stored)
    before = latest(container, stored)

    with pytest.raises(GoalPlanApprovalError):
        approve(container, result.action_id)

    assert container.action_queue.list() == []
    assert [a.id for a in container.pending_approval_registry.list_pending()] == [
        result.action_id
    ]
    assert latest(container, stored) == before
    delegation = container.goal_plan_repository.get_step_delegation_for_action(
        result.action_id
    )
    assert delegation is not None
    assert delegation.status is StepDelegationStatus.AWAITING_APPROVAL


def test_unrelated_approvals_are_unaffected(container: ApplicationContainer) -> None:
    result = container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Send the reply",
            target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY,
            capability="gmail.send",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )

    approve(container, result.action_id)

    assert [a.id for a in container.action_queue.list()] == [result.action_id]


def test_approval_conflict_maps_to_http_409(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.server.src.api import events
    from apps.server.src.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(events, "get_container", lambda: container)
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    cancel_step(container, stored)

    response = TestClient(app).post(f"/actions/{result.action_id}/approve")

    assert response.status_code == 409
    assert container.action_queue.list() == []


# --- Authority, account routing and idempotency ---------------------------------


@pytest.mark.parametrize("requested_by", ["", "   ", " owner", "owner\n", None, 42])
def test_missing_or_ambiguous_authority_fails_closed(
    container: ApplicationContainer,
    requested_by: object,
) -> None:
    stored = planned(container)

    with pytest.raises(GoalPlanDelegationRequestError, match="requester"):
        ask(container, stored, requested_by=requested_by)
    assert_untouched(container, stored)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_revision": 0},
        {"expected_revision": True},
        {"version": 0},
        {"goal_id": "goal"},
        {"account_context": {"account_identifier": "gmail-local-account"}},
        {"account_context": WorkerAccountContext(principal=None, account_identifier=" x")},
    ],
)
def test_malformed_requests_fail_before_any_side_effect(
    container: ApplicationContainer,
    overrides: dict[str, Any],
) -> None:
    stored = planned(container)

    with pytest.raises(GoalPlanDelegationRequestError):
        ask(container, stored, **overrides)
    assert_untouched(container, stored)


def test_retry_returns_the_existing_delegation(container: ApplicationContainer) -> None:
    stored = planned(container)
    first = ask(container, stored)

    second = ask(container, stored)
    third = ask(container, latest(container, stored))

    for again in (second, third):
        assert again.duplicate is True
        assert again.action_id == first.action_id
        assert again.outcome is first.outcome
        assert again.plan_revision == first.plan_revision
    assert len(container.action_queue.list()) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"requested_by": "someone-else"},
        {"account_context": WorkerAccountContext("other", "gmail-local-account")},
        {"account_context": None},
    ],
)
def test_retry_cannot_silently_change_authority_or_account(
    container: ApplicationContainer,
    overrides: dict[str, Any],
) -> None:
    stored = planned(container)
    ask(container, stored)

    with pytest.raises(GoalPlanConflictError, match="different requester or account"):
        ask(container, stored, **overrides)
    assert len(container.action_queue.list()) == 1


def test_stale_revision_is_rejected(container: ApplicationContainer) -> None:
    first, second = make_step(), make_step(objective="Second")
    stored = planned(container, first, second)
    ask(container, stored, first.id)

    with pytest.raises(GoalPlanConflictError, match="stale plan revision"):
        ask(container, stored, second.id)
    assert len(container.action_queue.list()) == 1


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_requests_hand_a_step_off_once(
    container: ApplicationContainer,
    attempt: int,
) -> None:
    stored = planned(container)
    barrier = threading.Barrier(8)
    results: list[GoalPlanDelegationResult] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            outcome = ask(container, stored)
        except BaseException as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 8
    assert [result.duplicate for result in results].count(False) == 1
    assert len({result.action_id for result in results}) == 1
    assert len(container.action_queue.list()) == 1
    assert latest(container, stored).revision == stored.revision + 1


# --- Failure and interruption ---------------------------------------------------


def test_raising_delegator_reports_failure_and_records_nothing(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(request: TaskDelegationRequest) -> TaskDelegationResult:
        raise RuntimeError("registry unavailable")

    stored = planned(container)
    monkeypatch.setattr(ActionTaskDelegator, "delegate", explode)

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.FAILED
    assert result.action_id is None
    assert result.delegation_status is None
    assert_untouched(container, stored)
    monkeypatch.undo()
    assert ask(container, stored).outcome is GoalPlanDelegationOutcome.QUEUED


@pytest.mark.parametrize("step_kind", [SAFE, GATED])
def test_unrecordable_handoff_is_revoked_and_reported_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_kind: dict[str, Any],
) -> None:
    path = tmp_path / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    container = ApplicationContainer()
    stored = planned(container, make_step(**step_kind))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_fail_delegation BEFORE INSERT ON goal_plan_step_delegations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """
        )

    result = ask(container, stored)

    assert result.outcome is GoalPlanDelegationOutcome.FAILED
    assert result.action_id is not None
    assert result.reason is not None and "never published" in result.reason
    assert_untouched(container, stored)
    assert container.action_lifecycle_repository.get(result.action_id) is None
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER test_fail_delegation")
    assert ask(container, stored).outcome is not GoalPlanDelegationOutcome.FAILED


CRASHER = """
import os
import sys
from uuid import UUID
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.goal_plan_delegation import GoalPlanStepDelegationRequest
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT

container = ApplicationContainer()
repository = container.goal_plan_repository
connect = repository._connect

def crashing_connect():
    connection = connect()
    connection.set_trace_callback(
        lambda sql: os._exit(23) if "INSERT INTO goal_plan_step_delegations" in sql else None
    )
    return connection

repository._connect = crashing_connect
container.goal_plan_delegation.delegate(GoalPlanStepDelegationRequest(
    goal_id=UUID(sys.argv[1]), version=1, step_id=UUID(sys.argv[2]),
    expected_revision=int(sys.argv[3]), requested_by="owner",
    account_context=GMAIL_ACCOUNT_CONTEXT,
))
"""


def test_process_killed_mid_handoff_leaves_no_false_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    container = ApplicationContainer()
    stored = planned(container)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VELOX_")
    }
    env.update(
        {"PYTHONPATH": str(REPO_ROOT), "VELOX_GOAL_PLAN_STATE_DATABASE_PATH": str(path)},
    )

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASHER,
            str(stored.plan.goal_id),
            str(stored.plan.steps[0].id),
            str(stored.revision),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert crashed.returncode == 23, crashed.stderr
    assert_untouched(container, stored)
    assert ask(container, stored).outcome is GoalPlanDelegationOutcome.QUEUED


# --- Composition and isolation --------------------------------------------------


def test_container_uses_in_memory_repository_by_default() -> None:
    assert isinstance(ApplicationContainer().goal_plan_repository, InMemoryGoalPlanRepository)


def test_container_uses_durable_repository_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))

    repository = ApplicationContainer().goal_plan_repository

    assert isinstance(repository, SqliteGoalPlanRepository)
    assert repository.path == path


@pytest.mark.parametrize("value", ["relative/goals.sqlite3", ""])
def test_invalid_durable_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", value)

    with pytest.raises(ValidationError, match="must be absolute"):
        Settings()


def test_corrupted_delegation_record_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    container = ApplicationContainer()
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE goal_plan_step_delegations SET status = 'approved!'")

    with pytest.raises(GoalPlanCorruptedError):
        ask(container, stored)
    with pytest.raises(GoalPlanCorruptedError):
        approve(container, result.action_id)
    assert container.action_queue.list() == []


def test_delegation_flow_opens_no_sockets(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("goal/plan delegation must not open sockets")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None

    approve(container, result.action_id)

    assert len(container.action_queue.list()) == 1


def test_service_depends_only_on_existing_core_roles() -> None:
    tree = ast.parse(SERVICE_MODULE.read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(module.startswith("apps.server.src.integrations") for module in imported)
    assert "apps.server.src.workers.runtime" not in imported
    assert {module for module in imported if module.startswith("apps.")} == {
        "apps.server.src.core.action_lifecycle_repository",
        "apps.server.src.core.action_queue",
        "apps.server.src.core.actions",
        "apps.server.src.core.approvals",
        "apps.server.src.core.delegation",
        "apps.server.src.core.goal_lifecycle",
        "apps.server.src.core.goal_plan_repository",
        "apps.server.src.core.goals",
        "apps.server.src.core.permission",
        "apps.server.src.workers.executor",
    }


# --- Pre-commit visibility (staged publication) ---------------------------------


class RecordingWorker:
    """Fake provider that only records which Actions reached execution."""

    def __init__(self) -> None:
        self.executed: list[UUID] = []
        self._lock = threading.Lock()

    def execute(self, action: Action, **kwargs: object) -> WorkerExecutionResult:
        with self._lock:
            self.executed.append(action.id)
        return WorkerExecutionResult(action=action, status=WorkerExecutionStatus.SUCCEEDED)


def consumer_container(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ApplicationContainer, RecordingWorker, Path]:
    path = tmp_path / "consumer.sqlite3"
    if backend == "sqlite":
        monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    container = ApplicationContainer()
    worker = RecordingWorker()
    monkeypatch.setattr(container.gmail_worker_executor, "execute", worker.execute)
    return container, worker, path


def pause_inside_handoff(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_after_staging: bool = False,
) -> tuple[threading.Event, threading.Event]:
    """Block the delegator inside the open durable transaction until released."""
    handed_off, released = threading.Event(), threading.Event()
    service = container.goal_plan_delegation
    build = service._staged_delegator

    class PausingDelegator:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def delegate(self, request: TaskDelegationRequest) -> TaskDelegationResult:
            result: TaskDelegationResult = self._inner.delegate(request)
            handed_off.set()
            assert released.wait(10)
            if fail_after_staging:
                raise RuntimeError("delegator failed after staging its side effects")
            return result

    monkeypatch.setattr(
        service,
        "_staged_delegator",
        lambda queue, holds, lifecycle: PausingDelegator(build(queue, holds, lifecycle)),
    )
    return handed_off, released


def visible_work(container: ApplicationContainer) -> dict[str, Any]:
    return {
        "queued": container.action_queue.list(),
        "held": container.pending_approval_registry.list_pending(),
        "lifecycle": container.action_lifecycle_repository.list_states(),
    }


def nothing_visible() -> dict[str, Any]:
    return {"queued": [], "held": [], "lifecycle": {}}


VISIBILITY_CASES = [
    (backend, fate)
    for backend in ("memory", "sqlite")
    for fate in ("commit", "delegator_fails", "durable_write_fails")
    # The durable write failure is injected through a SQLite trigger.
    if not (backend == "memory" and fate == "durable_write_fails")
]


@pytest.mark.parametrize(("backend", "fate"), VISIBILITY_CASES)
@pytest.mark.parametrize("step_kind", [SAFE, GATED], ids=["allowed", "approval"])
def test_concurrent_consumer_sees_nothing_before_the_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    step_kind: dict[str, Any],
    fate: str,
) -> None:
    container, worker, path = consumer_container(backend, tmp_path, monkeypatch)
    stored = planned(container, make_step(**step_kind))
    if fate == "durable_write_fails":
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TRIGGER test_fail_delegation
                BEFORE INSERT ON goal_plan_step_delegations
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END
                """
            )
    handed_off, released = pause_inside_handoff(
        container,
        monkeypatch,
        fail_after_staging=fate == "delegator_fails",
    )
    observed: dict[str, Any] = {}

    def consumer() -> None:
        assert handed_off.wait(10)
        observed["visible"] = visible_work(container)
        observed["processed"] = container.worker_runtime.process_next().processed
        held = [a.id for a in container.pending_approval_registry.list_pending()]
        observed["approvable"] = held
        released.set()

    thread = threading.Thread(target=consumer)
    thread.start()
    result = ask(container, stored)
    thread.join(timeout=30)

    assert observed == {"visible": nothing_visible(), "processed": False, "approvable": []}
    assert worker.executed == []
    if fate == "commit":
        assert result.outcome in {
            GoalPlanDelegationOutcome.QUEUED,
            GoalPlanDelegationOutcome.AWAITING_APPROVAL,
        }
        visible = visible_work(container)
        published = visible["queued"] if step_kind is SAFE else visible["held"]
        assert [action.id for action in published] == [result.action_id]
        assert result.action_id in visible["lifecycle"]
    else:
        assert result.outcome is GoalPlanDelegationOutcome.FAILED
        assert visible_work(container) == nothing_visible()
        assert container.worker_runtime.process_next().processed is False
        assert_untouched(container, stored)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_polling_consumer_only_ever_executes_committed_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    container, worker, _ = consumer_container(backend, tmp_path, monkeypatch)
    steps = tuple(make_step(objective=f"Summarize {index}") for index in range(12))
    stored = planned(container, *steps)
    doomed = {step.id for index, step in enumerate(steps) if index % 3 == 0}
    build = container.goal_plan_delegation._staged_delegator
    current_step: dict[str, UUID] = {}

    class FailingForDoomedSteps:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def delegate(self, request: TaskDelegationRequest) -> TaskDelegationResult:
            result: TaskDelegationResult = self._inner.delegate(request)
            if current_step["id"] in doomed:
                raise RuntimeError("injected failure after staging")
            return result

    monkeypatch.setattr(
        container.goal_plan_delegation,
        "_staged_delegator",
        lambda queue, holds, lifecycle: FailingForDoomedSteps(build(queue, holds, lifecycle)),
    )
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            container.worker_runtime.process_next()

    consumer = threading.Thread(target=poll)
    consumer.start()
    committed: set[UUID] = set()
    try:
        for step in steps:
            current_step["id"] = step.id
            stored = latest(container, stored)
            result = ask(container, stored, step.id)
            if result.outcome is GoalPlanDelegationOutcome.QUEUED:
                assert result.action_id is not None
                committed.add(result.action_id)
            else:
                assert result.outcome is GoalPlanDelegationOutcome.FAILED
                assert step.id in doomed
    finally:
        stop.set()
        consumer.join(timeout=30)
    while container.worker_runtime.process_next().processed:
        pass

    assert len(committed) == len(steps) - len(doomed)
    assert set(worker.executed) == committed
    assert len(worker.executed) == len(committed)


def test_durable_approval_commits_before_the_action_is_queued(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None
    repository = container.goal_plan_repository
    resolve = repository.resolve_step_approval
    seen_during_durable_write: list[list[Action]] = []

    def observing(action_id: UUID, *, approved: bool) -> Any:
        seen_during_durable_write.append(container.action_queue.list())
        return resolve(action_id, approved=approved)

    monkeypatch.setattr(repository, "resolve_step_approval", observing)

    approve(container, result.action_id)

    assert seen_during_durable_write == [[]]
    assert [a.id for a in container.action_queue.list()] == [result.action_id]


def test_failed_durable_approval_queues_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    container = ApplicationContainer()
    stored = planned(container, make_step(**GATED))
    result = ask(container, stored)
    assert result.action_id is not None
    held = latest(container, stored)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_fail_approval
            BEFORE UPDATE OF status ON goal_plan_step_delegations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """
        )

    with pytest.raises(GoalPlanRepositoryError):
        approve(container, result.action_id)

    assert container.action_queue.list() == []
    assert [a.id for a in container.pending_approval_registry.list_pending()] == [
        result.action_id
    ]
    assert latest(container, stored) == held
    state = container.action_lifecycle_repository.get(result.action_id)
    assert state is not None and state.status is ActionStatus.QUEUED
