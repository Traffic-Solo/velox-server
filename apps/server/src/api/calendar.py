"""Structured Calendar agenda HTTP adapter."""

import logging
from typing import Annotated, Any

from apps.server.src.api.dependencies import require_api_token
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolverError,
    SemanticRoutingError,
)
from apps.server.src.core.semantic_query import (
    SemanticQueryRequest,
    SemanticRequestError,
    execute_semantic_query,
)
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaResult,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
)
from apps.server.src.integrations.calendar_agenda_query import (
    CALENDAR_AGENDA_COMMAND_INTENTS,
    CALENDAR_AGENDA_REQUEST_FIELDS,
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


def _account_context(request: CalendarAccountContextRequest) -> WorkerAccountContext:
    return WorkerAccountContext(
        principal=request.principal,
        account_identifier=request.account_identifier,
    )


def _execution_failed() -> HTTPException:
    logger.error("calendar agenda execution failed")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="calendar agenda execution failed",
    )


def _invalid_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="invalid calendar agenda request",
    )


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
                account_context=_account_context(request.account_context),
                timezone=request.timezone,
            ),
        )
    except CalendarAgendaWorkflowError as error:
        if error.category is None and error.field in CALENDAR_AGENDA_REQUEST_FIELDS:
            raise _invalid_request() from None
        raise _execution_failed() from None
    except Exception:
        raise _execution_failed() from None


@router.post(
    "/calendar/agenda/query",
    response_model=CalendarTomorrowAgendaResult,
    deprecated=True,
)
def calendar_agenda_query(
    request: CalendarAgendaQueryHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> dict[str, Any]:
    """Compatibility adapter over the canonical ``POST /semantic/query`` path.

    Uses the same resolver, route table and handler, restricted to Calendar
    agenda intents, and keeps this endpoint's historical response and errors.
    """
    try:
        result = execute_semantic_query(
            SemanticQueryRequest(
                text=request.text,
                account_context=_account_context(request.account_context),
                timezone=request.timezone,
            ),
            resolver=container.semantic_resolver,
            router=container.semantic_router,
            allowed_intents=CALENDAR_AGENDA_COMMAND_INTENTS.keys(),
        )
    except SemanticInputError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid calendar agenda query",
        ) from None
    except SemanticRoutingError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unsupported calendar agenda query",
        ) from None
    except SemanticRequestError:
        raise _invalid_request() from None
    except SemanticResolverError:
        logger.error("calendar agenda query resolution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="calendar agenda query resolution failed",
        ) from None
    except Exception:
        raise _execution_failed() from None
    return dict(result.result)
