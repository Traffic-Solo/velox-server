"""Deterministic user-facing Calendar agenda workflows."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.server.src.core.actions import Action
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CALENDAR_LIST_EVENTS_CAPABILITY,
    CALENDAR_LIST_MAX_MAX_PAGES,
    CALENDAR_LIST_MAX_MAX_RESULTS,
    CalendarEventListOrchestrator,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
)

CALENDAR_TOMORROW_INTENT = "tomorrow"


class CalendarAgendaWorkflowError(Exception):
    """Safe failure at the deterministic Calendar agenda boundary."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        category: WorkerExecutionFailureCategory | None = None,
    ) -> None:
        self.field = field
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class CalendarTomorrowAgendaRequest:
    """Explicit semantic, routing, timezone and clock inputs for tomorrow."""

    intent: str
    account_context: WorkerAccountContext
    timezone: str
    now: datetime


@dataclass(frozen=True)
class CalendarTomorrowAgendaResult:
    """Allowlisted workflow-level result for one local tomorrow agenda."""

    intent: str
    timezone: str
    local_date: str
    time_min: str
    time_max: str
    events: tuple[dict[str, Any], ...]
    event_count: int
    aggregate_complete: bool
    skipped_event_count: int
    termination_reason: str


class CalendarTomorrowAgendaWorkflow:
    """Resolve local tomorrow and invoke the bounded Calendar orchestrator."""

    def __init__(self, orchestrator: CalendarEventListOrchestrator) -> None:
        self._orchestrator = orchestrator

    def execute(
        self,
        request: CalendarTomorrowAgendaRequest,
    ) -> CalendarTomorrowAgendaResult:
        """Return the bounded agenda for the next local civil day."""
        timezone = self._validate_request(request)
        local_now = request.now.astimezone(timezone)
        local_date = local_now.date() + timedelta(days=1)
        following_local_date = local_date + timedelta(days=1)
        time_min = self._local_midnight(local_date, timezone)
        time_max = self._local_midnight(following_local_date, timezone)

        action = Action(
            type=CALENDAR_LIST_EVENTS_CAPABILITY.identifier,
            target="calendar-agenda-tomorrow",
            payload={
                "time_min": time_min,
                "time_max": time_max,
                "max_results": CALENDAR_LIST_MAX_MAX_RESULTS,
                "max_pages": CALENDAR_LIST_MAX_MAX_PAGES,
            },
            executor_role=CALENDAR_EXECUTOR_ROLE,
        )
        orchestration_result = self._orchestrator.execute(
            action,
            account_context=request.account_context,
        )
        if orchestration_result.status != WorkerExecutionStatus.SUCCEEDED:
            raise CalendarAgendaWorkflowError(
                "calendar tomorrow agenda orchestration failed",
                category=(
                    orchestration_result.failure.category
                    if orchestration_result.failure is not None
                    else WorkerExecutionFailureCategory.INTERNAL
                ),
            )

        metadata = orchestration_result.metadata
        events = metadata.get("events")
        event_count = metadata.get("event_count")
        aggregate_complete = metadata.get("aggregate_complete")
        skipped_event_count = metadata.get("skipped_event_count")
        termination_reason = metadata.get("termination_reason")
        if (
            not isinstance(events, tuple)
            or not all(isinstance(event, dict) for event in events)
            or type(event_count) is not int
            or event_count != len(events)
            or not isinstance(aggregate_complete, bool)
            or type(skipped_event_count) is not int
            or skipped_event_count < 0
            or termination_reason
            not in {"exhausted", "page_limit", "repeated_page_token"}
        ):
            raise CalendarAgendaWorkflowError(
                "calendar tomorrow agenda orchestration returned invalid data",
                category=WorkerExecutionFailureCategory.INTERNAL,
            )

        return CalendarTomorrowAgendaResult(
            intent=CALENDAR_TOMORROW_INTENT,
            timezone=request.timezone,
            local_date=local_date.isoformat(),
            time_min=time_min,
            time_max=time_max,
            events=cast(tuple[dict[str, Any], ...], events),
            event_count=event_count,
            aggregate_complete=aggregate_complete,
            skipped_event_count=skipped_event_count,
            termination_reason=termination_reason,
        )

    @staticmethod
    def _validate_request(request: CalendarTomorrowAgendaRequest) -> ZoneInfo:
        if request.intent != CALENDAR_TOMORROW_INTENT:
            raise CalendarAgendaWorkflowError(
                "calendar agenda intent is unsupported",
                field="intent",
            )
        if (
            not isinstance(request.timezone, str)
            or not request.timezone
            or request.timezone != request.timezone.strip()
        ):
            raise CalendarAgendaWorkflowError(
                "calendar agenda timezone must be a non-blank unpadded string",
                field="timezone",
            )
        try:
            timezone = ZoneInfo(request.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise CalendarAgendaWorkflowError(
                "calendar agenda timezone is not a valid IANA timezone",
                field="timezone",
            ) from None

        if not isinstance(request.now, datetime) or request.now.utcoffset() is None:
            raise CalendarAgendaWorkflowError(
                "calendar agenda now must be a timezone-aware datetime",
                field="now",
            )
        if not isinstance(request.account_context, WorkerAccountContext):
            raise CalendarAgendaWorkflowError(
                "calendar agenda account context is invalid",
                field="account_context",
            )
        for field_name, value in (
            ("principal", request.account_context.principal),
            ("account_identifier", request.account_context.account_identifier),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise CalendarAgendaWorkflowError(
                    "calendar agenda account context is invalid",
                    field=field_name,
                )
        return timezone

    @staticmethod
    def _local_midnight(local_date: date, timezone: ZoneInfo) -> str:
        return datetime.combine(
            local_date,
            time.min,
            tzinfo=timezone,
        ).isoformat(timespec="seconds")
