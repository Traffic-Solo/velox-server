from fastapi.testclient import TestClient

from apps.server.src.main import app


client = TestClient(app)


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
