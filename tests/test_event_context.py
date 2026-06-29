from apps.server.src.core.events import (
    BaseContextResolver,
    EventClassification,
    ResolvedContext,
    UniversalEvent,
)


def create_event() -> UniversalEvent:
    return UniversalEvent(source="gmail", type="message.received")


def create_classification() -> EventClassification:
    return EventClassification(
        category="communication",
        confidence=0.75,
        labels=["gmail", "message.received"],
        reason="test classification",
    )


def test_base_context_resolver_returns_resolved_context() -> None:
    resolver = BaseContextResolver()

    resolved = resolver.resolve(create_event(), create_classification())

    assert isinstance(resolved, ResolvedContext)


def test_resolved_context_includes_original_event() -> None:
    resolver = BaseContextResolver()
    event = create_event()

    resolved = resolver.resolve(event, create_classification())

    assert resolved.event == event


def test_resolved_context_includes_classification() -> None:
    resolver = BaseContextResolver()
    classification = create_classification()

    resolved = resolver.resolve(create_event(), classification)

    assert resolved.classification == classification


def test_resolved_context_defaults_to_empty_context() -> None:
    resolver = BaseContextResolver()

    resolved = resolver.resolve(create_event(), create_classification())

    assert resolved.context == {}


def test_resolved_context_sources_default_to_empty_list() -> None:
    resolver = BaseContextResolver()

    resolved = resolver.resolve(create_event(), create_classification())

    assert resolved.sources == []


def test_resolved_context_confidence_defaults_to_classification_confidence() -> None:
    resolver = BaseContextResolver()
    classification = create_classification()

    resolved = resolver.resolve(create_event(), classification)

    assert resolved.confidence == classification.confidence


def test_resolved_context_reason_is_set() -> None:
    resolver = BaseContextResolver()

    resolved = resolver.resolve(create_event(), create_classification())

    assert resolved.reason is not None
    assert "base context resolver v0" in resolved.reason.lower()
