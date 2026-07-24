"""Vendor-neutral application service for event workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.events.inbox import EventInbox
from apps.server.src.core.events.lifecycle import EventLifecycleState
from apps.server.src.core.events.lifecycle_manager import EventLifecycleManager
from apps.server.src.core.events.models import UniversalEvent
from apps.server.src.core.events.pipeline import (
    EventProcessingPipeline,
    IntegrationRouteContext,
    ProcessedEvent,
)
from apps.server.src.core.events.repository import EventRepository
from apps.server.src.core.permission import PermissionEngineRuntime, PermissionEvaluation

if TYPE_CHECKING:
    from apps.server.src.core.planner import Planner


class DuplicateEventError(Exception):
    """Raised when an event id has already been accepted."""


class EventNotFoundError(Exception):
    """Raised when an event workflow cannot find the requested event."""


class EventLifecycleConflictError(Exception):
    """Raised when an event lifecycle transition is invalid."""


class EventProcessingError(Exception):
    """Raised when the event processing pipeline fails."""


@dataclass(frozen=True)
class EventAcceptanceResult:
    """Result of accepting and storing one event."""

    event: UniversalEvent


@dataclass(frozen=True)
class EventProcessingResult:
    """Domain results needed by callers after processing one event."""

    processed_event: ProcessedEvent
    permission_evaluations: tuple[PermissionEvaluation, ...]


class EventWorkflowService:
    """Own event acceptance and processing application orchestration."""

    def __init__(
        self,
        *,
        event_repository: EventRepository,
        event_inbox: EventInbox,
        event_lifecycle_manager: EventLifecycleManager,
        event_lifecycle_states: dict[UUID, EventLifecycleState],
        event_processing_pipeline: EventProcessingPipeline,
        planner: Planner,
        permission_runtime: PermissionEngineRuntime,
        action_queue: ActionQueue,
    ) -> None:
        self.event_repository = event_repository
        self.event_inbox = event_inbox
        self.event_lifecycle_manager = event_lifecycle_manager
        self.event_lifecycle_states = event_lifecycle_states
        self.event_processing_pipeline = event_processing_pipeline
        self.planner = planner
        self.permission_runtime = permission_runtime
        self.action_queue = action_queue

    def accept(self, event: UniversalEvent) -> EventAcceptanceResult:
        """Accept, store, enqueue, and initialize one event."""
        if self.event_repository.get_event(event.id) is not None:
            raise DuplicateEventError(f"event {event.id} already exists")

        stored_event = self.event_repository.append(event)
        self.event_inbox.enqueue(stored_event)
        accepted_state = EventLifecycleState(
            event_id=stored_event.id,
            status="accepted",
        )
        pending_state = self.event_lifecycle_manager.transition(
            accepted_state,
            "pending",
        )
        self.event_lifecycle_states[stored_event.id] = pending_state
        return EventAcceptanceResult(event=stored_event)

    def process(
        self,
        event_id: UUID,
        integration_route: IntegrationRouteContext | None = None,
    ) -> EventProcessingResult:
        """Process one stored event and prepare its queueable actions."""
        event = self.event_repository.get_event(event_id)
        if event is None:
            raise EventNotFoundError(f"event {event_id} not found")

        lifecycle_state = self.event_lifecycle_states.get(event_id)
        if lifecycle_state is None:
            lifecycle_state = EventLifecycleState(event_id=event_id, status="pending")

        try:
            processing_state = self.event_lifecycle_manager.transition(
                lifecycle_state,
                "processing",
            )
        except ValueError as error:
            raise EventLifecycleConflictError(str(error)) from error

        self.event_lifecycle_states[event_id] = processing_state

        try:
            if integration_route is not None:
                processed_event = self.event_processing_pipeline.process(
                    event,
                    integration_route=integration_route,
                )
            else:
                processed_event = self.event_processing_pipeline.process(event)
        except Exception as error:
            self.event_lifecycle_states[event_id] = (
                self.event_lifecycle_manager.transition(
                    processing_state,
                    "failed",
                    reason=str(error),
                )
            )
            raise EventProcessingError("event processing failed") from error

        self.event_lifecycle_states[event_id] = (
            self.event_lifecycle_manager.transition(processing_state, "processed")
        )
        self.event_inbox.mark_processed(event_id)

        actions = self.planner.plan(processed_event)
        permission_evaluations = self.permission_runtime.evaluate(actions)
        self.action_queue.enqueue_many(
            self.permission_runtime.queueable_actions(permission_evaluations)
        )
        return EventProcessingResult(
            processed_event=processed_event,
            permission_evaluations=tuple(permission_evaluations),
        )
