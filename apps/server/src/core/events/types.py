"""Shared type aliases for the core event model.

These aliases keep the event contract readable while preserving precise Python
3.12 typing at the model boundary.
"""

from typing import Any

EventMetadata = dict[str, Any]
"""Additional context attached to an event.

Metadata is intended for routing, tracing, source details, and other contextual
information that should remain separate from the event's domain payload.
"""

EventPayload = dict[str, Any]
"""The source-specific body of an event.

Payload values are intentionally flexible because integrations can produce
events with different shapes. The UniversalEvent model guarantees the envelope;
individual event types can define stricter payload schemas later.
"""
