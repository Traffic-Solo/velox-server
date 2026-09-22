"""Structured Calendar agenda HTTP adapter."""

import logging
from typing import Annotated

from apps.server.src.api.events import require_api_token
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaResult,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
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


@router.post("/calendar/agenda")
def calendar_agenda(
    request: CalendarAgendaHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> CalendarTomorrowAgendaResult:
    """Delegate a structured command and expose only safe HTTP failures."""
    try:
        return container.calendar_agenda_command_service.execute(
            CalendarAgendaCommandRequest(
                intent=request.intent,
                account_context=WorkerAccountContext(
                    principal=request.account_context.principal,
                    account_identifier=request.account_context.account_identifier,
                ),
                timezone=request.timezone,
            )
        )
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
