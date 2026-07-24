"""Planner contract for turning processed events into actions."""

from typing import Any, ClassVar, Protocol

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.events import ProcessedEvent


class Planner(Protocol):
    """Contract for planning actions from processed events."""

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        """Return candidate actions for a processed event."""
        ...


class BasePlanner:
    """Deterministic rule-based planner for known event categories."""

    _action_types_by_category: ClassVar[dict[str, str]] = {
        "github": "review_pull_request",
        "gmail": "summarize_email",
        "calendar": "prepare_meeting",
    }
    _executor_roles_by_category: ClassVar[dict[str, ExecutorRole]] = {
        "github": ExecutorRole.CONTENT_REVIEW,
        "gmail": ExecutorRole.CONTENT_SUMMARY,
        "calendar": ExecutorRole.CONTEXT_PREPARATION,
    }

    @staticmethod
    def _build_action_payload(processed_event: ProcessedEvent) -> dict[str, Any]:
        """Build explicit domain and integration routing inputs for an action."""
        payload: dict[str, Any] = {}
        if (
            processed_event.classification.category == "calendar"
            and "calendar_event_id" in processed_event.event.payload
        ):
            payload["calendar_event_id"] = processed_event.event.payload[
                "calendar_event_id"
            ]

        if processed_event.integration_route is not None:
            payload["capability_provider"] = processed_event.integration_route.provider
            payload["account_context"] = {
                "principal": processed_event.integration_route.principal,
                "account_identifier": (
                    processed_event.integration_route.account_identifier
                ),
            }

        return payload

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
                payload=self._build_action_payload(processed_event),
                executor_role=self._executor_roles_by_category[
                    processed_event.classification.category
                ],
                metadata={
                    "event_id": str(processed_event.event.id),
                    "category": processed_event.classification.category,
                },
            )
        ]
