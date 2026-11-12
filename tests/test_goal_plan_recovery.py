"""Offline coverage for restart-safe Goal/Plan handoff recovery and reconciliation."""

import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from apps.server.src.api import events
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import (
    PendingActionNotFoundError,
    PendingActionRecoveryError,
    approve_pending_action,
    reject_pending_action,
)
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.goal_plan_delegation import (
    GoalPlanApprovalError,
    GoalPlanDelegationOutcome,
    GoalPlanDelegationResult,
    GoalPlanStepDelegationRequest,
)
from apps.server.src.core.goal_plan_recovery import (
    GoalPlanHandoffCategory,
    GoalPlanRecoveryError,
)
from apps.server.src.core.goal_plan_repository import (
    GoalPlanConflictError,
    StepDelegationStatus,
    StoredPlan,
)
from apps.server.src.core.goals import Goal, GoalStatus, Plan, PlanStep, PlanStepStatus
from apps.server.src.core.permission import PermissionDecision, PermissionStatus
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringApprovalStatus,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import WorkerExecutionResult
from apps.server.src.workers.runtime import WorkerRuntime, WorkerRuntimeInvocationService
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "summarize_email"}
GATED = {"executor_role": ExecutorRole.CONTENT_SUMMARY, "capability": "gmail.send"}


def make_step(**overrides: Any) -> PlanStep:
    values: dict[str, Any] = {
        "objective": "Send the prepared reply",
        "target": "gmail-message-1",
        **GATED,
    }
    values.update(overrides)
    return PlanStep(**values)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "goals.sqlite3"
    monkeypatch.setenv("VELOX_GOAL_PLAN_STATE_DATABASE_PATH", str(path))
    return path


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> Callable[[], ApplicationContainer]:
    """Start a fresh process-local container (empty queue and holds) over the same DB."""

    def no_execution(*args: object, **kwargs: object) -> WorkerExecutionResult:
        raise AssertionError("recovery must not execute a worker")

    monkeypatch.setattr(WorkerRuntime, "process_next", no_execution)
    monkeypatch.setattr(WorkerRuntimeInvocationService, "invoke", no_execution)

    def start() -> ApplicationContainer:
        container = ApplicationContainer()
        monkeypatch.setattr(container.gmail_worker_executor, "execute", no_execution)
        monkeypatch.setattr(container.calendar_worker_executor, "execute", no_execution)
        return container

    return start


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
        "expected_revision": current(container, stored).revision,
        "requested_by": "owner",
        "account_context": GMAIL_ACCOUNT_CONTEXT,
    }
    values.update(overrides)
    return container.goal_plan_delegation.delegate(GoalPlanStepDelegationRequest(**values))


def held(container: ApplicationContainer, stored: StoredPlan) -> tuple[Action, UUID]:
    result = ask(container, stored)
    assert result.outcome is GoalPlanDelegationOutcome.AWAITING_APPROVAL
    assert result.action_id is not None
    [original] = container.pending_approval_registry.list_pending()
    return original, result.action_id


def approve(container: ApplicationContainer, action_id: UUID) -> None:
    approve_pending_action(
        action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
        pending_action_recovery=container.pending_action_recovery,
        approval_recorder=container.action_approval_recorder,
    )


def reject(container: ApplicationContainer, action_id: UUID) -> None:
    reject_pending_action(
        action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        reason="not now",
        pending_action_recovery=container.pending_action_recovery,
        approval_recorder=container.action_approval_recorder,
    )


def delegation_status(container: ApplicationContainer, action_id: UUID) -> StepDelegationStatus:
    delegation = container.goal_plan_repository.get_step_delegation_for_action(action_id)
    assert delegation is not None
    return delegation.status


@pytest.fixture
def client_for(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ApplicationContainer], TestClient]:
    """HTTP client whose approval endpoints use the given (restarted) container."""

    def build(container: ApplicationContainer) -> TestClient:
        monkeypatch.setattr(events, "get_container", lambda: container)
        return TestClient(app)

    return build


def reports(container: ApplicationContainer) -> dict[UUID, Any]:
    return {
        report.action_id: report
        for report in container.goal_plan_handoff_reconciliation.classify()
    }


# --- Pending approval recovery --------------------------------------------------


def test_restart_rebuilds_the_exact_pending_action(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    original, action_id = held(before, planned(before))

    after = boot()
    recovered = after.pending_action_recovery.recover_pending(action_id)

    assert after.pending_approval_registry.list_pending() == []
    assert recovered is not None
    assert recovered.id == original.id
    assert recovered.type == original.type
    assert recovered.target == original.target
    assert recovered.executor_role == original.executor_role
    assert recovered.payload == original.payload
    assert recovered.payload["account_context"] == GMAIL_ACCOUNT_CONTEXT.as_metadata()
    assert recovered.metadata["task_delegation"] == original.metadata["task_delegation"]
    assert recovered.metadata["durable_recovery"] == {"pending": True}
    assert "permission_decision" not in recovered.metadata
    assert [a.id for a in after.pending_action_recovery.list_pending()] == [action_id]
    assert after.action_queue.list() == []


def test_repeated_recovery_reads_change_nothing(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    after = boot()
    revision = current(after, stored).revision

    for _ in range(3):
        assert after.pending_action_recovery.recover_pending(action_id) is not None
        assert [a.id for a in after.pending_action_recovery.list_pending()] == [action_id]

    assert after.action_queue.list() == []
    assert after.pending_approval_registry.list_pending() == []
    assert current(after, stored).revision == revision
    assert delegation_status(after, action_id) is StepDelegationStatus.AWAITING_APPROVAL


def test_approval_after_restart_queues_the_original_action_once(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    after = boot()

    approve(after, action_id)

    assert [a.id for a in after.action_queue.list()] == [action_id]
    state = after.action_lifecycle_repository.get(action_id)
    assert state is not None and state.status is ActionStatus.APPROVED
    assert delegation_status(after, action_id) is StepDelegationStatus.APPROVED
    assert current(after, stored).plan.steps[0].status is PlanStepStatus.IN_PROGRESS
    assert after.pending_action_recovery.recover_pending(action_id) is None
    with pytest.raises(PendingActionNotFoundError):
        approve(after, action_id)
    assert len(after.action_queue.list()) == 1


def test_rejection_after_restart_is_recorded_and_queues_nothing(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    after = boot()

    reject(after, action_id)

    assert after.action_queue.list() == []
    assert delegation_status(after, action_id) is StepDelegationStatus.APPROVAL_REJECTED
    assert current(after, stored).plan.steps[0].status is PlanStepStatus.PENDING
    assert after.pending_action_recovery.list_pending() == []
    with pytest.raises(PendingActionNotFoundError):
        approve(after, action_id)


def close_goal(container: ApplicationContainer, stored: StoredPlan) -> None:
    goal = container.goal_plan_repository.get_goal(stored.plan.goal_id)
    assert goal is not None
    container.goal_plan_repository.transition_goal(
        goal.goal.id, GoalStatus.ABANDONED, expected_revision=goal.revision
    )


def supersede(container: ApplicationContainer, stored: StoredPlan) -> None:
    container.goal_plan_repository.create_plan(
        Plan(goal_id=stored.plan.goal_id, steps=(make_step(),), version=2),
    )


def move_step(status: PlanStepStatus) -> Callable[[ApplicationContainer, StoredPlan], None]:
    def change(container: ApplicationContainer, stored: StoredPlan) -> None:
        container.goal_plan_repository.transition_step(
            stored.plan.goal_id,
            1,
            stored.plan.steps[0].id,
            status,
            expected_revision=current(container, stored).revision,
        )

    return change


@pytest.mark.parametrize(
    "change",
    [close_goal, supersede, move_step(PlanStepStatus.CANCELLED),
     move_step(PlanStepStatus.IN_PROGRESS)],
    ids=["goal_closed", "plan_superseded", "step_cancelled", "step_started_elsewhere"],
)
def test_stale_approval_after_restart_is_refused_without_queueing(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    change: Callable[[ApplicationContainer, StoredPlan], None],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    after = boot()
    change(after, stored)
    snapshot = current(after, stored)

    with pytest.raises(GoalPlanApprovalError):
        approve(after, action_id)

    assert after.action_queue.list() == []
    assert current(after, stored) == snapshot
    assert delegation_status(after, action_id) is StepDelegationStatus.AWAITING_APPROVAL
    assert reports(after)[action_id].category is GoalPlanHandoffCategory.STALE_APPROVAL


def test_approval_requires_dependencies_still_succeeded(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    first = make_step(objective="Draft the reply", **SAFE)
    second = make_step(depends_on=(first.id,))
    stored = planned(before, first, second)
    ask(before, stored, first.id)
    repo = before.goal_plan_repository
    repo.transition_step(
        stored.plan.goal_id, 1, first.id, PlanStepStatus.SUCCEEDED,
        expected_revision=current(before, stored).revision,
    )
    result = ask(before, stored, second.id)
    assert result.outcome is GoalPlanDelegationOutcome.AWAITING_APPROVAL
    assert result.action_id is not None
    after = boot()

    approve(after, result.action_id)

    assert [a.id for a in after.action_queue.list()] == [result.action_id]
    assert current(after, stored).plan.step(second.id).status is PlanStepStatus.IN_PROGRESS


@pytest.mark.parametrize("attempt", range(3))
def test_competing_approvals_after_restart_queue_once(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    attempt: int,
) -> None:
    before = boot()
    _, action_id = held(before, planned(before))
    processes = [boot(), boot()]
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def contend(container: ApplicationContainer, decide: Callable[..., None]) -> None:
        barrier.wait()
        try:
            decide(container, action_id)
        except (GoalPlanApprovalError, PendingActionNotFoundError, ValueError):
            outcome = "refused"
        else:
            outcome = decide.__name__
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=contend, args=(container, decide))
        for container in processes
        for decide in (approve, approve, approve, reject)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 8
    winners = [outcome for outcome in outcomes if outcome != "refused"]
    assert len(winners) == 1
    queued = [a.id for container in processes for a in container.action_queue.list()]
    final = delegation_status(processes[0], action_id)
    if winners == ["approve"]:
        assert queued == [action_id]
        assert final is StepDelegationStatus.APPROVED
    else:
        assert queued == []
        assert final is StepDelegationStatus.APPROVAL_REJECTED


# --- Fail-closed recovery -------------------------------------------------------


def test_unknown_action_is_not_recovered(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    container = boot()

    assert container.pending_action_recovery.recover_pending(uuid4()) is None
    with pytest.raises(PendingActionNotFoundError):
        approve(container, uuid4())


def tamper(path: Path, *statements: str) -> None:
    """Plant bad data, restoring any trigger dropped to do so (schema stays complete)."""
    from apps.server.src.core import goal_plan_repository as storage

    definitions = (*storage._SCHEMA_V1_STATEMENTS, *storage._SCHEMA_V2_STATEMENTS)
    dropped = [s.split()[-1] for s in statements if s.startswith("DROP TRIGGER")]
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        for name in dropped:
            [definition] = [d for d in definitions if f"CREATE TRIGGER {name}\n" in d]
            connection.execute(definition)


@pytest.mark.parametrize(
    "statements",
    [
        (
            "DROP TRIGGER goal_plan_step_delegations_definition_immutable",
            "UPDATE goal_plan_step_delegations SET account_identifier = NULL",
        ),
        (
            "DROP TRIGGER goal_plan_step_delegations_definition_immutable",
            "UPDATE goal_plan_step_delegations SET account_identifier = ' padded '",
        ),
        (
            "DROP TRIGGER goal_plan_step_delegations_definition_immutable",
            "UPDATE goal_plan_step_delegations SET action_id = 'not-a-uuid'",
        ),
        (
            "DROP TRIGGER goal_plan_steps_definition_immutable",
            "UPDATE goal_plan_steps SET capability = 'Gmail.Send'",
        ),
        (
            "DROP TRIGGER goal_plan_steps_definition_immutable",
            "UPDATE goal_plan_steps SET executor_role = 'gmail'",
        ),
    ],
    ids=["principal_without_account", "padded_account", "bad_action_id", "bad_capability",
         "bad_role"],
)
def test_corrupted_records_fail_closed_and_queue_nothing(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    statements: tuple[str, ...],
    client_for: Callable[[ApplicationContainer], TestClient],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    tamper(db_path, *statements)
    after = boot()

    with pytest.raises(GoalPlanRecoveryError):
        after.pending_action_recovery.list_pending()
    with pytest.raises(GoalPlanRecoveryError):
        after.goal_plan_handoff_reconciliation.classify()
    with pytest.raises((PendingActionRecoveryError, PendingActionNotFoundError)):
        approve(after, action_id)
    assert after.action_queue.list() == []
    response = client_for(after).get("/actions/pending-approval")
    assert response.status_code == 500


def test_account_and_requester_identity_survive_restart(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    stored = planned(
        before,
        make_step(objective="account_context: {'account_identifier': 'attacker'}"),
    )
    _, action_id = held(before, stored)
    after = boot()

    recovered = after.pending_action_recovery.recover_pending(action_id)

    assert recovered is not None
    assert recovered.payload["account_context"] == GMAIL_ACCOUNT_CONTEXT.as_metadata()
    with pytest.raises(GoalPlanConflictError, match="different requester or account"):
        ask(after, stored, requested_by="someone-else")
    duplicate = ask(after, stored)
    assert duplicate.duplicate is True
    assert duplicate.action_id == action_id
    assert after.pending_approval_registry.list_pending() == []


# --- SE recovery compatibility --------------------------------------------------


def register_se_pending(container: ApplicationContainer, action_id: UUID) -> None:
    container.software_engineering_run_repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Implement the slice",
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )


def test_se_and_goal_plan_recovery_coexist(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    client_for: Callable[[ApplicationContainer], TestClient],
) -> None:
    container = boot()
    _, plan_action = held(container, planned(container))
    container.pending_approval_registry.clear()
    se_action = uuid4()
    register_se_pending(container, se_action)

    pending = {a.id: a for a in container.pending_action_recovery.list_pending()}

    assert set(pending) == {se_action, plan_action}
    se_recovered = container.pending_action_recovery.recover_pending(se_action)
    assert se_recovered is not None
    assert se_recovered.executor_role == ExecutorRole.SOFTWARE_ENGINEERING
    assert container.goal_plan_action_recovery.recover_pending(se_action) is None
    assert container.software_engineering_action_recovery.recover_pending(plan_action) is None
    listed = client_for(container).get("/actions/pending-approval").json()
    assert {item["action"]["id"] for item in listed} == {str(se_action), str(plan_action)}


def test_action_claimed_by_two_recovery_owners_fails_closed(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    container = boot()
    _, action_id = held(container, planned(container))
    container.pending_approval_registry.clear()
    register_se_pending(container, action_id)

    with pytest.raises(PendingActionRecoveryError, match="more than one recovery owner"):
        container.pending_action_recovery.recover_pending(action_id)
    with pytest.raises(PendingActionRecoveryError, match="more than one recovery owner"):
        container.pending_action_recovery.list_pending()
    with pytest.raises(PendingActionRecoveryError):
        approve(container, action_id)
    assert container.action_queue.list() == []


# --- Reconciliation of ambiguous handoffs ---------------------------------------


def test_queued_handoff_is_reported_uncertain_and_never_replayed(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    before = boot()
    stored = planned(before, make_step(**SAFE))
    result = ask(before, stored)
    assert result.outcome is GoalPlanDelegationOutcome.QUEUED
    assert result.action_id is not None
    assert reports(before)[result.action_id].held_in_process is True

    after = boot()
    report = reports(after)[result.action_id]

    assert report.category is GoalPlanHandoffCategory.EXECUTION_UNCERTAIN
    assert report.requires_operator is True
    assert report.held_in_process is False
    assert report.step_status is PlanStepStatus.IN_PROGRESS
    assert after.action_queue.list() == []
    assert after.pending_action_recovery.list_pending() == []
    assert after.action_lifecycle_repository.get(result.action_id) is None
    assert current(after, stored).plan.steps[0].status is PlanStepStatus.IN_PROGRESS


def test_classification_covers_every_durable_handoff_state(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = boot()
    queued_step, finished_step = make_step(**SAFE), make_step(objective="Done", **SAFE)
    held_step, rejected_step = make_step(), make_step(objective="Rejected")
    unroutable_step = make_step(objective="No account")
    stored = planned(
        container, queued_step, finished_step, held_step, rejected_step, unroutable_step
    )
    queued = ask(container, stored, queued_step.id).action_id
    finished = ask(container, stored, finished_step.id).action_id
    container.goal_plan_repository.transition_step(
        stored.plan.goal_id, 1, finished_step.id, PlanStepStatus.SUCCEEDED,
        expected_revision=current(container, stored).revision,
    )
    pending = ask(container, stored, held_step.id).action_id
    rejected = ask(container, stored, rejected_step.id).action_id
    assert rejected is not None
    reject(container, rejected)
    unroutable = ask(container, stored, unroutable_step.id, account_context=None).action_id

    class DenyAll:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(status=PermissionStatus.DENIED, reason="blocked")

    denied_plan = planned(container, make_step(objective="Denied"))
    monkeypatch.setattr(container, "permission_engine", DenyAll())
    denied = ask(container, denied_plan).action_id

    found = reports(boot())

    expected = {
        queued: (GoalPlanHandoffCategory.EXECUTION_UNCERTAIN, True),
        finished: (GoalPlanHandoffCategory.RESOLVED, False),
        pending: (GoalPlanHandoffCategory.RECOVERABLE_APPROVAL, True),
        rejected: (GoalPlanHandoffCategory.NOT_EXECUTABLE, False),
        unroutable: (GoalPlanHandoffCategory.NOT_EXECUTABLE, False),
        denied: (GoalPlanHandoffCategory.NOT_EXECUTABLE, False),
    }
    assert {
        action_id: (report.category, report.requires_operator)
        for action_id, report in found.items()
    } == expected
    assert list(found) == [queued, finished, pending, rejected, unroutable, denied]


def test_inconsistent_handoff_is_reported_not_repaired(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    container = boot()
    stored = planned(container, make_step(**SAFE))
    action_id = ask(container, stored).action_id
    assert action_id is not None
    tamper(db_path, "UPDATE goal_plan_steps SET status = 'pending'")

    report = reports(boot())[action_id]

    assert report.category is GoalPlanHandoffCategory.INCONSISTENT
    assert report.requires_operator is True
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM goal_plan_steps").fetchone() == (
            "pending",
        )


# --- Real process restart -------------------------------------------------------


CHILD = """
import sys
from uuid import UUID
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.goal_plan_delegation import GoalPlanStepDelegationRequest
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT

container = ApplicationContainer()
goal_id = UUID(sys.argv[1])
for step_id in sys.argv[2:]:
    stored = container.goal_plan_repository.get_plan(goal_id, 1)
    result = container.goal_plan_delegation.delegate(GoalPlanStepDelegationRequest(
        goal_id=goal_id, version=1, step_id=UUID(step_id), expected_revision=stored.revision,
        requested_by="owner", account_context=GMAIL_ACCOUNT_CONTEXT,
    ))
    print(result.outcome.value, result.action_id)
"""


def test_handoffs_from_a_dead_process_are_recovered_or_reported(
    db_path: Path,
    tmp_path: Path,
    boot: Callable[[], ApplicationContainer],
) -> None:
    parent = boot()
    queued_step, held_step = make_step(**SAFE), make_step()
    stored = planned(parent, queued_step, held_step)
    env = {key: value for key, value in os.environ.items() if not key.startswith("VELOX_")}
    env.update({"PYTHONPATH": str(REPO_ROOT), "VELOX_GOAL_PLAN_STATE_DATABASE_PATH": str(db_path)})

    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(stored.plan.goal_id), str(queued_step.id),
         str(held_step.id)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )

    assert child.returncode == 0, child.stderr
    (queued_outcome, queued_id), (held_outcome, held_id) = (
        line.split() for line in child.stdout.strip().splitlines()
    )
    assert (queued_outcome, held_outcome) == ("queued", "awaiting_approval")
    restarted = boot()
    found = reports(restarted)
    assert found[UUID(queued_id)].category is GoalPlanHandoffCategory.EXECUTION_UNCERTAIN
    assert found[UUID(held_id)].category is GoalPlanHandoffCategory.RECOVERABLE_APPROVAL
    assert [a.id for a in restarted.pending_action_recovery.list_pending()] == [UUID(held_id)]

    approve(restarted, UUID(held_id))

    assert [a.id for a in restarted.action_queue.list()] == [UUID(held_id)]


def test_http_approval_after_restart(
    db_path: Path,
    boot: Callable[[], ApplicationContainer],
    client_for: Callable[[ApplicationContainer], TestClient],
) -> None:
    before = boot()
    stored = planned(before)
    _, action_id = held(before, stored)
    after = boot()
    client = client_for(after)

    listed = client.get("/actions/pending-approval").json()
    response = client.post(f"/actions/{action_id}/approve")

    assert [item["action"]["id"] for item in listed] == [str(action_id)]
    assert listed[0]["lifecycle"]["metadata"]["durable_recovery"] is True
    assert response.status_code == 200
    assert [a.id for a in after.action_queue.list()] == [action_id]
    assert client.post(f"/actions/{action_id}/approve").status_code == 404
