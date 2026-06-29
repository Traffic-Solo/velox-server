from uuid import uuid4

from apps.server.src.core.events import EventStore, UniversalEvent


def test_event_store_appends_events() -> None:
    store = EventStore()
    event = UniversalEvent(source="test-suite", type="test.created")

    stored_event = store.append(event)

    assert stored_event == event
    assert store.list_events() == [event]


def test_event_store_retrieves_event_by_id() -> None:
    store = EventStore()
    event = UniversalEvent(source="test-suite", type="test.created")

    store.append(event)

    assert store.get_event(event.id) == event


def test_event_store_returns_none_for_missing_id() -> None:
    store = EventStore()

    assert store.get_event(uuid4()) is None


def test_event_store_clear_removes_events() -> None:
    store = EventStore()
    store.append(UniversalEvent(source="test-suite", type="test.created"))

    store.clear()

    assert store.list_events() == []
