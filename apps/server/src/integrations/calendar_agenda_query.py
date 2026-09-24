"""Provider-neutral Calendar agenda query resolution."""

from collections.abc import Mapping
from typing import Protocol


class CalendarAgendaQueryValidationError(ValueError):
    """Raised when a free-form Calendar agenda query is structurally invalid."""


class CalendarAgendaIntentResolutionError(ValueError):
    """Raised when a non-blank Calendar agenda query is not supported."""


class CalendarAgendaIntentResolverExecutionError(RuntimeError):
    """Raised when a configured resolver cannot safely classify a query."""


class CalendarAgendaIntentResolver(Protocol):
    """Resolve bounded user text to an existing Calendar agenda intent."""

    def resolve(self, text: str) -> str:
        """Return a supported semantic Calendar agenda intent."""
        ...


class BoundedCalendarAgendaIntentResolver:
    """Resolve the fixed Slice 9 Calendar agenda phrase set."""

    _PHRASES: Mapping[str, str] = {
        "що в мене завтра": "tomorrow",
        "що у мене завтра": "tomorrow",  # noqa: RUF001
        "what do i have tomorrow": "tomorrow",
    }
    _TERMINAL_PUNCTUATION = "!?.,;"

    def resolve(self, text: str) -> str:
        """Resolve supported text without calling any provider or runtime."""
        normalized = text.strip().casefold().rstrip(self._TERMINAL_PUNCTUATION).strip()
        if not normalized:
            raise CalendarAgendaQueryValidationError(
                "calendar agenda query text is required",
            )
        if normalized not in self._PHRASES:
            raise CalendarAgendaIntentResolutionError(
                "calendar agenda query is unsupported",
            )
        return self._PHRASES[normalized]
