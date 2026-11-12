"""Offline coverage for the pre-execution Goal/Plan admission gate."""

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import approve_pending_action
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import TaskDelegationRequest
from apps.server.src.core.goal_plan_admission import GoalPlanAdmissionGate
from apps.server.src.core.goal_plan_delegation import (
    GoalPlanDelegationOutcome,
    GoalPlanStepDelegationRequest,
)
from apps.server.src.core.goal_plan_execution import (
    GoalPlanExecutionEvidenceError,
    StepExecutionCategory,
)
from apps.server.src.core.goal_plan_repository import GoalPlanRepositoryError, StoredPlan
from apps.server.src.core.goals import GoalStatus, Plan, PlanStepStatus
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.workers.executor import WorkerExecutionResult, WorkerExecutionStatus
from apps.server.src.workers.runtime import WorkerAdmission
from tests import test_goal_plan_execution as base


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "goals.sqlite3"


def rejected(result: Any, reason: str) -> None:
    assert result.admission_rejected is True
    assert result.processed is False
    assert result.external_execution_performed is False
    assert result.execution_status is None
    assert result.execution_reason == f"pre-execution admission rejected: {reason}"
    assert result.lifecycle_state.status is ActionStatus.REJECTED


def assert_blocked(
    container: ApplicationContainer,
    worker: base.FakeWorker,
    stored: StoredPlan,
    action_id: UUID,
    reason: str,
) -> None:
    observed = len(container.worker_execution_observer.list())
    before = base.current(container, stored)

    result = container.worker_runtime.process_next()

    rejected(result, reason)
    assert worker.executed == []
    assert len(container.worker_execution_observer.list()) == observed
    assert base.attempts(container, stored) == ()
    assert container.action_queue.list() == []
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.REJECTED
    assert base.current(container, stored) == before
    assert container.worker_runtime.process_next().processed is False
    assert worker.executed == []


def move(container: ApplicationContainer, stored: StoredPlan, status: PlanStepStatus) -> None:
    container.goal_plan_repository.transition_step(
        stored.plan.goal_id, 1, stored.plan.steps[0].id, status,
        expected_revision=base.current(container, stored).revision,
    )


def close_goal(container: ApplicationContainer, stored: StoredPlan, *path: GoalStatus) -> None:
    for status in path:
        goal = base.goal_of(container, stored)
        container.goal_plan_repository.transition_goal(
            goal.goal.id, status, expected_revision=goal.revision
        )


# --- Admitted ----------------------------------------------------------------------


def test_current_in_progress_step_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)

    result = container.worker_runtime.process_next()

    assert result.admission_rejected is False
    assert result.processed is True
    assert worker.executed == [action_id]
    assert len(base.attempts(container, stored)) == 1


def test_approved_action_recovered_after_restart_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    before, _ = base.boot(monkeypatch, db_path)
    stored = base.planned(before, base.make_step(**base.GATED))
    held = before.goal_plan_delegation.delegate(
        GoalPlanStepDelegationRequest(
            goal_id=stored.plan.goal_id, version=1, step_id=stored.plan.steps[0].id,
            expected_revision=stored.revision, requested_by="owner",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    assert held.outcome is GoalPlanDelegationOutcome.AWAITING_APPROVAL
    assert held.action_id is not None
    after, worker = base.boot(monkeypatch, db_path)
    approve_pending_action(
        held.action_id,
        pending_approval_registry=after.pending_approval_registry,
        lifecycle_repository=after.action_lifecycle_repository,
        lifecycle_manager=after.action_lifecycle_manager,
        action_queue=after.action_queue,
        pending_action_recovery=after.pending_action_recovery,
        approval_recorder=after.action_approval_recorder,
    )

    result = after.worker_runtime.process_next()

    assert result.processed is True
    assert worker.executed == [held.action_id]


def test_unrelated_and_software_engineering_actions_are_admitted(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    gate = GoalPlanAdmissionGate(container.goal_plan_repository)
    se_action = Action(
        type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
        payload={"capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY, "objective": "x"},
    )
    assert gate.admit(se_action) == WorkerAdmission(admitted=True)
    container.action_lifecycle_repository.set(
        se_action.id, ActionLifecycleState(status=ActionStatus.APPROVED)
    )
    container.action_queue.enqueue(se_action)

    se_result = container.worker_runtime.process_next()

    assert se_result.admission_rejected is False
    assert se_result.execution_status is WorkerExecutionStatus.SKIPPED
    se_state = container.software_engineering_run_repository.get(se_action.id)
    assert se_state is not None and se_state.execution_status == "skipped"
    unrelated = container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Summarize something else",
            target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY,
            capability="summarize_email",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    assert container.worker_runtime.process_next().processed is True
    assert worker.executed == [unrelated.action_id]


# --- Lifecycle refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        [PlanStepStatus.CANCELLED],
        [PlanStepStatus.FAILED],
        [PlanStepStatus.SUCCEEDED],
    ],
    ids=["cancelled", "failed", "succeeded"],
)
def test_finished_step_blocks_its_queued_action(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    path: list[PlanStepStatus],
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    for status in path:
        move(container, stored, status)

    assert_blocked(container, worker, stored, action_id, "step_not_in_progress")


@pytest.mark.parametrize(
    "path",
    [[GoalStatus.ABANDONED], [GoalStatus.ACHIEVED]],
    ids=["abandoned", "achieved"],
)
def test_closed_goal_blocks_its_queued_action(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    path: list[GoalStatus],
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    close_goal(container, stored, *path)

    assert_blocked(container, worker, stored, action_id, "goal_not_active")
    assert base.current(container, stored).plan.steps[0].status is PlanStepStatus.IN_PROGRESS


def test_superseded_plan_blocks_its_queued_action(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    move(container, stored, PlanStepStatus.CANCELLED)
    container.goal_plan_repository.create_plan(
        Plan(goal_id=stored.plan.goal_id, steps=(base.make_step(),), version=2),
    )

    assert_blocked(container, worker, stored, action_id, "plan_not_current")


@pytest.mark.parametrize("decision", ["pending", "rejected"])
def test_unreleased_delegation_blocks_a_forced_action(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    decision: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored = base.planned(container, base.make_step(**base.GATED))
    held = container.goal_plan_delegation.delegate(
        GoalPlanStepDelegationRequest(
            goal_id=stored.plan.goal_id, version=1, step_id=stored.plan.steps[0].id,
            expected_revision=stored.revision, requested_by="owner",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    assert held.action_id is not None
    [action] = container.pending_approval_registry.list_pending()
    if decision == "rejected":
        container.goal_plan_repository.resolve_step_approval(held.action_id, approved=False)
    container.action_lifecycle_repository.set(
        action.id, ActionLifecycleState(status=ActionStatus.APPROVED)
    )
    container.action_queue.enqueue(action)
    stored = base.current(container, stored)

    assert_blocked(container, worker, stored, held.action_id, "delegation_not_released")


MUTATIONS: list[tuple[str, Callable[[Action], Action]]] = [
    ("target", lambda a: a.model_copy(update={"target": "gmail-message-999"})),
    ("role", lambda a: a.model_copy(update={"executor_role": ExecutorRole.CONTENT_REVIEW})),
    (
        "capability",
        lambda a: a.model_copy(
            update={
                "type": "gmail.archive",
                "payload": {**a.payload, "capability": "gmail.archive"},
            },
        ),
    ),
    ("objective", lambda a: a.model_copy(update={"payload": {**a.payload, "objective": "x"}})),
    (
        "account",
        lambda a: a.model_copy(
            update={
                "payload": {
                    **a.payload,
                    "account_context": {"principal": None, "account_identifier": "other"},
                },
            },
        ),
    ),
    (
        "no_account",
        lambda a: a.model_copy(
            update={"payload": {k: v for k, v in a.payload.items() if k != "account_context"}},
        ),
    ),
]


@pytest.mark.parametrize(
    "mutate", [m for _, m in MUTATIONS], ids=[name for name, _ in MUTATIONS]
)
def test_action_that_differs_from_its_stored_step_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    mutate: Callable[[Action], Action],
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    [queued] = container.action_queue.list()
    container.action_queue.clear()
    container.action_queue.enqueue(mutate(queued))

    assert_blocked(container, worker, stored, action_id, "action_mismatch")


def test_previously_attempted_action_is_never_started_again(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    [queued] = container.action_queue.list()
    container.worker_runtime.process_next()
    assert worker.executed == [action_id]
    container.action_lifecycle_repository.set(
        action_id, ActionLifecycleState(status=ActionStatus.APPROVED)
    )
    container.action_queue.enqueue(queued)

    result = container.worker_runtime.process_next()

    rejected(result, "already_attempted")
    assert worker.executed == [action_id]
    assert len(base.attempts(container, stored)) == 1


# --- Fail closed ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("UPDATE goal_plan_step_delegations SET status = 'bogus'", "ownership_unreadable"),
        ("UPDATE goals SET status = 'bogus'", "state_unreadable"),
        ("UPDATE goal_plan_steps SET status = 'bogus'", "state_unreadable"),
    ],
)
def test_corrupted_durable_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    statement: str,
    reason: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path)
    _, action_id = base.delegated(container)
    with sqlite3.connect(db_path) as connection:
        connection.execute(statement)

    result = container.worker_runtime.process_next()

    rejected(result, reason)
    assert worker.executed == []
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.REJECTED


@pytest.mark.parametrize(
    ("method", "reason"),
    [
        ("get_step_delegation_for_action", "ownership_unreadable"),
        ("get_goal", "state_unreadable"),
        ("get_latest_plan", "state_unreadable"),
        ("list_step_executions", "state_unreadable"),
    ],
)
def test_repository_read_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    method: str,
    reason: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    base.delegated(container)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise GoalPlanRepositoryError("database is unavailable")

    monkeypatch.setattr(container.goal_plan_repository, method, unavailable)

    rejected(container.worker_runtime.process_next(), reason)
    assert worker.executed == []


def test_unrelated_action_is_refused_when_ownership_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Summarize something else", target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY, capability="summarize_email",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )

    def unavailable(*args: object, **kwargs: object) -> None:
        raise GoalPlanRepositoryError("database is unavailable")

    monkeypatch.setattr(
        container.goal_plan_repository, "get_step_delegation_for_action", unavailable
    )

    rejected(container.worker_runtime.process_next(), "ownership_unreadable")
    assert worker.executed == []


def test_crashing_gate_refuses_execution(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    base.delegated(container)

    def explode(self: GoalPlanAdmissionGate, action: Action) -> WorkerAdmission:
        raise RuntimeError("gate bug")

    monkeypatch.setattr(GoalPlanAdmissionGate, "admit", explode)

    rejected(container.worker_runtime.process_next(), "admission_unavailable")
    assert worker.executed == []


# --- Batch processing, retries and lifecycle ------------------------------------------


def test_rejected_action_does_not_block_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, blocked = base.delegated(container)
    move(container, stored, PlanStepStatus.CANCELLED)
    unrelated = container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Summarize something else", target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY, capability="summarize_email",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )

    invocation = container.worker_runtime_invocation.invoke(max_actions=5)

    assert [r.admission_rejected for r in invocation.results[:2]] == [True, False]
    assert invocation.processed_count == 1
    assert invocation.queue_empty is True
    assert worker.executed == [unrelated.action_id]
    state = container.action_lifecycle_repository.get(blocked)
    assert state is not None and state.status is ActionStatus.REJECTED


def test_rejected_action_is_never_retried_or_requeued(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, _ = base.delegated(container)
    [queued] = container.action_queue.list()
    move(container, stored, PlanStepStatus.CANCELLED)
    container.worker_runtime.process_next()

    container.action_queue.enqueue(queued)
    again = container.worker_runtime.process_next()

    assert again.processed is False
    assert again.admission_rejected is False
    assert "not approved for execution" in (again.execution_reason or "")
    assert worker.executed == []
    assert container.action_queue.list() == []


def test_approved_actions_may_be_rejected_before_execution() -> None:
    manager = ActionLifecycleManager()
    approved = ActionLifecycleState(status=ActionStatus.APPROVED)

    state = manager.transition(approved, ActionStatus.REJECTED, reason="not admitted")

    assert state.status is ActionStatus.REJECTED
    assert state.reason == "not admitted"


# --- Races --------------------------------------------------------------------------


def test_cancellation_after_admission_is_caught_by_the_durable_start_claim(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    admit = GoalPlanAdmissionGate.admit

    def admit_then_cancel(self: GoalPlanAdmissionGate, action: Action) -> WorkerAdmission:
        decision = admit(self, action)
        assert decision.admitted is True
        move(container, stored, PlanStepStatus.CANCELLED)
        return decision

    monkeypatch.setattr(GoalPlanAdmissionGate, "admit", admit_then_cancel)

    with pytest.raises(GoalPlanExecutionEvidenceError):
        container.worker_runtime.process_next()

    assert worker.executed == []
    assert base.attempts(container, stored) == ()
    assert container.action_queue.list() == []
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.EXECUTING


def test_cancellation_while_the_worker_runs_is_residual_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    executed: list[UUID] = []

    def cancel_mid_execution(action: Action, **kwargs: object) -> WorkerExecutionResult:
        executed.append(action.id)
        move(container, stored, PlanStepStatus.CANCELLED)
        return WorkerExecutionResult(action=action, status=WorkerExecutionStatus.SUCCEEDED)

    monkeypatch.setattr(container.gmail_worker_executor, "execute", cancel_mid_execution)

    container.worker_runtime.process_next()

    assert executed == [action_id]
    report = base.reconcile(container, stored)
    assert report.category is StepExecutionCategory.INCONSISTENT
    assert report.requires_operator is True


@pytest.mark.parametrize("attempt", range(10))
def test_concurrent_cancellation_never_runs_unrecorded_work(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    attempt: int,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    barrier = threading.Barrier(2)
    outcome: dict[str, Any] = {}

    def run() -> None:
        barrier.wait()
        try:
            outcome["result"] = container.worker_runtime.process_next()
        except GoalPlanExecutionEvidenceError as error:
            outcome["result"] = error

    def cancel() -> None:
        barrier.wait()
        move(container, stored, PlanStepStatus.CANCELLED)

    threads = [threading.Thread(target=run), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    recorded = base.attempts(container, stored)
    if worker.executed:
        assert worker.executed == [action_id]
        assert [a.action_id for a in recorded] == [action_id]
    else:
        assert recorded == ()
    assert base.current(container, stored).plan.steps[0].status is PlanStepStatus.CANCELLED
    assert container.action_queue.list() == []
    assert container.worker_runtime.process_next().processed is False


# --- Once-only start claim under duplicate queue entries ------------------------------


def contend(
    container: ApplicationContainer,
    monkeypatch: pytest.MonkeyPatch,
    consumers: int = 2,
) -> list[Any]:
    """Run ``consumers`` workers that all pass admission before any durable start."""
    admit = GoalPlanAdmissionGate.admit
    admitted = threading.Barrier(consumers)

    def admit_together(self: GoalPlanAdmissionGate, action: Action) -> WorkerAdmission:
        decision = admit(self, action)
        admitted.wait(timeout=10)
        return decision

    monkeypatch.setattr(GoalPlanAdmissionGate, "admit", admit_together)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def consume() -> None:
        try:
            outcome: Any = container.worker_runtime.process_next()
        except GoalPlanExecutionEvidenceError as error:
            outcome = error
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=consume) for _ in range(consumers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    monkeypatch.setattr(GoalPlanAdmissionGate, "admit", admit)
    return outcomes


@pytest.mark.parametrize("attempt", range(10))
def test_duplicate_queue_entries_start_the_worker_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    attempt: int,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    stored, action_id = base.delegated(container)
    [queued] = container.action_queue.list()
    container.action_queue.enqueue(queued)

    outcomes = contend(container, monkeypatch)

    assert worker.executed == [action_id]
    assert len(base.attempts(container, stored)) == 1
    executed = [o for o in outcomes if not isinstance(o, Exception) and o.processed]
    refused = [o for o in outcomes if isinstance(o, GoalPlanExecutionEvidenceError)]
    assert (len(executed), len(refused)) == (1, 1)
    assert container.action_queue.list() == []


@pytest.mark.parametrize("backend_name", ["memory", "sqlite"])
def test_second_start_claim_for_one_action_is_refused_atomically(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend_name: str,
) -> None:
    container, _ = base.boot(monkeypatch, db_path, backend_name)
    stored, action_id = base.delegated(container)
    repo = container.goal_plan_repository
    barrier = threading.Barrier(8)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        try:
            outcome: Any = base.start(repo, action_id)
        except Exception as error:
            outcome = error
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    claimed = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(claimed) == 1
    assert all("already has an execution attempt" in str(o) for o in outcomes if o not in claimed)
    assert base.attempts(container, stored) == (claimed[0],)


def test_contention_after_reopen_still_starts_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    first, _ = base.boot(monkeypatch, db_path)
    stored, action_id = base.delegated(first)
    [queued] = first.action_queue.list()
    first.worker_runtime.process_next()

    reopened, worker = base.boot(monkeypatch, db_path)
    for _ in range(2):
        reopened.action_queue.enqueue(queued)
    reopened.action_lifecycle_repository.set(
        action_id, ActionLifecycleState(status=ActionStatus.APPROVED)
    )
    stale = contend(reopened, monkeypatch)

    assert worker.executed == []
    assert all(o.admission_rejected for o in stale)
    assert len(base.attempts(reopened, stored)) == 1

    fresh_stored = base.planned(reopened, base.make_step(objective="Fresh"))
    fresh = reopened.goal_plan_delegation.delegate(
        GoalPlanStepDelegationRequest(
            goal_id=fresh_stored.plan.goal_id, version=1, step_id=fresh_stored.plan.steps[0].id,
            expected_revision=fresh_stored.revision, requested_by="owner",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    [fresh_action] = reopened.action_queue.list()
    reopened.action_queue.enqueue(fresh_action)

    contend(reopened, monkeypatch)

    assert worker.executed == [fresh.action_id]
    again, _ = base.boot(monkeypatch, db_path)
    assert len(base.attempts(again, base.current(again, fresh_stored))) == 1


def test_duplicate_unrelated_entries_keep_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = base.boot(monkeypatch, db_path, backend)
    unrelated = container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Summarize something else", target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY, capability="summarize_email",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    [queued] = container.action_queue.list()
    container.action_queue.enqueue(queued)

    outcomes = contend(container, monkeypatch)

    assert all(not isinstance(o, Exception) and o.processed for o in outcomes)
    assert worker.executed == [unrelated.action_id, unrelated.action_id]
