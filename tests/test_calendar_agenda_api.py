"""Offline HTTP coverage for structured Calendar agenda commands."""

from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaResult,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import WorkerExecutionFailureCategory
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

RESULT = CalendarTomorrowAgendaResult(
    intent="tomorrow", timezone="Europe/Tirane", local_date="2026-03-29",
    time_min="2026-03-29T00:00:00+01:00",
    time_max="2026-03-30T00:00:00+02:00",
    events=({"event_id": "event-1", "title": "Planning", "start": "09:00",
             "end": "10:00", "attendees": ()},),
    event_count=1, aggregate_complete=False, skipped_event_count=2,
    termination_reason="page_limit",
)
SECRET = "provider-payload access_token=secret refresh_token=secret"


class RecordingCommandService:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[CalendarAgendaCommandRequest] = []
        self.error = error

    def execute(self, request: CalendarAgendaCommandRequest) -> CalendarTomorrowAgendaResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RESULT


@pytest.fixture
def container() -> Iterator[ApplicationContainer]:
    instance = ApplicationContainer()
    app.dependency_overrides[get_container] = lambda: instance
    yield instance
    del app.dependency_overrides[get_container]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("VELOX_API_TOKEN", "test-token")
    get_settings.cache_clear()
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        yield client
    get_settings.cache_clear()


def payload() -> dict[str, Any]:
    return {
        "intent": "tomorrow",
        "account_context": {
            "principal": "explicit-principal",
            "account_identifier": "explicit-account",
        },
        "timezone": "Europe/Tirane",
    }


def install_spy(
    container: ApplicationContainer, error: Exception | None = None,
) -> RecordingCommandService:
    spy = RecordingCommandService(error)
    container.calendar_agenda_command_service = cast(CalendarAgendaCommandService, spy)
    return spy


def test_success_delegates_once_and_reuses_result(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    response = client.post("/calendar/agenda", json=payload())
    assert response.status_code == 200
    assert response.json() == jsonable_encoder(RESULT)
    assert len(spy.calls) == 1
    assert asdict(spy.calls[0]) == payload()
    assert not hasattr(spy.calls[0], "now")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic test-token"])
def test_auth_rejects_before_service(
    client: TestClient, container: ApplicationContainer, authorization: str | None,
) -> None:
    spy = install_spy(container)
    client.headers.pop("Authorization")
    headers = {} if authorization is None else {"Authorization": authorization}
    assert client.post("/calendar/agenda", json=payload(), headers=headers).status_code == 401
    assert spy.calls == []


@pytest.mark.parametrize("body", [
    {}, {"intent": "tomorrow"},
    {**payload(), "intent": 42},
    {**payload(), "timezone": None},
    {**payload(), "account_context": {}},
    {**payload(), "account_context": {"principal": "p"}},
    {**payload(), "account_context": {"principal": None, "account_identifier": "a"}},
    {**payload(), "account_context": {"principal": "p", "account_identifier": 42}},
    {**payload(), "now": "2026-01-01T00:00:00Z"},
    {**payload(), "account_context": {**payload()["account_context"], "now": "ignored"}},
    {**payload(), "text": "what is tomorrow"},
])
def test_structural_validation_precedes_service(
    client: TestClient, container: ApplicationContainer, body: dict[str, Any],
) -> None:
    spy = install_spy(container)
    assert client.post("/calendar/agenda", json=body).status_code == 422
    assert spy.calls == []


@pytest.mark.parametrize("field,value", [
    ("intent", "today"), ("timezone", "Invalid/Timezone"),
    ("principal", ""), ("account_identifier", " padded "),
])
def test_real_service_semantic_validation_is_client_error(
    client: TestClient, container: ApplicationContainer, field: str, value: str,
) -> None:
    container.calendar_agenda_command_service = CalendarAgendaCommandService(
        container.calendar_tomorrow_agenda_workflow,
        lambda: datetime(2026, 3, 28, 12, tzinfo=UTC),
    )
    body = payload()
    if field in {"principal", "account_identifier"}:
        body["account_context"][field] = value
    else:
        body[field] = value
    response = client.post("/calendar/agenda", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid calendar agenda request"}


@pytest.mark.parametrize("error", [
    RuntimeError(SECRET),
    CalendarAgendaWorkflowError(SECRET, field="now"),
    CalendarAgendaWorkflowError(SECRET),
    CalendarAgendaWorkflowError(SECRET, field="unknown"),
    *[CalendarAgendaWorkflowError(SECRET, category=category)
      for category in WorkerExecutionFailureCategory],
    CalendarAgendaWorkflowError(
        SECRET, field="principal", category=WorkerExecutionFailureCategory.PERMANENT,
    ),
])
def test_execution_failures_never_become_empty_agendas_or_leak(
    client: TestClient, container: ApplicationContainer,
    caplog: pytest.LogCaptureFixture, error: Exception,
) -> None:
    spy = install_spy(container, error)
    response = client.post("/calendar/agenda", json=payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "calendar agenda execution failed"}
    assert SECRET not in response.text + caplog.text
    assert len(spy.calls) == 1


def test_validation_error_text_is_not_exposed(
    client: TestClient, container: ApplicationContainer, caplog: pytest.LogCaptureFixture,
) -> None:
    install_spy(container, CalendarAgendaWorkflowError(SECRET, field="intent"))
    response = client.post("/calendar/agenda", json=payload())
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid calendar agenda request"}
    assert SECRET not in response.text + caplog.text


def test_real_command_service_success_is_deterministic(
    client: TestClient, container: ApplicationContainer,
) -> None:
    container.calendar_agenda_command_service = CalendarAgendaCommandService(
        container.calendar_tomorrow_agenda_workflow,
        lambda: datetime(2026, 3, 28, 12, tzinfo=UTC),
    )
    body = payload()
    body["account_context"] = container.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    response = client.post("/calendar/agenda", json=body)
    assert response.status_code == 200
    assert response.json()["local_date"] == "2026-03-29"
    assert response.json()["time_min"] == RESULT.time_min
    assert response.json()["time_max"] == RESULT.time_max
