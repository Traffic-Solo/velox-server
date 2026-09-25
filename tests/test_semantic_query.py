"""Offline coverage for the domain-neutral semantic query ingress."""

import ast
import json
import socket
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from apps.server.src import main
from apps.server.src.api import calendar as calendar_api
from apps.server.src.api import semantic as semantic_api
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.semantic import SemanticResolution, SemanticRoute, SemanticRouter
from apps.server.src.core.semantic_query import (
    ResolvedSemanticQuery,
    SemanticQueryRequest,
    SemanticQueryResult,
    execute_semantic_query,
)
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaResult,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)
from apps.server.src.integrations.calendar_agenda_query import (
    CALENDAR_AGENDA_TOMORROW_INTENT,
    calendar_agenda_semantic_handler,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import WorkerAccountContext
from fastapi.testclient import TestClient

SRC = Path(__file__).resolve().parents[1] / "apps" / "server" / "src"
RESULT = CalendarTomorrowAgendaResult(
    intent="tomorrow", timezone="Europe/Tirane", local_date="2026-03-29",
    time_min="2026-03-29T00:00:00+01:00", time_max="2026-03-30T00:00:00+02:00",
    events=({"event_id": "event-1", "title": "Planning", "start": "09:00",
             "end": "10:00", "attendees": ("a@example.test",)},),
    event_count=1, aggregate_complete=True, skipped_event_count=0,
    termination_reason="exhausted",
)
SUMMARY_ROUTE = SemanticRoute("content.summary", ExecutorRole.CONTENT_SUMMARY, "summarize")


class RecordingResolver:
    """Vendor-neutral resolver double; ``result`` may be deliberately malformed."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(self, text: str) -> SemanticResolution:
        self.calls.append(text)
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
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def install_spy(
    container: ApplicationContainer, error: Exception | None = None,
) -> RecordingCommandService:
    spy = RecordingCommandService(error)
    container.calendar_agenda_command_service = cast(CalendarAgendaCommandService, spy)
    return spy


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "text": "що в мене завтра?",
        "account_context": {"principal": "caller-principal", "account_identifier": "caller-acct"},
        "timezone": "Europe/Tirane",
    }
    body.update(overrides)
    return body


def add_summary_route(container: ApplicationContainer) -> list[ResolvedSemanticQuery]:
    """Register a second, non-Calendar test route next to the production one."""
    calls: list[ResolvedSemanticQuery] = []

    def summarize(query: ResolvedSemanticQuery) -> SemanticQueryResult:
        calls.append(query)
        return SemanticQueryResult(intent=query.intent, result={"summary": "ok"})

    agenda = container.semantic_router.resolve(CALENDAR_AGENDA_TOMORROW_INTENT)
    agenda_handler = calendar_agenda_semantic_handler(
        lambda: container.calendar_agenda_command_service,
    )
    container.semantic_router = SemanticRouter(
        (agenda, SUMMARY_ROUTE),
        {
            (agenda.role, agenda.capability): agenda_handler,
            (SUMMARY_ROUTE.role, SUMMARY_ROUTE.capability): summarize,
        },
    )
    return calls


def test_generic_ingress_resolves_calendar_agenda_into_neutral_envelope(
    client: TestClient, container: ApplicationContainer,
) -> None:
    spy = install_spy(container)
    response = client.post("/semantic/query", json=payload())
    assert response.status_code == 200
    assert response.json() == {
        "intent": CALENDAR_AGENDA_TOMORROW_INTENT,
        "result": json.loads(json.dumps(asdict(RESULT))),
    }
    assert spy.calls == [CalendarAgendaCommandRequest(
        intent="tomorrow",
        account_context=WorkerAccountContext("caller-principal", "caller-acct"),
        timezone="Europe/Tirane",
    )]


def test_generic_and_legacy_ingress_return_the_same_agenda(
    client: TestClient, container: ApplicationContainer,
) -> None:
    install_spy(container)
    generic = client.post("/semantic/query", json=payload())
    legacy = client.post("/calendar/agenda/query", json=payload())
    assert generic.status_code == legacy.status_code == 200
    assert generic.json()["result"] == legacy.json()


@pytest.mark.parametrize(
    "body",
    [
        payload(provider="google"),
        payload(intent=CALENDAR_AGENDA_TOMORROW_INTENT),
        payload(handler="calendar"),
        {"text": "що в мене завтра?", "timezone": "Europe/Tirane"},
        {"text": "що в мене завтра?", "account_context": payload()["account_context"]},
        payload(account_context={"account_identifier": "caller-acct"}),
        payload(account_context={**payload()["account_context"], "credentials": "x"}),
    ],
)
def test_generic_request_accepts_only_caller_owned_context(
    client: TestClient, container: ApplicationContainer, body: dict[str, Any],
) -> None:
    spy = install_spy(container)
    assert client.post("/semantic/query", json=body).status_code == 422
    assert spy.calls == []


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_text_is_invalid(
    client: TestClient, container: ApplicationContainer, text: str,
) -> None:
    spy = install_spy(container)
    response = client.post("/semantic/query", json=payload(text=text))
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid semantic query"}
    assert spy.calls == []


@pytest.mark.parametrize(
    "resolution",
    [
        SemanticResolution.unresolved(),
        SemanticResolution.resolved("calendar.agenda.today"),
        SemanticResolution.resolved("gmail.send"),
    ],
)
def test_unresolved_or_unregistered_intents_fail_closed(
    client: TestClient, container: ApplicationContainer, resolution: SemanticResolution,
) -> None:
    container.semantic_resolver = RecordingResolver(resolution)
    spy = install_spy(container)
    response = client.post("/semantic/query", json=payload())
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported semantic query"}
    assert spy.calls == []


@pytest.mark.parametrize(
    "malformed",
    [
        "calendar.agenda.tomorrow",
        {"status": "resolved", "intent": "calendar.agenda.tomorrow"},
        None,
    ],
)
def test_malformed_resolver_output_cannot_execute_a_handler(
    client: TestClient, container: ApplicationContainer, malformed: object,
) -> None:
    container.semantic_resolver = RecordingResolver(malformed)
    spy = install_spy(container)
    response = client.post("/semantic/query", json=payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "semantic query resolution failed"}
    assert spy.calls == []


def test_resolver_sees_only_text_and_cannot_change_caller_context(
    client: TestClient, container: ApplicationContainer,
) -> None:
    resolver = RecordingResolver(SemanticResolution.resolved(CALENDAR_AGENDA_TOMORROW_INTENT))
    container.semantic_resolver = resolver
    spy = install_spy(container)
    response = client.post("/semantic/query", json=payload(text="anything at all"))
    assert response.status_code == 200
    assert resolver.calls == ["anything at all"]
    assert spy.calls[0].account_context == WorkerAccountContext("caller-principal", "caller-acct")
    assert spy.calls[0].timezone == "Europe/Tirane"


def test_handler_rejection_of_caller_context_is_a_safe_422(
    client: TestClient, container: ApplicationContainer,
) -> None:
    install_spy(container, CalendarAgendaWorkflowError("bad secret-zone", field="timezone"))
    response = client.post("/semantic/query", json=payload())
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid semantic query request"}
    assert "secret-zone" not in response.text


def test_handler_failure_is_a_safe_500(
    client: TestClient, container: ApplicationContainer,
) -> None:
    install_spy(container, RuntimeError("provider secret detail"))
    response = client.post("/semantic/query", json=payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "semantic query execution failed"}
    assert "provider secret detail" not in response.text


def test_generic_ingress_dispatches_any_declared_route_but_legacy_stays_calendar_only(
    client: TestClient, container: ApplicationContainer,
) -> None:
    summary_calls = add_summary_route(container)
    container.semantic_resolver = RecordingResolver(SemanticResolution.resolved("content.summary"))
    spy = install_spy(container)

    legacy = client.post("/calendar/agenda/query", json=payload())
    assert legacy.status_code == 422
    assert legacy.json() == {"detail": "unsupported calendar agenda query"}
    assert summary_calls == []

    generic = client.post("/semantic/query", json=payload())
    assert generic.status_code == 200
    assert generic.json() == {"intent": "content.summary", "result": {"summary": "ok"}}
    assert len(summary_calls) == 1
    assert spy.calls == []


def test_both_ingresses_share_the_single_semantic_query_path(
    client: TestClient, container: ApplicationContainer,
) -> None:
    install_spy(container)
    with (
        patch.object(semantic_api, "execute_semantic_query", wraps=execute_semantic_query) as a,
        patch.object(calendar_api, "execute_semantic_query", wraps=execute_semantic_query) as b,
    ):
        assert client.post("/semantic/query", json=payload()).status_code == 200
        assert client.post("/calendar/agenda/query", json=payload()).status_code == 200
    assert a.call_count == b.call_count == 1
    for module in ("api/semantic.py", "api/calendar.py"):
        assert "semantic_resolver.resolve" not in (SRC / module).read_text()
        assert "semantic_router.execute" not in (SRC / module).read_text()


def imported_modules(relative: str) -> set[str]:
    tree = ast.parse((SRC / relative).read_text())
    return {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }


@pytest.mark.parametrize(
    "relative", ["api/semantic.py", "core/semantic_query.py", "core/semantic.py"],
)
def test_generic_boundary_does_not_depend_on_integrations(relative: str) -> None:
    assert not any(".integrations" in module for module in imported_modules(relative))


def test_public_contract_has_no_calendar_schema(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/semantic/query"]["post"]
    rendered = json.dumps(operation)
    assert "Calendar" not in rendered
    assert client.get("/openapi.json").json()["paths"]["/calendar/agenda/query"]["post"][
        "deprecated"
    ] is True


def test_execute_rejects_handler_results_for_another_intent() -> None:
    route = SemanticRoute(CALENDAR_AGENDA_TOMORROW_INTENT, ExecutorRole.CONTEXT_PREPARATION, "x")
    router = SemanticRouter[ResolvedSemanticQuery, SemanticQueryResult](
        (route,),
        {(route.role, route.capability): lambda q: SemanticQueryResult("gmail.send", {})},
    )
    request = SemanticQueryRequest("t", WorkerAccountContext("p", "a"), "UTC")
    with pytest.raises(RuntimeError, match="invalid result"):
        execute_semantic_query(
            request,
            resolver=RecordingResolver(SemanticResolution.resolved(route.intent)),
            router=router,
        )


def test_default_composition_answers_without_external_calls(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, container: ApplicationContainer,
) -> None:
    def fail_external_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("external call attempted")

    install_spy(container)
    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)
    assert client.post("/semantic/query", json=payload()).status_code == 200


def test_explicit_ollama_opt_in_serves_the_generic_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def ollama(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": '{"intent":"tomorrow"}'}})

    real_client = httpx.Client
    monkeypatch.setattr(
        "apps.server.src.integrations.calendar_agenda_runtime.httpx.Client",
        lambda **_: real_client(transport=httpx.MockTransport(ollama)),
    )
    monkeypatch.setenv("VELOX_CALENDAR_AGENDA_RESOLVER", "ollama")
    monkeypatch.setenv("VELOX_OLLAMA_MODEL", "local-model")
    get_settings.cache_clear()
    container = ApplicationContainer()
    spy = install_spy(container)
    app.dependency_overrides[get_container] = lambda: container
    try:
        with patch.object(main, "get_container", return_value=container), TestClient(app) as client:
            response = client.post("/semantic/query", json=payload(text="які плани на завтра"))
    finally:
        del app.dependency_overrides[get_container]
    assert response.status_code == 200
    assert response.json()["intent"] == CALENDAR_AGENDA_TOMORROW_INTENT
    assert len(requests) == 1
    assert len(spy.calls) == 1
