"""The single explicit-approval transition shared by every approval entrypoint."""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.core.approvals import PendingApprovalRegistry


class PendingActionNotFoundError(LookupError):
    """No action with this id is awaiting approval."""


class PendingActionRecovery(Protocol):
    """Optional recovery boundary for process-local pending approval state."""

    def recover_pending(self, action_id: UUID) -> Action | None:
        ...


class ApprovalDecisionRecorder(Protocol):
    """Optional durable recorder invoked before process-local approval mutation."""

    def record_approved(self, action: Action) -> None:
        ...

    def record_rejected(self, action: Action) -> None:
        ...


def _resolve_pending_action(
    action_id: UUID,
    *,
    pending_approval_registry: PendingApprovalRegistry,
    pending_action_recovery: PendingActionRecovery | None,
) -> Action:
    action = pending_approval_registry.get(action_id)
    if action is None and pending_action_recovery is not None:
        action = pending_action_recovery.recover_pending(action_id)
    if action is None:
        raise PendingActionNotFoundError("action is not awaiting approval")
    return action


def approve_pending_action(
    action_id: UUID,
    *,
    pending_approval_registry: PendingApprovalRegistry,
    lifecycle_repository: ActionLifecycleRepository,
    lifecycle_manager: ActionLifecycleManager,
    action_queue: ActionQueue,
    pending_action_recovery: PendingActionRecovery | None = None,
    approval_recorder: ApprovalDecisionRecorder | None = None,
) -> ActionLifecycleState:
    """Approve one held action and move it to the execution queue.

    Raises ``PendingActionNotFoundError`` when nothing is pending under the id and
    ``ValueError`` when the lifecycle transition is not allowed.
    """
    action = _resolve_pending_action(
        action_id,
        pending_approval_registry=pending_approval_registry,
        pending_action_recovery=pending_action_recovery,
    )
    lifecycle_state = lifecycle_repository.get(action_id)
    if lifecycle_state is None:
        lifecycle_state = ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        )
    approved_state = lifecycle_manager.transition(lifecycle_state, ActionStatus.APPROVED)
    if approval_recorder is not None:
        approval_recorder.record_approved(action)
    lifecycle_repository.set(action_id, approved_state)
    pending_approval_registry.remove(action_id)
    action_queue.enqueue(action)
    return approved_state



def reject_pending_action(
    action_id: UUID,
    *,
    pending_approval_registry: PendingApprovalRegistry,
    lifecycle_repository: ActionLifecycleRepository,
    lifecycle_manager: ActionLifecycleManager,
    reason: str,
    pending_action_recovery: PendingActionRecovery | None = None,
    approval_recorder: ApprovalDecisionRecorder | None = None,
) -> ActionLifecycleState:
    """Reject one pending Action, persisting the decision before local removal."""
    action = _resolve_pending_action(
        action_id,
        pending_approval_registry=pending_approval_registry,
        pending_action_recovery=pending_action_recovery,
    )
    lifecycle_state = lifecycle_repository.get(action_id)
    if lifecycle_state is None:
        lifecycle_state = ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        )
    rejected_state = lifecycle_manager.transition(
        lifecycle_state,
        ActionStatus.REJECTED,
        reason=reason,
    )
    if approval_recorder is not None:
        approval_recorder.record_rejected(action)
    lifecycle_repository.set(action_id, rejected_state)
    pending_approval_registry.remove(action_id)
    return rejected_state
