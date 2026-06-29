"""Event normalization contracts for VELOX.

Normalizers adapt raw, source-specific event data into the Universal Event
Model. They do not persist events, dispatch them, or apply business workflows;
their responsibility is limited to producing a valid UniversalEvent envelope.
"""

from abc import ABC
from collections.abc import Mapping
from typing import Any, Protocol

from apps.server.src.core.events.models import UniversalEvent


class NormalizationError(ValueError):
    """Raised when raw event data cannot be normalized."""


class EventNormalizer(Protocol):
    """Protocol for components that normalize raw event data.

    Implementations accept mapping-like raw event data and return a
    UniversalEvent. This protocol allows integrations, API adapters, and tests
    to depend on behavior rather than inheritance.
    """

    def normalize(self, raw_event: Mapping[str, Any]) -> UniversalEvent:
        """Convert raw source event data into a UniversalEvent."""
        ...


class BaseEventNormalizer(ABC):
    """Base implementation for simple UniversalEvent normalizers.

    Subclasses provide the canonical source and event type. The base
    implementation validates that the incoming event is mapping-like, copies it
    into a plain dictionary payload, and records the normalizer class name in
    event metadata.
    """

    source: str
    event_type: str

    def normalize(self, raw_event: Mapping[str, Any]) -> UniversalEvent:
        """Normalize mapping-like raw data into a UniversalEvent.

        Args:
            raw_event: Source-specific event data represented as a mapping.

        Returns:
            A UniversalEvent using this normalizer's source and event type.

        Raises:
            NormalizationError: If raw_event is not mapping-like.
        """
        if not isinstance(raw_event, Mapping):
            raise NormalizationError("raw_event must be mapping-like")

        return UniversalEvent(
            source=self.source,
            type=self.event_type,
            payload=dict(raw_event),
            metadata={"normalizer": type(self).__name__},
        )
