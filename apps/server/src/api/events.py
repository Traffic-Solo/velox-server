"""Event API endpoints."""

from typing import Any

from fastapi import APIRouter, status

from apps.server.src.core.events import EventRepository, EventStore, UniversalEvent

router = APIRouter(prefix="/events", tags=["events"])
event_store: EventRepository = EventStore()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def accept_event(event: UniversalEvent) -> dict[str, str]:
    """Accept and store a valid event without processing it."""
    stored_event = event_store.append(event)
    return {
        "status": "accepted",
        "event_id": str(stored_event.id),
    }


@router.get("")
def list_events() -> list[dict[str, Any]]:
    """Return stored events in append order."""
    return [event.model_dump(mode="json") for event in event_store.list_events()]


@router.get("/schema")
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
