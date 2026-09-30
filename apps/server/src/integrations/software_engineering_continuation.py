"""Exact-Action Software Engineering execution and review continuation."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_recovery import (
    SoftwareEngineeringActionRecovery,
    SoftwareEngineeringRecoveryError,
    SoftwareEngineeringRecoveryNotFoundError,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductIdentityError,
    WorkProductReview,
)
from apps.server.src.workers.executor import WorkerExecutionStatus
from apps.server.src.workers.runtime import WorkerRuntime


class SoftwareEngineeringContinuationNotFoundError(LookupError):
    """No governed Action lifecycle exists for the requested id."""


class SoftwareEngineeringContinuationStateError(RuntimeError):
    """The Action is not in the exact state required for continuation."""


class SoftwareEngineeringContinuationRouteError(RuntimeError):
    """The Action is not the canonical Software Engineering implementation route."""


class WorkProductReviewStatus(StrEnum):
    """Whether a verified work-product review could be returned."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class SoftwareEngineeringWorkProductReviewer(Protocol):
    """Provider-neutral review boundary used after execution."""

    def review(self, action_id: UUID) -> WorkProductReview:
        ...


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringContinuationResult:
    """Provider-neutral execution outcome plus optional verified work-product review."""

    action_id: UUID
    processed: bool
    execution_status: WorkerExecutionStatus
    lifecycle_status: ActionStatus
    execution_reason: str | None
    external_execution_performed: bool
    review_status: WorkProductReviewStatus
    review: WorkProductReview | None


class SoftwareEngineeringTaskContinuation:
    """Execute exactly one approved SE Action, then review its derived worktree."""

    def __init__(
        self,
        *,
        action_queue: ActionQueue,
        lifecycle_repository: ActionLifecycleRepository,
        worker_runtime: WorkerRuntime,
        action_recovery: SoftwareEngineeringActionRecovery,
        work_product_reviewer: SoftwareEngineeringWorkProductReviewer | None,
    ) -> None:
        self._action_queue = action_queue
        self._lifecycle_repository = lifecycle_repository
        self._worker_runtime = worker_runtime
        self._action_recovery = action_recovery
        self._work_product_reviewer = work_product_reviewer

    def execute(self, action_id: UUID) -> SoftwareEngineeringContinuationResult:
        """Durably claim and continue one approved canonical SE Action by UUID."""
        lifecycle = self._lifecycle_repository.get(action_id)
        queued = self._action_queue.get(action_id)
        if lifecycle is not None and lifecycle.status not in {
            ActionStatus.QUEUED,
            ActionStatus.APPROVED,
        }:
            raise SoftwareEngineeringContinuationStateError(
                "software engineering action is not approved for execution"
            )
        if queued is not None:
            self._require_canonical_action(queued)

        try:
            recovered = self._action_recovery.claim(action_id)
        except SoftwareEngineeringRecoveryNotFoundError:
            raise SoftwareEngineeringContinuationNotFoundError(
                "software engineering action was not found"
            ) from None
        except SoftwareEngineeringRecoveryError:
            raise SoftwareEngineeringContinuationStateError(
                "software engineering action could not be claimed for execution"
            ) from None

        if queued is None:
            action = recovered
            self._action_queue.enqueue(action)
        else:
            action = queued
            if (
                action.target != recovered.target
                or action.payload.get("objective")
                != recovered.payload.get("objective")
            ):
                raise SoftwareEngineeringContinuationRouteError(
                    "queued Action conflicts with durable Software Engineering identity"
                )

        if lifecycle is None or lifecycle.status is ActionStatus.QUEUED:
            lifecycle = ActionLifecycleState(
                status=ActionStatus.APPROVED,
                metadata={
                    "approval_required": True,
                    "durable_recovery": True,
                },
            )
            self._lifecycle_repository.set(action_id, lifecycle)

        processing = self._worker_runtime.process_action(action_id)
        if (
            not processing.processed
            or processing.execution_status is None
            or processing.lifecycle_state is None
        ):
            raise SoftwareEngineeringContinuationStateError(
                "software engineering action could not be claimed for execution"
            )

        review: WorkProductReview | None = None
        review_status = WorkProductReviewStatus.UNAVAILABLE
        if self._work_product_reviewer is not None:
            try:
                review = self._work_product_reviewer.review(action_id)
            except WorkProductIdentityError:
                review = None
            else:
                review_status = WorkProductReviewStatus.AVAILABLE

        execution_reason = None
        if processing.execution_status is WorkerExecutionStatus.FAILED:
            execution_reason = "worker execution failed"
        elif processing.execution_status is WorkerExecutionStatus.SKIPPED:
            execution_reason = "worker execution skipped"

        return SoftwareEngineeringContinuationResult(
            action_id=action_id,
            processed=True,
            execution_status=processing.execution_status,
            lifecycle_status=processing.lifecycle_state.status,
            execution_reason=execution_reason,
            external_execution_performed=processing.external_execution_performed,
            review_status=review_status,
            review=review,
        )


    @staticmethod
    def _require_canonical_action(action: object) -> None:
        if not hasattr(action, "executor_role"):
            raise SoftwareEngineeringContinuationRouteError(
                "queued action is not a Software Engineering Action"
            )
        typed = action
        if (
            typed.executor_role != ExecutorRole.SOFTWARE_ENGINEERING
            or typed.type != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
            or typed.payload.get("capability")
            != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringContinuationRouteError(
                "action is not the canonical software engineering implementation route"
            )
