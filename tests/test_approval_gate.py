"""Tests for the deny-by-default approval gate."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approvals import InMemoryPendingApprovalRegistry
from apps.server.src.core.container import get_container
from apps.server.src.core.events import ProcessedEvent
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionEngineRuntime,
    PermissionStatus,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import NoOpWorkerExecutor
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


def test_base_engine_allows_safe_action_types() -> None:
    engine = BasePermissionEngine()

    decision = engine.evaluate(Action(type="summarize_email", target="event-1"))

    assert decision.status == PermissionStatus.ALLOWED
    assert decision.reason is not None


def test_base_engine_requires_approval_for_unknown_action_types() -> None:
    engine = BasePermissionEngine()

    decision = engine.evaluate(Action(type="gmail.send", target="message-1"))

    assert decision.status == PermissionStatus.REQUIRES_APPROVAL
    assert decision.reason is not None


def test_requires_approval_action_is_held_not_queued() -> None:
    lifecycle_repository = InMemoryActionLifecycleRepository()
    registry = InMemoryPendingApprovalRegistry()
    runtime = PermissionEngineRuntime(
        permission_engine=BasePermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
        pending_approval_registry=registry,
    )
    action = Action(type="gmail.send", target="message-1")

    evaluations = runtime.evaluate([action])

    assert runtime.queueable_actions(evaluations) == []
    assert runtime.pending_approval_actions(evaluations)[0].id == action.id
    assert registry.get(action.id) is not None
    state = lifecycle_repository.get(action.id)
    assert state is not None
    assert state.status == ActionStatus.QUEUED
    assert state.metadata["approval_required"] is True


def test_worker_runtime_refuses_unapproved_action() -> None:
    """Defense in depth: unapproved actions in the queue must not execute."""
    lifecycle_repository = InMemoryActionLifecycleRepository()
    queue = ActionQueue()
    action = Action(type="gmail.send", target="message-1")
    lifecycle_repository.set(
        action.id,
        ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        ),
    )
    queue.enqueue(action)
    runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=NoOpWorkerExecutor(),
        lifecycle_repository=lifecycle_repository,
    )

    result = runtime.process_next()

    assert result.processed is False
    assert result.execution_reason == "action awaits explicit approval"
    assert result.external_execution_performed is False
    assert queue.count() == 1  # re-enqueued, not lost


def test_worker_runtime_drops_rejected_action_without_executing() -> None:
    lifecycle_repository = InMemoryActionLifecycleRepository()
    queue = ActionQueue()
    action = Action(type="gmail.send", target="message-1")
    lifecycle_repository.set(
        action.id,
        ActionLifecycleState(status=ActionStatus.REJECTED, reason="blocked"),
    )
    queue.enqueue(action)
    runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=NoOpWorkerExecutor(),
        lifecycle_repository=lifecycle_repository,
    )

    result = runtime.process_next()

    assert result.processed is False
    assert result.execution_reason is not None
    assert "rejected" in result.execution_reason
    assert queue.count() == 0


class UnsafeActionPlanner:
    """Planner double that produces an action outside the safe list."""

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        return [
            Action(
                type="gmail.send",
                target=str(processed_event.event.id),
                executor_role=ExecutorRole.CONTENT_SUMMARY,
                payload={
                    "to": ["client@example.com"],
                    "subject": "Hello",
                    "body": "Test",
                },
            )
        ]


def _post_and_process_event(monkeypatch: pytest.MonkeyPatch) -> str:
    container = get_container()
    monkeypatch.setattr(container, "planner", UnsafeActionPlanner())
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "gmail.thread.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )
    response = client.post(f"/events/{event_id}/process")
    decision = response.json()["permission_decisions"][0]
    assert decision["decision"]["status"] == "requires_approval"
    assert decision["lifecycle"]["status"] == "queued"
    action_id: str = decision["action_id"]
    return action_id


def test_approval_flow_via_api(monkeypatch: pytest.MonkeyPatch) -> None:
    action_id = _post_and_process_event(monkeypatch)
    container = get_container()

    # Action is held for approval, not queued for execution.
    assert container.action_queue.count() == 0
    pending = client.get("/actions/pending-approval").json()
    assert pending[0]["action"]["id"] == action_id
    assert pending[0]["lifecycle"]["status"] == "queued"

    approve_response = client.post(f"/actions/{action_id}/approve")

    assert approve_response.status_code == 200
    assert approve_response.json()["lifecycle"]["status"] == "approved"
    assert container.action_queue.count() == 1
    assert client.get("/actions/pending-approval").json() == []

    # The approved action executes normally.
    result = container.worker_runtime.process_next()
    assert result.processed is True


def test_rejection_flow_via_api(monkeypatch: pytest.MonkeyPatch) -> None:
    action_id = _post_and_process_event(monkeypatch)
    container = get_container()

    reject_response = client.post(
        f"/actions/{action_id}/reject",
        json={"reason": "not needed"},
    )

    assert reject_response.status_code == 200
    assert reject_response.json()["lifecycle"]["status"] == "rejected"
    assert reject_response.json()["lifecycle"]["reason"] == "not needed"
    assert container.action_queue.count() == 0
    assert client.get("/actions/pending-approval").json() == []


def test_approve_unknown_action_returns_404() -> None:
    response = client.post(f"/actions/{uuid4()}/approve")

    assert response.status_code == 404


def test_reject_unknown_action_returns_404() -> None:
    response = client.post(f"/actions/{uuid4()}/reject")

    assert response.status_code == 404
