"""GitHub CLI implementation of the provider-neutral pull-request role."""

import json
from pathlib import Path
from typing import Any

from apps.server.src.integrations.pull_request import (
    PullRequestPublication,
    PullRequestPublicationError,
)
from apps.server.src.integrations.software_engineering import (
    ExecutableNotFoundError,
    ProcessRunner,
)


class GitHubCliPullRequestPublisher:
    """Create or recover a PR through an existing authenticated gh CLI session."""

    def __init__(
        self,
        runner: ProcessRunner,
        *,
        executable: str = "gh",
        timeout_seconds: float = 60.0,
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def _run(self, argv: list[str], *, cwd: Path) -> str:
        try:
            result = self._runner.run(
                [self._executable, *argv],
                cwd=cwd,
                timeout_seconds=self._timeout_seconds,
            )
        except ExecutableNotFoundError:
            raise PullRequestPublicationError(
                "pull-request publisher is unavailable"
            ) from None
        if result.returncode != 0 or result.timed_out:
            raise PullRequestPublicationError("pull-request publisher command failed")
        return result.stdout

    def _find(
        self,
        *,
        repository_root: Path,
        base_branch: str,
        head_branch: str,
    ) -> PullRequestPublication | None:
        raw = self._run(
            [
                "pr",
                "list",
                "--state",
                "all",
                "--head",
                head_branch,
                "--limit",
                "10",
                "--json",
                "number,url,headRefName,baseRefName",
            ],
            cwd=repository_root,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise PullRequestPublicationError(
                "pull-request publisher returned invalid data"
            ) from None
        if not isinstance(payload, list):
            raise PullRequestPublicationError(
                "pull-request publisher returned invalid data"
            )

        matches: list[dict[str, Any]] = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("headRefName") == head_branch
        ]
        if len(matches) > 1:
            raise PullRequestPublicationError(
                "multiple pull requests match the promotion branch"
            )
        if not matches:
            return None
        item = matches[0]
        if item.get("baseRefName") != base_branch:
            raise PullRequestPublicationError(
                "existing pull request targets an unexpected base"
            )
        number = item.get("number")
        url = item.get("url")
        if not isinstance(number, int) or not isinstance(url, str) or not url.strip():
            raise PullRequestPublicationError(
                "pull-request publisher returned invalid identity"
            )
        return PullRequestPublication(
            number=number,
            url=url,
            base_branch=base_branch,
            head_branch=head_branch,
            created=False,
        )

    def publish(
        self,
        *,
        repository_root: Path,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> PullRequestPublication:
        existing = self._find(
            repository_root=repository_root,
            base_branch=base_branch,
            head_branch=head_branch,
        )
        if existing is not None:
            return existing

        self._run(
            [
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                head_branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repository_root,
        )
        published = self._find(
            repository_root=repository_root,
            base_branch=base_branch,
            head_branch=head_branch,
        )
        if published is None:
            raise PullRequestPublicationError(
                "pull request was not discoverable after creation"
            )
        return PullRequestPublication(
            number=published.number,
            url=published.url,
            base_branch=published.base_branch,
            head_branch=published.head_branch,
            created=True,
        )
