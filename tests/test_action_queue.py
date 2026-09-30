from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action


def test_action_queue_enqueues_single_action() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")

    queued_action = queue.enqueue(action)

    assert queued_action == action
    assert queue.list() == [action]


def test_action_queue_enqueues_multiple_actions() -> None:
    queue = ActionQueue()
    first_action = Action(type="summarize_email", target="event-1")
    second_action = Action(type="prepare_meeting", target="event-2")

    queue.enqueue_many([first_action, second_action])

    assert queue.list() == [first_action, second_action]


def test_action_queue_preserves_fifo_ordering() -> None:
    queue = ActionQueue()
    first_action = Action(type="summarize_email", target="event-1")
    second_action = Action(type="prepare_meeting", target="event-2")

    queue.enqueue(first_action)
    queue.enqueue(second_action)

    assert queue.list()[0] == first_action
    assert queue.list()[1] == second_action


def test_action_queue_dequeues_in_fifo_order() -> None:
    queue = ActionQueue()
    first_action = Action(type="summarize_email", target="event-1")
    second_action = Action(type="prepare_meeting", target="event-2")

    queue.enqueue(first_action)
    queue.enqueue(second_action)

    assert queue.dequeue() == first_action
    assert queue.list() == [second_action]


def test_action_queue_dequeue_empty_queue_returns_none() -> None:
    queue = ActionQueue()

    assert queue.dequeue() is None


def test_action_queue_clear_removes_actions() -> None:
    queue = ActionQueue()
    queue.enqueue(Action(type="summarize_email", target="event-1"))

    queue.clear()

    assert queue.list() == []


def test_action_queue_count_returns_number_of_actions() -> None:
    queue = ActionQueue()
    queue.enqueue(Action(type="summarize_email", target="event-1"))
    queue.enqueue(Action(type="prepare_meeting", target="event-2"))

    assert queue.count() == 2


def test_action_queue_get_is_non_destructive() -> None:
    queue = ActionQueue()
    first = Action(type="first", target="one")
    target = Action(type="target", target="two")
    third = Action(type="third", target="three")
    queue.enqueue_many([first, target, third])

    assert queue.get(target.id) == target
    assert queue.list() == [first, target, third]


def test_action_queue_remove_exact_action_preserves_unrelated_order() -> None:
    queue = ActionQueue()
    first = Action(type="first", target="one")
    target = Action(type="target", target="two")
    third = Action(type="third", target="three")
    queue.enqueue_many([first, target, third])

    assert queue.remove(target.id) == target
    assert queue.list() == [first, third]


def test_action_queue_exact_lookup_and_remove_missing_return_none() -> None:
    queue = ActionQueue()
    queued = Action(type="queued", target="one")
    missing = Action(type="missing", target="two")
    queue.enqueue(queued)

    assert queue.get(missing.id) is None
    assert queue.remove(missing.id) is None
    assert queue.list() == [queued]
