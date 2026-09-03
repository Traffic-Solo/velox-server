"""Deterministic tests for the live smoke gate.

These run in ordinary CI and never touch the network, the Keychain or Google.
They exist because the gate is the mechanism that keeps the live smoke out of
normal runs, so its behavior must itself be covered deterministically.
"""

import pytest
from tests import test_google_calendar_live_smoke as live

PRINCIPAL = "velox-test-principal"
ACCOUNT = "velox-test-account"
EVENT_ID = "test-event-id"


def full_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "VELOX_LIVE_GOOGLE_PRINCIPAL": PRINCIPAL,
        "VELOX_LIVE_GOOGLE_ACCOUNT_IDENTIFIER": ACCOUNT,
        "VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID": EVENT_ID,
    }
    environment.update(overrides)
    return environment


def test_module_is_marked_so_it_is_deselected_by_default() -> None:
    assert live.pytestmark.name == "live_google_calendar"


def test_documented_environment_names_are_the_velox_ones() -> None:
    assert live._LIVE_ENV_NAMES == (
        "VELOX_LIVE_GOOGLE_PRINCIPAL",
        "VELOX_LIVE_GOOGLE_ACCOUNT_IDENTIFIER",
        "VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID",
    )


def test_complete_environment_resolves() -> None:
    config = live.resolve_live_config(full_environment())

    assert config == live.LiveConfig(
        principal=PRINCIPAL, account_identifier=ACCOUNT, event_id=EVENT_ID
    )


def test_absent_environment_skips_rather_than_fails() -> None:
    with pytest.raises(live.LiveConfigAbsent):
        live.resolve_live_config({})


def test_unrelated_environment_is_still_treated_as_absent() -> None:
    with pytest.raises(live.LiveConfigAbsent):
        live.resolve_live_config({"PATH": "/usr/bin", "HOME": "/root"})


@pytest.mark.parametrize(
    "omitted",
    [
        "VELOX_LIVE_GOOGLE_PRINCIPAL",
        "VELOX_LIVE_GOOGLE_ACCOUNT_IDENTIFIER",
        "VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID",
    ],
)
def test_partial_configuration_fails_closed(omitted: str) -> None:
    environment = full_environment()
    del environment[omitted]

    with pytest.raises(live.LiveConfigInvalid) as failure:
        live.resolve_live_config(environment)

    assert omitted in str(failure.value)


@pytest.mark.parametrize("bad_value", ["", "   ", " padded", "padded ", "\tvalue"])
def test_malformed_values_fail_closed(bad_value: str) -> None:
    with pytest.raises(live.LiveConfigInvalid):
        live.resolve_live_config(
            full_environment(VELOX_LIVE_GOOGLE_CALENDAR_EVENT_ID=bad_value)
        )


def test_malformed_failure_names_the_offending_variable_only() -> None:
    with pytest.raises(live.LiveConfigInvalid) as failure:
        live.resolve_live_config(full_environment(VELOX_LIVE_GOOGLE_PRINCIPAL="  "))

    message = str(failure.value)
    assert "VELOX_LIVE_GOOGLE_PRINCIPAL" in message
    # The offending value itself must never be echoed back.
    assert ACCOUNT not in message and EVENT_ID not in message


def test_nonexistent_probe_id_cannot_collide_with_a_real_event() -> None:
    # Google event IDs are base32hex-ish; hyphens make a real collision impossible.
    assert "-" in live.NONEXISTENT_EVENT_ID
    assert live.NONEXISTENT_EVENT_ID.startswith("velox-")


def test_allowlisted_event_fields_are_exactly_the_safe_five() -> None:
    assert {
        "event_id",
        "title",
        "start",
        "end",
        "attendees",
    } == live.ALLOWLISTED_EVENT_FIELDS


def test_no_real_identifiers_are_committed_in_the_live_module() -> None:
    """The live module must carry no real account, email or event identifier."""
    import pathlib
    import re

    source = pathlib.Path(live.__file__).read_text()

    # No email address of any form.
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", source)
    # A real Google recurring-instance ID looks like "<20+ chars>_20560824T083000Z".
    # Detect the shape without naming a real one.
    assert not re.search(r"[a-z0-9]{20,}_\d{8}T\d{6}Z", source)
    # No filesystem path to a client-secret file may be committed.
    assert not re.search(r"client[-_]secret[^\s]*\.json", source)
