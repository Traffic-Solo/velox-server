from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.events import UniversalEvent
from pydantic import ValidationError


def test_universal_event_can_be_created_with_minimal_required_fields() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    assert event.source == "test-suite"
    assert event.type == "test.created"


def test_universal_event_id_is_uuid() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    assert isinstance(event.id, UUID)


def test_universal_event_timestamp_is_timezone_aware_utc() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset() == UTC.utcoffset(event.timestamp)


def test_universal_event_payload_and_metadata_default_to_empty_dicts() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    assert event.payload == {}
    assert event.metadata == {}


def test_universal_event_is_immutable() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    with pytest.raises(ValidationError):
        event.source = "updated"


def test_universal_event_rejects_blank_source() -> None:
    with pytest.raises(ValidationError):
        UniversalEvent(source="   ", type="test.created")


def test_universal_event_rejects_blank_type() -> None:
    with pytest.raises(ValidationError):
        UniversalEvent(source="test-suite", type="   ")


def test_universal_event_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        UniversalEvent(
            source="test-suite",
            type="test.created",
            timestamp=datetime(2026, 6, 29, 12, 0, 0),
        )


def test_universal_event_correlation_and_causation_ids_are_optional() -> None:
    event = UniversalEvent(source="test-suite", type="test.created")

    assert event.correlation_id is None
    assert event.causation_id is None


def test_universal_event_accepts_correlation_and_causation_uuid_values() -> None:
    correlation_id = uuid4()
    causation_id = uuid4()

    event = UniversalEvent(
        source="test-suite",
        type="test.created",
        correlation_id=correlation_id,
        causation_id=causation_id,
    )

    assert event.correlation_id == correlation_id
    assert event.causation_id == causation_id
