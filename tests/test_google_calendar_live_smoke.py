"""Opt-in read-only live Google Calendar smoke.

Deselected by default through the `live_google_calendar` marker, and skipped even
when selected unless every live prerequisite is present. Nothing here runs in
ordinary test runs or in CI, and no Calendar write is ever performed.

Select explicitly with:

    uv run pytest -m live_google_calendar tests/test_google_calendar_live_smoke.py

Required environment values:

    VELOX_LIVE_GOOGLE_PRINCIPAL          VELOX principal
    VELOX_LIVE_GOOGLE_ACCOUNT_IDENTIFIER VELOX account identifier (Keychain username)
    VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID  real primary-calendar event ID

No real principal, account identifier, event ID, email, secret path or credential
material appears in this file.
"""

import json
import os
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from apps.server.src.core.actions import Action
from apps.server.src.core.credentials import (
    CredentialReference,
    CredentialStoreBackendError,
)
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CALENDAR_LIST_EVENTS_CAPABILITY,
    CalendarProviderComposition,
    CalendarWorkerExecutor,
    HttpxCalendarTransportClient,
)
from apps.server.src.integrations.google_oauth import (
    GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
    StoredGoogleCredentialsProvider,
)
from apps.server.src.integrations.keyring_credentials import (
    MacOSKeychainCredentialStore,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

pytestmark = pytest.mark.live_google_calendar

CAPABILITY = "prepare_meeting"
LIST_CAPABILITY = CALENDAR_LIST_EVENTS_CAPABILITY.identifier
# A deliberately narrow read-only window and page bound for the live listing.
LIST_WINDOW = timedelta(days=1)
LIST_MAX_RESULTS = 5
# Pagination needs Google to genuinely truncate: a wider read-only window and the
# smallest legal page size make a real nextPageToken likely without touching any
# calendar data. Nothing here creates, edits or deletes an event.
LIST_PAGINATION_WINDOW = timedelta(days=7)
LIST_PAGINATION_MAX_RESULTS = 1
BOUNDED_LIST_QUERY_KEYS = {"timeMin", "timeMax", "maxResults", "singleEvents", "orderBy"}
HTTP_TIMEOUT_SECONDS = 10.0
# An ID that cannot collide with a real Google event ID.
NONEXISTENT_EVENT_ID = "velox-live-smoke-definitely-nonexistent-event"
ALLOWLISTED_EVENT_FIELDS = frozenset({"event_id", "title", "start", "end", "attendees"})
FORBIDDEN_SUBSTRINGS = ("Bearer", "Authorization", "access_token", "refresh_token")

_PRINCIPAL_ENV = "VELOX_LIVE_GOOGLE_PRINCIPAL"
_ACCOUNT_ENV = "VELOX_LIVE_GOOGLE_ACCOUNT_IDENTIFIER"
_EVENT_ID_ENV = "VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID"
_LIVE_ENV_NAMES = (_PRINCIPAL_ENV, _ACCOUNT_ENV, _EVENT_ID_ENV)


@dataclass(frozen=True)
class LiveConfig:
    """Explicit live prerequisites supplied only through the environment."""

    principal: str
    account_identifier: str
    event_id: str


class LiveConfigAbsent(Exception):
    """Raised when no live prerequisite is supplied at all, so the run skips."""


class LiveConfigInvalid(Exception):
    """Raised when live prerequisites are partial or malformed, so the run fails."""


def resolve_live_config(environment: Mapping[str, str]) -> LiveConfig:
    """Resolve live prerequisites, distinguishing 'absent' from 'misconfigured'.

    Absent means no value at all was supplied, which is the ordinary case and
    skips. Anything partially or badly supplied is a misconfiguration and fails
    closed rather than silently skipping, so a broken live setup cannot be
    mistaken for 'live checks are simply off'.
    """
    supplied = {
        name: value
        for name in _LIVE_ENV_NAMES
        if (value := environment.get(name)) is not None
    }
    if not supplied:
        raise LiveConfigAbsent(
            "live smoke prerequisites absent; set "
            + ", ".join(_LIVE_ENV_NAMES)
            + " to run it"
        )

    missing = [name for name in _LIVE_ENV_NAMES if name not in supplied]
    if missing:
        raise LiveConfigInvalid(
            "live smoke is partially configured; missing " + ", ".join(missing)
        )

    malformed = [
        name
        for name, value in sorted(supplied.items())
        if not value.strip() or value != value.strip()
    ]
    if malformed:
        raise LiveConfigInvalid(
            "live smoke values must be non-blank and unpadded: " + ", ".join(malformed)
        )

    return LiveConfig(
        principal=supplied[_PRINCIPAL_ENV],
        account_identifier=supplied[_ACCOUNT_ENV],
        event_id=supplied[_EVENT_ID_ENV],
    )


@pytest.fixture(scope="module")
def live_config() -> LiveConfig:
    """Skip when live prerequisites are absent; fail closed when malformed."""
    if sys.platform != "darwin":
        pytest.skip("live smoke requires the macOS Keychain credential store")
    try:
        return resolve_live_config(os.environ)
    except LiveConfigAbsent as absent:
        pytest.skip(str(absent))
    except LiveConfigInvalid as invalid:
        pytest.fail(str(invalid))


@pytest.fixture(scope="module")
def stored_secret_values(live_config: LiveConfig) -> tuple[str, ...]:
    """Read stored secrets solely to assert they never appear in any output.

    The credentials provider already loads this material in-process to refresh, so
    reading it here adds no exposure. Values are only ever used inside boolean
    containment checks, never asserted on directly and never printed.
    """
    try:
        store = MacOSKeychainCredentialStore()
        material = store.get(
            CredentialReference(
                namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
                account_identifier=live_config.account_identifier,
            )
        )
    except CredentialStoreBackendError:
        pytest.fail("macOS Keychain credential store is unavailable")
    if material is None:
        pytest.fail(
            "no stored Google credential for the supplied VELOX account identifier; "
            "run the OAuth bootstrap connect command first"
        )
    parsed = json.loads(material.value)
    return tuple(
        value
        for key, value in parsed.items()
        if key in {"refresh_token", "client_secret"} and isinstance(value, str)
    )


@pytest.fixture(scope="module")
def calendar_executor() -> Iterator[CalendarWorkerExecutor]:
    """Compose the real production credential, provider and transport chain."""
    with httpx.Client() as http_client:
        yield CalendarWorkerExecutor(
            provider_composition=CalendarProviderComposition(
                credentials_provider=StoredGoogleCredentialsProvider(
                    MacOSKeychainCredentialStore()
                ),
                transport_client=HttpxCalendarTransportClient(
                    http_client,
                    timeout_seconds=HTTP_TIMEOUT_SECONDS,
                ),
            )
        )


def read_event(
    executor: CalendarWorkerExecutor,
    config: LiveConfig,
    event_id: str,
) -> WorkerExecutionResult:
    """Perform one real read-only primary-calendar events.get."""
    action = Action(
        type=CAPABILITY,
        target="live-google-calendar-smoke",
        payload={"calendar_event_id": event_id},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    return executor.execute(
        action,
        capability=CAPABILITY,
        account_context=WorkerAccountContext(
            principal=config.principal,
            account_identifier=config.account_identifier,
        ),
    )


def list_events(
    executor: CalendarWorkerExecutor,
    config: LiveConfig,
) -> WorkerExecutionResult:
    """Perform one real read-only bounded primary-calendar events.list page."""
    now = datetime.now(UTC).replace(microsecond=0)
    action = Action(
        type=LIST_CAPABILITY,
        target="live-google-calendar-smoke",
        payload={
            "time_min": (now - LIST_WINDOW).isoformat().replace("+00:00", "Z"),
            "time_max": (now + LIST_WINDOW).isoformat().replace("+00:00", "Z"),
            "max_results": LIST_MAX_RESULTS,
        },
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    return executor.execute(
        action,
        capability=LIST_CAPABILITY,
        account_context=WorkerAccountContext(
            principal=config.principal,
            account_identifier=config.account_identifier,
        ),
    )


def list_events_page(
    executor: CalendarWorkerExecutor,
    config: LiveConfig,
    *,
    time_min: str,
    time_max: str,
    max_results: int,
    page_token: str | None = None,
) -> WorkerExecutionResult:
    """Perform exactly one real read-only bounded primary-calendar events.list page."""
    payload: dict[str, object] = {
        "time_min": time_min,
        "time_max": time_max,
        "max_results": max_results,
    }
    if page_token is not None:
        payload["page_token"] = page_token
    return executor.execute(
        Action(
            type=LIST_CAPABILITY,
            target="live-google-calendar-smoke",
            payload=payload,
            executor_role=CALENDAR_EXECUTOR_ROLE,
        ),
        capability=LIST_CAPABILITY,
        account_context=WorkerAccountContext(
            principal=config.principal,
            account_identifier=config.account_identifier,
        ),
    )


def assert_allowlisted_page(events: object) -> tuple[str, ...]:
    """Assert the page carries only allowlisted fields; return its event IDs."""
    assert isinstance(events, tuple)
    identifiers: list[str] = []
    for event in events:
        assert set(event) == ALLOWLISTED_EVENT_FIELDS
        assert isinstance(event["event_id"], str) and event["event_id"].strip()
        assert isinstance(event["title"], str)
        assert isinstance(event["start"], str) and event["start"].strip()
        assert isinstance(event["end"], str) and event["end"].strip()
        assert all(isinstance(attendee, str) for attendee in event["attendees"])
        identifiers.append(event["event_id"])
    return tuple(identifiers)


def assert_no_credential_material(
    result: WorkerExecutionResult,
    secrets: tuple[str, ...],
) -> None:
    """Assert on precomputed booleans so no secret can reach assertion output."""
    blob = repr(result) + repr(result.metadata)
    leaked_secret = any(secret and secret in blob for secret in secrets)
    assert not leaked_secret, "stored credential material appeared in the result"
    leaked_marker = [token for token in FORBIDDEN_SUBSTRINGS if token in blob]
    assert not leaked_marker, f"forbidden token markers in result: {leaked_marker}"


def test_live_primary_calendar_events_get_returns_allowlisted_event(
    calendar_executor: CalendarWorkerExecutor,
    live_config: LiveConfig,
    stored_secret_values: tuple[str, ...],
) -> None:
    result = read_event(calendar_executor, live_config, live_config.event_id)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata.get("found") is True
    assert result.metadata.get("external_execution_performed") is True

    event = result.metadata.get("event")
    assert isinstance(event, dict)
    # Only the allowlisted provider fields may cross the boundary.
    assert set(event) == ALLOWLISTED_EVENT_FIELDS
    assert event["event_id"] == live_config.event_id
    assert isinstance(event["title"], str)
    assert isinstance(event["start"], str) and event["start"].strip()
    assert isinstance(event["end"], str) and event["end"].strip()
    attendees = event["attendees"]
    assert isinstance(attendees, list | tuple)
    assert all(isinstance(attendee, str) and attendee.strip() for attendee in attendees)

    assert_no_credential_material(result, stored_secret_values)


def test_live_nonexistent_event_is_a_safe_not_found(
    calendar_executor: CalendarWorkerExecutor,
    live_config: LiveConfig,
    stored_secret_values: tuple[str, ...],
) -> None:
    result = read_event(calendar_executor, live_config, NONEXISTENT_EVENT_ID)

    # Google returns 404; the transport maps it to a successful not-found read
    # rather than a failure, and fabricates no event.
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata.get("found") is False
    assert "event" not in result.metadata

    assert_no_credential_material(result, stored_secret_values)


def test_live_bounded_primary_calendar_events_list_returns_allowlisted_events(
    calendar_executor: CalendarWorkerExecutor,
    live_config: LiveConfig,
    stored_secret_values: tuple[str, ...],
) -> None:
    result = list_events(calendar_executor, live_config)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata.get("external_execution_performed") is True

    events = result.metadata.get("events")
    assert isinstance(events, tuple)
    # The page is bounded by the VELOX-owned request, never by what Google returns.
    assert len(events) <= LIST_MAX_RESULTS
    assert result.metadata.get("event_count") == len(events)
    assert result.metadata.get("page_complete") is True
    assert result.metadata.get("skipped_event_count") == 0
    for event in events:
        # Only the allowlisted provider fields may cross the boundary.
        assert set(event) == ALLOWLISTED_EVENT_FIELDS
        assert isinstance(event["event_id"], str) and event["event_id"].strip()
        assert isinstance(event["title"], str)
        assert isinstance(event["start"], str) and event["start"].strip()
        assert isinstance(event["end"], str) and event["end"].strip()
        assert all(isinstance(attendee, str) for attendee in event["attendees"])

    # A returned token stays opaque metadata; this slice never follows it.
    next_page_token = result.metadata.get("next_page_token")
    assert next_page_token is None or (
        isinstance(next_page_token, str) and next_page_token.strip()
    )
    assert result.metadata.get("has_more_pages") is (next_page_token is not None)

    assert_no_credential_material(result, stored_secret_values)


def test_live_caller_driven_pagination_reads_a_real_second_page(
    calendar_executor: CalendarWorkerExecutor,
    live_config: LiveConfig,
    stored_secret_values: tuple[str, ...],
) -> None:
    """Read a real first page, then explicitly request the real next page.

    Skips only when the live calendar cannot naturally produce a nextPageToken in
    the window. No calendar data is ever created or modified to force the result.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    time_min = (now - LIST_PAGINATION_WINDOW).isoformat().replace("+00:00", "Z")
    time_max = (now + LIST_PAGINATION_WINDOW).isoformat().replace("+00:00", "Z")

    first = list_events_page(
        calendar_executor,
        live_config,
        time_min=time_min,
        time_max=time_max,
        max_results=LIST_PAGINATION_MAX_RESULTS,
    )

    assert first.status == WorkerExecutionStatus.SUCCEEDED
    assert first.metadata.get("external_execution_performed") is True
    assert first.metadata.get("page_token_supplied") is False
    first_ids = assert_allowlisted_page(first.metadata.get("events"))
    assert len(first_ids) <= LIST_PAGINATION_MAX_RESULTS
    assert first.metadata.get("event_count") == len(first_ids)
    assert first.metadata.get("page_complete") is True
    assert first.metadata.get("skipped_event_count") == 0

    next_page_token = first.metadata.get("next_page_token")
    if next_page_token is None:
        pytest.skip(
            "live calendar returned no nextPageToken for the bounded window, so a "
            "real second page cannot be read without altering calendar data"
        )
    assert isinstance(next_page_token, str) and next_page_token.strip()
    assert first.metadata.get("has_more_pages") is True

    second = list_events_page(
        calendar_executor,
        live_config,
        time_min=time_min,
        time_max=time_max,
        max_results=LIST_PAGINATION_MAX_RESULTS,
        page_token=next_page_token,
    )

    assert second.status == WorkerExecutionStatus.SUCCEEDED
    assert second.metadata.get("external_execution_performed") is True
    assert second.metadata.get("page_token_supplied") is True
    second_ids = assert_allowlisted_page(second.metadata.get("events"))
    assert len(second_ids) <= LIST_PAGINATION_MAX_RESULTS
    assert second.metadata.get("event_count") == len(second_ids)
    assert second.metadata.get("page_complete") is True
    assert second.metadata.get("skipped_event_count") == 0

    # The continuation advanced rather than replaying the first page.
    assert second_ids
    assert not set(first_ids) & set(second_ids)

    # Both executions were one bounded read-only GET on the primary events path,
    # differing only by the opaque token, which never reaches result metadata.
    first_request = first.metadata["provider_request"]
    second_request = second.metadata["provider_request"]
    assert first_request["method"] == second_request["method"] == "GET"
    assert first_request["path"] == second_request["path"]
    assert first_request["body"] is None and second_request["body"] is None
    assert set(first_request["query"]) == BOUNDED_LIST_QUERY_KEYS
    assert set(second_request["query"]) == BOUNDED_LIST_QUERY_KEYS | {"pageToken"}
    assert second_request["query"]["pageToken"] == "<redacted>"
    assert {
        key: value
        for key, value in second_request["query"].items()
        if key != "pageToken"
    } == first_request["query"]

    # The first page legitimately surfaces the token: it is the handoff a caller
    # needs in order to ask for the next page at all.
    assert first.metadata["next_page_token"] == next_page_token

    # The second execution consumed that token as input, so it must not reappear
    # on any surface it produces. Its own freshly returned token is excluded,
    # being the legitimate handoff for a further page.
    consumed_metadata = {
        key: value
        for key, value in second.metadata.items()
        if key != "next_page_token"
    }
    assert next_page_token not in repr(consumed_metadata)
    assert next_page_token not in repr(second.action)
    assert next_page_token not in str(second.reason)
    assert next_page_token not in repr(second.failure)
    assert second.action.payload["page_token"] == "<redacted>"

    for result in (first, second):
        assert_no_credential_material(result, stored_secret_values)
