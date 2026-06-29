from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.server.src.core.container import get_container
from apps.server.src.main import app


client = TestClient(app)


def setup_function() -> None:
    container = get_container()
    container.event_repository.clear()
    container.event_inbox.clear()


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

    assert response.status_code == 404


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

    assert response.json()["classification"]["category"] == "communication"


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
