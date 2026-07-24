"""Adversarial post-remediation checks: approval bypasses and API edges."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.container import get_container
from apps.server.src.core.events import ProcessedEvent
from apps.server.src.main import app
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


class UnsafeActionPlanner:
    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        return [
            Action(
                type="gmail.send",
                target=str(processed_event.event.id),
                executor_role=ExecutorRole.CONTENT_SUMMARY,
            )
        ]


def _held_action_id(monkeypatch: pytest.MonkeyPatch) -> str:
    container = get_container()
    monkeypatch.setattr(
        container.event_workflow_service,
        "planner",
        UnsafeActionPlanner(),
    )
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
    action_id: str = response.json()["permission_decisions"][0]["action_id"]
    return action_id


def test_double_approve_does_not_double_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = _held_action_id(monkeypatch)
    container = get_container()

    first = client.post(f"/actions/{action_id}/approve")
    second = client.post(f"/actions/{action_id}/approve")

    assert first.status_code == 200
    assert second.status_code == 404  # no longer pending, cannot re-approve
    assert container.action_queue.count() == 1  # enqueued exactly once


def test_reject_after_approve_is_not_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = _held_action_id(monkeypatch)

    assert client.post(f"/actions/{action_id}/approve").status_code == 200
    assert client.post(f"/actions/{action_id}/reject").status_code == 404


def test_approve_after_reject_is_not_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = _held_action_id(monkeypatch)
    container = get_container()

    assert client.post(f"/actions/{action_id}/reject").status_code == 200
    assert client.post(f"/actions/{action_id}/approve").status_code == 404
    assert container.action_queue.count() == 0


def test_held_action_never_enters_queue_without_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing lifecycle state must not leak a held action into execution."""
    _held_action_id(monkeypatch)
    container = get_container()

    # Even if lifecycle state is lost, the action is only in the registry,
    # never in the execution queue, so nothing can execute it.
    container.action_lifecycle_repository.clear()

    assert container.action_queue.count() == 0
    result = container.worker_runtime.process_next()
    assert result.processed is False
    assert result.action is None


def test_events_schema_route_is_not_shadowed_by_event_id_route() -> None:
    response = client.get("/events/schema")

    assert response.status_code == 200
    assert response.json()["model_name"] == "UniversalEvent"


def test_events_pending_route_is_not_shadowed_by_event_id_route() -> None:
    response = client.get("/events/pending")

    assert response.status_code == 200
    assert response.json() == []
