from apps.server.src.core.container import ApplicationContainer, get_container


def test_container_exposes_event_repository() -> None:
    container = ApplicationContainer()

    assert container.event_repository is not None


def test_container_exposes_event_inbox() -> None:
    container = ApplicationContainer()

    assert container.event_inbox is not None


def test_container_exposes_event_classifier() -> None:
    container = ApplicationContainer()

    assert container.event_classifier is not None


def test_container_exposes_context_resolver() -> None:
    container = ApplicationContainer()

    assert container.context_resolver is not None


def test_container_exposes_event_processing_pipeline() -> None:
    container = ApplicationContainer()

    assert container.event_processing_pipeline is not None


def test_get_container_returns_application_container() -> None:
    container = get_container()

    assert isinstance(container, ApplicationContainer)


def test_get_container_returns_same_instance() -> None:
    first_container = get_container()
    second_container = get_container()

    assert first_container is second_container
