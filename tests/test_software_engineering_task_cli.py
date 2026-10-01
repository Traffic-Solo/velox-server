"""Deterministic offline coverage for the Software Engineering operator CLI."""

import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest
from apps.server.src.integrations.software_engineering_task_cli import (
    DEFAULT_BASE_URL,
    DEFAULT_TARGET,
    DEFAULT_TIMEOUT_SECONDS,
    main,
)


@dataclass
class FakeVeloxApi:
    """Deterministic in-memory fake of the public VELOX HTTP API."""

    action_id: str = field(default_factory=lambda: str(uuid4()))
    base_branch: str = "main"
    pull_request_number: int = 42
    pull_request_url: str = "https://github.example/velox/velox-server/pull/42"
    commit_sha: str = "a" * 40
    review: dict[str, Any] = field(
        default_factory=lambda: {
            "changed_files": ["apps/server/src/api/tasks.py"],
            "untracked_files": ["tests/test_new.py"],
            "diff_stat": " 2 files changed",
            "diff": "diff --git a/tasks b/tasks",
            "diff_truncated": False,
            "dirty": True,
            "canonical_clean": True,
            "canonical_unchanged": True,
        }
    )
    health_status: int = 200
    execution_status: str = "succeeded"
    execution_reason: str | None = None
    disposition_recorded: str | None = None
    promotion_recorded: bool = False
    promotion_identity_mismatch: bool = False
    requests: list[tuple[str, str, dict[str, str], bytes]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers_snapshot = {
            key: value for key, value in request.headers.items()
        }
        self.requests.append(
            (request.method, request.url.path, headers_snapshot, request.content)
        )
        route = (request.method, request.url.path)

        if route == ("GET", "/health"):
            if self.health_status != 200:
                return httpx.Response(self.health_status)
            return httpx.Response(200, json={"status": "ok", "service": "VELOX"})

        if route == ("POST", "/tasks/software-engineering"):
            return httpx.Response(
                200,
                json={
                    "action_id": self.action_id,
                    "status": "awaiting_approval",
                    "permission_status": "requires_approval",
                    "routing_reason": "capability_route",
                },
            )

        if route == ("POST", f"/actions/{self.action_id}/approve"):
            return httpx.Response(
                200,
                json={
                    "status": "approved",
                    "action_id": self.action_id,
                    "lifecycle": {"status": "approved"},
                },
            )

        if route == ("POST", f"/tasks/software-engineering/{self.action_id}/execute"):
            return httpx.Response(
                200,
                json={
                    "action_id": self.action_id,
                    "processed": True,
                    "execution_status": self.execution_status,
                    "lifecycle_status": (
                        "completed" if self.execution_status == "succeeded" else "failed"
                    ),
                    "execution_reason": self.execution_reason,
                    "external_execution_performed": True,
                    "review_status": "available",
                    "review": self.review,
                },
            )

        if route == (
            "POST",
            f"/tasks/software-engineering/{self.action_id}/work-product/disposition",
        ):
            body = json.loads(request.content or b"{}")
            disposition = body.get("disposition")
            self.disposition_recorded = disposition
            keep = disposition == "keep"
            return httpx.Response(
                200,
                json={
                    "action_id": self.action_id,
                    "disposition": disposition,
                    "succeeded": True,
                    "worktree_present": keep,
                    "branch_present": keep,
                    "canonical_unchanged": True,
                    "remaining": [],
                },
            )

        if route == ("POST", f"/tasks/software-engineering/{self.action_id}/promote"):
            self.promotion_recorded = True
            return httpx.Response(
                200,
                json={
                    "action_id": self.action_id,
                    "commit_sha": self.commit_sha,
                    "pull_request_number": self.pull_request_number,
                    "pull_request_url": self.pull_request_url,
                    "base_branch": self.base_branch,
                    "head_branch": f"velox/se-{self.action_id}",
                    "pull_request_created": True,
                },
            )

        if route == ("GET", f"/tasks/software-engineering/{self.action_id}/status"):
            durable_number = (
                self.pull_request_number + 1
                if self.promotion_identity_mismatch
                else self.pull_request_number
            )
            return httpx.Response(
                200,
                json={
                    "action_id": self.action_id,
                    "target": DEFAULT_TARGET,
                    "phase": "promoted" if self.promotion_recorded else "kept",
                    "approval_status": "approved",
                    "claimed": True,
                    "worker_started": True,
                    "execution_status": "succeeded",
                    "external_execution_performed": True,
                    "disposition": "keep",
                    "claim_resolution": None,
                    "promoted": self.promotion_recorded,
                    "pull_request_number": (
                        durable_number if self.promotion_recorded else None
                    ),
                    "pull_request_url": (
                        self.pull_request_url if self.promotion_recorded else None
                    ),
                    "promotion_base_branch": (
                        self.base_branch if self.promotion_recorded else None
                    ),
                    "promotion_head_branch": (
                        f"velox/se-{self.action_id}" if self.promotion_recorded else None
                    ),
                    "reconciliation_options": [],
                    "retriable": False,
                    "state_token": "0" * 64,
                    "work_product": {
                        "status": "available",
                        "dirty": True,
                        "changed_files_count": 1,
                        "untracked_files_count": 1,
                        "canonical_clean": True,
                        "canonical_unchanged": True,
                    },
                },
            )

        return httpx.Response(404, json={"detail": "unmapped route"})


def scripted(answers: list[str]) -> Callable[[], str]:
    iterator: Iterator[str] = iter(answers)

    def read_line() -> str:
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover - defensive test guard
            raise AssertionError("CLI asked for more input than the test provided") from exc

    return read_line


def run_cli(
    api: FakeVeloxApi,
    *,
    answers: list[str],
    extra_argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    argv = [
        "--objective",
        "Add regression test for the operator CLI",
    ]
    if extra_argv is not None:
        argv.extend(extra_argv)
    out = io.StringIO()
    err = io.StringIO()
    transport = httpx.MockTransport(api.handler)
    exit_code = main(
        argv,
        transport=transport,
        read_line=scripted(answers),
        env=env if env is not None else {},
        out=out,
        err=err,
    )
    return exit_code, out.getvalue(), err.getvalue()


def test_happy_promote_walks_full_flow_and_verifies_pr_identity() -> None:
    api = FakeVeloxApi()

    exit_code, stdout, _ = run_cli(
        api,
        answers=[api.action_id, "keep", "promote", "Slice title", "Slice body"],
    )

    assert exit_code == 0
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert paths_visited == [
        ("GET", "/health"),
        ("POST", "/tasks/software-engineering"),
        ("POST", f"/actions/{api.action_id}/approve"),
        ("POST", f"/tasks/software-engineering/{api.action_id}/execute"),
        (
            "POST",
            f"/tasks/software-engineering/{api.action_id}/work-product/disposition",
        ),
        ("POST", f"/tasks/software-engineering/{api.action_id}/promote"),
        ("GET", f"/tasks/software-engineering/{api.action_id}/status"),
    ]
    assert api.disposition_recorded == "keep"
    assert api.promotion_recorded is True
    assert "Promoted." in stdout
    assert api.pull_request_url in stdout


def test_keep_only_without_promotion_finishes_in_kept_state() -> None:
    api = FakeVeloxApi()

    exit_code, stdout, _ = run_cli(
        api,
        answers=[api.action_id, "keep", "skip"],
    )

    assert exit_code == 0
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert ("POST", f"/tasks/software-engineering/{api.action_id}/promote") not in (
        paths_visited
    )
    assert api.disposition_recorded == "keep"
    assert api.promotion_recorded is False
    assert "Finished in kept state without promotion." in stdout


def test_discard_stops_without_promotion() -> None:
    api = FakeVeloxApi()

    exit_code, stdout, _ = run_cli(
        api,
        answers=[api.action_id, "discard"],
    )

    assert exit_code == 0
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert ("POST", f"/tasks/software-engineering/{api.action_id}/promote") not in (
        paths_visited
    )
    assert api.disposition_recorded == "discard"
    assert api.promotion_recorded is False
    assert "Discarded" in stdout


def test_rejected_approval_stops_before_execution() -> None:
    api = FakeVeloxApi()

    exit_code, _stdout, stderr = run_cli(
        api,
        answers=["not-the-action-id"],
    )

    assert exit_code == 1
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert paths_visited == [
        ("GET", "/health"),
        ("POST", "/tasks/software-engineering"),
    ]
    assert api.disposition_recorded is None
    assert api.promotion_recorded is False
    assert "Approval declined" in stderr


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:8000",
        "https://10.0.0.5:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?token=secret",
        "http://127.0.0.1:8000#fragment",
        "ftp://127.0.0.1:8000",
    ],
)
def test_non_loopback_or_credential_bearing_base_url_is_rejected(base_url: str) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP must not be contacted for an invalid base URL")

    out = io.StringIO()
    err = io.StringIO()
    exit_code = main(
        [
            "--objective",
            "verify base URL",
            "--base-url",
            base_url,
        ],
        transport=httpx.MockTransport(refuse),
        read_line=lambda: "unused",
        env={},
        out=out,
        err=err,
    )

    assert exit_code == 2
    assert "error" in err.getvalue().lower()


def test_http_failure_at_health_check_returns_non_zero_exit() -> None:
    api = FakeVeloxApi(health_status=500)

    exit_code, _stdout, stderr = run_cli(api, answers=[])

    assert exit_code == 1
    assert "health" in stderr.lower()
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert paths_visited == [("GET", "/health")]


def test_failed_execution_surfaces_safe_server_reason() -> None:
    api = FakeVeloxApi(
        execution_status="failed",
        execution_reason="timeout",
    )

    exit_code, _stdout, stderr = run_cli(
        api,
        answers=[api.action_id],
    )

    assert exit_code == 1
    assert "status='failed'" in stderr
    assert "reason='timeout'" in stderr
    paths_visited = [(method, path) for method, path, _headers, _body in api.requests]
    assert paths_visited == [
        ("GET", "/health"),
        ("POST", "/tasks/software-engineering"),
        ("POST", f"/actions/{api.action_id}/approve"),
        ("POST", f"/tasks/software-engineering/{api.action_id}/execute"),
    ]


def test_promotion_identity_mismatch_fails_after_publication() -> None:
    api = FakeVeloxApi(promotion_identity_mismatch=True)

    exit_code, _stdout, stderr = run_cli(
        api,
        answers=[api.action_id, "keep", "promote", "Slice title", ""],
    )

    assert exit_code == 1
    assert api.promotion_recorded is True
    assert "identity" in stderr.lower()


def test_api_token_is_sent_as_bearer_header_but_never_printed() -> None:
    api = FakeVeloxApi()

    exit_code, stdout, stderr = run_cli(
        api,
        answers=[api.action_id, "discard"],
        env={"VELOX_API_TOKEN": "sk-test-secret-123"},
    )

    assert exit_code == 0
    for _method, _path, headers, _body in api.requests:
        assert headers.get("authorization") == "Bearer sk-test-secret-123"
    assert "sk-test-secret-123" not in stdout
    assert "sk-test-secret-123" not in stderr


def test_default_base_url_is_loopback_and_timeout_covers_worker_budget() -> None:
    assert DEFAULT_BASE_URL.startswith("http://127.0.0.1")
    assert DEFAULT_TARGET == "velox-server"
    assert DEFAULT_TIMEOUT_SECONDS == 1_900.0


def test_non_positive_timeout_is_rejected_before_http() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP must not be contacted for an invalid timeout")

    err = io.StringIO()
    exit_code = main(
        [
            "--objective",
            "verify timeout",
            "--timeout-seconds",
            "0",
        ],
        transport=httpx.MockTransport(refuse),
        read_line=lambda: "unused",
        env={},
        out=io.StringIO(),
        err=err,
    )

    assert exit_code == 2
    assert "timeout-seconds must be positive" in err.getvalue()
