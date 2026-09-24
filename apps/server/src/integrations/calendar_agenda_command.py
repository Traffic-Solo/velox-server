"""Application boundary for already-recognized semantic Calendar commands."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from apps.server.src.integrations.calendar import (
    CalendarEventListOrchestrator,
    CalendarProviderComposition,
    CalendarWorkerExecutor,
    HttpxCalendarTransportClient,
)
from apps.server.src.integrations.calendar_agenda import (
    CALENDAR_TOMORROW_INTENT,
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaRequest,
    CalendarTomorrowAgendaResult,
    CalendarTomorrowAgendaWorkflow,
)
from apps.server.src.integrations.google_oauth import StoredGoogleCredentialsProvider
from apps.server.src.integrations.keyring_credentials import MacOSKeychainCredentialStore
from apps.server.src.workers.executor import WorkerAccountContext


@dataclass(frozen=True)
class CalendarAgendaCommandRequest:
    """Explicit semantic intent, account context and IANA timezone."""

    intent: str
    account_context: WorkerAccountContext
    timezone: str


class CalendarAgendaCommandService:
    """Supply an aware clock value and delegate to the existing workflow."""

    def __init__(
        self,
        workflow: CalendarTomorrowAgendaWorkflow,
        clock: Callable[[], datetime],
    ) -> None:
        self._workflow = workflow
        self._clock = clock

    def execute(
        self, request: CalendarAgendaCommandRequest,
    ) -> CalendarTomorrowAgendaResult:
        if request.intent != CALENDAR_TOMORROW_INTENT:
            raise CalendarAgendaWorkflowError(
                "calendar agenda intent is unsupported", field="intent",
            )
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise CalendarAgendaWorkflowError(
                "calendar agenda clock must return a timezone-aware datetime",
                field="now",
            )
        return self._workflow.execute(
            CalendarTomorrowAgendaRequest(
                intent=request.intent,
                account_context=request.account_context,
                timezone=request.timezone,
                now=now,
            )
        )


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
