"""Trusted exact-Action Software Engineering work-product disposition."""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductDispositionResult,
    WorkProductIdentityError,
)
from apps.server.src.workers.runtime import WorkerExecutionObservation


class SoftwareEngineeringDispositionNotFoundError(LookupError):
    """No governed Action lifecycle exists for the requested id."""


class SoftwareEngineeringDispositionStateError(RuntimeError):
    """The Action is not in a state where disposition is permitted."""


class SoftwareEngineeringDispositionRouteError(RuntimeError):
    """Execution evidence is not the canonical Software Engineering route."""


class SoftwareEngineeringWorkProductDisposer(Protocol):
    """Provider-neutral work-product mutation boundary."""

    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        ...


class WorkerExecutionEvidenceSource(Protocol):
    """Provider-neutral source of worker execution observations."""

    def list(self) -> list[WorkerExecutionObservation]:
        ...


class SoftwareEngineeringWorkProductDispositionService:
    """Apply KEEP/DISCARD only to a verified executed Software Engineering Action."""

    _terminal_statuses = frozenset(
        {
            ActionStatus.COMPLETED,
            ActionStatus.FAILED,
            ActionStatus.SKIPPED,
        }
    )

    def __init__(
        self,
        *,
        lifecycle_repository: ActionLifecycleRepository,
        execution_observer: WorkerExecutionEvidenceSource,
        work_products: SoftwareEngineeringWorkProductDisposer | None,
    ) -> None:
        self._lifecycle_repository = lifecycle_repository
        self._execution_observer = execution_observer
        self._work_products = work_products

    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        """Apply one exact disposition after trusted route and state verification."""
        lifecycle = self._lifecycle_repository.get(action_id)
        if lifecycle is None:
            raise SoftwareEngineeringDispositionNotFoundError(
                "software engineering action was not found"
            )
        if lifecycle.status not in self._terminal_statuses:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering action has not finished execution"
            )

        observation = next(
            (
                item
                for item in reversed(self._execution_observer.list())
                if item.action_id == action_id
            ),
            None,
        )
        if observation is None or observation.finished_at is None:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering execution evidence is unavailable"
            )
        if (
            observation.requested_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or observation.requested_capability
            != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringDispositionRouteError(
                "action was not executed through the software engineering route"
            )

        if self._work_products is None:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering work-product service is unavailable"
            )
        try:
            return self._work_products.apply(action_id, disposition)
        except WorkProductIdentityError:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering work product is unavailable or unverifiable"
            ) from None
