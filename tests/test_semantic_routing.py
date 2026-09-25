"""Deterministic semantic routing without providers, models or runtime execution."""

import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.semantic import SemanticRoute, SemanticRouter, SemanticRoutingError

AGENDA = SemanticRoute(
    "calendar.agenda.tomorrow", ExecutorRole.CONTEXT_PREPARATION, "list_calendar_events",
)
SUMMARY = SemanticRoute("content.summary", ExecutorRole.CONTENT_SUMMARY, "summarize_content")


def test_dispatches_two_roles_and_preserves_request_and_result_identity() -> None:
    request = object()
    agenda_result, summary_result = object(), object()
    calls: list[tuple[str, object]] = []

    def agenda(value: object) -> object:
        calls.append(("agenda", value))
        return agenda_result

    def summary(value: object) -> object:
        calls.append(("summary", value))
        return summary_result

    router = SemanticRouter[object, object](
        (AGENDA, SUMMARY),
        {
            (AGENDA.role, AGENDA.capability): agenda,
            (SUMMARY.role, SUMMARY.capability): summary,
        },
    )
    assert router.resolve(AGENDA.intent) is AGENDA
    assert calls == []
    assert router.execute(AGENDA.intent, request) is agenda_result
    assert router.execute(SUMMARY.intent, request) is summary_result
    assert calls == [("agenda", request), ("summary", request)]


@pytest.mark.parametrize("intent", [
    "", "tomorrow", "calendar.agenda.today", " calendar.agenda.tomorrow",
    "CALENDAR.AGENDA.TOMORROW", "gmail.send", "secret-provider-token",
])
def test_unknown_intent_never_executes_or_logs_input(
    intent: str, caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[object] = []
    router = SemanticRouter[object, None](
        (AGENDA,), {(AGENDA.role, AGENDA.capability): calls.append},
    )
    with caplog.at_level(logging.INFO), pytest.raises(SemanticRoutingError) as error:
        router.execute(intent, object())
    assert str(error.value) == "unsupported semantic intent"
    assert calls == []
    assert "secret-provider-token" not in caplog.text


def test_rejects_duplicate_intent_even_for_different_role() -> None:
    conflict = SemanticRoute(AGENDA.intent, SUMMARY.role, SUMMARY.capability)
    with pytest.raises(ValueError, match="duplicate semantic intent"):
        SemanticRouter[object, object](
            (AGENDA, conflict),
            {
                (AGENDA.role, AGENDA.capability): lambda request: request,
                (SUMMARY.role, SUMMARY.capability): lambda request: request,
            },
        )


@pytest.mark.parametrize("key", [
    (ExecutorRole.CONTENT_SUMMARY, AGENDA.capability),
    (AGENDA.role, "another_capability"),
])
def test_requires_exact_role_and_capability_handler(key: tuple[ExecutorRole, str]) -> None:
    with pytest.raises(ValueError, match="registered handler"):
        SemanticRouter[object, object]((AGENDA,), {key: lambda request: request})


def test_empty_table_fails_closed() -> None:
    router = SemanticRouter[object, object]((), {})
    with pytest.raises(SemanticRoutingError):
        router.execute(AGENDA.intent, object())


@pytest.mark.parametrize("intent, capability", [("", "read"), ("x", ""), (" x", "read")])
def test_rejects_invalid_route_identifiers(intent: str, capability: str) -> None:
    with pytest.raises(ValueError, match="identifiers"):
        SemanticRoute(intent, ExecutorRole.CONTENT_SUMMARY, capability)


def test_route_is_immutable_and_tables_are_snapshotted() -> None:
    routes = [AGENDA]
    handlers: dict[tuple[ExecutorRole, str], Callable[[str], str]] = {
        (AGENDA.role, AGENDA.capability): lambda request: request,
    }
    router = SemanticRouter[str, str](routes, handlers)
    routes.clear()
    handlers.clear()
    assert router.execute(AGENDA.intent, "unchanged") == "unchanged"
    with pytest.raises(FrozenInstanceError):
        router.resolve(AGENDA.intent).capability = "write"  # type: ignore[misc]


def test_handler_failure_is_not_retried_or_converted_to_success() -> None:
    calls: list[object] = []
    failure = RuntimeError("test failure")

    def fail(request: object) -> object:
        calls.append(request)
        raise failure

    router = SemanticRouter[object, object]((AGENDA,), {(AGENDA.role, AGENDA.capability): fail})
    request = object()
    with pytest.raises(RuntimeError) as error:
        router.execute(AGENDA.intent, request)
    assert error.value is failure
    assert calls == [request]
