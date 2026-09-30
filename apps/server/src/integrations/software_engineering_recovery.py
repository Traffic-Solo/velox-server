"""Restart-safe approval and exact-claim recovery for Software Engineering Actions."""

from datetime import UTC, datetime
from uuid import UUID

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringRunRepository,
    SoftwareEngineeringRunState,
    SoftwareEngineeringRunStateError,
)


class SoftwareEngineeringRecoveryError(RuntimeError):
    """Durable SE approval or claim recovery failed closed."""


class SoftwareEngineeringRecoveryNotFoundError(LookupError):
    """No durable SE Action exists for the requested id."""


class SoftwareEngineeringActionRecovery:
    """Recover only canonical SE Actions from durable state."""

    def __init__(self, repository: SoftwareEngineeringRunRepository) -> None:
        self._repository = repository

    def recover_pending(self, action_id: UUID) -> Action | None:
        """Reconstruct one awaiting-approval SE Action after restart."""
        state = self._repository.get(action_id)
        if state is None:
            return None
        self._require_canonical_state(state)
        if (
            state.approval_status
            is not SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL
        ):
            return None
        if state.claim_id is not None or state.execution_status is not None:
            raise SoftwareEngineeringRecoveryError(
                "pending software engineering Action has execution evidence"
            )
        return self._action_from_state(state)

    def record_approved(self, action: Action) -> None:
        """Persist approval before process-local queue mutation."""
        if not self._is_canonical_action(action):
            return
        objective = self._objective(action)
        self._ensure_registered(action, objective)
        try:
            self._repository.record_approval(
                action_id=action.id,
                objective=objective,
                approved_at=datetime.now(UTC),
            )
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRecoveryError(
                "software engineering approval could not be persisted"
            ) from error

    def record_rejected(self, action: Action) -> None:
        """Persist rejection and erase the pending objective."""
        if not self._is_canonical_action(action):
            return
        objective = self._objective(action)
        self._ensure_registered(action, objective)
        try:
            self._repository.record_rejection(
                action_id=action.id,
                rejected_at=datetime.now(UTC),
            )
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRecoveryError(
                "software engineering rejection could not be persisted"
            ) from error

    def claim(self, action_id: UUID) -> Action:
        """Atomically claim one approved Action and reconstruct its exact envelope."""
        state = self._repository.get(action_id)
        if state is None:
            raise SoftwareEngineeringRecoveryNotFoundError(
                "software engineering action was not found"
            )
        self._require_canonical_state(state)
        try:
            claim = self._repository.claim_approved(action_id)
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRecoveryError(
                "software engineering action could not be claimed"
            ) from error
        return Action(
            id=claim.action_id,
            type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            target=claim.target,
            executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
            payload={
                "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
                "objective": claim.objective,
            },
            metadata={
                "task_delegation": {
                    "requested_role": ExecutorRole.SOFTWARE_ENGINEERING.value,
                    "requested_capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
                },
                "durable_recovery": {
                    "claim_id": str(claim.claim_id),
                },
            },
        )

    def _ensure_registered(self, action: Action, objective: str) -> None:
        current = self._repository.get(action.id)
        approval_status = (
            SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL
            if current is None
            else None
        )
        try:
            self._repository.register_action(
                action_id=action.id,
                target=action.target,
                executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
                capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
                objective=objective,
                approval_status=approval_status,
            )
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRecoveryError(
                "software engineering Action identity could not be persisted"
            ) from error

    @staticmethod
    def _is_canonical_action(action: Action) -> bool:
        return (
            action.executor_role == ExecutorRole.SOFTWARE_ENGINEERING
            and action.type == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
            and action.payload.get("capability")
            == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        )

    @staticmethod
    def _objective(action: Action) -> str:
        objective = action.payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise SoftwareEngineeringRecoveryError(
                "software engineering Action has no recoverable objective"
            )
        return objective.strip()

    @staticmethod
    def _require_canonical_state(state: SoftwareEngineeringRunState) -> None:
        if (
            state.executor_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or state.capability != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringRecoveryError(
                "durable Action is not the canonical Software Engineering route"
            )

    def _action_from_state(self, state: SoftwareEngineeringRunState) -> Action:
        objective = state.pending_objective
        if objective is None or not objective.strip():
            raise SoftwareEngineeringRecoveryError(
                "durable Software Engineering Action has no pending objective"
            )
        return Action(
            id=state.action_id,
            type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            target=state.target,
            executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
            payload={
                "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
                "objective": objective.strip(),
            },
            metadata={
                "task_delegation": {
                    "requested_role": ExecutorRole.SOFTWARE_ENGINEERING.value,
                    "requested_capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
                },
                "durable_recovery": {"pending": True},
            },
        )
