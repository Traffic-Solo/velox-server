"""Offline coverage for the opt-in local Ollama agenda resolver."""

import json
from typing import Any

import httpx
import pytest
from apps.server.src.core.config import get_settings
from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolution,
    SemanticResolverError,
)
from apps.server.src.integrations.calendar_agenda_ollama import (
    OllamaCalendarAgendaIntentResolver,
)
from apps.server.src.integrations.calendar_agenda_query import (
    CALENDAR_AGENDA_TOMORROW_INTENT,
    BoundedCalendarAgendaIntentResolver,
)
from apps.server.src.integrations.calendar_agenda_runtime import (
    calendar_agenda_semantic_resolver,
)


def make_resolver(
    payload: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    error: Exception | None = None,
) -> tuple[OllamaCalendarAgendaIntentResolver, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error is not None:
            raise error
        return httpx.Response(status_code, json=payload or {})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        OllamaCalendarAgendaIntentResolver(
            client,
            base_url="http://127.0.0.1:11434",
            model="qwen-local",
        ),
        requests,
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_urls_are_accepted(host: str) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    resolver = OllamaCalendarAgendaIntentResolver(
        client, base_url=f"http://[{host}]:11434" if host == "::1" else f"http://{host}:11434",
        model="local-model",
    )
    assert resolver is not None
    client.close()


@pytest.mark.parametrize("base_url", ["http://example.com:11434", "https://10.0.0.2"])
def test_non_loopback_urls_are_rejected_before_http_call(base_url: str) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("HTTP called")))
    with pytest.raises(ValueError, match="loopback"):
        OllamaCalendarAgendaIntentResolver(client, base_url=base_url, model="local-model")
    client.close()


def test_valid_tomorrow_result_sends_native_chat_request() -> None:
    resolver, requests = make_resolver(
        {"message": {"content": '{"intent":"tomorrow"}'}},
    )
    assert resolver.resolve("які зустрічі завтра") == SemanticResolution.resolved(
        CALENDAR_AGENDA_TOMORROW_INTENT,
    )
    request = requests[0]
    assert request.url.path == "/api/chat"
    sent = json.loads(request.content)
    assert sent["model"] == "qwen-local"
    assert sent["stream"] is False
    assert sent["options"]["temperature"] == 0
    assert sent["format"]["properties"]["intent"]["enum"] == ["tomorrow", "unsupported"]
    assert sent["messages"][1] == {"role": "user", "content": "які зустрічі завтра"}
    system_prompt = sent["messages"][0]["content"]
    assert "schedule, plans, meetings, availability, or calendar for tomorrow" in system_prompt
    assert "any language or natural paraphrase" in system_prompt
    assert "today, another date, creating or changing events" in system_prompt


def test_unsupported_result_is_an_explicit_unresolved_resolution() -> None:
    resolver, _ = make_resolver({"message": {"content": '{"intent":"unsupported"}'}})
    assert resolver.resolve("am I busy tomorrow?") == SemanticResolution.unresolved()


@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"content": "not-json"}},
        {"message": {"content": '{"intent":"today"}'}},
        {"message": {"content": '{"intent":"tomorrow","extra":"nope"}'}},
    ],
)
def test_malformed_or_schema_invalid_results_are_safe(payload: dict[str, Any]) -> None:
    resolver, _ = make_resolver(payload)
    with pytest.raises(SemanticResolverError):
        resolver.resolve("show my calendar tomorrow")


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("timeout"), httpx.ConnectError("connection")],
)
def test_transport_failures_are_safe(error: Exception) -> None:
    resolver, _ = make_resolver(error=error)
    with pytest.raises(SemanticResolverError) as raised:
        resolver.resolve("show my calendar tomorrow")
    assert str(raised.value) == "calendar agenda query resolution failed"


def test_http_failure_is_safe_and_details_do_not_leak() -> None:
    resolver, _ = make_resolver({"error": "provider-secret"}, status_code=503)
    with pytest.raises(SemanticResolverError) as raised:
        resolver.resolve("show my calendar tomorrow")
    assert str(raised.value) == "calendar agenda query resolution failed"
    assert "provider-secret" not in str(raised.value)


def test_blank_input_does_not_call_http() -> None:
    resolver, requests = make_resolver()
    with pytest.raises(SemanticInputError):
        resolver.resolve("  ")
    assert requests == []


def test_ollama_mode_composes_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class TrackingClient:
        closed = False

        def __enter__(self) -> "TrackingClient":
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

    tracking = TrackingClient()
    monkeypatch.setattr(
        "apps.server.src.integrations.calendar_agenda_runtime.httpx.Client",
        lambda **_: tracking,
    )
    monkeypatch.setenv("VELOX_CALENDAR_AGENDA_RESOLVER", "ollama")
    monkeypatch.setenv("VELOX_OLLAMA_MODEL", "configured-model")
    get_settings.cache_clear()
    with calendar_agenda_semantic_resolver() as resolver:
        assert isinstance(resolver, OllamaCalendarAgendaIntentResolver)
    assert tracking.closed is True
    get_settings.cache_clear()


def test_default_mode_composes_bounded_adapter_without_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.server.src.integrations.calendar_agenda_runtime.httpx.Client",
        lambda **_: pytest.fail("HTTP client created in default resolver mode"),
    )
    with calendar_agenda_semantic_resolver() as resolver:
        assert isinstance(resolver, BoundedCalendarAgendaIntentResolver)
