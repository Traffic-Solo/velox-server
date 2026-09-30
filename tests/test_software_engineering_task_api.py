"""Offline coverage for the Software Engineering task HTTP ingress."""

import json
from collections.abc import Iterator
from uuid import UUID

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import NoOpWorkerExecutor, WorkerCapability
from fastapi.testclient import TestClient


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApplicationContainer]:
    """Use deterministic composition with no real Software Engineering provider."""
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_PROVIDER", "disabled")
    get_settings.cache_clear()
    instance = ApplicationContainer()
    app.dependency_overrides[get_container] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_container, None)
    get_settings.cache_clear()


@pytest.fixture
def client(container: ApplicationContainer) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "objective": "Add one regression test for the task ingress",
        "target": "velox-server",
    }
    body.update(overrides)
    return body


def register_software_engineering_route(container: ApplicationContainer) -> None:
    container.worker_executor_registry.register_capability(
        WorkerCapability(
            identifier=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            role=ExecutorRole.SOFTWARE_ENGINEERING,
            provider="test_software_engineering",
        ),
        NoOpWorkerExecutor(),
    )


def test_disabled_provider_route_is_rejected_without_side_effects(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "route_rejected"
    assert body["permission_status"] is None
    assert body["routing_reason"] == "no_handler"
    action_id = UUID(body["action_id"])
    assert container.action_lifecycle_repository.get(action_id) is None
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    assert container.worker_execution_observer.list() == []


def test_ingress_maps_to_trusted_software_engineering_route_and_stops_at_approval(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    register_software_engineering_route(container)

    def forbidden_execution(*args: object, **kwargs: object) -> None:
        raise AssertionError("task ingress must not execute a worker")

    monkeypatch.setattr(container.worker_runtime, "process_next", forbidden_execution)

    response = client.post("/tasks/software-engineering", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["permission_status"] == "requires_approval"
    assert body["routing_reason"] == "capability_route"
    assert set(body) == {"action_id", "status", "permission_status", "routing_reason"}

    [pending] = container.pending_approval_registry.list_pending()
    assert pending.id == UUID(body["action_id"])
    assert pending.executor_role is ExecutorRole.SOFTWARE_ENGINEERING
    assert pending.type == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
    assert pending.target == "velox-server"
    assert pending.payload == {
        "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        "objective": "Add one regression test for the task ingress",
    }
    assert pending.metadata["task_delegation"] == {
        "requested_role": ExecutorRole.SOFTWARE_ENGINEERING.value,
        "requested_capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    }
    assert "capability_provider" not in pending.payload
    assert "account_context" not in pending.payload
    assert container.action_queue.list() == []
    assert container.worker_execution_observer.list() == []
    assert "test_software_engineering" not in json.dumps(body)


@pytest.mark.parametrize("field", ["objective", "target"])
@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_task_fields_fail_before_delegation_side_effects(
    client: TestClient,
    container: ApplicationContainer,
    field: str,
    value: str,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload(**{field: value}))

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid software engineering task"}
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "claude_code"),
        ("executor_role", "software_engineering"),
        ("capability", "code.implement"),
        ("account_context", {"account_identifier": "secret-account"}),
        ("approval", "approved"),
        ("workspace", "/tmp/repo"),
        ("branch", "main"),
        ("retry_policy", "always"),
    ],
)
def test_request_cannot_supply_execution_authority(
    client: TestClient,
    container: ApplicationContainer,
    field: str,
    value: object,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload(**{field: value}))

    assert response.status_code == 422
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


@pytest.mark.parametrize("body", [{"target": "velox-server"}, {"objective": "Do the work"}])
def test_required_fields_are_enforced_by_http_schema(
    client: TestClient,
    container: ApplicationContainer,
    body: dict[str, str],
) -> None:
    response = client.post("/tasks/software-engineering", json=body)

    assert response.status_code == 422
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


def test_bearer_auth_protects_task_ingress(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    monkeypatch.setenv("VELOX_API_TOKEN", "test-secret")
    get_settings.cache_clear()

    assert client.post("/tasks/software-engineering", json=payload()).status_code == 401
    response = client.post(
        "/tasks/software-engineering",
        json=payload(),
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "route_rejected"
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


def test_openapi_request_schema_exposes_only_caller_owned_fields(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/tasks/software-engineering"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][schema_name]

    assert set(request_schema["properties"]) == {"objective", "target"}
    assert set(request_schema["required"]) == {"objective", "target"}
    assert request_schema["additionalProperties"] is False
