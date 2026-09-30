"""Event API endpoints."""

import logging
from typing import Annotated, Any
from uuid import UUID

from apps.server.src.api.dependencies import require_api_token
from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.approval_decisions import (
    PendingActionNotFoundError,
    approve_pending_action,
    reject_pending_action,
)
from apps.server.src.core.container import get_container
from apps.server.src.core.events import (
    DuplicateEventError,
    EventLifecycleConflictError,
    EventNotFoundError,
    EventProcessingError,
    IntegrationRouteContext,
    UniversalEvent,
)
from apps.server.src.integrations.software_engineering_recovery import (
    SoftwareEngineeringRecoveryError,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
    """Return process-local plus restart-recovered SE approval work."""
    container = get_container()
    actions = list(container.pending_approval_registry.list_pending())
    known = {action.id for action in actions}
    try:
        for action in container.software_engineering_action_recovery.list_pending():
            if action.id not in known:
                actions.append(action)
                known.add(action.id)
    except SoftwareEngineeringRecoveryError:
        logger.exception("software engineering pending approval recovery failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="pending approval recovery failed",
        ) from None

    response: list[dict[str, Any]] = []
    for action in actions:
        lifecycle = container.action_lifecycle_repository.get(action.id)
        if lifecycle is None:
            lifecycle = ActionLifecycleState(
                status=ActionStatus.QUEUED,
                metadata={"approval_required": True, "durable_recovery": True},
            )
        response.append(
            {
                "action": action.model_dump(mode="json"),
                "lifecycle": lifecycle.model_dump(mode="json"),
            }
        )
    return response


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: UUID) -> dict[str, Any]:
    """Approve a pending action and move it to the execution queue."""
    container = get_container()
    try:
        approved_state = approve_pending_action(
            action_id,
            pending_approval_registry=container.pending_approval_registry,
            lifecycle_repository=container.action_lifecycle_repository,
            lifecycle_manager=container.action_lifecycle_manager,
            action_queue=container.action_queue,
            pending_action_recovery=container.software_engineering_action_recovery,
            approval_recorder=container.software_engineering_action_recovery,
        )
    except PendingActionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except SoftwareEngineeringRecoveryError:
        logger.exception("software engineering approval persistence failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="action approval persistence failed",
        ) from None

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
    reason = body.reason if body is not None and body.reason else "rejected by user"
    try:
        rejected_state = reject_pending_action(
            action_id,
            pending_approval_registry=container.pending_approval_registry,
            lifecycle_repository=container.action_lifecycle_repository,
            lifecycle_manager=container.action_lifecycle_manager,
            reason=reason,
            pending_action_recovery=container.software_engineering_action_recovery,
            approval_recorder=container.software_engineering_action_recovery,
        )
    except PendingActionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except SoftwareEngineeringRecoveryError:
        logger.exception("software engineering rejection persistence failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="action rejection persistence failed",
        ) from None

    return {
        "status": "rejected",
        "action_id": str(action_id),
        "lifecycle": rejected_state.model_dump(mode="json"),
    }


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
def accept_event(event: UniversalEvent) -> dict[str, str]:
    """Accept and store a valid event without processing it."""
    container = get_container()
    try:
        result = container.event_workflow_service.accept(event)
    except DuplicateEventError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return {
        "status": "accepted",
        "event_id": str(result.event.id),
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
    try:
        result = container.event_workflow_service.process(
            event_id,
            integration_route=(
                body.integration_route
                if body is not None and body.integration_route is not None
                else None
            ),
        )
    except EventNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except EventLifecycleConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except EventProcessingError as error:
        logger.exception("event %s failed processing", event_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="event processing failed",
        ) from error

    response = result.processed_event.model_dump(mode="json")
    response["actions"] = [
        evaluation.action.model_dump(mode="json")
        for evaluation in result.permission_evaluations
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
        for evaluation in result.permission_evaluations
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
