"""Operator-controlled live Sprint 4 Software Engineering acceptance pilot.

Run this only against a local VELOX Server that was explicitly started with the
live Software Engineering provider and guarded promotion enabled.

    uv run --env-file .env.live python -m \
        apps.server.src.integrations.software_engineering_acceptance

The harness is deliberately only an HTTP client. It never imports or invokes
TaskDelegator, WorkerRuntime, work-product services or promotion services
directly, so a successful run proves the public control-plane path.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 1_900.0
_TARGET = "velox-server"
_ACCEPTANCE_LOG = "docs/engineering/acceptance/SPRINT4_LIVE_ACCEPTANCE.md"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SoftwareEngineeringAcceptanceError(RuntimeError):
    """The live acceptance pilot failed without exposing response internals."""


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringAcceptanceResult:
    """Safe acceptance evidence returned after the final promoted status."""

    action_id: UUID
    marker_path: str
    pull_request_number: int
    pull_request_url: str
    commit_sha: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one operator-controlled live VELOX Software Engineering acceptance.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Loopback VELOX Server URL (default: http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-request timeout; exact execution may legitimately take several minutes.",
    )
    return parser


def _validated_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SoftwareEngineeringAcceptanceError(
            "acceptance base URL must be an uncredentialed loopback HTTP(S) URL"
        )
    path = parsed.path.rstrip("/")
    if path:
        raise SoftwareEngineeringAcceptanceError(
            "acceptance base URL must not contain a path"
        )
    return value.rstrip("/")


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json_body: dict[str, object] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    try:
        response = client.request(method, path, json=json_body)
    except httpx.HTTPError as error:
        raise SoftwareEngineeringAcceptanceError(
            f"{method} {path} could not reach the local VELOX Server"
        ) from error
    if response.status_code != expected_status:
        raise SoftwareEngineeringAcceptanceError(
            f"{method} {path} returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise SoftwareEngineeringAcceptanceError(
            f"{method} {path} returned invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise SoftwareEngineeringAcceptanceError(
            f"{method} {path} returned an unexpected response shape"
        )
    return payload


def _acceptance_objective(marker_line: str) -> str:
    return (
        f"Append exactly this one new line to {_ACCEPTANCE_LOG}:\n"
        f"{marker_line}\n"
        "Do not modify, rename, delete, or create any other file. "
        "Do not alter any existing line in that file. "
        "Do not run git commit, git push, or gh commands. "
        "Stop immediately after the requested line has been appended."
    )


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SoftwareEngineeringAcceptanceError(
            f"acceptance response field {key!r} is missing"
        )
    return value


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SoftwareEngineeringAcceptanceError(
            f"acceptance response field {key!r} is missing"
        )
    return value


def _show_review(
    execution: dict[str, Any],
    *,
    expected_line: str,
    out: TextIO,
) -> None:
    if execution.get("review_status") != "available":
        raise SoftwareEngineeringAcceptanceError(
            "live execution completed without an available work-product review"
        )
    review = execution.get("review")
    if not isinstance(review, dict):
        raise SoftwareEngineeringAcceptanceError(
            "live execution returned no bounded work-product review"
        )
    if (
        review.get("canonical_clean") is not True
        or review.get("canonical_unchanged") is not True
        or review.get("dirty") is not True
    ):
        raise SoftwareEngineeringAcceptanceError(
            "live work-product review failed canonical checkout invariants"
        )
    changed = review.get("changed_files")
    untracked = review.get("untracked_files")
    if changed != [_ACCEPTANCE_LOG] or untracked != []:
        raise SoftwareEngineeringAcceptanceError(
            "live worker modified files outside the exact acceptance target"
        )
    diff = str(review.get("diff", ""))
    if expected_line not in diff:
        raise SoftwareEngineeringAcceptanceError(
            "live work-product diff does not contain the exact acceptance marker"
        )
    print(
        "=== VELOX bounded work-product review ===",
        f"changed_files: {changed}",
        f"untracked_files: {untracked}",
        f"canonical_clean: {review['canonical_clean']}",
        f"canonical_unchanged: {review['canonical_unchanged']}",
        "--- diff --stat ---",
        str(review.get("diff_stat", "")).rstrip() or "(none)",
        "--- diff ---",
        diff.rstrip() or "(none)",
        sep="\n",
        file=out,
    )


def run_acceptance(
    client: httpx.Client,
    *,
    read_line: Callable[[], str],
    out: TextIO,
    now: datetime | None = None,
    marker_suffix: str | None = None,
) -> SoftwareEngineeringAcceptanceResult:
    """Run one live acceptance through only the public HTTP control plane."""
    current = now or datetime.now(UTC)
    suffix = marker_suffix or uuid4().hex[:8]
    stamp = current.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    marker_path = _ACCEPTANCE_LOG
    marker_line = (
        f"- Acceptance {stamp}-{suffix}: "
        "VELOX live Software Engineering control plane."
    )

    health = _request_json(client, "GET", "/health")
    if health.get("status") != "ok":
        raise SoftwareEngineeringAcceptanceError("VELOX health check is not ok")

    delegation = _request_json(
        client,
        "POST",
        "/tasks/software-engineering",
        json_body={
            "objective": _acceptance_objective(marker_line),
            "target": _TARGET,
        },
    )
    if delegation.get("status") != "awaiting_approval":
        raise SoftwareEngineeringAcceptanceError(
            "acceptance task was not held for explicit approval"
        )
    action_id = UUID(_require_text(delegation, "action_id"))

    print(
        f"Action: {action_id}",
        f"Acceptance marker: {marker_path}",
        "Type the Action UUID exactly to approve the live Claude Code execution:",
        sep="\n",
        file=out,
    )
    if read_line().strip() != str(action_id):
        raise SoftwareEngineeringAcceptanceError(
            "operator did not approve the live acceptance Action"
        )

    approved = _request_json(
        client,
        "POST",
        f"/actions/{action_id}/approve",
    )
    lifecycle = approved.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("status") != "approved":
        raise SoftwareEngineeringAcceptanceError(
            "live acceptance Action was not approved"
        )

    print("Approved. Executing exact Action through VELOX...", file=out)
    execution = _request_json(
        client,
        "POST",
        f"/tasks/software-engineering/{action_id}/execute",
    )
    if (
        execution.get("processed") is not True
        or execution.get("execution_status") != "succeeded"
        or execution.get("lifecycle_status") != "completed"
        or execution.get("external_execution_performed") is not True
    ):
        raise SoftwareEngineeringAcceptanceError(
            "live Software Engineering execution did not complete successfully"
        )
    _show_review(execution, expected_line=marker_line, out=out)

    executed_status = _request_json(
        client,
        "GET",
        f"/tasks/software-engineering/{action_id}/status",
    )
    if executed_status.get("phase") != "succeeded":
        raise SoftwareEngineeringAcceptanceError(
            "durable run status did not reach succeeded"
        )

    print("Type keep to preserve this reviewed work product:", file=out)
    if read_line().strip() != "keep":
        raise SoftwareEngineeringAcceptanceError(
            "operator did not KEEP the acceptance work product"
        )
    disposition = _request_json(
        client,
        "POST",
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json_body={"disposition": "keep"},
    )
    if (
        disposition.get("disposition") != "keep"
        or disposition.get("succeeded") is not True
        or disposition.get("canonical_unchanged") is not True
    ):
        raise SoftwareEngineeringAcceptanceError(
            "acceptance KEEP disposition failed"
        )

    kept_status = _request_json(
        client,
        "GET",
        f"/tasks/software-engineering/{action_id}/status",
    )
    if kept_status.get("phase") != "kept":
        raise SoftwareEngineeringAcceptanceError(
            "durable run status did not reach kept"
        )

    print(
        "KEEP verified. Promotion will create a VELOX commit, push the "
        "Action-derived branch and create a real GitHub PR.",
        "Type promote to continue:",
        sep="\n",
        file=out,
    )
    if read_line().strip() != "promote":
        raise SoftwareEngineeringAcceptanceError(
            "operator did not approve guarded promotion"
        )

    title = f"VELOX Sprint 4 live acceptance {stamp}"
    promotion = _request_json(
        client,
        "POST",
        f"/tasks/software-engineering/{action_id}/promote",
        json_body={
            "title": title,
            "body": (
                "Live Sprint 4 acceptance produced through the canonical VELOX "
                f"Software Engineering control plane.\n\nAction: {action_id}"
            ),
        },
    )
    commit_sha = _require_text(promotion, "commit_sha")
    pr_number = _require_int(promotion, "pull_request_number")
    pr_url = _require_text(promotion, "pull_request_url")

    final_status = _request_json(
        client,
        "GET",
        f"/tasks/software-engineering/{action_id}/status",
    )
    if (
        final_status.get("phase") != "promoted"
        or final_status.get("promoted") is not True
        or final_status.get("pull_request_number") != pr_number
        or final_status.get("pull_request_url") != pr_url
    ):
        raise SoftwareEngineeringAcceptanceError(
            "final durable status did not match the promoted pull request"
        )

    print(
        "Sprint 4 live acceptance PASSED.",
        f"Action: {action_id}",
        f"Commit: {commit_sha}",
        f"Pull request: #{pr_number} {pr_url}",
        sep="\n",
        file=out,
    )
    return SoftwareEngineeringAcceptanceResult(
        action_id=action_id,
        marker_path=marker_path,
        pull_request_number=pr_number,
        pull_request_url=pr_url,
        commit_sha=commit_sha,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    read_line: Callable[[], str] = input,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        base_url = _validated_base_url(args.base_url)
        if args.timeout_seconds <= 0:
            raise SoftwareEngineeringAcceptanceError(
                "acceptance timeout must be positive"
            )
        headers: dict[str, str] = {}
        api_token = os.environ.get("VELOX_API_TOKEN")
        if api_token is not None and api_token.strip():
            headers["Authorization"] = f"Bearer {api_token}"
        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(args.timeout_seconds),
        ) as client:
            run_acceptance(client, read_line=read_line, out=out)
    except (
        SoftwareEngineeringAcceptanceError,
        ValueError,
    ) as error:
        print(f"Acceptance failed: {error}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
