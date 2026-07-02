from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.workers.runtime import WorkerRuntime


def create_runtime(queue: ActionQueue) -> WorkerRuntime:
    return WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
    )


def test_worker_runtime_processes_queued_action() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.processed is True
    assert result.action is not None
    assert result.action.id == action.id
    assert result.action.status == "completed"
    assert queue.count() == 0


def test_worker_runtime_applies_lifecycle_transitions() -> None:
    queue = ActionQueue()
    queue.enqueue(Action(type="summarize_email", target="event-1"))
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.lifecycle_state is not None
    assert result.lifecycle_state.status == ActionStatus.COMPLETED
    assert result.action is not None
    assert result.action.metadata["action_lifecycle"]["status"] == "completed"


def test_worker_runtime_handles_empty_queue_safely() -> None:
    runtime = create_runtime(ActionQueue())

    result = runtime.process_next()

    assert result.processed is False
    assert result.action is None
    assert result.lifecycle_state is None
    assert result.external_execution_performed is False


def test_worker_runtime_processes_one_action_at_a_time() -> None:
    queue = ActionQueue()
    first_action = Action(type="summarize_email", target="event-1")
    second_action = Action(type="prepare_meeting", target="event-2")
    queue.enqueue_many([first_action, second_action])
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.action is not None
    assert result.action.id == first_action.id
    assert queue.list() == [second_action]


def test_worker_runtime_does_not_perform_external_execution() -> None:
    queue = ActionQueue()
    queue.enqueue(
        Action(
            type="external.vendor.call",
            target="remote-system",
            payload={"should_not_execute": True},
        )
    )
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.external_execution_performed is False
    assert result.action is not None
    assert result.action.metadata["external_execution_performed"] is False
