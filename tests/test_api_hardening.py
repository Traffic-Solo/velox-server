"""Tests for API auth, idempotency, pagination and new event endpoints."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import get_container
from apps.server.src.core.events import UniversalEvent
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


def _event_payload(event_id: str | None = None) -> dict[str, object]:
    return {
        "id": event_id or str(uuid4()),
        "source": "test-suite",
        "type": "test.created",
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {},
        "metadata": {},
    }


@pytest.fixture
def api_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("VELOX_API_TOKEN", "test-secret")
    get_settings.cache_clear()
    yield "test-secret"
    get_settings.cache_clear()


def test_endpoints_are_open_when_no_token_configured() -> None:
    assert client.get("/events").status_code == 200


def test_api_requires_bearer_token_when_configured(api_token: str) -> None:
    assert client.get("/events").status_code == 401
    assert (
        client.post("/events", json=_event_payload()).status_code == 401
    )


def test_api_accepts_valid_bearer_token(api_token: str) -> None:
    headers = {"Authorization": f"Bearer {api_token}"}

    assert client.get("/events", headers=headers).status_code == 200


def test_api_rejects_wrong_bearer_token(api_token: str) -> None:
    headers = {"Authorization": "Bearer wrong-token"}

    assert client.get("/events", headers=headers).status_code == 401


def test_health_and_root_stay_open(api_token: str) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_duplicate_event_id_is_rejected() -> None:
    payload = _event_payload()

    first = client.post("/events", json=payload)
    second = client.post("/events", json=payload)

    assert first.status_code == 202
    assert second.status_code == 409


def test_get_event_by_id() -> None:
    payload = _event_payload()
    client.post("/events", json=payload)

    response = client.get(f"/events/{payload['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == payload["id"]


def test_get_unknown_event_returns_404() -> None:
    assert client.get(f"/events/{uuid4()}").status_code == 404


def test_get_event_lifecycle() -> None:
    payload = _event_payload()
    client.post("/events", json=payload)

    response = client.get(f"/events/{payload['id']}/lifecycle")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_get_lifecycle_for_unknown_event_returns_404() -> None:
    assert client.get(f"/events/{uuid4()}/lifecycle").status_code == 404


def test_events_pagination() -> None:
    container = get_container()
    for index in range(5):
        container.event_repository.append(
            UniversalEvent(source="test-suite", type=f"test.event.{index}")
        )

    first_page = client.get("/events", params={"limit": 2, "offset": 0}).json()
    second_page = client.get("/events", params={"limit": 2, "offset": 2}).json()

    assert len(first_page) == 2
    assert len(second_page) == 2
    assert first_page[0]["type"] == "test.event.0"
    assert second_page[0]["type"] == "test.event.2"


def test_processing_failure_does_not_leak_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = get_container()
    payload = _event_payload()
    client.post("/events", json=payload)

    class ExplodingPipeline:
        def process(self, event: UniversalEvent) -> None:
            raise RuntimeError("secret internal detail: /etc/velox/config")

    monkeypatch.setattr(
        container.event_workflow_service,
        "event_processing_pipeline",
        ExplodingPipeline(),
    )

    response = client.post(f"/events/{payload['id']}/process")

    assert response.status_code == 500
    assert response.json()["detail"] == "event processing failed"
    assert "secret" not in response.text
