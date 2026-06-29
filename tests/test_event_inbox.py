from uuid import uuid4

from apps.server.src.core.events import EventInbox, UniversalEvent


def test_event_inbox_enqueues_events() -> None:
    inbox = EventInbox()
    event = UniversalEvent(source="test-suite", type="test.created")

    enqueued_event = inbox.enqueue(event)

    assert enqueued_event == event
    assert inbox.list_pending() == [event]


def test_event_inbox_lists_pending_events() -> None:
    inbox = EventInbox()
    event = UniversalEvent(source="test-suite", type="test.created")

    inbox.enqueue(event)

    assert inbox.list_pending() == [event]


def test_event_inbox_marks_event_as_processed() -> None:
    inbox = EventInbox()
    event = UniversalEvent(source="test-suite", type="test.created")

    inbox.enqueue(event)

    assert inbox.mark_processed(event.id) == event
    assert inbox.list_pending() == []


def test_event_inbox_returns_none_for_missing_event_id() -> None:
    inbox = EventInbox()

    assert inbox.mark_processed(uuid4()) is None


def test_event_inbox_clear_removes_events() -> None:
    inbox = EventInbox()
    inbox.enqueue(UniversalEvent(source="test-suite", type="test.created"))

    inbox.clear()

    assert inbox.list_pending() == []
