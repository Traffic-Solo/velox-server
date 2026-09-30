"""Software Engineering task control-plane HTTP ingress."""

import logging
from typing import Annotated
from uuid import UUID

from apps.server.src.api.dependencies import require_api_token
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.delegation import (
    TaskDelegationRequestError,
    TaskDelegationStatus,
)
from apps.server.src.core.permission import PermissionStatus
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"], dependencies=[Depends(require_api_token)])


class SoftwareEngineeringTaskHttpRequest(BaseModel):
    """Caller-owned task fields. Routing and execution authority are not accepted."""

    model_config = ConfigDict(extra="forbid")

    objective: str
    target: str


class SoftwareEngineeringTaskHttpResponse(BaseModel):
    """Governed delegation result without provider or worker internals."""

    action_id: UUID
    status: TaskDelegationStatus
    permission_status: PermissionStatus | None
    routing_reason: str | None


@router.post("/tasks/software-engineering")
def delegate_software_engineering_task(
    request: SoftwareEngineeringTaskHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringTaskHttpResponse:
    """Delegate one Software Engineering task through the existing governed path."""
    try:
        result = container.software_engineering_task_ingress.delegate(
            objective=request.objective,
            target=request.target,
        )
    except TaskDelegationRequestError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid software engineering task",
        ) from None
    except Exception:
        logger.exception("software engineering task delegation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering task delegation failed",
        ) from None

    return SoftwareEngineeringTaskHttpResponse(
        action_id=result.action_id,
        status=result.status,
        permission_status=result.permission_status,
        routing_reason=result.routing_reason,
    )
