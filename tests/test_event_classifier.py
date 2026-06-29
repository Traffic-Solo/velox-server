from apps.server.src.core.events import RuleBasedEventClassifier, UniversalEvent


def classify_source(source: str):
    classifier = RuleBasedEventClassifier()
    event = UniversalEvent(source=source, type="event.created")

    return classifier.classify(event)


def test_classifier_returns_github_for_github_source() -> None:
    classification = classify_source("github")

    assert classification.category == "github"


def test_classifier_returns_github_for_github_com_source() -> None:
    classification = classify_source("github.com")

    assert classification.category == "github"


def test_classifier_returns_gmail_for_gmail_source() -> None:
    classification = classify_source("gmail")

    assert classification.category == "gmail"


def test_classifier_returns_gmail_for_google_mail_source() -> None:
    classification = classify_source("google-mail")

    assert classification.category == "gmail"


def test_classifier_returns_calendar_for_calendar_source() -> None:
    classification = classify_source("calendar")

    assert classification.category == "calendar"


def test_classifier_returns_calendar_for_google_calendar_source() -> None:
    classification = classify_source("google-calendar")

    assert classification.category == "calendar"


def test_classifier_returns_calendar_for_apple_calendar_source() -> None:
    classification = classify_source("apple-calendar")

    assert classification.category == "calendar"


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
