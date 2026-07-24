from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.actions import Action
from apps.server.src.core.container import get_container
from apps.server.src.core.permission import (
    PermissionDecision,
    PermissionEngineRuntime,
    PermissionStatus,
)
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


def test_post_events_accepts_universal_event() -> None:
    event_id = uuid4()

    response = client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
            "correlation_id": None,
            "causation_id": None,
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "event_id": str(event_id),
    }


def test_post_events_stores_event() -> None:
    event_id = uuid4()

    response = client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
        },
    )

    assert response.status_code == 202
    assert get_container().event_repository.get_event(event_id) is not None


def test_post_events_adds_event_to_pending_inbox() -> None:
    event_id = uuid4()

    response = client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
        },
    )

    assert response.status_code == 202
    assert get_container().event_inbox.list_pending()[0].id == event_id


def test_post_events_creates_pending_lifecycle_state() -> None:
    event_id = uuid4()

    response = client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
        },
    )

    assert response.status_code == 202
    assert get_container().event_lifecycle_states[event_id].status == "pending"


def test_get_events_returns_stored_events() -> None:
    event_id = uuid4()

    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
        },
    )

    response = client.get("/events")

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(event_id)


def test_get_events_pending_returns_pending_events() -> None:
    event_id = uuid4()

    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "test-suite",
            "type": "test.created",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"name": "example"},
            "metadata": {"test": True},
        },
    )

    response = client.get("/events/pending")

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(event_id)


def test_get_events_inbox_is_not_public() -> None:
    response = client.get("/events/inbox")

    # "inbox" is parsed by the /events/{event_id} route and is not a valid
    # UUID, so the request is rejected; no inbox route is exposed.
    assert response.status_code == 422


def test_process_existing_event_returns_200() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200


def test_process_event_response_contains_event_classification_and_context() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    body = response.json()
    assert "event" in body
    assert "classification" in body
    assert "context" in body


def test_process_event_accepts_explicit_integration_route() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    response = client.post(
        f"/events/{event_id}/process",
        json={
            "integration_route": {
                "provider": "calendar",
                "account_identifier": "calendar-account",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["integration_route"] == {
        "provider": "calendar",
        "principal": None,
        "account_identifier": "calendar-account",
    }


def test_process_event_rejects_malformed_integration_route() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    response = client.post(
        f"/events/{event_id}/process",
        json={
            "integration_route": {
                "provider": 123,
                "principal": None,
                "account_identifier": "calendar-account",
            },
        },
    )

    assert response.status_code == 422


def test_process_event_without_integration_route_returns_none() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert response.json()["integration_route"] is None


def test_process_event_with_empty_json_body_returns_none_route() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process", json={})

    assert response.status_code == 200
    assert response.json()["integration_route"] is None


def test_process_event_with_explicit_null_route_returns_none() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            "metadata": {},
        },
    )

    response = client.post(
        f"/events/{event_id}/process",
        json={"integration_route": None},
    )

    assert response.status_code == 200
    assert response.json()["integration_route"] is None


def test_process_event_ignores_event_fields_as_integration_route_authority() -> None:
    event_id = uuid4()
    untrusted_fields = {
        "account_context": {
            "principal": "untrusted",
            "account_identifier": "untrusted-account",
        },
        "capability_provider": "untrusted-provider",
        "provider": "untrusted-provider",
    }
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "calendar",
            "type": "event.updated",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": untrusted_fields,
            "metadata": untrusted_fields,
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert response.json()["integration_route"] is None


def test_process_event_response_includes_actions() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert "actions" in response.json()


def test_process_event_actions_include_planner_output() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.json()["actions"][0]["type"] == "summarize_email"


def test_processing_endpoint_enqueues_planner_actions() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"number": 1},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert get_container().action_queue.count() == 1
    assert get_container().action_queue.list()[0].type == "review_pull_request"


def test_processing_endpoint_filters_denied_actions_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DenyingPermissionEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(
                status=PermissionStatus.DENIED,
                reason="blocked",
            )

    container = get_container()
    monkeypatch.setattr(
        container,
        "permission_runtime",
        PermissionEngineRuntime(
            permission_engine=DenyingPermissionEngine(),
            action_lifecycle_manager=ActionLifecycleManager(),
            lifecycle_repository=container.action_lifecycle_repository,
        ),
    )
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"number": 1},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert container.action_queue.count() == 0


def test_processing_endpoint_returns_denied_actions_with_permission_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DenyingPermissionEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(
                status=PermissionStatus.DENIED,
                reason="blocked",
            )

    container = get_container()
    monkeypatch.setattr(
        container,
        "permission_runtime",
        PermissionEngineRuntime(
            permission_engine=DenyingPermissionEngine(),
            action_lifecycle_manager=ActionLifecycleManager(),
            lifecycle_repository=container.action_lifecycle_repository,
        ),
    )
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"number": 1},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")
    action = response.json()["actions"][0]
    permission_decision = response.json()["permission_decisions"][0]

    assert action["metadata"]["permission_decision"]["status"] == "denied"
    assert permission_decision["decision"]["status"] == "denied"
    assert permission_decision["lifecycle"]["status"] == "rejected"


def test_actions_queue_endpoint_returns_empty_queue_by_default() -> None:
    response = client.get("/actions/queue")

    assert response.status_code == 200
    assert response.json() == []


def test_actions_queue_endpoint_returns_queued_action() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"number": 1},
            "metadata": {},
        },
    )
    client.post(f"/events/{event_id}/process")

    response = client.get("/actions/queue")

    assert response.status_code == 200
    assert response.json()[0]["type"] == "review_pull_request"


def test_actions_queue_endpoint_does_not_clear_queue() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "github",
            "type": "pull_request.opened",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"number": 1},
            "metadata": {},
        },
    )
    client.post(f"/events/{event_id}/process")

    first_response = client.get("/actions/queue")
    second_response = client.get("/actions/queue")

    assert first_response.json() == second_response.json()
    assert get_container().action_queue.count() == 1


def test_processed_event_classification_category_is_correct() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.json()["classification"]["category"] == "gmail"


def test_process_missing_event_returns_404() -> None:
    response = client.post(f"/events/{uuid4()}/process")

    assert response.status_code == 404


def test_processed_event_is_removed_from_pending_inbox() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert get_container().event_inbox.list_pending() == []


def test_successful_processing_sets_lifecycle_state_to_processed() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 200
    assert get_container().event_lifecycle_states[event_id].status == "processed"


def test_processing_failure_sets_lifecycle_state_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingPipeline:
        def process(self, event):
            raise RuntimeError("processing failed")

    event_id = uuid4()
    container = get_container()
    monkeypatch.setattr(container, "event_processing_pipeline", FailingPipeline())
    failure_client = TestClient(app, raise_server_exceptions=False)

    failure_client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )

    response = failure_client.post(f"/events/{event_id}/process")

    lifecycle_state = container.event_lifecycle_states[event_id]
    assert response.status_code == 500
    assert lifecycle_state.status == "failed"
    assert lifecycle_state.reason == "processing failed"


def test_invalid_lifecycle_transition_is_rejected() -> None:
    event_id = uuid4()
    client.post(
        "/events",
        json={
            "id": str(event_id),
            "source": "gmail",
            "type": "message.received",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"subject": "example"},
            "metadata": {},
        },
    )
    client.post(f"/events/{event_id}/process")

    response = client.post(f"/events/{event_id}/process")

    assert response.status_code == 409


def test_post_events_validates_universal_event_request_body() -> None:
    response = client.post(
        "/events",
        json={
            "source": "   ",
            "type": "test.created",
        },
    )

    assert response.status_code == 422


def test_events_schema_returns_status_200() -> None:
    response = client.get("/events/schema")

    assert response.status_code == 200


def test_events_schema_response_contains_required_sections() -> None:
    response = client.get("/events/schema")

    body = response.json()

    assert "model_name" in body
    assert "fields" in body
    assert "sample_event" in body
    assert "normalizer_contract" in body


def test_events_schema_response_uses_universal_event_model_name() -> None:
    response = client.get("/events/schema")

    assert response.json()["model_name"] == "UniversalEvent"


def test_events_schema_sample_event_contains_required_fields() -> None:
    response = client.get("/events/schema")

    sample_event = response.json()["sample_event"]

    assert "id" in sample_event
    assert "source" in sample_event
    assert "type" in sample_event
    assert "timestamp" in sample_event
    assert "payload" in sample_event
    assert "metadata" in sample_event
