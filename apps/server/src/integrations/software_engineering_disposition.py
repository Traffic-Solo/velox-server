"""Trusted exact-Action Software Engineering work-product disposition."""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringRunRepository,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductDispositionResult,
    WorkProductIdentityError,
)


class SoftwareEngineeringDispositionNotFoundError(LookupError):
    """No durable Software Engineering Action record exists for the requested id."""


class SoftwareEngineeringDispositionStateError(RuntimeError):
    """The Action is not in a state where disposition is permitted."""


class SoftwareEngineeringDispositionRouteError(RuntimeError):
    """Durable evidence is not the canonical Software Engineering route."""


class SoftwareEngineeringWorkProductDisposer(Protocol):
    """Provider-neutral work-product mutation boundary."""

    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        ...


class SoftwareEngineeringWorkProductDispositionService:
    """Apply KEEP/DISCARD using durable execution evidence."""

    _terminal_execution_statuses = frozenset({"succeeded", "failed", "skipped"})

    def __init__(
        self,
        *,
        run_repository: SoftwareEngineeringRunRepository,
        work_products: SoftwareEngineeringWorkProductDisposer | None,
    ) -> None:
        self._run_repository = run_repository
        self._work_products = work_products

    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        """Apply one exact disposition after durable route/state verification."""
        state = self._run_repository.get(action_id)
        if state is None:
            raise SoftwareEngineeringDispositionNotFoundError(
                "software engineering action was not found"
            )
        if (
            state.executor_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or state.capability != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringDispositionRouteError(
                "action was not executed through the software engineering route"
            )
        if (
            state.execution_status not in self._terminal_execution_statuses
            or state.execution_finished_at is None
        ):
            raise SoftwareEngineeringDispositionStateError(
                "software engineering action has not finished execution"
            )

        if self._work_products is None:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering work-product service is unavailable"
            )
        try:
            result = self._work_products.apply(action_id, disposition)
        except WorkProductIdentityError:
            raise SoftwareEngineeringDispositionStateError(
                "software engineering work product is unavailable or unverifiable"
            ) from None
        if result.succeeded:
            self._run_repository.record_disposition(result)
        return result
