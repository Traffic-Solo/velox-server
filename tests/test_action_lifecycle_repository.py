from uuid import uuid4

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionEngineRuntime,
)
from apps.server.src.workers.executor import NoOpWorkerExecutor
from apps.server.src.workers.runtime import WorkerRuntime


def test_repository_returns_none_for_unknown_action() -> None:
    repository = InMemoryActionLifecycleRepository()

    assert repository.get(uuid4()) is None


def test_repository_stores_and_returns_state() -> None:
    repository = InMemoryActionLifecycleRepository()
    action_id = uuid4()
    state = ActionLifecycleState(status=ActionStatus.QUEUED)

    stored = repository.set(action_id, state)

    assert stored is state
    assert repository.get(action_id) is state


def test_repository_lists_states_by_action_id() -> None:
    repository = InMemoryActionLifecycleRepository()
    action_id = uuid4()
    state = ActionLifecycleState(status=ActionStatus.QUEUED)
    repository.set(action_id, state)

    assert repository.list_states() == {action_id: state}


def test_repository_clear_removes_all_states() -> None:
    repository = InMemoryActionLifecycleRepository()
    repository.set(uuid4(), ActionLifecycleState(status=ActionStatus.QUEUED))

    repository.clear()

    assert repository.list_states() == {}


def test_lifecycle_flows_from_permission_stage_to_worker_runtime() -> None:
    """WorkerRuntime must consume the state stored by the permission stage."""
    repository = InMemoryActionLifecycleRepository()
    lifecycle_manager = ActionLifecycleManager()
    queue = ActionQueue()
    permission_runtime = PermissionEngineRuntime(
        permission_engine=BasePermissionEngine(),
        action_lifecycle_manager=lifecycle_manager,
        lifecycle_repository=repository,
    )
    worker_runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=lifecycle_manager,
        worker_executor=NoOpWorkerExecutor(),
        lifecycle_repository=repository,
    )
    action = Action(type="summarize_email", target="event-1")

    evaluations = permission_runtime.evaluate([action])
    queue.enqueue_many(permission_runtime.queueable_actions(evaluations))
    queued_state = repository.get(action.id)
    assert queued_state is not None
    assert queued_state.status == ActionStatus.QUEUED

    result = worker_runtime.process_next()

    assert result.processed is True
    final_state = repository.get(action.id)
    assert final_state is not None
    assert final_state.status == ActionStatus.COMPLETED
    assert final_state.created_at == queued_state.created_at
