"""Event API endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from apps.server.src.core.container import get_container
from apps.server.src.core.events import EventLifecycleState, UniversalEvent

router = APIRouter(tags=["events"])


@router.get("/actions/queue")
def list_action_queue() -> list[dict[str, Any]]:
    """Return currently queued actions without mutating the queue."""
    container = get_container()
    return [action.model_dump(mode="json") for action in container.action_queue.list()]


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
def accept_event(event: UniversalEvent) -> dict[str, str]:
    """Accept and store a valid event without processing it."""
    container = get_container()
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
def list_events() -> list[dict[str, Any]]:
    """Return stored events in append order."""
    container = get_container()
    return [
        event.model_dump(mode="json")
        for event in container.event_repository.list_events()
    ]


@router.get("/events/pending")
def list_pending_events() -> list[dict[str, Any]]:
    """Return pending inbox events in enqueue order."""
    container = get_container()
    return [event.model_dump(mode="json") for event in container.event_inbox.list_pending()]


@router.post("/events/{event_id}/process")
def process_event(event_id: UUID) -> dict[str, Any]:
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
        processed_event = container.event_processing_pipeline.process(event)
    except Exception as error:
        container.event_lifecycle_states[event_id] = (
            container.event_lifecycle_manager.transition(
                processing_state,
                "failed",
                reason=str(error),
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error

    container.event_lifecycle_states[event_id] = (
        container.event_lifecycle_manager.transition(processing_state, "processed")
    )
    container.event_inbox.mark_processed(event_id)

    actions = container.planner.plan(processed_event)
    container.action_queue.enqueue_many(actions)
    response = processed_event.model_dump(mode="json")
    response["actions"] = [
        action.model_dump(mode="json")
        for action in actions
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
