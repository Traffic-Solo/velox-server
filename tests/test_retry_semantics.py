"""Tests for transient action retry and failed-event replay."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.core.container import get_container
from apps.server.src.core.events import UniversalEvent
from apps.server.src.main import app
from apps.server.src.workers.executor import (
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)
from apps.server.src.workers.runtime import WorkerRuntime
from fastapi.testclient import TestClient

client = TestClient(app)


def setup_function() -> None:
    container = get_container()
    container.event_repository.clear()
    container.event_inbox.clear()
    container.event_lifecycle_states.clear()
    container.action_queue.clear()
    container.action_lifecycle_repository.clear()
    container.pending_approval_registry.clear()


class FlakyExecutor:
    """Fails with a transient error a fixed number of times, then succeeds."""

    def __init__(self, failures_before_success: int) -> None:
        self._remaining_failures = failures_before_success

    def execute(self, action: Action) -> WorkerExecutionResult:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason="temporary upstream error",
                metadata={"external_execution_performed": False},
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.TRANSIENT,
                    message="temporary upstream error",
                ),
            )
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"external_execution_performed": False},
        )


class AlwaysPermanentFailureExecutor:
    def execute(self, action: Action) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.FAILED,
            reason="permanent error",
            metadata={"external_execution_performed": False},
            failure=WorkerExecutionFailure(
                category=WorkerExecutionFailureCategory.PERMANENT,
                message="permanent error",
            ),
        )


def _create_runtime(
    queue: ActionQueue,
    executor: object,
    repository: InMemoryActionLifecycleRepository,
    max_transient_retries: int = 3,
) -> WorkerRuntime:
    return WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=executor,  # type: ignore[arg-type]
        lifecycle_repository=repository,
        max_transient_retries=max_transient_retries,
    )


def test_transient_failure_requeues_action_for_retry() -> None:
    queue = ActionQueue()
    repository = InMemoryActionLifecycleRepository()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = _create_runtime(queue, FlakyExecutor(failures_before_success=1), repository)

    first = runtime.process_next()

    assert first.execution_status == WorkerExecutionStatus.FAILED
    assert queue.count() == 1  # re-queued for retry
    retry_state = repository.get(action.id)
    assert retry_state is not None
    assert retry_state.status == ActionStatus.APPROVED
    assert retry_state.metadata["transient_retry_count"] == 1

    second = runtime.process_next()

    assert second.execution_status == WorkerExecutionStatus.SUCCEEDED
    final_state = repository.get(action.id)
    assert final_state is not None
    assert final_state.status == ActionStatus.COMPLETED
    assert queue.count() == 0


def test_transient_retries_are_bounded() -> None:
    queue = ActionQueue()
    repository = InMemoryActionLifecycleRepository()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = _create_runtime(
        queue,
        FlakyExecutor(failures_before_success=10),
        repository,
        max_transient_retries=2,
    )

    for _ in range(3):  # initial attempt + 2 retries
        runtime.process_next()

    assert queue.count() == 0  # retries exhausted, not re-queued again
    final_state = repository.get(action.id)
    assert final_state is not None
    assert final_state.status == ActionStatus.FAILED
    assert final_state.metadata["transient_retry_count"] == 2


def test_permanent_failure_is_not_retried() -> None:
    queue = ActionQueue()
    repository = InMemoryActionLifecycleRepository()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = _create_runtime(queue, AlwaysPermanentFailureExecutor(), repository)

    result = runtime.process_next()

    assert result.execution_status == WorkerExecutionStatus.FAILED
    assert queue.count() == 0
    final_state = repository.get(action.id)
    assert final_state is not None
    assert final_state.status == ActionStatus.FAILED


def test_failed_event_can_be_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    container = get_container()
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    class ExplodingPipeline:
        def process(self, event: UniversalEvent) -> None:
            raise RuntimeError("classifier temporarily unavailable")

    original_pipeline = container.event_processing_pipeline
    monkeypatch.setattr(container, "event_processing_pipeline", ExplodingPipeline())

    failed_response = client.post(f"/events/{event_id}/process")
    assert failed_response.status_code == 500
    assert container.event_lifecycle_states[event_id].status == "failed"
    # The event stays in the pending inbox for replay.
    assert any(
        event.id == event_id for event in container.event_inbox.list_pending()
    )

    monkeypatch.setattr(container, "event_processing_pipeline", original_pipeline)

    retry_response = client.post(f"/events/{event_id}/process")

    assert retry_response.status_code == 200
    assert container.event_lifecycle_states[event_id].status == "processed"
    assert all(
        event.id != event_id for event in container.event_inbox.list_pending()
    )
