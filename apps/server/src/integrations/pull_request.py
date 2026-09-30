"""Provider-neutral pull-request publication role."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PullRequestPublicationError(RuntimeError):
    """Pull-request publication or recovery failed."""


@dataclass(frozen=True, slots=True)
class PullRequestPublication:
    """Provider-neutral pull-request identity returned after publication."""

    number: int
    url: str
    base_branch: str
    head_branch: str
    created: bool


class PullRequestPublisher(Protocol):
    """Role: create or recover one pull request for an already-pushed branch."""

    def publish(
        self,
        *,
        repository_root: Path,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> PullRequestPublication:
        ...
