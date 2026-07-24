"""Deterministic Calendar event normalization and workflow ingress."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from apps.server.src.core.events.models import UniversalEvent
from apps.server.src.core.events.normalizer import BaseEventNormalizer, EventNormalizer
from apps.server.src.core.events.pipeline import IntegrationRouteContext
from apps.server.src.core.events.workflow import (
    EventAcceptanceResult,
    EventProcessingResult,
    EventWorkflowService,
)


class CalendarEventNormalizer(BaseEventNormalizer):
    """Normalize raw Calendar mappings into Universal Events."""

    source = "calendar"
    event_type = "calendar.event"

    def normalize(self, raw_event: Mapping[str, Any]) -> UniversalEvent:
        """Copy raw Calendar data and explicitly map its external identity."""
        event = super().normalize(raw_event)
        payload = dict(event.payload)
        if "event_id" in raw_event:
            payload["calendar_event_id"] = raw_event["event_id"]
        return event.model_copy(update={"payload": payload})


@dataclass(frozen=True)
class CalendarIngressResult:
    """Immutable result of deterministic Calendar ingress."""

    event: UniversalEvent
    acceptance: EventAcceptanceResult
    processing: EventProcessingResult


class CalendarIngressAdapter:
    """Accept normalized Calendar events through the shared workflow service."""

    def __init__(
        self,
        *,
        normalizer: EventNormalizer,
        workflow_service: EventWorkflowService,
    ) -> None:
        self._normalizer = normalizer
        self._workflow_service = workflow_service

    def ingest(
        self,
        raw_event: Mapping[str, Any],
        *,
        integration_route: IntegrationRouteContext,
    ) -> CalendarIngressResult:
        """Normalize, accept, and process one Calendar event."""
        event = self._normalizer.normalize(raw_event)
        acceptance = self._workflow_service.accept(event)
        processing = self._workflow_service.process(
            event.id,
            integration_route=integration_route,
        )
        return CalendarIngressResult(
            event=event,
            acceptance=acceptance,
            processing=processing,
        )
