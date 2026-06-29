"""Planner contract for turning processed events into actions."""

from typing import Protocol

from apps.server.src.core.actions import Action
from apps.server.src.core.events import ProcessedEvent


class Planner(Protocol):
    """Contract for planning actions from processed events."""

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        """Return candidate actions for a processed event."""
        ...


class BasePlanner:
    """Base planner that intentionally produces no actions."""

    def plan(self, processed_event: ProcessedEvent) -> list[Action]:
        """Return an empty action list without side effects."""
        return []
