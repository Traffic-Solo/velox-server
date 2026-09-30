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
from apps.server.src.integrations.software_engineering_continuation import (
    SoftwareEngineeringContinuationNotFoundError,
    SoftwareEngineeringContinuationRouteError,
    SoftwareEngineeringContinuationStateError,
    WorkProductReviewStatus,
)
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


class SoftwareEngineeringWorkProductReviewHttpResponse(BaseModel):
    """Bounded git-generated review without exposing local filesystem paths."""

    changed_files: list[str]
    untracked_files: list[str]
    diff_stat: str
    diff: str
    diff_truncated: bool
    dirty: bool
    canonical_clean: bool
    canonical_unchanged: bool


class SoftwareEngineeringExecutionHttpResponse(BaseModel):
    """Provider-neutral exact-Action execution and review result."""

    action_id: UUID
    processed: bool
    execution_status: str
    lifecycle_status: str
    execution_reason: str | None
    external_execution_performed: bool
    review_status: WorkProductReviewStatus
    review: SoftwareEngineeringWorkProductReviewHttpResponse | None


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


@router.post("/tasks/software-engineering/{action_id}/execute")
def execute_software_engineering_task(
    action_id: UUID,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringExecutionHttpResponse:
    """Execute one exact approved Software Engineering Action and review its work."""
    try:
        result = container.software_engineering_task_continuation.execute(action_id)
    except SoftwareEngineeringContinuationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except (
        SoftwareEngineeringContinuationStateError,
        SoftwareEngineeringContinuationRouteError,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="software engineering action is not executable",
        ) from None
    except Exception:
        logger.exception("software engineering task continuation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering task continuation failed",
        ) from None

    review = result.review
    review_response = (
        SoftwareEngineeringWorkProductReviewHttpResponse(
            changed_files=list(review.changed_files),
            untracked_files=list(review.untracked_files),
            diff_stat=review.diff_stat,
            diff=review.diff,
            diff_truncated=review.diff_truncated,
            dirty=review.dirty,
            canonical_clean=review.canonical_clean,
            canonical_unchanged=review.canonical_unchanged,
        )
        if review is not None
        else None
    )
    return SoftwareEngineeringExecutionHttpResponse(
        action_id=result.action_id,
        processed=result.processed,
        execution_status=result.execution_status.value,
        lifecycle_status=result.lifecycle_status.value,
        execution_reason=result.execution_reason,
        external_execution_performed=result.external_execution_performed,
        review_status=result.review_status,
        review=review_response,
    )
