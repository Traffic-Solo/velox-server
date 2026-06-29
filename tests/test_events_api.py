from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.server.src.api.events import event_store
from apps.server.src.main import app


client = TestClient(app)


def setup_function() -> None:
    event_store.clear()


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
    assert event_store.get_event(event_id) is not None


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
