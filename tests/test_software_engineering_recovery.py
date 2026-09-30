"""Restart-safe Software Engineering approval and exact claim coverage."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import approve_pending_action
from apps.server.src.core.approvals import InMemoryPendingApprovalRegistry
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_continuation import (
    SoftwareEngineeringContinuationStateError,
    SoftwareEngineeringTaskContinuation,
)
from apps.server.src.integrations.software_engineering_recovery import (
    SoftwareEngineeringActionRecovery,
)
from apps.server.src.integrations.software_engineering_state import (
    DurableSoftwareEngineeringExecutionObserver,
    SoftwareEngineeringApprovalStatus,
    SqliteSoftwareEngineeringRunRepository,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerCapability,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutorRegistry,
)
from apps.server.src.workers.runtime import (
    InMemoryWorkerExecutionObserver,
    WorkerRuntime,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[Action] = []

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: WorkerAccountContext | None = None,
    ) -> WorkerExecutionResult:
        self.calls.append(action)
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"external_execution_performed": True},
        )


def register_approved(
    repository: SqliteSoftwareEngineeringRunRepository,
    *,
    action_id: UUID,
    objective: str = "Implement restart-safe exact execution",
) -> None:
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
        objective=objective,
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )
    repository.record_approval(
        action_id=action_id,
        objective=objective,
        approved_at=datetime.now(UTC),
    )


def make_restarted_continuation(
    repository: SqliteSoftwareEngineeringRunRepository,
    executor: RecordingExecutor,
) -> tuple[
    SoftwareEngineeringTaskContinuation,
    ActionQueue,
    InMemoryActionLifecycleRepository,
]:
    queue = ActionQueue()
    lifecycle = InMemoryActionLifecycleRepository()
    registry = WorkerExecutorRegistry()
    registry.register_capability(
        WorkerCapability(
            SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            ExecutorRole.SOFTWARE_ENGINEERING,
            "recovery-test-provider",
        ),
        executor,
    )
    observer = DurableSoftwareEngineeringExecutionObserver(
        InMemoryWorkerExecutionObserver(),
        repository,
    )
    runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=executor,
        executor_registry=registry,
        execution_observer=observer,
        lifecycle_repository=lifecycle,
    )
    continuation = SoftwareEngineeringTaskContinuation(
        action_queue=queue,
        lifecycle_repository=lifecycle,
        worker_runtime=runtime,
        action_recovery=SoftwareEngineeringActionRecovery(repository),
        work_product_reviewer=None,
    )
    return continuation, queue, lifecycle


def test_approved_action_executes_after_restart_with_empty_local_queue(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    first_process = SqliteSoftwareEngineeringRunRepository(path)
    action_id = uuid4()
    register_approved(first_process, action_id=action_id)

    restarted_repository = SqliteSoftwareEngineeringRunRepository(path)
    executor = RecordingExecutor()
    continuation, queue, lifecycle = make_restarted_continuation(
        restarted_repository,
        executor,
    )

    result = continuation.execute(action_id)

    assert result.processed is True
    assert result.execution_status is WorkerExecutionStatus.SUCCEEDED
    assert result.lifecycle_status is ActionStatus.COMPLETED
    assert queue.list() == []
    assert [action.id for action in executor.calls] == [action_id]
    assert executor.calls[0].payload["objective"] == (
        "Implement restart-safe exact execution"
    )
    local_state = lifecycle.get(action_id)
    assert local_state is not None
    assert local_state.status is ActionStatus.COMPLETED
    durable = SqliteSoftwareEngineeringRunRepository(path).get(action_id)
    assert durable is not None
    assert durable.claim_id is not None
    assert durable.execution_started_at is not None
    assert durable.execution_status == "succeeded"
    assert durable.pending_objective is None


def test_claimed_action_is_not_automatically_replayed_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    first_process = SqliteSoftwareEngineeringRunRepository(path)
    action_id = uuid4()
    register_approved(first_process, action_id=action_id)
    first_process.claim_approved(action_id)

    restarted_repository = SqliteSoftwareEngineeringRunRepository(path)
    executor = RecordingExecutor()
    continuation, queue, lifecycle = make_restarted_continuation(
        restarted_repository,
        executor,
    )

    with pytest.raises(
        SoftwareEngineeringContinuationStateError,
        match="could not be claimed",
    ):
        continuation.execute(action_id)

    assert executor.calls == []
    assert queue.list() == []
    assert lifecycle.get(action_id) is None


def test_pending_action_can_be_approved_after_restart_without_registry_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    first_process = SqliteSoftwareEngineeringRunRepository(path)
    first_process.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
        objective="Recover my pending approval",
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )

    restarted_repository = SqliteSoftwareEngineeringRunRepository(path)
    recovery = SoftwareEngineeringActionRecovery(restarted_repository)
    pending = InMemoryPendingApprovalRegistry()
    lifecycle = InMemoryActionLifecycleRepository()
    queue = ActionQueue()

    approved = approve_pending_action(
        action_id,
        pending_approval_registry=pending,
        lifecycle_repository=lifecycle,
        lifecycle_manager=ActionLifecycleManager(),
        action_queue=queue,
        pending_action_recovery=recovery,
        approval_recorder=recovery,
    )

    assert approved.status is ActionStatus.APPROVED
    [queued] = queue.list()
    assert queued.id == action_id
    assert queued.payload["objective"] == "Recover my pending approval"
    durable = restarted_repository.get(action_id)
    assert durable is not None
    assert durable.approval_status is SoftwareEngineeringApprovalStatus.APPROVED


def test_durable_pending_listing_reconstructs_only_awaiting_actions(
    tmp_path: Path,
) -> None:
    repository = SqliteSoftwareEngineeringRunRepository(tmp_path / "state.sqlite3")
    awaiting_id = uuid4()
    approved_id = uuid4()
    repository.register_action(
        action_id=awaiting_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Awaiting",
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )
    repository.register_action(
        action_id=approved_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Approved",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )

    actions = SoftwareEngineeringActionRecovery(repository).list_pending()

    assert [action.id for action in actions] == [awaiting_id]
