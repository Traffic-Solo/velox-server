"""Offline coverage for Goal/Plan worker execution evidence and reconciliation."""

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import TaskDelegationRequest
from apps.server.src.core.goal_plan_delegation import (
    GoalPlanDelegationOutcome,
    GoalPlanStepDelegationRequest,
)
from apps.server.src.core.goal_plan_execution import (
    GoalPlanExecutionEvidenceError,
    GoalPlanRetryPolicy,
    StepExecutionCategory,
)
from apps.server.src.core.goal_plan_repository import (
    GoalPlanConflictError,
    GoalPlanCorruptedError,
    GoalPlanNotFoundError,
    GoalPlanRepository,
    GoalPlanRepositoryError,
    SqliteGoalPlanRepository,
    StepExecution,
    StepExecutionStatus,
    StoredPlan,
)
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep, PlanStepStatus
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "summarize_email"}
GATED = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "gmail.send"}


class FakeWorker:
    """Fake provider: returns a scripted result and counts invocations."""

    def __init__(
        self,
        status: WorkerExecutionStatus = WorkerExecutionStatus.SUCCEEDED,
        *,
        metadata: dict[str, Any] | None = None,
        reason: str | None = None,
        failure: WorkerExecutionFailure | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self.metadata = {"external_execution_performed": True} if metadata is None else metadata
        self.reason = reason
        self.failure = failure
        self.raises = raises
        self.executed: list[UUID] = []
        self._lock = threading.Lock()

    def execute(self, action: Action, **kwargs: object) -> WorkerExecutionResult:
        with self._lock:
            self.executed.append(action.id)
        if self.raises is not None:
            raise self.raises
        return WorkerExecutionResult(
            action=action,
            status=self.status,
            reason=self.reason,
            metadata=dict(self.metadata),
            failure=self.failure,
        )


TRANSIENT = WorkerExecutionFailure(
    category=WorkerExecutionFailureCategory.TRANSIENT,
    message="provider timed out",
)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "goals.sqlite3"


def boot(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str = "sqlite",
    worker: FakeWorker | None = None,
) -> tuple[ApplicationContainer, FakeWorker]:
    if backend == "sqlite":
        monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(db_path))
    else:
        monkeypatch.delenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", raising=False)
    container = ApplicationContainer()
    fake = worker or FakeWorker()
    monkeypatch.setattr(container.gmail_worker_executor, "execute", fake.execute)
    return container, fake


def make_step(**overrides: Any) -> PlanStep:
    values: dict[str, Any] = {
        "objective": "Summarize the latest message",
        "target": "gmail-message-1",
        **SAFE,
    }
    values.update(overrides)
    return PlanStep(**values)


def planned(container: ApplicationContainer, *steps: PlanStep) -> StoredPlan:
    repo = container.goal_plan_repository
    goal = repo.create_goal(Goal(objective="Keep the inbox handled", success_criteria=("Done",)))
    stored = repo.create_plan(Plan(goal_id=goal.goal.id, steps=steps or (make_step(),)))
    repo.transition_goal(goal.goal.id, GoalStatus.ACTIVE, expected_revision=goal.revision)
    return stored


def current(container: ApplicationContainer, stored: StoredPlan) -> StoredPlan:
    found = container.goal_plan_repository.get_plan(stored.plan.goal_id, stored.plan.version)
    assert found is not None
    return found


def delegated(
    container: ApplicationContainer,
    *steps: PlanStep,
) -> tuple[StoredPlan, UUID]:
    stored = planned(container, *steps)
    result = container.goal_plan_delegation.delegate(
        GoalPlanStepDelegationRequest(
            goal_id=stored.plan.goal_id,
            version=1,
            step_id=stored.plan.steps[0].id,
            expected_revision=stored.revision,
            requested_by="owner",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    assert result.outcome is GoalPlanDelegationOutcome.QUEUED
    assert result.action_id is not None
    return current(container, stored), result.action_id


def attempts(container: ApplicationContainer, stored: StoredPlan) -> tuple[StepExecution, ...]:
    return container.goal_plan_repository.list_step_executions(
        stored.plan.goal_id, 1, stored.plan.steps[0].id
    )


def reconcile(container: ApplicationContainer, stored: StoredPlan) -> Any:
    return container.goal_plan_execution_reconciliation.reconcile_step(
        stored.plan.goal_id, 1, stored.plan.steps[0].id
    )


def goal_of(container: ApplicationContainer, stored: StoredPlan) -> Any:
    goal = container.goal_plan_repository.get_goal(stored.plan.goal_id)
    assert goal is not None
    return goal


# --- A. Evidence recording ------------------------------------------------------


def test_successful_attempt_is_recorded_without_changing_the_step(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = boot(monkeypatch, db_path, backend)
    stored, action_id = delegated(container)
    goal_before = goal_of(container, stored)

    result = container.worker_runtime.process_next()

    assert result.processed is True
    assert worker.executed == [action_id]
    [observation] = container.worker_execution_observer.list()
    [attempt] = attempts(container, stored)
    step = stored.plan.steps[0]
    assert attempt == StepExecution(
        execution_id=observation.execution_id,
        action_id=action_id,
        goal_id=stored.plan.goal_id,
        version=1,
        step_id=step.id,
        executor_role=step.executor_role,
        capability=step.capability,
        started_at=observation.started_at,
        status=StepExecutionStatus.SUCCEEDED,
        finished_at=observation.finished_at,
        external_execution_reported=True,
        failure_category=None,
        reason=None,
    )
    assert current(container, stored) == stored
    assert goal_of(container, stored) == goal_before


@pytest.mark.parametrize(
    ("worker", "status", "category", "reason", "reported"),
    [
        (
            FakeWorker(
                WorkerExecutionStatus.FAILED,
                reason="mailbox rejected the message",
                failure=WorkerExecutionFailure(WorkerExecutionFailureCategory.PERMANENT),
                metadata={"external_execution_performed": False},
            ),
            StepExecutionStatus.FAILED,
            "permanent",
            "mailbox rejected the message",
            False,
        ),
        (
            FakeWorker(WorkerExecutionStatus.SKIPPED, reason="nothing to do", metadata={}),
            StepExecutionStatus.SKIPPED,
            None,
            "nothing to do",
            False,
        ),
        (
            FakeWorker(raises=RuntimeError("boom")),
            StepExecutionStatus.FAILED,
            "internal",
            "executor raised exception: boom",
            False,
        ),
        (
            FakeWorker(metadata={"external_execution_performed": "yes"}),
            StepExecutionStatus.SUCCEEDED,
            None,
            None,
            False,
        ),
    ],
    ids=["failed", "skipped", "raised", "non_boolean_report"],
)
def test_completion_facts_are_preserved_as_reported(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    worker: FakeWorker,
    status: StepExecutionStatus,
    category: str | None,
    reason: str | None,
    reported: bool,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend, worker)
    stored, _ = delegated(container)

    container.worker_runtime.process_next()

    [attempt] = attempts(container, stored)
    assert attempt.status is status
    assert attempt.failure_category == category
    assert attempt.reason == reason
    assert attempt.external_execution_reported is reported
    assert current(container, stored).plan.steps[0].status is PlanStepStatus.IN_PROGRESS


def test_no_op_executor_stays_skipped(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    monkeypatch.setattr(
        container.gmail_worker_executor, "execute", NoOpWorkerExecutor().execute
    )
    stored, action_id = delegated(container)

    container.worker_runtime.process_next()

    [attempt] = attempts(container, stored)
    assert attempt.status is StepExecutionStatus.SKIPPED
    assert attempt.external_execution_reported is False
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.SKIPPED
    assert reconcile(container, stored).category is StepExecutionCategory.SKIPPED


def test_long_reasons_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    worker = FakeWorker(WorkerExecutionStatus.SKIPPED, reason="x" * 5000, metadata={})
    container, _ = boot(monkeypatch, db_path, backend, worker)
    stored, _ = delegated(container)

    container.worker_runtime.process_next()

    [attempt] = attempts(container, stored)
    assert attempt.reason == "x" * 1000


# --- B. Identity, ownership and integrity ---------------------------------------


def test_unrelated_actions_produce_no_plan_evidence(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = boot(monkeypatch, db_path, backend)
    stored = planned(container, make_step())
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
    assert attempts(container, stored) == ()
    assert container.goal_plan_repository.get_step_delegation_for_action(
        unrelated.action_id
    ) is None


def start(
    repo: GoalPlanRepository,
    action_id: UUID,
    *,
    execution_id: UUID | None = None,
    started_at: datetime | None = None,
    **overrides: Any,
) -> StepExecution:
    values: dict[str, Any] = {
        "execution_id": execution_id or uuid4(),
        "action_id": action_id,
        "executor_role": ExecutorRole.CONTENT_SUMMARY,
        "capability": "summarize_email",
        "started_at": started_at or datetime.now(UTC),
    }
    values.update(overrides)
    return repo.record_execution_start(**values)


def finish(
    repo: GoalPlanRepository,
    execution: StepExecution,
    status: StepExecutionStatus = StepExecutionStatus.SUCCEEDED,
    **overrides: Any,
) -> StepExecution:
    values: dict[str, Any] = {
        "execution_id": execution.execution_id,
        "action_id": execution.action_id,
        "status": status,
        "finished_at": execution.started_at + timedelta(seconds=1),
        "external_execution_reported": True,
        "failure_category": None,
        "reason": None,
    }
    values.update(overrides)
    return repo.record_execution_finish(**values)


def test_evidence_requires_a_released_delegation_of_the_exact_route(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    repo = container.goal_plan_repository
    stored, action_id = delegated(container)
    held_plan = planned(container, make_step(**GATED))
    held = container.goal_plan_delegation.delegate(
        GoalPlanStepDelegationRequest(
            goal_id=held_plan.plan.goal_id, version=1, step_id=held_plan.plan.steps[0].id,
            expected_revision=held_plan.revision, requested_by="owner",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )
    assert held.action_id is not None

    with pytest.raises(GoalPlanNotFoundError):
        start(repo, uuid4())
    with pytest.raises(GoalPlanConflictError, match="not released for execution"):
        start(repo, held.action_id, capability="gmail.send")
    with pytest.raises(GoalPlanConflictError, match="differs from its plan step"):
        start(repo, action_id, capability="gmail.send")
    with pytest.raises(GoalPlanConflictError, match="differs from its plan step"):
        start(repo, action_id, executor_role=ExecutorRole.CONTENT_REVIEW)
    with pytest.raises(GoalPlanNotFoundError, match="never started"):
        finish(repo, StepExecution(
            execution_id=uuid4(), action_id=action_id, goal_id=stored.plan.goal_id,
            version=1, step_id=stored.plan.steps[0].id, executor_role=ExecutorRole.CONTENT_SUMMARY,
            capability="summarize_email", started_at=datetime.now(UTC),
        ))
    assert attempts(container, stored) == ()


def test_forged_queued_action_cannot_attach_evidence_or_run(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = boot(monkeypatch, db_path, backend)
    stored, _ = delegated(container)
    [queued] = container.action_queue.list()
    container.action_queue.clear()
    forged = queued.model_copy(
        update={
            "type": "gmail.archive",
            "payload": {**queued.payload, "capability": "gmail.archive"},
        },
    )
    container.action_queue.enqueue(forged)

    result = container.worker_runtime.process_next()

    assert result.admission_rejected is True
    assert result.execution_reason == "pre-execution admission rejected: action_mismatch"
    assert worker.executed == []
    assert attempts(container, stored) == ()
    assert container.action_queue.list() == []


def test_duplicate_and_conflicting_observations(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    repo = container.goal_plan_repository
    stored, action_id = delegated(container)
    started = start(repo, action_id)

    assert start(repo, action_id, execution_id=started.execution_id,
                 started_at=started.started_at) == started
    with pytest.raises(GoalPlanConflictError, match="different identity"):
        start(repo, action_id, execution_id=started.execution_id,
              started_at=started.started_at + timedelta(seconds=5))

    done = finish(repo, started)
    assert finish(repo, started) == done
    with pytest.raises(GoalPlanConflictError, match="different outcome"):
        finish(repo, started, StepExecutionStatus.FAILED, external_execution_reported=False)
    with pytest.raises(GoalPlanConflictError, match="different outcome"):
        finish(repo, started, reason="changed story")
    assert start(repo, action_id, execution_id=started.execution_id,
                 started_at=started.started_at) == done
    with pytest.raises(GoalPlanConflictError, match="different action"):
        finish(repo, done.__class__(**{**_fields(done), "action_id": uuid4()}))
    assert attempts(container, stored) == (done,)


def _fields(execution: StepExecution) -> dict[str, Any]:
    return {name: getattr(execution, name) for name in StepExecution.__slots__}


def test_distinct_attempts_are_all_preserved(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    repo = container.goal_plan_repository
    stored, action_id = delegated(container)
    first = finish(repo, start(repo, action_id), StepExecutionStatus.FAILED,
                   failure_category="transient", external_execution_reported=False)
    second = finish(repo, start(repo, action_id, authorized_retry=True))

    assert attempts(container, stored) == (first, second)
    report = reconcile(container, stored)
    assert report.category is StepExecutionCategory.RETRY_INTERMEDIATE
    assert report.attempts == (first, second)
    assert report.requires_operator is True


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_writes_to_one_execution_never_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    attempt: int,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    repo = container.goal_plan_repository
    _, action_id = delegated(container)
    started = start(repo, action_id)
    barrier = threading.Barrier(8)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def write(index: int) -> None:
        barrier.wait()
        status = StepExecutionStatus.SUCCEEDED if index % 2 else StepExecutionStatus.FAILED
        try:
            outcome: Any = finish(repo, started, status)
        except GoalPlanConflictError as error:
            outcome = error
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    recorded = [o for o in outcomes if isinstance(o, StepExecution)]
    conflicts = [o for o in outcomes if isinstance(o, GoalPlanConflictError)]
    assert len(recorded) + len(conflicts) == 8
    assert len({r.status for r in recorded}) == 1
    assert len(recorded) == 4 and len(conflicts) == 4
    [stored_attempt] = repo.list_step_executions(
        recorded[0].goal_id, 1, recorded[0].step_id
    )
    assert stored_attempt == recorded[0]


def test_completed_evidence_is_immutable_in_storage(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    container, _ = boot(monkeypatch, db_path)
    container.worker_runtime.process_next()
    delegated(container)
    container.worker_runtime.process_next()

    with sqlite3.connect(db_path) as connection:
        for statement in (
            "UPDATE goal_plan_step_executions SET status = 'failed'",
            "UPDATE goal_plan_step_executions SET reason = 'rewritten'",
            "UPDATE goal_plan_step_executions SET action_id = 'x'",
            "UPDATE goal_plan_step_executions SET started_at = '2020-01-01T00:00:00+00:00'",
            "DELETE FROM goal_plan_step_executions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE goal_plan_step_executions SET status = 'done'",
        "UPDATE goal_plan_step_executions SET started_at = '2026-01-01T00:00:00'",
        "UPDATE goal_plan_step_executions SET external_execution_reported = 7",
    ],
)
def test_corrupted_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    statement: str,
) -> None:
    container, _ = boot(monkeypatch, db_path)
    stored, action_id = delegated(container)
    start(container.goal_plan_repository, action_id)
    with sqlite3.connect(db_path) as connection:
        if "started_at" in statement:
            connection.execute("DROP TRIGGER goal_plan_step_executions_identity_immutable")
        connection.execute(statement)

    with pytest.raises(GoalPlanCorruptedError, match="execution evidence"):
        attempts(container, stored)
    with pytest.raises(GoalPlanCorruptedError):
        reconcile(container, stored)


def test_software_engineering_evidence_stays_with_its_own_owner(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    se_action = Action(
        type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
        payload={"capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY, "objective": "x"},
    )
    observer = container.worker_execution_observer

    observation = observer.start(
        action=se_action,
        requested_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        executor_registered=False,
        requested_capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    )
    observer.finish(observation, WorkerExecutionStatus.SKIPPED, {})

    se_state = container.software_engineering_run_repository.get(se_action.id)
    assert se_state is not None and se_state.execution_status == "skipped"
    assert container.goal_plan_repository.get_step_delegation_for_action(se_action.id) is None
    stored, action_id = delegated(container)
    container.worker_runtime.process_next()
    assert container.software_engineering_run_repository.get(action_id) is None
    assert len(attempts(container, stored)) == 1


# --- C. Reconciliation ------------------------------------------------------------


def test_reconciliation_never_changes_step_or_goal(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = boot(monkeypatch, db_path, backend)
    first, second = make_step(), make_step(objective="Second", target="gmail-message-2")
    stored, _ = delegated(container, first, second)
    service = container.goal_plan_execution_reconciliation

    before = service.reconcile_plan(stored.plan.goal_id, 1)
    assert [r.category for r in before] == [
        StepExecutionCategory.NO_EVIDENCE,
        StepExecutionCategory.NO_EVIDENCE,
    ]
    assert [r.requires_operator for r in before] == [True, False]

    container.worker_runtime.process_next()
    goal_before = goal_of(container, stored)
    plan_before = current(container, stored)
    reports = [service.reconcile_step(stored.plan.goal_id, 1, first.id) for _ in range(3)]

    assert len(set(reports)) == 1
    assert reports[0].category is StepExecutionCategory.WORKER_REPORTED_SUCCESS
    assert reports[0].requires_operator is True
    assert "not verified" in reports[0].reason
    assert current(container, stored) == plan_before
    assert goal_of(container, stored) == goal_before
    assert reports[0].delegation is not None
    assert worker.executed == [reports[0].delegation.action_id]
    assert container.action_queue.list() == []


CONSISTENT = StepExecutionCategory.EXPLICITLY_FINALIZED_CONSISTENT
INCONSISTENT = StepExecutionCategory.INCONSISTENT
WORKER_OK = WorkerExecutionStatus.SUCCEEDED
WORKER_FAILED = WorkerExecutionStatus.FAILED
WORKER_SKIPPED = WorkerExecutionStatus.SKIPPED
FINALIZATIONS = [
    (WORKER_OK, PlanStepStatus.SUCCEEDED, CONSISTENT),
    (WORKER_OK, PlanStepStatus.FAILED, INCONSISTENT),
    (WORKER_OK, PlanStepStatus.CANCELLED, INCONSISTENT),
    (WORKER_FAILED, PlanStepStatus.FAILED, CONSISTENT),
    (WORKER_FAILED, PlanStepStatus.CANCELLED, CONSISTENT),
    (WORKER_FAILED, PlanStepStatus.SUCCEEDED, INCONSISTENT),
    (WORKER_SKIPPED, PlanStepStatus.CANCELLED, CONSISTENT),
    (WORKER_SKIPPED, PlanStepStatus.FAILED, CONSISTENT),
    (WORKER_SKIPPED, PlanStepStatus.SUCCEEDED, INCONSISTENT),
]


@pytest.mark.parametrize(("worker_status", "final", "category"), FINALIZATIONS)
def test_explicit_finalization_is_reflected_not_performed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    worker_status: WorkerExecutionStatus,
    final: PlanStepStatus,
    category: StepExecutionCategory,
) -> None:
    worker = FakeWorker(worker_status, metadata={})
    container, _ = boot(monkeypatch, db_path, backend, worker)
    stored, _ = delegated(container)
    container.worker_runtime.process_next()
    assert current(container, stored).plan.steps[0].status is PlanStepStatus.IN_PROGRESS

    container.goal_plan_repository.transition_step(
        stored.plan.goal_id, 1, stored.plan.steps[0].id, final,
        expected_revision=current(container, stored).revision,
    )
    report = reconcile(container, stored)

    assert report.category is category
    assert report.step_status is final
    assert report.requires_operator is (category is StepExecutionCategory.INCONSISTENT)
    if category is StepExecutionCategory.EXPLICITLY_FINALIZED_CONSISTENT:
        assert "no independent objective verification" in report.reason
    assert goal_of(container, stored).goal.status is GoalStatus.ACTIVE


def test_cancelled_step_blocks_its_queued_action(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    """Slice 6 closes the Slice 5 gap where cancelled queued work still executed."""
    container, worker = boot(monkeypatch, db_path, backend)
    stored, _ = delegated(container)
    container.goal_plan_repository.transition_step(
        stored.plan.goal_id, 1, stored.plan.steps[0].id, PlanStepStatus.CANCELLED,
        expected_revision=stored.revision,
    )

    result = container.worker_runtime.process_next()

    assert result.admission_rejected is True
    assert worker.executed == []
    assert attempts(container, stored) == ()
    assert current(container, stored).plan.steps[0].status is PlanStepStatus.CANCELLED


def test_started_without_finish_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    stored, action_id = delegated(container)
    start(container.goal_plan_repository, action_id)

    report = reconcile(container, stored)

    assert report.category is StepExecutionCategory.STARTED_UNCERTAIN
    assert report.requires_operator is True
    assert len(report.attempts) == 1


# --- D. Retry safety ----------------------------------------------------------------


@pytest.mark.parametrize("reported", [False, True])
def test_goal_plan_transient_failure_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    reported: bool,
) -> None:
    worker = FakeWorker(
        WorkerExecutionStatus.FAILED,
        reason="provider timed out",
        failure=TRANSIENT,
        metadata={"external_execution_performed": reported},
    )
    container, _ = boot(monkeypatch, db_path, backend, worker)
    stored, action_id = delegated(container)

    first = container.worker_runtime.process_next()
    later = [container.worker_runtime.process_next() for _ in range(3)]

    assert first.processed is True
    assert all(result.processed is False for result in later)
    assert worker.executed == [action_id]
    assert container.action_queue.list() == []
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.FAILED
    [attempt] = attempts(container, stored)
    assert attempt.failure_category == "transient"
    assert reconcile(container, stored).category is StepExecutionCategory.WORKER_REPORTED_FAILURE


def test_unrelated_transient_failures_keep_retrying(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    worker = FakeWorker(WorkerExecutionStatus.FAILED, failure=TRANSIENT, metadata={})
    container, _ = boot(monkeypatch, db_path, backend, worker)
    unrelated = container.task_delegator.delegate(
        TaskDelegationRequest(
            objective="Summarize something else",
            target="gmail-message-2",
            executor_role=ExecutorRole.CONTENT_SUMMARY,
            capability="summarize_email",
            account_context=GMAIL_ACCOUNT_CONTEXT,
        ),
    )

    while container.worker_runtime.process_next().processed:
        pass

    assert worker.executed == [unrelated.action_id] * 4


def test_retry_policy_fails_closed_when_ownership_is_unreadable() -> None:
    class Broken:
        def get_step_delegation_for_action(self, action_id: UUID) -> None:
            raise GoalPlanRepositoryError("down")

    policy = GoalPlanRetryPolicy(Broken())  # type: ignore[arg-type]

    assert policy.allows_automatic_retry(Action(type="x", target="y")) is False


def install_failure(path: Path, event: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER test_fail_evidence BEFORE {event} ON goal_plan_step_executions
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """
        )


def test_start_write_failure_blocks_the_worker_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    container, worker = boot(monkeypatch, db_path)
    stored, action_id = delegated(container)
    install_failure(db_path, "INSERT")

    with pytest.raises(GoalPlanExecutionEvidenceError):
        container.worker_runtime.process_next()

    assert worker.executed == []
    assert container.action_queue.list() == []
    state = container.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.EXECUTING
    assert attempts(container, stored) == ()
    assert container.worker_runtime.process_next().processed is False
    assert reconcile(container, stored).category is StepExecutionCategory.NO_EVIDENCE


def test_finish_write_failure_reports_no_success_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    worker = FakeWorker(WorkerExecutionStatus.FAILED, failure=TRANSIENT, metadata={})
    container, _ = boot(monkeypatch, db_path, worker=worker)
    stored, action_id = delegated(container)
    install_failure(db_path, "UPDATE")

    with pytest.raises(GoalPlanExecutionEvidenceError):
        container.worker_runtime.process_next()

    assert worker.executed == [action_id]
    assert container.action_queue.list() == []
    assert container.worker_runtime.process_next().processed is False
    [attempt] = attempts(container, stored)
    assert attempt.status is StepExecutionStatus.STARTED
    assert reconcile(container, stored).category is StepExecutionCategory.STARTED_UNCERTAIN


# --- E. Crash, restart and migration ----------------------------------------------


def test_evidence_survives_reopen(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    container, _ = boot(monkeypatch, db_path)
    stored, _ = delegated(container)
    container.worker_runtime.process_next()
    recorded = attempts(container, stored)

    reopened, worker = boot(monkeypatch, db_path)

    assert attempts(reopened, stored) == recorded
    assert reconcile(reopened, stored).category is StepExecutionCategory.WORKER_REPORTED_SUCCESS
    assert reopened.worker_runtime.process_next().processed is False
    assert worker.executed == []


CHILD = """
import os
import sys
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.goal_plan_delegation import GoalPlanStepDelegationRequest
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.workers.executor import WorkerExecutionResult, WorkerExecutionStatus

mode = sys.argv[1]
container = ApplicationContainer()
repo = container.goal_plan_repository
goal = repo.create_goal(Goal(objective="o", success_criteria=("d",)))
step = PlanStep(objective="Summarize", target="m", executor_role=ExecutorRole.CONTENT_SUMMARY,
                capability="summarize_email")
plan = repo.create_plan(Plan(goal_id=goal.goal.id, steps=(step,)))
repo.transition_goal(goal.goal.id, GoalStatus.ACTIVE, expected_revision=1)
container.goal_plan_delegation.delegate(GoalPlanStepDelegationRequest(
    goal_id=goal.goal.id, version=1, step_id=step.id, expected_revision=plan.revision,
    requested_by="owner", account_context=GMAIL_ACCOUNT_CONTEXT,
))
print(goal.goal.id, step.id, flush=True)

def execute(action, **kwargs):
    if mode == "crash_during_execution":
        os._exit(31)
    return WorkerExecutionResult(action=action, status=WorkerExecutionStatus.SUCCEEDED,
                                 metadata={"external_execution_performed": True})

container.gmail_worker_executor.execute = execute
if mode == "crash_before_execution":
    os._exit(32)
container.worker_runtime.process_next()
"""


@pytest.mark.parametrize(
    ("mode", "returncode", "category"),
    [
        ("crash_before_execution", 32, StepExecutionCategory.NO_EVIDENCE),
        ("crash_during_execution", 31, StepExecutionCategory.STARTED_UNCERTAIN),
        ("completed", 0, StepExecutionCategory.WORKER_REPORTED_SUCCESS),
    ],
)
def test_process_death_is_reconciled_and_never_replayed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    tmp_path: Path,
    mode: str,
    returncode: int,
    category: StepExecutionCategory,
) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VELOX_")}
    env.update({"PYTHONPATH": str(REPO_ROOT), "VELOX_GOAL_PLAN_STATE_DATABASE_PATH": str(db_path)})

    child = subprocess.run(
        [sys.executable, "-c", CHILD, mode],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )

    assert child.returncode == returncode, child.stderr
    goal_id, step_id = (UUID(value) for value in child.stdout.split())
    parent, worker = boot(monkeypatch, db_path)
    report = parent.goal_plan_execution_reconciliation.reconcile_step(goal_id, 1, step_id)
    assert report.category is category
    assert report.requires_operator is True
    assert report.step_status is PlanStepStatus.IN_PROGRESS
    assert parent.worker_runtime.process_next().processed is False
    assert worker.executed == []


def test_v2_database_upgrades_to_v3_with_data_intact(db_path: Path) -> None:
    repo = SqliteGoalPlanRepository(db_path)
    goal = repo.create_goal(Goal(objective="o", success_criteria=("d",)))
    stored = repo.create_plan(Plan(goal_id=goal.goal.id, steps=(make_step(),)))
    with sqlite3.connect(db_path) as connection:
        for trigger in (
            "goal_plan_step_executions_identity_immutable",
            "goal_plan_step_executions_completed_immutable",
            "goal_plan_step_executions_retained",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE goal_plan_step_executions")
        connection.execute("UPDATE velox_schema SET version = 2 WHERE name = 'goal_plans'")

    upgraded = SqliteGoalPlanRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT version FROM velox_schema WHERE name = 'goal_plans'"
        ).fetchone()
    assert version == (3,)
    assert upgraded.get_latest_plan(goal.goal.id) == stored
    assert upgraded.list_step_executions(goal.goal.id, 1, stored.plan.steps[0].id) == ()


def test_incomplete_v3_schema_is_rejected(db_path: Path) -> None:
    SqliteGoalPlanRepository(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER goal_plan_step_executions_completed_immutable")

    with pytest.raises(GoalPlanCorruptedError, match="incomplete"):
        SqliteGoalPlanRepository(db_path)


# --- Terminal steps never hide missing or contrary evidence ----------------------


def finalize(container: ApplicationContainer, stored: StoredPlan, *path: PlanStepStatus) -> None:
    for status in path:
        container.goal_plan_repository.transition_step(
            stored.plan.goal_id, 1, stored.plan.steps[0].id, status,
            expected_revision=current(container, stored).revision,
        )


def snapshot(container: ApplicationContainer, stored: StoredPlan, db_path: Path) -> Any:
    repo = container.goal_plan_repository
    state: list[Any] = [
        repo.get_goal(stored.plan.goal_id),
        repo.get_plan(stored.plan.goal_id, 1),
        repo.list_step_delegations(),
        [repo.list_step_executions(stored.plan.goal_id, 1, step.id) for step in stored.plan.steps],
        container.action_queue.list(),
        container.action_lifecycle_repository.list_states(),
    ]
    if db_path.exists():
        with sqlite3.connect(db_path) as connection:
            state.append(list(connection.iterdump()))
    return state


def test_success_finalized_without_any_evidence_needs_review(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    handed_off, _ = delegated(container)
    finalize(container, handed_off, PlanStepStatus.SUCCEEDED)
    never_delegated = planned(container, make_step())
    finalize(container, never_delegated, PlanStepStatus.IN_PROGRESS, PlanStepStatus.SUCCEEDED)

    for stored in (handed_off, never_delegated):
        report = reconcile(container, stored)
        assert report.category is StepExecutionCategory.NO_EVIDENCE
        assert report.requires_operator is True
        assert "without any execution evidence" in report.reason


@pytest.mark.parametrize("final", [PlanStepStatus.FAILED, PlanStepStatus.CANCELLED])
def test_handed_off_step_closed_before_any_attempt_needs_review(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    final: PlanStepStatus,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    stored, _ = delegated(container)
    finalize(container, stored, final)

    report = reconcile(container, stored)

    assert report.category is StepExecutionCategory.NO_EVIDENCE
    assert report.requires_operator is True
    assert "may still run" in report.reason


@pytest.mark.parametrize(
    "final",
    [PlanStepStatus.SUCCEEDED, PlanStepStatus.FAILED, PlanStepStatus.CANCELLED],
)
def test_unfinished_attempt_stays_uncertain_whatever_the_step_says(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    final: PlanStepStatus,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    stored, action_id = delegated(container)
    start(container.goal_plan_repository, action_id)
    finalize(container, stored, final)

    report = reconcile(container, stored)

    assert report.category is StepExecutionCategory.STARTED_UNCERTAIN
    assert report.requires_operator is True
    assert report.step_status is final


@pytest.mark.parametrize("unfinished", [True, False])
def test_multiple_attempts_on_a_terminal_step_need_review(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
    unfinished: bool,
) -> None:
    container, _ = boot(monkeypatch, db_path, backend)
    repo = container.goal_plan_repository
    stored, action_id = delegated(container)
    finish(repo, start(repo, action_id), StepExecutionStatus.FAILED,
           failure_category="transient", external_execution_reported=False)
    second = start(repo, action_id, authorized_retry=True)
    if not unfinished:
        finish(repo, second)
    finalize(container, stored, PlanStepStatus.SUCCEEDED)

    report = reconcile(container, stored)

    assert report.category is StepExecutionCategory.RETRY_INTERMEDIATE
    assert report.requires_operator is True
    assert len(report.attempts) == 2


def test_ordinary_pending_and_failed_work(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    worker = FakeWorker(
        WorkerExecutionStatus.FAILED,
        failure=WorkerExecutionFailure(WorkerExecutionFailureCategory.PERMANENT),
        metadata={},
    )
    container, _ = boot(monkeypatch, db_path, backend, worker)
    pending = planned(container, make_step())
    stored, _ = delegated(container)
    container.worker_runtime.process_next()

    idle = reconcile(container, pending)
    failed = reconcile(container, stored)

    assert (idle.category, idle.requires_operator) == (StepExecutionCategory.NO_EVIDENCE, False)
    assert (failed.category, failed.requires_operator) == (
        StepExecutionCategory.WORKER_REPORTED_FAILURE,
        True,
    )


def test_reconciliation_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    backend: str,
) -> None:
    container, worker = boot(monkeypatch, db_path, backend)
    first, second = make_step(), make_step(objective="Second", target="gmail-message-2")
    stored, action_id = delegated(container, first, second)
    container.worker_runtime.process_next()
    start(container.goal_plan_repository, action_id, authorized_retry=True)
    before = snapshot(container, stored, db_path)

    for _ in range(3):
        container.goal_plan_execution_reconciliation.reconcile_plan(stored.plan.goal_id, 1)

    assert snapshot(container, stored, db_path) == before
    assert worker.executed == [action_id]
