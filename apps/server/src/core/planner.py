"""Planner contract for turning processed events into actions."""

from typing import Protocol

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.events import ProcessedEvent


class Planner(Protocol):
    """Contract for planning actions from processed events."""

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        """Return candidate actions for a processed event."""
        ...


class BasePlanner:
    """Deterministic rule-based planner for known event categories."""

    _action_types_by_category = {
        "github": "review_pull_request",
        "gmail": "summarize_email",
        "calendar": "prepare_meeting",
    }
    _executor_roles_by_category = {
        "github": ExecutorRole.CONTENT_REVIEW,
        "gmail": ExecutorRole.CONTENT_SUMMARY,
        "calendar": ExecutorRole.CONTEXT_PREPARATION,
    }

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        """Return candidate actions without executing them."""
        action_type = self._action_types_by_category.get(
            processed_event.classification.category
        )
        if action_type is None:
            return []

        return [
            Action(
                type=action_type,
                target=str(processed_event.event.id),
                executor_role=self._executor_roles_by_category[
                    processed_event.classification.category
                ],
                metadata={
                    "event_id": str(processed_event.event.id),
                    "category": processed_event.classification.category,
                },
            )
        ]
