"""Calendar agenda adapters for the vendor-neutral semantic resolver Role."""

from collections.abc import Mapping

from apps.server.src.core.semantic import SemanticInputError, SemanticResolution

CALENDAR_AGENDA_TOMORROW_INTENT = "calendar.agenda.tomorrow"
"""Canonical semantic intent for the existing tomorrow agenda command."""

CALENDAR_AGENDA_COMMAND_INTENTS: Mapping[str, str] = {
    CALENDAR_AGENDA_TOMORROW_INTENT: "tomorrow",
}
"""Application-owned bridge from canonical intents to agenda command intents."""


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
