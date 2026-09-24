"""Structured Calendar agenda HTTP adapter."""

import logging
from typing import Annotated

from apps.server.src.api.dependencies import require_api_token
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaResult,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
)
from apps.server.src.integrations.calendar_agenda_query import (
    CalendarAgendaIntentResolutionError,
    CalendarAgendaIntentResolverExecutionError,
    CalendarAgendaQueryValidationError,
)
from apps.server.src.workers.executor import WorkerAccountContext
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
router = APIRouter(tags=["calendar"], dependencies=[Depends(require_api_token)])


class CalendarAccountContextRequest(BaseModel):
    """Explicit account fields; no account discovery or defaults."""

    model_config = ConfigDict(extra="forbid")

    principal: str
    account_identifier: str


class CalendarAgendaHttpRequest(BaseModel):
    """Structured semantic command; the service owns the clock."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    account_context: CalendarAccountContextRequest
    timezone: str


class CalendarAgendaQueryHttpRequest(BaseModel):
    """Bounded free-form query with explicit account and timezone context."""

    model_config = ConfigDict(extra="forbid")

    text: str
    account_context: CalendarAccountContextRequest
    timezone: str


def _execute_calendar_agenda(
    request: CalendarAgendaCommandRequest,
    container: ApplicationContainer,
) -> CalendarTomorrowAgendaResult:
    """Execute a recognized command and expose only safe HTTP failures."""
    try:
        return container.calendar_agenda_command_service.execute(request)
    except CalendarAgendaWorkflowError as error:
        if error.category is None and error.field in {
            "intent", "timezone", "account_context", "principal", "account_identifier",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid calendar agenda request",
            ) from None
        logger.error("calendar agenda execution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="calendar agenda execution failed",
        ) from None
    except Exception:
        logger.error("calendar agenda execution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="calendar agenda execution failed",
        ) from None


@router.post("/calendar/agenda")
def calendar_agenda(
    request: CalendarAgendaHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> CalendarTomorrowAgendaResult:
    """Delegate a structured command and expose only safe HTTP failures."""
    return _execute_calendar_agenda(
        CalendarAgendaCommandRequest(
            intent=request.intent,
            account_context=WorkerAccountContext(
                principal=request.account_context.principal,
                account_identifier=request.account_context.account_identifier,
            ),
            timezone=request.timezone,
        ),
        container,
    )


@router.post("/calendar/agenda/query")
def calendar_agenda_query(
    request: CalendarAgendaQueryHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> CalendarTomorrowAgendaResult:
    """Resolve a bounded query, then delegate the recognized command."""
    try:
        intent = container.calendar_agenda_intent_resolver.resolve(request.text)
    except CalendarAgendaQueryValidationError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid calendar agenda query",
        ) from None
    except CalendarAgendaIntentResolutionError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unsupported calendar agenda query",
        ) from None
    except CalendarAgendaIntentResolverExecutionError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="calendar agenda query resolution failed",
        ) from None
    return _execute_calendar_agenda(
        CalendarAgendaCommandRequest(
            intent=intent,
            account_context=WorkerAccountContext(
                principal=request.account_context.principal,
                account_identifier=request.account_context.account_identifier,
            ),
            timezone=request.timezone,
        ),
        container,
    )
