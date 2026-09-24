"""Application boundary for already-recognized semantic Calendar commands."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from apps.server.src.integrations.calendar_agenda import (
    CALENDAR_TOMORROW_INTENT,
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaRequest,
    CalendarTomorrowAgendaResult,
    CalendarTomorrowAgendaWorkflow,
)
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
