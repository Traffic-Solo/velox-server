"""Deterministic tests for the operator-controlled live SE acceptance harness."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from uuid import UUID

import httpx
import pytest

from apps.server.src.integrations import software_engineering_acceptance as acceptance
from apps.server.src.integrations.software_engineering_acceptance import (
    SoftwareEngineeringAcceptanceError,
)

ACTION_ID = UUID("11111111-2222-3333-4444-555555555555")
STAMP = "20260930T220000Z"
SUFFIX = "accept01"
MARKER_LINE = (
    f"- Acceptance {STAMP}-{SUFFIX}: "
    "VELOX live Software Engineering control plane."
)
PR_URL = "https://github.example/Traffic-Solo/velox-server/pull/99"
COMMIT_SHA = "a" * 40


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )


def full_success_handler(
    calls: list[tuple[str, str, dict[str, object] | None]],
) -> Callable[[httpx.Request], httpx.Response]:
    statuses = iter(
        (
            "succeeded",
            "kept",
            "promoted",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, object] | None = None
        if request.content:
            decoded = json.loads(request.content)
            assert isinstance(decoded, dict)
            body = decoded
        calls.append((request.method, request.url.path, body))

        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/tasks/software-engineering":
            assert body is not None
            objective = body["objective"]
            assert isinstance(objective, str)
            assert acceptance._ACCEPTANCE_LOG in objective
            assert MARKER_LINE in objective
            assert body["target"] == "velox-server"
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "status": "awaiting_approval",
                    "permission_status": "requires_approval",
                    "routing_reason": "approval required",
                },
            )
        if request.url.path == f"/actions/{ACTION_ID}/approve":
            return httpx.Response(
                200,
                json={
                    "status": "approved",
                    "action_id": str(ACTION_ID),
                    "lifecycle": {"status": "approved", "retry_count": 0},
                },
            )
        if request.url.path == f"/tasks/software-engineering/{ACTION_ID}/execute":
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "processed": True,
                    "execution_status": "succeeded",
                    "lifecycle_status": "completed",
                    "execution_reason": None,
                    "external_execution_performed": True,
                    "review_status": "available",
                    "review": {
                        "changed_files": [acceptance._ACCEPTANCE_LOG],
                        "untracked_files": [],
                        "diff_stat": (
                            f"{acceptance._ACCEPTANCE_LOG} | 1 +\n"
                            "1 file changed, 1 insertion(+)"
                        ),
                        "diff": f"+{MARKER_LINE}\n",
                        "diff_truncated": False,
                        "dirty": True,
                        "canonical_clean": True,
                        "canonical_unchanged": True,
                    },
                },
            )
        if request.url.path == f"/tasks/software-engineering/{ACTION_ID}/status":
            phase = next(statuses)
            payload: dict[str, object] = {
                "action_id": str(ACTION_ID),
                "target": "velox-server",
                "phase": phase,
                "promoted": phase == "promoted",
            }
            if phase == "promoted":
                payload["pull_request_number"] = 99
                payload["pull_request_url"] = PR_URL
            return httpx.Response(200, json=payload)
        if request.url.path == (
            f"/tasks/software-engineering/{ACTION_ID}/work-product/disposition"
        ):
            assert body == {"disposition": "keep"}
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "disposition": "keep",
                    "succeeded": True,
                    "worktree_present": True,
                    "branch_present": True,
                    "canonical_unchanged": True,
                    "remaining": [],
                },
            )
        if request.url.path == f"/tasks/software-engineering/{ACTION_ID}/promote":
            assert body is not None
            assert body["title"] == f"VELOX Sprint 4 live acceptance {STAMP}"
            assert str(ACTION_ID) in str(body["body"])
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "commit_sha": COMMIT_SHA,
                    "pull_request_number": 99,
                    "pull_request_url": PR_URL,
                    "base_branch": "main",
                    "head_branch": f"velox/se-{ACTION_ID}",
                    "pull_request_created": True,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    return handler


def input_sequence(*values: str) -> Callable[[], str]:
    iterator = iter(values)
    return lambda: next(iterator)


def test_live_acceptance_harness_uses_only_public_control_plane() -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    out = StringIO()
    client = client_for(full_success_handler(calls))

    result = acceptance.run_acceptance(
        client,
        read_line=input_sequence(str(ACTION_ID), "keep", "promote"),
        out=out,
        now=datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
        marker_suffix=SUFFIX,
    )

    assert result.action_id == ACTION_ID
    assert result.marker_path == acceptance._ACCEPTANCE_LOG
    assert result.pull_request_number == 99
    assert result.pull_request_url == PR_URL
    assert result.commit_sha == COMMIT_SHA
    assert [path for _, path, _ in calls] == [
        "/health",
        "/tasks/software-engineering",
        f"/actions/{ACTION_ID}/approve",
        f"/tasks/software-engineering/{ACTION_ID}/execute",
        f"/tasks/software-engineering/{ACTION_ID}/status",
        f"/tasks/software-engineering/{ACTION_ID}/work-product/disposition",
        f"/tasks/software-engineering/{ACTION_ID}/status",
        f"/tasks/software-engineering/{ACTION_ID}/promote",
        f"/tasks/software-engineering/{ACTION_ID}/status",
    ]
    rendered = out.getvalue()
    assert "Sprint 4 live acceptance PASSED." in rendered
    assert PR_URL in rendered


def test_operator_must_explicitly_approve_before_worker_execution() -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    client = client_for(full_success_handler(calls))

    with pytest.raises(
        SoftwareEngineeringAcceptanceError,
        match="did not approve",
    ):
        acceptance.run_acceptance(
            client,
            read_line=input_sequence("no"),
            out=StringIO(),
            now=datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
            marker_suffix=SUFFIX,
        )

    assert [path for _, path, _ in calls] == [
        "/health",
        "/tasks/software-engineering",
    ]


def test_worker_must_modify_only_tracked_acceptance_log() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/tasks/software-engineering":
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "status": "awaiting_approval",
                },
            )
        if request.url.path == f"/actions/{ACTION_ID}/approve":
            return httpx.Response(
                200,
                json={"lifecycle": {"status": "approved"}},
            )
        if request.url.path == f"/tasks/software-engineering/{ACTION_ID}/execute":
            return httpx.Response(
                200,
                json={
                    "processed": True,
                    "execution_status": "succeeded",
                    "lifecycle_status": "completed",
                    "external_execution_performed": True,
                    "review_status": "available",
                    "review": {
                        "changed_files": [
                            acceptance._ACCEPTANCE_LOG,
                            "apps/server/src/evil.py",
                        ],
                        "untracked_files": [],
                        "diff_stat": "",
                        "diff": f"+{MARKER_LINE}\n",
                        "diff_truncated": False,
                        "dirty": True,
                        "canonical_clean": True,
                        "canonical_unchanged": True,
                    },
                },
            )
        raise AssertionError("unexpected request")

    with pytest.raises(
        SoftwareEngineeringAcceptanceError,
        match="outside the exact acceptance target",
    ):
        acceptance.run_acceptance(
            client_for(handler),
            read_line=input_sequence(str(ACTION_ID)),
            out=StringIO(),
            now=datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
            marker_suffix=SUFFIX,
        )


def test_worker_diff_must_contain_exact_acceptance_marker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/tasks/software-engineering":
            return httpx.Response(
                200,
                json={
                    "action_id": str(ACTION_ID),
                    "status": "awaiting_approval",
                },
            )
        if request.url.path == f"/actions/{ACTION_ID}/approve":
            return httpx.Response(
                200,
                json={"lifecycle": {"status": "approved"}},
            )
        if request.url.path == f"/tasks/software-engineering/{ACTION_ID}/execute":
            return httpx.Response(
                200,
                json={
                    "processed": True,
                    "execution_status": "succeeded",
                    "lifecycle_status": "completed",
                    "external_execution_performed": True,
                    "review_status": "available",
                    "review": {
                        "changed_files": [acceptance._ACCEPTANCE_LOG],
                        "untracked_files": [],
                        "diff_stat": "",
                        "diff": "+wrong marker\n",
                        "diff_truncated": False,
                        "dirty": True,
                        "canonical_clean": True,
                        "canonical_unchanged": True,
                    },
                },
            )
        raise AssertionError("unexpected request")

    with pytest.raises(
        SoftwareEngineeringAcceptanceError,
        match="exact acceptance marker",
    ):
        acceptance.run_acceptance(
            client_for(handler),
            read_line=input_sequence(str(ACTION_ID)),
            out=StringIO(),
            now=datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
            marker_suffix=SUFFIX,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://192.168.1.10:8000",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?token=secret",
    ],
)
def test_acceptance_base_url_must_be_loopback_and_uncredentialed(url: str) -> None:
    with pytest.raises(SoftwareEngineeringAcceptanceError):
        acceptance._validated_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000/",
        "https://[::1]:8443",
    ],
)
def test_acceptance_base_url_accepts_loopback(url: str) -> None:
    assert acceptance._validated_base_url(url) == url.rstrip("/")
