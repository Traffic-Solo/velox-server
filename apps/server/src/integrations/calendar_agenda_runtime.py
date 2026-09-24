"""Explicit live Calendar agenda composition and HTTP client ownership."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
from apps.server.src.core.config import get_settings
from apps.server.src.integrations.calendar import (
    CalendarEventListOrchestrator,
    CalendarProviderComposition,
    CalendarWorkerExecutor,
    HttpxCalendarTransportClient,
)
from apps.server.src.integrations.calendar_agenda import CalendarTomorrowAgendaWorkflow
from apps.server.src.integrations.calendar_agenda_command import CalendarAgendaCommandService
from apps.server.src.integrations.calendar_agenda_ollama import (
    OllamaCalendarAgendaIntentResolver,
)
from apps.server.src.integrations.calendar_agenda_query import (
    BoundedCalendarAgendaIntentResolver,
    CalendarAgendaIntentResolver,
)
from apps.server.src.integrations.google_oauth import StoredGoogleCredentialsProvider
from apps.server.src.integrations.keyring_credentials import MacOSKeychainCredentialStore


@contextmanager
def live_calendar_agenda_command_service(
    *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Iterator[CalendarAgendaCommandService]:
    """Own the live agenda HTTP client for exactly the caller's context lifetime.

    Explicit opt-in only. Account context still comes from each command request.
    Construction and execution failures both unwind the owned client.
    """
    with httpx.Client(timeout=10.0) as client:
        provider = CalendarProviderComposition(
            credentials_provider=StoredGoogleCredentialsProvider(
                MacOSKeychainCredentialStore(),
            ),
            transport_client=HttpxCalendarTransportClient(client),
        )
        yield CalendarAgendaCommandService(
            CalendarTomorrowAgendaWorkflow(
                CalendarEventListOrchestrator(CalendarWorkerExecutor(provider)),
            ),
            clock=clock,
        )


@contextmanager
def calendar_agenda_intent_resolver() -> Iterator[CalendarAgendaIntentResolver]:
    """Compose the configured resolver and own any local client it requires."""
    settings = get_settings()
    if settings.calendar_agenda_resolver == "bounded":
        yield BoundedCalendarAgendaIntentResolver()
        return
    with httpx.Client(timeout=10.0) as client:
        yield OllamaCalendarAgendaIntentResolver(
            client,
            base_url=settings.ollama_base_url,
            model=settings.ollama_model or "",
        )
