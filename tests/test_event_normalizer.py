from typing import Any

import pytest
from apps.server.src.core.events import (
    BaseEventNormalizer,
    NormalizationError,
    UniversalEvent,
)


class ConcreteEventNormalizer(BaseEventNormalizer):
    source = "test-source"
    event_type = "test.created"


def test_concrete_normalizer_converts_raw_dict_into_universal_event() -> None:
    normalizer = ConcreteEventNormalizer()

    event = normalizer.normalize({"name": "example"})

    assert isinstance(event, UniversalEvent)
    assert event.payload == {"name": "example"}


def test_normalizer_copies_payload_into_plain_dict() -> None:
    normalizer = ConcreteEventNormalizer()
    raw_event: dict[str, Any] = {"name": "example"}

    event = normalizer.normalize(raw_event)

    assert event.payload == raw_event
    assert event.payload is not raw_event
    assert type(event.payload) is dict


def test_normalizer_metadata_includes_normalizer_class_name() -> None:
    normalizer = ConcreteEventNormalizer()

    event = normalizer.normalize({"name": "example"})

    assert event.metadata["normalizer"] == "ConcreteEventNormalizer"


def test_normalizer_rejects_non_mapping_raw_event() -> None:
    normalizer = ConcreteEventNormalizer()

    with pytest.raises(NormalizationError):
        normalizer.normalize(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_normalizer_uses_source_and_event_type() -> None:
    normalizer = ConcreteEventNormalizer()

    event = normalizer.normalize({"name": "example"})

    assert event.source == "test-source"
    assert event.type == "test.created"
