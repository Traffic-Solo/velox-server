"""The single explicit-approval transition shared by every approval entrypoint."""

from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.approvals import PendingApprovalRegistry


class PendingActionNotFoundError(LookupError):
    """No action with this id is awaiting approval."""


def approve_pending_action(
    action_id: UUID,
    *,
    pending_approval_registry: PendingApprovalRegistry,
    lifecycle_repository: ActionLifecycleRepository,
    lifecycle_manager: ActionLifecycleManager,
    action_queue: ActionQueue,
) -> ActionLifecycleState:
    """Approve one held action and move it to the execution queue.

    Raises ``PendingActionNotFoundError`` when nothing is pending under the id and
    ``ValueError`` when the lifecycle transition is not allowed.
    """
    action = pending_approval_registry.get(action_id)
    if action is None:
        raise PendingActionNotFoundError("action is not awaiting approval")
    lifecycle_state = lifecycle_repository.get(action_id)
    if lifecycle_state is None:
        lifecycle_state = ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        )
    approved_state = lifecycle_manager.transition(lifecycle_state, ActionStatus.APPROVED)
    lifecycle_repository.set(action_id, approved_state)
    pending_approval_registry.remove(action_id)
    action_queue.enqueue(action)
    return approved_state
