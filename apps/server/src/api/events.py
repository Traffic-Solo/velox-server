"""Event API endpoints."""

import logging
from typing import Annotated, Any
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import get_container
from apps.server.src.core.events import (
    EventLifecycleState,
    IntegrationRouteContext,
    UniversalEvent,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require a bearer token on every API route when VELOX_API_TOKEN is set."""
    expected_token = get_settings().api_token
    if expected_token is None:
        return
    if authorization != f"Bearer {expected_token}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
        )


router = APIRouter(tags=["events"], dependencies=[Depends(require_api_token)])


class RejectActionRequest(BaseModel):
    """Optional request body for rejecting a pending action."""

    reason: str | None = None


class ProcessEventRequest(BaseModel):
    """Explicit processing inputs supplied separately from the stored event."""

    integration_route: IntegrationRouteContext | None = None


@router.get("/actions/queue")
def list_action_queue() -> list[dict[str, Any]]:
    """Return currently queued actions without mutating the queue."""
    container = get_container()
    return [action.model_dump(mode="json") for action in container.action_queue.list()]


@router.get("/actions/pending-approval")
def list_pending_approval_actions() -> list[dict[str, Any]]:
    """Return actions held for explicit approval, with their lifecycle state."""
    container = get_container()
    return [
        {
            "action": action.model_dump(mode="json"),
            "lifecycle": (
                lifecycle.model_dump(mode="json")
                if (
                    lifecycle := container.action_lifecycle_repository.get(action.id)
                )
                is not None
                else None
            ),
        }
        for action in container.pending_approval_registry.list_pending()
    ]


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: UUID) -> dict[str, Any]:
    """Approve a pending action and move it to the execution queue."""
    container = get_container()
    action = container.pending_approval_registry.get(action_id)
    if action is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    lifecycle_state = container.action_lifecycle_repository.get(action_id)
    if lifecycle_state is None:
        lifecycle_state = ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        )

    try:
        approved_state = container.action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.APPROVED,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    container.action_lifecycle_repository.set(action_id, approved_state)
    container.pending_approval_registry.remove(action_id)
    container.action_queue.enqueue(action)
    return {
        "status": "approved",
        "action_id": str(action_id),
        "lifecycle": approved_state.model_dump(mode="json"),
    }


@router.post("/actions/{action_id}/reject")
def reject_action(
    action_id: UUID,
    body: RejectActionRequest | None = None,
) -> dict[str, Any]:
    """Reject a pending action so it never reaches the execution queue."""
    container = get_container()
    action = container.pending_approval_registry.get(action_id)
    if action is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    lifecycle_state = container.action_lifecycle_repository.get(action_id)
    if lifecycle_state is None:
        lifecycle_state = ActionLifecycleState(
            status=ActionStatus.QUEUED,
            metadata={"approval_required": True},
        )

    reason = body.reason if body is not None and body.reason else "rejected by user"
    try:
        rejected_state = container.action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.REJECTED,
            reason=reason,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    container.action_lifecycle_repository.set(action_id, rejected_state)
    container.pending_approval_registry.remove(action_id)
    return {
        "status": "rejected",
        "action_id": str(action_id),
        "lifecycle": rejected_state.model_dump(mode="json"),
    }


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
def accept_event(event: UniversalEvent) -> dict[str, str]:
    """Accept and store a valid event without processing it."""
    container = get_container()
    if container.event_repository.get_event(event.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"event {event.id} already exists",
        )
    stored_event = container.event_repository.append(event)
    container.event_inbox.enqueue(stored_event)
    accepted_state = EventLifecycleState(
        event_id=stored_event.id,
        status="accepted",
    )
    container.event_lifecycle_states[stored_event.id] = (
        container.event_lifecycle_manager.transition(accepted_state, "pending")
    )
    return {
        "status": "accepted",
        "event_id": str(stored_event.id),
    }


@router.get("/events")
def list_events(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """Return stored events in append order, paginated."""
    container = get_container()
    events = container.event_repository.list_events()
    return [
        event.model_dump(mode="json") for event in events[offset : offset + limit]
    ]


@router.get("/events/pending")
def list_pending_events() -> list[dict[str, Any]]:
    """Return pending inbox events in enqueue order."""
    container = get_container()
    return [event.model_dump(mode="json") for event in container.event_inbox.list_pending()]


@router.post("/events/{event_id}/process")
def process_event(
    event_id: UUID,
    body: ProcessEventRequest | None = None,
) -> dict[str, Any]:
    """Manually process one stored event and remove it from the pending inbox."""
    container = get_container()
    event = container.event_repository.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    lifecycle_state = container.event_lifecycle_states.get(event_id)
    if lifecycle_state is None:
        lifecycle_state = EventLifecycleState(event_id=event_id, status="pending")

    try:
        processing_state = container.event_lifecycle_manager.transition(
            lifecycle_state,
            "processing",
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    container.event_lifecycle_states[event_id] = processing_state

    try:
        if body is not None and body.integration_route is not None:
            processed_event = container.event_processing_pipeline.process(
                event,
                integration_route=body.integration_route,
            )
        else:
            processed_event = container.event_processing_pipeline.process(event)
    except Exception as error:
        container.event_lifecycle_states[event_id] = (
            container.event_lifecycle_manager.transition(
                processing_state,
                "failed",
                reason=str(error),
            )
        )
        logger.exception("event %s failed processing", event_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="event processing failed",
        ) from error

    container.event_lifecycle_states[event_id] = (
        container.event_lifecycle_manager.transition(processing_state, "processed")
    )
    container.event_inbox.mark_processed(event_id)

    actions = container.planner.plan(processed_event)
    permission_evaluations = container.permission_runtime.evaluate(actions)
    container.action_queue.enqueue_many(
        container.permission_runtime.queueable_actions(permission_evaluations)
    )
    response = processed_event.model_dump(mode="json")
    response["actions"] = [
        evaluation.action.model_dump(mode="json")
        for evaluation in permission_evaluations
    ]
    response["permission_decisions"] = [
        {
            "action_id": str(evaluation.action.id),
            "decision": evaluation.decision.model_dump(mode="json"),
            "lifecycle": (
                action_lifecycle.model_dump(mode="json")
                if (
                    action_lifecycle := container.action_lifecycle_repository.get(
                        evaluation.action.id
                    )
                )
                is not None
                else None
            ),
        }
        for evaluation in permission_evaluations
    ]
    return response


@router.get("/events/schema")
def read_event_schema() -> dict[str, Any]:
    """Return the public schema contract for the Universal Event Model."""
    sample_event = UniversalEvent(
        source="velox.api",
        type="event.schema.sample",
        payload={"example": True},
        metadata={"description": "Sample UniversalEvent for schema introspection."},
    )

    return {
        "model_name": "UniversalEvent",
        "fields": list(UniversalEvent.model_fields),
        "sample_event": sample_event.model_dump(mode="json"),
        "normalizer_contract": (
            "EventNormalizer defines normalize(raw_event) -> UniversalEvent. "
            "BaseEventNormalizer validates mapping-like input, copies it into "
            "payload, and records the normalizer class name in metadata."
        ),
    }


@router.get("/events/{event_id}")
def read_event(event_id: UUID) -> dict[str, Any]:
    """Return one stored event by id."""
    container = get_container()
    event = container.event_repository.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return event.model_dump(mode="json")


@router.get("/events/{event_id}/lifecycle")
def read_event_lifecycle(event_id: UUID) -> dict[str, Any]:
    """Return the lifecycle state of one stored event."""
    container = get_container()
    if container.event_repository.get_event(event_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    lifecycle_state = container.event_lifecycle_states.get(event_id)
    if lifecycle_state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no lifecycle state recorded for this event",
        )
    return lifecycle_state.model_dump(mode="json")
