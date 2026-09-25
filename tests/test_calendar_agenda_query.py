"""Offline coverage for bounded free-form Calendar agenda ingress."""

import socket
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any, cast

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolution,
    SemanticResolver,
    SemanticResolverError,
    SemanticRoute,
    SemanticRouter,
)
from apps.server.src.core.semantic_query import (
    ResolvedSemanticQuery,
    SemanticQueryRequest,
    SemanticQueryResult,
)
from apps.server.src.integrations.calendar_agenda import CalendarTomorrowAgendaResult
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)
from apps.server.src.integrations.calendar_agenda_query import (
    CALENDAR_AGENDA_TOMORROW_INTENT,
    BoundedCalendarAgendaIntentResolver,
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
    resolver: SemanticResolver = BoundedCalendarAgendaIntentResolver()
    assert resolver.resolve(text) == SemanticResolution.resolved(CALENDAR_AGENDA_TOMORROW_INTENT)


@pytest.mark.parametrize("text", ["", "   "])
def test_bounded_resolver_rejects_blank_text(text: str) -> None:
    with pytest.raises(SemanticInputError):
        BoundedCalendarAgendaIntentResolver().resolve(text)


def test_bounded_resolver_returns_unresolved_for_unsupported_text() -> None:
    resolution = BoundedCalendarAgendaIntentResolver().resolve("what is on my calendar today?")
    assert resolution == SemanticResolution.unresolved()


def test_resolver_has_no_provider_or_runtime_dependencies() -> None:
    assert "google" not in BoundedCalendarAgendaIntentResolver.__module__


class RecordingIntentResolver:
    """Semantic resolver double; ``result`` may be deliberately malformed."""

    def __init__(
        self,
        result: object = None,
        error: Exception | None = None,
    ) -> None:
        self.result = (
            SemanticResolution.resolved(CALENDAR_AGENDA_TOMORROW_INTENT)
            if result is None else result
        )
        self.error = error
        self.calls: list[str] = []

    def resolve(self, text: str) -> SemanticResolution:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return cast(SemanticResolution, self.result)


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
    container.semantic_resolver = resolver
    spy = install_spy(container)
    body = query_payload()
    body["text"] = "delegate this text"
    response = client.post("/calendar/agenda/query", json=body)
    assert response.status_code == 200
    assert resolver.calls == ["delegate this text"]
    assert len(spy.calls) == 1
    assert spy.calls[0].intent == "tomorrow"


def test_query_resolver_execution_failure_is_fixed_safe_500(
    client: TestClient, container: ApplicationContainer,
) -> None:
    secret = "secret local model detail"
    resolver = RecordingIntentResolver(
        error=SemanticResolverError(secret),
    )
    container.semantic_resolver = resolver
    spy = install_spy(container)

    response = client.post("/calendar/agenda/query", json=query_payload())

    assert response.status_code == 500
    assert response.json() == {"detail": "calendar agenda query resolution failed"}
    assert secret not in response.text
    assert resolver.calls == [query_payload()["text"]]
    assert spy.calls == []


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


def test_query_uses_container_semantic_role_capability_handler(
    client: TestClient, container: ApplicationContainer,
) -> None:
    command_spy = install_spy(container)
    route = container.semantic_router.resolve("calendar.agenda.tomorrow")
    assert route == SemanticRoute(
        "calendar.agenda.tomorrow", ExecutorRole.CONTEXT_PREPARATION, "list_calendar_events",
    )
    routed: list[ResolvedSemanticQuery] = []

    def handler(query: ResolvedSemanticQuery) -> SemanticQueryResult:
        routed.append(query)
        return SemanticQueryResult(intent=query.intent, result=asdict(RESULT))

    container.semantic_router = SemanticRouter(
        (route,), {(route.role, route.capability): handler},
    )
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 200
    assert response.json()["intent"] == "tomorrow"
    assert command_spy.calls == []
    assert routed == [ResolvedSemanticQuery(
        intent=CALENDAR_AGENDA_TOMORROW_INTENT,
        request=SemanticQueryRequest(
            text=query_payload()["text"],
            account_context=WorkerAccountContext("explicit-principal", "explicit-account"),
            timezone="Europe/Tirane",
        ),
    )]


@pytest.mark.parametrize(
    "intent", ["calendar.agenda.today", "gmail.send", "calendar.agenda.tomorrow.extra"],
)
def test_unregistered_resolver_output_cannot_invoke_command(
    client: TestClient, container: ApplicationContainer, intent: str,
) -> None:
    container.semantic_resolver = RecordingIntentResolver(SemanticResolution.resolved(intent))
    spy = install_spy(container)
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported calendar agenda query"}
    assert spy.calls == []


def test_missing_semantic_route_fails_closed_but_structured_endpoint_is_unchanged(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    container.semantic_router = SemanticRouter((), {})
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 422
    assert spy.calls == []
    body = query_payload()
    del body["text"]
    body["intent"] = "tomorrow"
    response = client.post("/calendar/agenda", json=body)
    assert response.status_code == 200
    assert len(spy.calls) == 1


@pytest.mark.parametrize(
    "malformed",
    [
        "tomorrow",
        "calendar.agenda.tomorrow",
        {"status": "resolved", "intent": "calendar.agenda.tomorrow"},
        ("resolved", "calendar.agenda.tomorrow"),
        42,
    ],
)
def test_malformed_resolver_output_never_becomes_a_route(
    client: TestClient, container: ApplicationContainer, malformed: object,
) -> None:
    container.semantic_resolver = RecordingIntentResolver(malformed)
    spy = install_spy(container)
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "calendar agenda query resolution failed"}
    assert spy.calls == []


def test_unresolved_resolution_fails_closed_before_routing(
    client: TestClient, container: ApplicationContainer,
) -> None:
    container.semantic_resolver = RecordingIntentResolver(SemanticResolution.unresolved())
    spy = install_spy(container)
    routed: list[ResolvedSemanticQuery] = []

    def handler(query: ResolvedSemanticQuery) -> SemanticQueryResult:
        routed.append(query)
        return SemanticQueryResult(intent=query.intent, result=asdict(RESULT))

    route = container.semantic_router.resolve(CALENDAR_AGENDA_TOMORROW_INTENT)
    container.semantic_router = SemanticRouter((route,), {(route.role, route.capability): handler})
    response = client.post("/calendar/agenda/query", json=query_payload())
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported calendar agenda query"}
    assert routed == []
    assert spy.calls == []


def test_router_receives_canonical_intent_and_request_owned_context(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    dispatched: list[tuple[str, ResolvedSemanticQuery]] = []
    original_execute = container.semantic_router.execute

    def recording_execute(intent: str, request: ResolvedSemanticQuery) -> SemanticQueryResult:
        dispatched.append((intent, request))
        return original_execute(intent, request)

    container.semantic_router.execute = recording_execute  # type: ignore[method-assign]
    body = query_payload()
    body["account_context"] = {
        "principal": "request-principal", "account_identifier": "request-acct",
    }
    response = client.post("/calendar/agenda/query", json=body)
    assert response.status_code == 200
    account = WorkerAccountContext("request-principal", "request-acct")
    assert dispatched == [(CALENDAR_AGENDA_TOMORROW_INTENT, ResolvedSemanticQuery(
        intent=CALENDAR_AGENDA_TOMORROW_INTENT,
        request=SemanticQueryRequest(
            text=body["text"], account_context=account, timezone="Europe/Tirane",
        ),
    ))]
    assert spy.calls == [CalendarAgendaCommandRequest(
        intent="tomorrow", account_context=account, timezone="Europe/Tirane",
    )]


def test_default_composition_uses_bounded_resolver_without_external_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_external_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("external call attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)
    resolver = ApplicationContainer().semantic_resolver
    assert isinstance(resolver, BoundedCalendarAgendaIntentResolver)
    assert resolver.resolve("що в мене завтра?") == SemanticResolution.resolved(
        CALENDAR_AGENDA_TOMORROW_INTENT,
    )
