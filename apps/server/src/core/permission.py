"""Permission decision model."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    ActionLifecycleRepository,
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.actions import Action
from apps.server.src.core.approvals import (
    InMemoryPendingApprovalRegistry,
    PendingApprovalRegistry,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PermissionStatus(StrEnum):
    """Supported permission decision statuses."""

    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class PermissionDecision(BaseModel):
    """Immutable permission decision."""

    model_config = ConfigDict(frozen=True)

    status: PermissionStatus = PermissionStatus.ALLOWED
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_blank_reason(cls, value: str | None) -> str | None:
        """Normalize blank reasons to None."""
        if value is None:
            return None
        if not value.strip():
            return None
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        """Ensure created_at is timezone-aware UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("created_at must be in UTC")
        return value


class PermissionEngine(Protocol):
    """Contract for evaluating whether an action is permitted."""

    def evaluate(self, action: Action) -> PermissionDecision:
        """Return a permission decision for an action."""
        ...


class BasePermissionEngine:
    """Default approval-first permission engine.

    Only action types on the explicit safe list are auto-allowed. Every other
    action type requires explicit human approval before it may be queued for
    execution. This keeps the default posture safe-by-default instead of
    allow-by-default.
    """

    _safe_action_types: ClassVar[frozenset[str]] = frozenset(
        {
            "review_pull_request",
            "summarize_email",
            "prepare_meeting",
            "gmail.read",
        }
    )

    def evaluate(self, action: Action) -> PermissionDecision:
        """Return a permission decision without mutating or executing the action."""
        if action.type in self._safe_action_types:
            return PermissionDecision(
                status=PermissionStatus.ALLOWED,
                reason=(
                    f"action type '{action.type}' is on the safe list and is "
                    "auto-approved"
                ),
            )

        return PermissionDecision(
            status=PermissionStatus.REQUIRES_APPROVAL,
            reason=(
                f"action type '{action.type}' is not on the safe list and "
                "requires explicit approval"
            ),
        )


@dataclass(frozen=True)
class PermissionEvaluation:
    """Permission evaluation result for one planned action."""

    action: Action
    decision: PermissionDecision

    @property
    def is_allowed(self) -> bool:
        """Return whether the action may be queued."""
        return self.decision.status == PermissionStatus.ALLOWED


class PermissionEngineRuntime:
    """Evaluates planned actions before queueing without executing them."""

    def __init__(
        self,
        permission_engine: PermissionEngine,
        action_lifecycle_manager: ActionLifecycleManager,
        lifecycle_repository: ActionLifecycleRepository | None = None,
        pending_approval_registry: PendingApprovalRegistry | None = None,
    ) -> None:
        self._permission_engine = permission_engine
        self._action_lifecycle_manager = action_lifecycle_manager
        self._lifecycle_repository = (
            lifecycle_repository
            if lifecycle_repository is not None
            else InMemoryActionLifecycleRepository()
        )
        self._pending_approval_registry = (
            pending_approval_registry
            if pending_approval_registry is not None
            else InMemoryPendingApprovalRegistry()
        )

    def evaluate(self, actions: list[Action]) -> list[PermissionEvaluation]:
        """Return permission evaluations for planned actions."""
        return [self._evaluate_action(action) for action in actions]

    def queueable_actions(self, evaluations: list[PermissionEvaluation]) -> list[Action]:
        """Return only actions with allowed permission decisions."""
        return [
            evaluation.action
            for evaluation in evaluations
            if evaluation.is_allowed
        ]

    def pending_approval_actions(
        self, evaluations: list[PermissionEvaluation]
    ) -> list[Action]:
        """Return actions held for explicit approval."""
        return [
            evaluation.action
            for evaluation in evaluations
            if evaluation.decision.status == PermissionStatus.REQUIRES_APPROVAL
        ]

    def _evaluate_action(self, action: Action) -> PermissionEvaluation:
        decision = self._resolve_decision(action)
        lifecycle_state = self._resolve_lifecycle_state(decision)
        self._lifecycle_repository.set(action.id, lifecycle_state)
        evaluated_action = self._apply_permission_metadata(action, decision)
        if decision.status == PermissionStatus.REQUIRES_APPROVAL:
            self._pending_approval_registry.add(evaluated_action)
        return PermissionEvaluation(action=evaluated_action, decision=decision)

    def _resolve_decision(self, action: Action) -> PermissionDecision:
        try:
            decision = self._permission_engine.evaluate(action)
        except Exception:
            decision = None

        if isinstance(decision, PermissionDecision):
            return decision

        return PermissionDecision(
            status=PermissionStatus.DENIED,
            reason="permission decision could not be resolved",
        )

    def _resolve_lifecycle_state(
        self,
        decision: PermissionDecision,
    ) -> ActionLifecycleState:
        if decision.status == PermissionStatus.ALLOWED:
            queued_state = self._action_lifecycle_manager.transition(
                ActionLifecycleState(),
                ActionStatus.QUEUED,
            )
            return self._action_lifecycle_manager.transition(
                queued_state,
                ActionStatus.APPROVED,
            )

        if decision.status == PermissionStatus.REQUIRES_APPROVAL:
            return self._action_lifecycle_manager.transition(
                ActionLifecycleState(metadata={"approval_required": True}),
                ActionStatus.QUEUED,
            )

        return ActionLifecycleState(
            status=ActionStatus.REJECTED,
            reason=decision.reason,
            metadata={
                "permission_status": decision.status.value,
            },
        )

    def _apply_permission_metadata(
        self,
        action: Action,
        decision: PermissionDecision,
    ) -> Action:
        metadata = {
            **action.metadata,
            "permission_decision": decision.model_dump(mode="json"),
        }

        return action.model_copy(update={"metadata": metadata})
