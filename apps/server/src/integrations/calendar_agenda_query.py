"""Calendar agenda adapters for the semantic resolver Role and query ingress."""

from collections.abc import Callable, Mapping
from dataclasses import asdict

from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolution,
    SemanticRoutingError,
)
from apps.server.src.core.semantic_query import (
    ResolvedSemanticQuery,
    SemanticQueryResult,
    SemanticRequestError,
)
from apps.server.src.integrations.calendar_agenda import CalendarAgendaWorkflowError
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)

CALENDAR_AGENDA_TOMORROW_INTENT = "calendar.agenda.tomorrow"
"""Canonical semantic intent for the existing tomorrow agenda command."""

CALENDAR_AGENDA_COMMAND_INTENTS: Mapping[str, str] = {
    CALENDAR_AGENDA_TOMORROW_INTENT: "tomorrow",
}
"""Application-owned bridge from canonical intents to agenda command intents."""

CALENDAR_AGENDA_REQUEST_FIELDS = frozenset(
    {"intent", "timezone", "account_context", "principal", "account_identifier"},
)
"""Workflow validation fields that describe invalid caller request context."""


class BoundedCalendarAgendaIntentResolver:
    """Resolve the fixed Slice 9 Calendar agenda phrase set deterministically."""

    _PHRASES: Mapping[str, str] = {
        "що в мене завтра": CALENDAR_AGENDA_TOMORROW_INTENT,
        "що у мене завтра": CALENDAR_AGENDA_TOMORROW_INTENT,  # noqa: RUF001
        "what do i have tomorrow": CALENDAR_AGENDA_TOMORROW_INTENT,
    }
    _TERMINAL_PUNCTUATION = "!?.,;"

    def resolve(self, text: str) -> SemanticResolution:
        """Resolve supported text without calling any provider or runtime."""
        normalized = text.strip().casefold().rstrip(self._TERMINAL_PUNCTUATION).strip()
        if not normalized:
            raise SemanticInputError("calendar agenda query text is required")
        intent = self._PHRASES.get(normalized)
        if intent is None:
            return SemanticResolution.unresolved()
        return SemanticResolution.resolved(intent)


def calendar_agenda_semantic_handler(
    service: Callable[[], CalendarAgendaCommandService],
) -> Callable[[ResolvedSemanticQuery], SemanticQueryResult]:
    """Adapt the generic semantic query envelope to the agenda command path.

    ``service`` is read per call so lifespan-installed live composition applies.
    Account context and timezone come only from the caller-owned request.
    """

    def handle(query: ResolvedSemanticQuery) -> SemanticQueryResult:
        command_intent = CALENDAR_AGENDA_COMMAND_INTENTS.get(query.intent)
        if command_intent is None:
            raise SemanticRoutingError("semantic intent has no calendar agenda command")
        command = CalendarAgendaCommandRequest(
            intent=command_intent,
            account_context=query.request.account_context,
            timezone=query.request.timezone,
        )
        try:
            result = service().execute(command)
        except CalendarAgendaWorkflowError as error:
            if error.category is None and error.field in CALENDAR_AGENDA_REQUEST_FIELDS:
                raise SemanticRequestError("invalid calendar agenda request") from None
            raise
        return SemanticQueryResult(intent=query.intent, result=asdict(result))

    return handle
