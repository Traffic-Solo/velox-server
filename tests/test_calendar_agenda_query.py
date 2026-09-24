"""Offline coverage for bounded free-form Calendar agenda ingress."""

from collections.abc import Iterator
from typing import Any, cast

import pytest
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.calendar_agenda import CalendarTomorrowAgendaResult
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)
from apps.server.src.integrations.calendar_agenda_query import (
    BoundedCalendarAgendaIntentResolver,
    CalendarAgendaIntentResolutionError,
    CalendarAgendaIntentResolver,
    CalendarAgendaQueryValidationError,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import WorkerAccountContext
from fastapi.testclient import TestClient

RESULT = CalendarTomorrowAgendaResult(
    intent="tomorrow",
    timezone="Europe/Tirane",
    local_date="2026-03-29",
    time_min="2026-03-29T00:00:00+01:00",
    time_max="2026-03-30T00:00:00+02:00",
    events=(),
    event_count=0,
    aggregate_complete=True,
    skipped_event_count=0,
    termination_reason="complete",
)


@pytest.mark.parametrize(
    "text",
    [
        "що в мене завтра?",
        "що у мене завтра?",  # noqa: RUF001
        "WHAT DO I HAVE TOMORROW?",
        "  ЩО У МЕНЕ ЗАВТРА!!!  ",  # noqa: RUF001
    ],
)
def test_bounded_resolver_supports_normalized_phrases(text: str) -> None:
    resolver: CalendarAgendaIntentResolver = BoundedCalendarAgendaIntentResolver()
    assert resolver.resolve(text) == "tomorrow"


@pytest.mark.parametrize("text", ["", "   "])
def test_bounded_resolver_rejects_blank_text(text: str) -> None:
    with pytest.raises(CalendarAgendaQueryValidationError):
        BoundedCalendarAgendaIntentResolver().resolve(text)


def test_bounded_resolver_rejects_unsupported_text() -> None:
    with pytest.raises(CalendarAgendaIntentResolutionError):
        BoundedCalendarAgendaIntentResolver().resolve(
            "what is on my calendar today?"
        )


def test_resolver_has_no_provider_or_runtime_dependencies() -> None:
    assert "google" not in BoundedCalendarAgendaIntentResolver.__module__


class RecordingIntentResolver:
    def __init__(self, intent: str = "tomorrow") -> None:
        self.intent = intent
        self.calls: list[str] = []

    def resolve(self, text: str) -> str:
        self.calls.append(text)
        return self.intent


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
    monkeypatch.delenv("VELOX_API_TOKEN", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def install_spy(
    container: ApplicationContainer, error: Exception | None = None,
) -> RecordingCommandService:
    spy = RecordingCommandService(error)
    container.calendar_agenda_command_service = cast(CalendarAgendaCommandService, spy)
    return spy


def query_payload() -> dict[str, Any]:
    return {
        "text": "що в мене завтра?",
        "account_context": {
            "principal": "explicit-principal",
            "account_identifier": "explicit-account",
        },
        "timezone": "Europe/Tirane",
    }


def test_query_delegates_intent_and_explicit_context(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 200
    assert response.json()["intent"] == "tomorrow"
    assert len(spy.calls) == 1
    forwarded = spy.calls[0]
    assert forwarded.intent == "tomorrow"
    assert forwarded.account_context == WorkerAccountContext(
        principal="explicit-principal",
        account_identifier="explicit-account",
    )
    assert forwarded.timezone == "Europe/Tirane"


@pytest.mark.parametrize("text", ["", "   "])
def test_query_rejects_blank_text_without_service_call(
    client: TestClient, container: ApplicationContainer, text: str,
) -> None:
    spy = install_spy(container)
    body = query_payload()
    body["text"] = text
    response = client.post("/calendar/agenda/query", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid calendar agenda query"}
    assert spy.calls == []


def test_query_rejects_unsupported_text_without_service_call(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    body = query_payload()
    body["text"] = "unsupported request"
    response = client.post("/calendar/agenda/query", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported calendar agenda query"}
    assert spy.calls == []


def test_query_uses_container_owned_resolver(
    client: TestClient, container: ApplicationContainer,
) -> None:
    resolver = RecordingIntentResolver()
    container.calendar_agenda_intent_resolver = resolver
    spy = install_spy(container)
    body = query_payload()
    body["text"] = "delegate this text"
    response = client.post("/calendar/agenda/query", json=body)
    assert response.status_code == 200
    assert resolver.calls == ["delegate this text"]
    assert len(spy.calls) == 1
    assert spy.calls[0].intent == "tomorrow"


def test_query_preserves_safe_execution_error_behavior(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container, RuntimeError("secret provider detail"))
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "calendar agenda execution failed"}
    assert "secret provider detail" not in response.text
    assert len(spy.calls) == 1


def test_query_requires_request_context(client: TestClient) -> None:
    response = client.post("/calendar/agenda/query", json={"text": "що в мене завтра?"})
    assert response.status_code == 422
