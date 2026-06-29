from apps.server.src.core.events import RuleBasedEventClassifier, UniversalEvent


def classify_source(source: str):
    classifier = RuleBasedEventClassifier()
    event = UniversalEvent(source=source, type="event.created")

    return classifier.classify(event)


def test_classifier_returns_communication_for_gmail_source() -> None:
    classification = classify_source("gmail")

    assert classification.category == "communication"


def test_classifier_returns_schedule_for_calendar_source() -> None:
    classification = classify_source("google-calendar")

    assert classification.category == "schedule"


def test_classifier_returns_development_for_github_source() -> None:
    classification = classify_source("github")

    assert classification.category == "development"


def test_classifier_returns_system_for_system_source() -> None:
    classification = classify_source("system")

    assert classification.category == "system"


def test_classifier_returns_unknown_for_unknown_source() -> None:
    classification = classify_source("notion")

    assert classification.category == "unknown"


def test_classifier_labels_include_source_and_type() -> None:
    classifier = RuleBasedEventClassifier()
    event = UniversalEvent(source="gmail", type="message.received")

    classification = classifier.classify(event)

    assert "gmail" in classification.labels
    assert "message.received" in classification.labels


def test_classifier_confidence_is_one() -> None:
    classification = classify_source("gmail")

    assert classification.confidence == 1.0
