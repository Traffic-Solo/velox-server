"""Permission decision model."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.actions import Action


class PermissionStatus(StrEnum):
    """Supported permission decision statuses."""

    ALLOWED = "allowed"
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
    """Default permission engine that allows actions without side effects."""

    def evaluate(self, action: Action) -> PermissionDecision:
        """Return an allowed decision without mutating or executing the action."""
        return PermissionDecision(status=PermissionStatus.ALLOWED)


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
    ) -> None:
        self._permission_engine = permission_engine
        self._action_lifecycle_manager = action_lifecycle_manager

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

    def _evaluate_action(self, action: Action) -> PermissionEvaluation:
        decision = self._resolve_decision(action)
        lifecycle_state = self._resolve_lifecycle_state(decision)
        evaluated_action = self._apply_permission_metadata(
            action,
            decision,
            lifecycle_state,
        )
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
            return self._action_lifecycle_manager.transition(
                ActionLifecycleState(),
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
        lifecycle_state: ActionLifecycleState,
    ) -> Action:
        status = "pending"
        if decision.status == PermissionStatus.DENIED:
            status = "rejected"

        metadata = {
            **action.metadata,
            "permission_decision": decision.model_dump(mode="json"),
            "action_lifecycle": lifecycle_state.model_dump(mode="json"),
        }

        return action.model_copy(
            update={
                "status": status,
                "metadata": metadata,
            },
        )
