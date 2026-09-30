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
from apps.server.src.integrations.software_engineering_disposition import (
    SoftwareEngineeringDispositionNotFoundError,
    SoftwareEngineeringDispositionRouteError,
    SoftwareEngineeringDispositionStateError,
)
from apps.server.src.integrations.software_engineering_promotion import (
    SoftwareEngineeringPromotionExternalError,
    SoftwareEngineeringPromotionNotFoundError,
    SoftwareEngineeringPromotionStateError,
)
from apps.server.src.integrations.software_engineering_run_control import (
    SoftwareEngineeringClaimReconciliation,
    SoftwareEngineeringRunControlNotFoundError,
    SoftwareEngineeringRunControlStateError,
    SoftwareEngineeringRunPhase,
    SoftwareEngineeringRunStatus,
    WorkProductInspectionStatus,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringClaimResolution,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
)
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class SoftwareEngineeringDispositionHttpRequest(BaseModel):
    """Caller chooses only KEEP or DISCARD; work-product identity remains trusted."""

    model_config = ConfigDict(extra="forbid")

    disposition: WorkProductDisposition


class SoftwareEngineeringDispositionHttpResponse(BaseModel):
    """Provider-neutral disposition result without path or branch disclosure."""

    action_id: UUID
    disposition: WorkProductDisposition
    succeeded: bool
    worktree_present: bool
    branch_present: bool
    canonical_unchanged: bool
    remaining: list[str]


class SoftwareEngineeringPromotionHttpRequest(BaseModel):
    """Caller supplies PR copy only; git and provider authority remain trusted."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10_000)

    @field_validator("title")
    @classmethod
    def require_non_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("promotion title must not be blank")
        return value


class SoftwareEngineeringPromotionHttpResponse(BaseModel):
    """Safe promotion result after VELOX-owned commit, push and PR publication."""

    action_id: UUID
    commit_sha: str
    pull_request_number: int
    pull_request_url: str
    base_branch: str
    head_branch: str
    pull_request_created: bool


class SoftwareEngineeringRunWorkProductHttpResponse(BaseModel):
    """Safe ambiguous-claim work-product summary."""

    status: WorkProductInspectionStatus
    dirty: bool | None
    changed_files_count: int | None
    untracked_files_count: int | None
    canonical_clean: bool | None
    canonical_unchanged: bool | None


class SoftwareEngineeringRunStatusHttpResponse(BaseModel):
    """Provider-neutral durable Software Engineering status."""

    action_id: UUID
    target: str
    phase: SoftwareEngineeringRunPhase
    approval_status: SoftwareEngineeringApprovalStatus | None
    claimed: bool
    worker_started: bool
    execution_status: str | None
    external_execution_performed: bool | None
    disposition: WorkProductDisposition | None
    claim_resolution: SoftwareEngineeringClaimResolution | None
    promoted: bool
    pull_request_number: int | None
    pull_request_url: str | None
    promotion_base_branch: str | None
    promotion_head_branch: str | None
    reconciliation_options: list[SoftwareEngineeringClaimReconciliation]
    retriable: bool
    state_token: str
    work_product: SoftwareEngineeringRunWorkProductHttpResponse


class SoftwareEngineeringClaimReconciliationHttpRequest(BaseModel):
    """Operator claim decision with optimistic-concurrency state token."""

    model_config = ConfigDict(extra="forbid")

    resolution: SoftwareEngineeringClaimReconciliation
    state_token: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    acknowledge_possible_external_side_effects: bool = False


class SoftwareEngineeringClaimReconciliationHttpResponse(BaseModel):
    """Applied reconciliation plus the resulting safe run status."""

    resolution: SoftwareEngineeringClaimReconciliation
    run: SoftwareEngineeringRunStatusHttpResponse


def _run_status_http(
    result: SoftwareEngineeringRunStatus,
) -> SoftwareEngineeringRunStatusHttpResponse:
    summary = result.work_product
    return SoftwareEngineeringRunStatusHttpResponse(
        action_id=result.action_id,
        target=result.target,
        phase=result.phase,
        approval_status=result.approval_status,
        claimed=result.claimed,
        worker_started=result.worker_started,
        execution_status=result.execution_status,
        external_execution_performed=result.external_execution_performed,
        disposition=result.disposition,
        claim_resolution=result.claim_resolution,
        promoted=result.promoted,
        pull_request_number=result.pull_request_number,
        pull_request_url=result.pull_request_url,
        promotion_base_branch=result.promotion_base_branch,
        promotion_head_branch=result.promotion_head_branch,
        reconciliation_options=list(result.reconciliation_options),
        retriable=result.retriable,
        state_token=result.state_token,
        work_product=SoftwareEngineeringRunWorkProductHttpResponse(
            status=summary.status,
            dirty=summary.dirty,
            changed_files_count=summary.changed_files_count,
            untracked_files_count=summary.untracked_files_count,
            canonical_clean=summary.canonical_clean,
            canonical_unchanged=summary.canonical_unchanged,
        ),
    )


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


@router.post("/tasks/software-engineering/{action_id}/work-product/disposition")
def dispose_software_engineering_work_product(
    action_id: UUID,
    request: SoftwareEngineeringDispositionHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringDispositionHttpResponse:
    """Apply one trusted KEEP/DISCARD decision to an executed SE Action."""
    try:
        result = container.software_engineering_work_product_disposition.apply(
            action_id,
            request.disposition,
        )
    except SoftwareEngineeringDispositionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except (
        SoftwareEngineeringDispositionStateError,
        SoftwareEngineeringDispositionRouteError,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="software engineering work product is not disposable",
        ) from None
    except Exception:
        logger.exception("software engineering work-product disposition failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering work-product disposition failed",
        ) from None

    return SoftwareEngineeringDispositionHttpResponse(
        action_id=result.action_id,
        disposition=result.disposition,
        succeeded=result.succeeded,
        worktree_present=result.worktree_present,
        branch_present=result.branch_present,
        canonical_unchanged=result.canonical_unchanged,
        remaining=list(result.remaining),
    )


@router.post("/tasks/software-engineering/{action_id}/promote")
def promote_software_engineering_work_product(
    action_id: UUID,
    request: SoftwareEngineeringPromotionHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringPromotionHttpResponse:
    """Promote one explicitly kept SE work product under VELOX authority."""
    try:
        result = container.software_engineering_promotion.promote(
            action_id,
            title=request.title,
            body=request.body,
        )
    except SoftwareEngineeringPromotionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except SoftwareEngineeringPromotionStateError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="software engineering work product is not promotable",
        ) from None
    except SoftwareEngineeringPromotionExternalError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="software engineering promotion failed",
        ) from None
    except Exception:
        logger.exception("software engineering promotion failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering promotion failed",
        ) from None

    return SoftwareEngineeringPromotionHttpResponse(
        action_id=result.action_id,
        commit_sha=result.commit_sha,
        pull_request_number=result.pull_request_number,
        pull_request_url=result.pull_request_url,
        base_branch=result.base_branch,
        head_branch=result.head_branch,
        pull_request_created=result.pull_request_created,
    )



@router.get("/tasks/software-engineering/{action_id}/status")
def get_software_engineering_run_status(
    action_id: UUID,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringRunStatusHttpResponse:
    """Return durable SE status without provider or local workspace internals."""
    try:
        result = container.software_engineering_run_control.status(action_id)
    except SoftwareEngineeringRunControlNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except SoftwareEngineeringRunControlStateError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="software engineering run status is unavailable",
        ) from None
    except Exception:
        logger.exception("software engineering run status failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering run status failed",
        ) from None
    return _run_status_http(result)


@router.post("/tasks/software-engineering/{action_id}/claim/reconcile")
def reconcile_software_engineering_claim(
    action_id: UUID,
    request: SoftwareEngineeringClaimReconciliationHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SoftwareEngineeringClaimReconciliationHttpResponse:
    """Reconcile one stranded claim without invoking WorkerRuntime."""
    try:
        result = container.software_engineering_run_control.reconcile(
            action_id,
            resolution=request.resolution,
            state_token=request.state_token,
            acknowledge_possible_external_side_effects=(
                request.acknowledge_possible_external_side_effects
            ),
        )
    except SoftwareEngineeringRunControlNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except SoftwareEngineeringRunControlStateError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="software engineering claim is not reconcilable",
        ) from None
    except Exception:
        logger.exception("software engineering claim reconciliation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="software engineering claim reconciliation failed",
        ) from None
    return SoftwareEngineeringClaimReconciliationHttpResponse(
        resolution=request.resolution,
        run=_run_status_http(result),
    )
