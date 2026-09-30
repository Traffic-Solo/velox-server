"""Offline promotion coverage with real git repositories and no network calls."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.github_pull_request import GitHubCliPullRequestPublisher
from apps.server.src.integrations.pull_request import (
    PullRequestPublication,
    PullRequestPublicationError,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    ProcessResult,
    SubprocessRunner,
    TrustedGitWorkspace,
)
from apps.server.src.integrations.software_engineering_promotion import (
    SoftwareEngineeringPromotionService,
    SoftwareEngineeringPromotionStateError,
)
from apps.server.src.integrations.software_engineering_state import (
    InMemorySoftwareEngineeringRunRepository,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
    WorkProductDisposition,
    WorkProductDispositionResult,
)

GIT_USER = ("-c", "user.email=velox@example.test", "-c", "user.name=VELOX Test")


def git(cwd: Path, *args: str) -> str:
    result = SubprocessRunner().run(
        ["git", *GIT_USER, *args],
        cwd=cwd,
        timeout_seconds=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare")

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hello')\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str, str, str]] = []

    def publish(
        self,
        *,
        repository_root: Path,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> PullRequestPublication:
        self.calls.append((repository_root, base_branch, head_branch, title, body))
        return PullRequestPublication(
            number=31,
            url="https://github.example/owner/repo/pull/31",
            base_branch=base_branch,
            head_branch=head_branch,
            created=len(self.calls) == 1,
        )


def engineering_action(action_id: UUID) -> Action:
    return Action(
        id=action_id,
        type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
        payload={
            "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            "objective": "Change app greeting",
        },
    )


def promotion_fixture(
    tmp_path: Path,
) -> tuple[
    SoftwareEngineeringPromotionService,
    TrustedGitWorkspace,
    Path,
    UUID,
    FakePublisher,
]:
    repo, _ = make_repo(tmp_path)
    workspace = TrustedGitWorkspace(
        repo,
        SubprocessRunner(),
        worktrees_root=tmp_path / "worktrees",
    )
    action_id = uuid4()
    action = engineering_action(action_id)
    worktree = workspace.create_worktree(action_id)
    (worktree.path / "app.py").write_text("print('hello, velox')\n")

    run_repository = InMemorySoftwareEngineeringRunRepository()
    run_repository.register_action(
        action_id=action_id,
        target=action.target,
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
    )
    run_repository.record_execution(
        action_id=action_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        external_execution_performed=True,
    )
    run_repository.record_disposition(
        WorkProductDispositionResult(
            action_id=action_id,
            disposition=WorkProductDisposition.KEEP,
            worktree_path=worktree.path,
            branch=worktree.branch,
            worktree_present=True,
            branch_present=True,
            canonical_unchanged=True,
        )
    )
    publisher = FakePublisher()
    service = SoftwareEngineeringPromotionService(
        run_repository=run_repository,
        workspace=workspace,
        work_products=SoftwareEngineeringWorkProductService(workspace),
        pull_request_publisher=publisher,
        enabled=True,
        remote="origin",
        base_branch="main",
        author_name="VELOX",
        author_email="velox@example.test",
    )
    return service, workspace, repo, action_id, publisher


def test_promotion_creates_one_velox_commit_pushes_exact_branch_and_publishes_pr(
    tmp_path: Path,
) -> None:
    service, workspace, repo, action_id, publisher = promotion_fixture(tmp_path)
    canonical_before = git(repo, "rev-parse", "HEAD")

    result = service.promote(action_id, title="Slice 10", body="Guarded promotion")

    expected = workspace.expected_worktree(action_id)
    assert result.action_id == action_id
    assert result.base_branch == "main"
    assert result.head_branch == expected.branch
    assert result.pull_request_number == 31
    assert result.pull_request_created is True
    assert git(repo, "rev-parse", "HEAD") == canonical_before
    assert workspace.status(repo) == ""
    assert workspace.status(expected.path) == ""
    assert git(expected.path, "rev-list", "--count", f"{canonical_before}..HEAD") == "1"
    message = git(expected.path, "show", "-s", "--format=%B", "HEAD")
    assert f"VELOX-Action: {action_id}" in message
    remote_head = git(
        repo, "ls-remote", "--heads", "origin", f"refs/heads/{expected.branch}"
    ).split()[0]
    assert remote_head == result.commit_sha
    assert publisher.calls == [
        (repo, "main", expected.branch, "Slice 10", "Guarded promotion")
    ]


def test_promotion_retry_recovers_same_commit_instead_of_creating_second_commit(
    tmp_path: Path,
) -> None:
    service, workspace, repo, action_id, publisher = promotion_fixture(tmp_path)
    canonical = git(repo, "rev-parse", "HEAD")

    first = service.promote(action_id, title="Slice 10", body="")
    second = service.promote(action_id, title="Slice 10", body="")

    expected = workspace.expected_worktree(action_id)
    assert second.commit_sha == first.commit_sha
    assert git(expected.path, "rev-list", "--count", f"{canonical}..HEAD") == "1"
    assert len(publisher.calls) == 1
    assert second.pull_request_created is False


def test_promotion_refuses_without_explicit_keep(tmp_path: Path) -> None:
    service, _, _, action_id, publisher = promotion_fixture(tmp_path)
    state = service._run_repository.get(action_id)
    assert state is not None
    fresh = InMemorySoftwareEngineeringRunRepository()
    fresh.register_action(
        action_id=action_id,
        target=state.target,
        executor_role=state.executor_role,
        capability=state.capability,
    )
    fresh.record_execution(
        action_id=action_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        external_execution_performed=True,
    )
    service._run_repository = fresh

    with pytest.raises(SoftwareEngineeringPromotionStateError, match="explicitly kept"):
        service.promote(action_id, title="Slice 10", body="")
    assert publisher.calls == []


def test_promotion_refuses_if_canonical_head_changed_after_worker_branch(
    tmp_path: Path,
) -> None:
    service, _, repo, action_id, publisher = promotion_fixture(tmp_path)
    (repo / "other.txt").write_text("canonical change\n")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-q", "-m", "advance canonical")

    with pytest.raises(SoftwareEngineeringPromotionStateError):
        service.promote(action_id, title="Slice 10", body="")
    assert publisher.calls == []


def test_promotion_refuses_when_remote_base_advanced_past_local_canonical(
    tmp_path: Path,
) -> None:
    service, _, repo, action_id, publisher = promotion_fixture(tmp_path)
    canonical = git(repo, "rev-parse", "HEAD")
    (repo / "remote-only.txt").write_text("remote advance\n")
    git(repo, "add", "remote-only.txt")
    git(repo, "commit", "-q", "-m", "advance remote base")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "reset", "--hard", canonical)

    with pytest.raises(
        SoftwareEngineeringPromotionStateError,
        match="remote base",
    ):
        service.promote(action_id, title="Slice 10", body="")
    assert publisher.calls == []


class FakeGhRunner:
    def __init__(self, responses: list[ProcessResult]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        self.calls.append(list(argv))
        return self.responses.pop(0)


def process(stdout: str = "", returncode: int = 0) -> ProcessResult:
    return ProcessResult(returncode, stdout, "", False, 0.01)


def test_github_publisher_recovers_existing_exact_pr_without_create(tmp_path: Path) -> None:
    payload = json.dumps(
        [{
            "number": 42,
            "url": "https://github.example/owner/repo/pull/42",
            "headRefName": "velox/se-action",
            "baseRefName": "main",
        }]
    )
    runner = FakeGhRunner([process(payload)])
    publisher = GitHubCliPullRequestPublisher(runner)

    result = publisher.publish(
        repository_root=tmp_path,
        base_branch="main",
        head_branch="velox/se-action",
        title="Ignored",
        body="Ignored",
    )

    assert result.number == 42
    assert result.created is False
    assert len(runner.calls) == 1
    assert runner.calls[0][:3] == ["gh", "pr", "list"]


def test_github_publisher_rejects_existing_pr_with_unexpected_base(tmp_path: Path) -> None:
    payload = json.dumps(
        [{
            "number": 42,
            "url": "https://github.example/owner/repo/pull/42",
            "headRefName": "velox/se-action",
            "baseRefName": "release",
        }]
    )
    publisher = GitHubCliPullRequestPublisher(FakeGhRunner([process(payload)]))

    with pytest.raises(PullRequestPublicationError, match="unexpected base"):
        publisher.publish(
            repository_root=tmp_path,
            base_branch="main",
            head_branch="velox/se-action",
            title="Slice 10",
            body="",
        )



def test_github_publisher_creates_then_reads_back_exact_pr(tmp_path: Path) -> None:
    empty = json.dumps([])
    created = process("https://github.example/owner/repo/pull/77\n")
    found = json.dumps(
        [{
            "number": 77,
            "url": "https://github.example/owner/repo/pull/77",
            "headRefName": "velox/se-action",
            "baseRefName": "main",
        }]
    )
    runner = FakeGhRunner([process(empty), created, process(found)])
    publisher = GitHubCliPullRequestPublisher(runner)

    result = publisher.publish(
        repository_root=tmp_path,
        base_branch="main",
        head_branch="velox/se-action",
        title="Literal title",
        body="Literal body",
    )

    assert result.number == 77
    assert result.created is True
    assert [call[:3] for call in runner.calls] == [
        ["gh", "pr", "list"],
        ["gh", "pr", "create"],
        ["gh", "pr", "list"],
    ]
    create_call = runner.calls[1]
    assert create_call[create_call.index("--title") + 1] == "Literal title"
    assert create_call[create_call.index("--body") + 1] == "Literal body"
