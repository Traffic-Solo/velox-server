"""Local operator CLI for one Software Engineering task over the public API.

Usage:

    uv run python -m apps.server.src.integrations.software_engineering_task_cli \\
        --objective "..." [--target velox-server] [--base-url http://127.0.0.1:8000]

The CLI talks only over the existing public VELOX API on a loopback host. It
never touches git, the filesystem or provider credentials directly. Every step
is driven by an explicit typed operator confirmation: the action UUID for
approval, ``keep`` or ``discard`` for the disposition, and ``promote`` for the
separate promotion decision. ``VELOX_API_TOKEN`` is read from the environment
when set, sent only in the ``Authorization`` header and never printed.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TARGET = "velox-server"
DEFAULT_TIMEOUT_SECONDS = 900.0

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DISPOSITION_KEEP = "keep"
DISPOSITION_DISCARD = "discard"
PROMOTE_TOKEN = "promote"

EXIT_OK = 0
EXIT_OPERATOR_ERROR = 1
EXIT_INVALID_INPUT = 2


class OperatorError(RuntimeError):
    """Bounded operator failure that is safe to print without provider detail."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Delegate one bounded Software Engineering task through the public "
            "VELOX API on a loopback host."
        ),
    )
    parser.add_argument(
        "--objective",
        required=True,
        help="Bounded engineering objective (caller-owned task text).",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"Logical work target (default: {DEFAULT_TARGET}).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Loopback-only base URL of the VELOX API.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout applied to every VELOX API call.",
    )
    return parser


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise OperatorError("base URL must use http or https")
    host = parts.hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise OperatorError(
            "base URL must target loopback (127.0.0.1, localhost, ::1)"
        )
    if parts.username is not None or parts.password is not None:
        raise OperatorError("base URL must not embed credentials")
    return base_url.rstrip("/")


def _bearer_headers(token: str | None) -> dict[str, str]:
    if token is None or not token.strip():
        return {}
    return {"Authorization": f"Bearer {token}"}


def _expect_status(response: httpx.Response, *, expected: int, step: str) -> None:
    if response.status_code != expected:
        raise OperatorError(f"{step} failed with HTTP {response.status_code}")


def _parse_json(response: httpx.Response, *, step: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise OperatorError(f"{step} returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise OperatorError(f"{step} returned an unexpected payload shape")
    return payload


def _run_health(client: httpx.Client, headers: Mapping[str, str]) -> None:
    response = client.get("/health", headers=dict(headers))
    _expect_status(response, expected=200, step="VELOX health check")
    payload = _parse_json(response, step="VELOX health check")
    if payload.get("status") != "ok":
        raise OperatorError("VELOX health status is not ok")


def _delegate(
    client: httpx.Client,
    headers: Mapping[str, str],
    *,
    objective: str,
    target: str,
) -> str:
    response = client.post(
        "/tasks/software-engineering",
        json={"objective": objective, "target": target},
        headers=dict(headers),
    )
    _expect_status(response, expected=200, step="task delegation")
    body = _parse_json(response, step="task delegation")
    if body.get("status") != "awaiting_approval":
        raise OperatorError(
            "task delegation did not enter awaiting_approval "
            f"(status={body.get('status')!r})"
        )
    action_id = body.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise OperatorError("task delegation did not return an action id")
    return action_id


def _approve(
    client: httpx.Client, headers: Mapping[str, str], *, action_id: str
) -> None:
    response = client.post(f"/actions/{action_id}/approve", headers=dict(headers))
    _expect_status(response, expected=200, step="action approval")


def _execute(
    client: httpx.Client, headers: Mapping[str, str], *, action_id: str
) -> Mapping[str, Any]:
    response = client.post(
        f"/tasks/software-engineering/{action_id}/execute",
        headers=dict(headers),
    )
    _expect_status(response, expected=200, step="exact execution")
    body = _parse_json(response, step="exact execution")
    if body.get("execution_status") != "succeeded":
        raise OperatorError(
            "exact execution did not succeed "
            f"(status={body.get('execution_status')!r})"
        )
    return body


def _apply_disposition(
    client: httpx.Client,
    headers: Mapping[str, str],
    *,
    action_id: str,
    disposition: str,
) -> Mapping[str, Any]:
    response = client.post(
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json={"disposition": disposition},
        headers=dict(headers),
    )
    _expect_status(response, expected=200, step=f"{disposition} disposition")
    body = _parse_json(response, step=f"{disposition} disposition")
    if body.get("disposition") != disposition or not body.get("succeeded"):
        raise OperatorError(f"{disposition} disposition did not succeed")
    return body


def _promote(
    client: httpx.Client,
    headers: Mapping[str, str],
    *,
    action_id: str,
    title: str,
    body: str,
) -> Mapping[str, Any]:
    response = client.post(
        f"/tasks/software-engineering/{action_id}/promote",
        json={"title": title, "body": body},
        headers=dict(headers),
    )
    _expect_status(response, expected=200, step="promotion")
    return _parse_json(response, step="promotion")


def _verify_promoted(
    client: httpx.Client,
    headers: Mapping[str, str],
    *,
    action_id: str,
    promotion: Mapping[str, Any],
) -> None:
    response = client.get(
        f"/tasks/software-engineering/{action_id}/status", headers=dict(headers)
    )
    _expect_status(response, expected=200, step="promotion verification")
    body = _parse_json(response, step="promotion verification")
    if not body.get("promoted"):
        raise OperatorError("durable status did not settle as promoted")
    identity_pairs = (
        ("pull_request_number", "pull_request_number"),
        ("pull_request_url", "pull_request_url"),
        ("promotion_base_branch", "base_branch"),
        ("promotion_head_branch", "head_branch"),
    )
    for status_key, promotion_key in identity_pairs:
        if body.get(status_key) != promotion.get(promotion_key):
            raise OperatorError(
                "durable promotion identity does not match the published pull request"
            )


def _render_review(review: Mapping[str, Any], *, out: TextIO) -> None:
    changed_raw = review.get("changed_files") or []
    untracked_raw = review.get("untracked_files") or []
    changed = [str(item) for item in changed_raw if isinstance(item, str)]
    untracked = [str(item) for item in untracked_raw if isinstance(item, str)]
    print("", file=out)
    print("=== VELOX work-product review ===", file=out)
    print(f"Changed files: {', '.join(changed) or '(none)'}", file=out)
    print(
        f"Untracked files (content not shown): {', '.join(untracked) or '(none)'}",
        file=out,
    )
    print(f"Canonical checkout clean: {review.get('canonical_clean')}", file=out)
    print(
        f"Canonical checkout unchanged: {review.get('canonical_unchanged')}",
        file=out,
    )
    print("--- diff --stat ---", file=out)
    diff_stat = str(review.get("diff_stat") or "").rstrip()
    print(diff_stat or "(no tracked changes)", file=out)
    print("--- diff ---", file=out)
    diff = str(review.get("diff") or "").rstrip()
    print(diff or "(no tracked changes)", file=out)
    if review.get("diff_truncated"):
        print("[diff truncated]", file=out)


def _prompt(question: str, *, out: TextIO, read_line: Callable[[], str]) -> str:
    print(question, file=out)
    return read_line().strip()


def _run_session(
    *,
    client: httpx.Client,
    headers: Mapping[str, str],
    objective: str,
    target: str,
    read_line: Callable[[], str],
    out: TextIO,
    err: TextIO,
) -> int:
    _run_health(client, headers)
    action_id = _delegate(client, headers, objective=objective, target=target)
    print("Task delegated and awaiting approval.", file=out)
    print(f"Action id: {action_id}", file=out)
    print("Type the action id to approve; anything else cancels:", file=out)
    if read_line().strip() != action_id:
        print("Approval declined; nothing was executed.", file=err)
        return EXIT_OPERATOR_ERROR

    _approve(client, headers, action_id=action_id)
    print("Approved. Running the worker...", file=out)
    execution = _execute(client, headers, action_id=action_id)
    review = execution.get("review")
    if not isinstance(review, Mapping):
        raise OperatorError("execution response is missing a bounded review")
    _render_review(review, out=out)

    disposition_answer = _prompt(
        "Type keep to preserve the work product, discard to remove it:",
        out=out,
        read_line=read_line,
    )
    if disposition_answer == DISPOSITION_DISCARD:
        _apply_disposition(
            client, headers, action_id=action_id, disposition=DISPOSITION_DISCARD
        )
        print("Discarded. No promotion is possible.", file=out)
        return EXIT_OK
    if disposition_answer != DISPOSITION_KEEP:
        print("No disposition applied; nothing changed.", file=err)
        return EXIT_OPERATOR_ERROR

    _apply_disposition(
        client, headers, action_id=action_id, disposition=DISPOSITION_KEEP
    )
    print("Kept.", file=out)

    promote_answer = _prompt(
        "Type promote to publish the work product, or anything else to finish "
        "in kept state:",
        out=out,
        read_line=read_line,
    )
    if promote_answer != PROMOTE_TOKEN:
        print("Finished in kept state without promotion.", file=out)
        return EXIT_OK

    title = _prompt("Pull request title:", out=out, read_line=read_line)
    body = _prompt(
        "Pull request body (one line, empty for none):",
        out=out,
        read_line=read_line,
    )
    promotion = _promote(
        client, headers, action_id=action_id, title=title, body=body
    )
    _verify_promoted(client, headers, action_id=action_id, promotion=promotion)
    print("Promoted.", file=out)
    pr_url = promotion.get("pull_request_url")
    if isinstance(pr_url, str):
        print(f"pull_request_url: {pr_url}", file=out)
    pr_number = promotion.get("pull_request_number")
    if isinstance(pr_number, int):
        print(f"pull_request_number: {pr_number}", file=out)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    read_line: Callable[[], str] = input,
    env: Mapping[str, str] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Run one Software Engineering operator session and return an exit code."""
    args = _parser().parse_args(argv)
    try:
        base_url = _validate_base_url(args.base_url)
    except OperatorError as error:
        print(f"error: {error}", file=err)
        return EXIT_INVALID_INPUT

    environment: Mapping[str, str] = os.environ if env is None else env
    token = environment.get("VELOX_API_TOKEN")
    headers = _bearer_headers(token)

    try:
        with httpx.Client(
            base_url=base_url,
            timeout=args.timeout_seconds,
            transport=transport,
        ) as client:
            return _run_session(
                client=client,
                headers=headers,
                objective=args.objective,
                target=args.target,
                read_line=read_line,
                out=out,
                err=err,
            )
    except OperatorError as error:
        print(f"error: {error}", file=err)
        return EXIT_OPERATOR_ERROR
    except httpx.HTTPError as error:
        print(f"HTTP transport failure: {error.__class__.__name__}", file=err)
        return EXIT_OPERATOR_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
