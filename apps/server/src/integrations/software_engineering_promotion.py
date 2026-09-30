"""Guarded VELOX-owned promotion of one kept Software Engineering work product."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.pull_request import (
    PullRequestPublicationError,
    PullRequestPublisher,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    TrustedGitWorkspace,
    WorkspaceUnavailableError,
)
from apps.server.src.integrations.software_engineering_disposition import (
    SoftwareEngineeringDispositionRepository,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
    WorkProductDisposition,
    WorkProductIdentityError,
)
from apps.server.src.workers.runtime import InMemoryWorkerExecutionObserver


class SoftwareEngineeringPromotionNotFoundError(LookupError):
    """No governed Action lifecycle exists for the requested id."""


class SoftwareEngineeringPromotionStateError(RuntimeError):
    """Promotion preconditions or trusted git invariants are not satisfied."""


class SoftwareEngineeringPromotionExternalError(RuntimeError):
    """A bounded push or pull-request publication operation failed."""


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringPromotionResult:
    """Safe provider-neutral promotion result."""

    action_id: UUID
    commit_sha: str
    pull_request_number: int
    pull_request_url: str
    base_branch: str
    head_branch: str
    pull_request_created: bool


class SoftwareEngineeringPromotionService:
    """Commit, push and publish one verified kept work product under VELOX authority."""

    def __init__(
        self,
        *,
        lifecycle_repository: ActionLifecycleRepository,
        execution_observer: InMemoryWorkerExecutionObserver,
        disposition_repository: SoftwareEngineeringDispositionRepository,
        workspace: TrustedGitWorkspace | None,
        work_products: SoftwareEngineeringWorkProductService | None,
        pull_request_publisher: PullRequestPublisher | None,
        enabled: bool,
        remote: str,
        base_branch: str,
        author_name: str,
        author_email: str,
    ) -> None:
        self._lifecycle_repository = lifecycle_repository
        self._execution_observer = execution_observer
        self._disposition_repository = disposition_repository
        self._workspace = workspace
        self._work_products = work_products
        self._pull_request_publisher = pull_request_publisher
        self._enabled = enabled
        self._remote = remote
        self._base_branch = base_branch
        self._author_name = author_name
        self._author_email = author_email

    def _git(self, *args: str, cwd: Path) -> str:
        workspace = self._workspace
        if workspace is None:
            raise SoftwareEngineeringPromotionStateError(
                "trusted software engineering workspace is unavailable"
            )
        result = workspace.run_git(*args, cwd=cwd)
        if result.returncode != 0 or result.timed_out:
            raise SoftwareEngineeringPromotionStateError(
                "trusted git verification failed"
            )
        return result.stdout.strip()

    def _verify_action(self, action_id: UUID) -> None:
        lifecycle = self._lifecycle_repository.get(action_id)
        if lifecycle is None:
            raise SoftwareEngineeringPromotionNotFoundError(
                "software engineering action was not found"
            )
        if lifecycle.status is not ActionStatus.COMPLETED:
            raise SoftwareEngineeringPromotionStateError(
                "software engineering action did not complete successfully"
            )
        observation = next(
            (
                item
                for item in reversed(self._execution_observer.list())
                if item.action_id == action_id
            ),
            None,
        )
        if (
            observation is None
            or observation.finished_at is None
            or observation.status != "succeeded"
            or observation.requested_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or observation.requested_capability
            != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringPromotionStateError(
                "successful software engineering execution evidence is unavailable"
            )
        disposition = self._disposition_repository.get(action_id)
        if (
            disposition is None
            or disposition.disposition is not WorkProductDisposition.KEEP
            or not disposition.succeeded
            or not disposition.worktree_present
            or not disposition.branch_present
            or not disposition.canonical_unchanged
        ):
            raise SoftwareEngineeringPromotionStateError(
                "work product has not been explicitly kept"
            )

    def _verify_commit(
        self,
        *,
        action_id: UUID,
        root: Path,
        worktree_path: Path,
        branch: str,
        canonical_head: str,
        commit_sha: str,
    ) -> None:
        if self._git(
            "rev-list", "--count", f"{canonical_head}..{commit_sha}", cwd=root
        ) != "1":
            raise SoftwareEngineeringPromotionStateError(
                "promotion branch has unexpected commit history"
            )
        if self._git("rev-parse", f"{commit_sha}^", cwd=root) != canonical_head:
            raise SoftwareEngineeringPromotionStateError(
                "promotion commit does not descend directly from canonical HEAD"
            )
        message = self._git("show", "-s", "--format=%B", commit_sha, cwd=root)
        trailer = f"VELOX-Action: {action_id}"
        if trailer not in {line.strip() for line in message.splitlines()}:
            raise SoftwareEngineeringPromotionStateError(
                "promotion commit is missing trusted Action identity"
            )
        if self._git("rev-parse", branch, cwd=root) != commit_sha:
            raise SoftwareEngineeringPromotionStateError(
                "promotion branch does not point to the promotion commit"
            )
        workspace = self._workspace
        if workspace is None or workspace.status(worktree_path).strip():
            raise SoftwareEngineeringPromotionStateError(
                "promotion worktree is not clean after commit"
            )

    def _create_or_recover_commit(
        self,
        *,
        action_id: UUID,
        root: Path,
        worktree_path: Path,
        branch: str,
        canonical_head: str,
        dirty: bool,
    ) -> str:
        worktree_head = self._git("rev-parse", "HEAD", cwd=worktree_path)
        if worktree_head != canonical_head:
            if dirty:
                raise SoftwareEngineeringPromotionStateError(
                    "promotion worktree has both commits and uncommitted changes"
                )
            self._verify_commit(
                action_id=action_id,
                root=root,
                worktree_path=worktree_path,
                branch=branch,
                canonical_head=canonical_head,
                commit_sha=worktree_head,
            )
            return worktree_head

        if not dirty:
            raise SoftwareEngineeringPromotionStateError(
                "kept work product contains no promotable changes"
            )
        workspace = self._workspace
        if workspace is None:
            raise SoftwareEngineeringPromotionStateError(
                "trusted software engineering workspace is unavailable"
            )
        add = workspace.run_git("add", "--all", cwd=worktree_path)
        if add.returncode != 0 or add.timed_out:
            raise SoftwareEngineeringPromotionStateError(
                "work product could not be staged"
            )
        staged = workspace.run_git(
            "diff", "--cached", "--quiet", cwd=worktree_path
        )
        if staged.timed_out or staged.returncode not in {0, 1}:
            raise SoftwareEngineeringPromotionStateError(
                "staged diff could not be verified"
            )
        if staged.returncode == 0:
            raise SoftwareEngineeringPromotionStateError(
                "kept work product contains no promotable changes"
            )

        subject = f"VELOX: promote software engineering action {action_id}"
        trailer = f"VELOX-Action: {action_id}"
        commit = workspace.run_git(
            "-c",
            f"user.name={self._author_name}",
            "-c",
            f"user.email={self._author_email}",
            "commit",
            "--no-verify",
            "-m",
            subject,
            "-m",
            trailer,
            cwd=worktree_path,
        )
        if commit.returncode != 0 or commit.timed_out:
            raise SoftwareEngineeringPromotionStateError(
                "VELOX promotion commit failed"
            )
        commit_sha = self._git("rev-parse", "HEAD", cwd=worktree_path)
        self._verify_commit(
            action_id=action_id,
            root=root,
            worktree_path=worktree_path,
            branch=branch,
            canonical_head=canonical_head,
            commit_sha=commit_sha,
        )
        return commit_sha

    def promote(
        self,
        action_id: UUID,
        *,
        title: str,
        body: str,
    ) -> SoftwareEngineeringPromotionResult:
        if not self._enabled:
            raise SoftwareEngineeringPromotionStateError(
                "software engineering promotion is disabled"
            )
        self._verify_action(action_id)
        workspace = self._workspace
        work_products = self._work_products
        publisher = self._pull_request_publisher
        if workspace is None or work_products is None or publisher is None:
            raise SoftwareEngineeringPromotionStateError(
                "software engineering promotion is unavailable"
            )
        try:
            root = workspace.validate()
            review = work_products.review(action_id)
        except (WorkspaceUnavailableError, WorkProductIdentityError):
            raise SoftwareEngineeringPromotionStateError(
                "software engineering work product is unavailable or unverifiable"
            ) from None

        if not review.canonical_clean or not review.canonical_unchanged:
            raise SoftwareEngineeringPromotionStateError(
                "canonical checkout changed during promotion verification"
            )
        current_branch = self._git(
            "symbolic-ref", "--quiet", "--short", "HEAD", cwd=root
        )
        if current_branch != self._base_branch:
            raise SoftwareEngineeringPromotionStateError(
                "canonical checkout is not on the trusted base branch"
            )
        if workspace.status(root).strip():
            raise SoftwareEngineeringPromotionStateError(
                "canonical checkout is not clean"
            )
        if not self._remote or self._remote.startswith("-"):
            raise SoftwareEngineeringPromotionStateError(
                "trusted promotion remote is invalid"
            )
        if not self._base_branch or self._base_branch.startswith("-"):
            raise SoftwareEngineeringPromotionStateError(
                "trusted promotion base branch is invalid"
            )
        self._git("remote", "get-url", self._remote, cwd=root)
        canonical_head = self._git("rev-parse", "HEAD", cwd=root)
        expected = workspace.expected_worktree(action_id)
        commit_sha = self._create_or_recover_commit(
            action_id=action_id,
            root=root,
            worktree_path=expected.path,
            branch=expected.branch,
            canonical_head=canonical_head,
            dirty=review.dirty,
        )

        push = workspace.run_git(
            "push",
            "--porcelain",
            self._remote,
            f"refs/heads/{expected.branch}:refs/heads/{expected.branch}",
            cwd=root,
        )
        if push.returncode != 0 or push.timed_out:
            raise SoftwareEngineeringPromotionExternalError(
                "promotion push failed"
            )
        remote_head = self._git(
            "ls-remote",
            "--heads",
            self._remote,
            f"refs/heads/{expected.branch}",
            cwd=root,
        ).split()
        if not remote_head or remote_head[0] != commit_sha:
            raise SoftwareEngineeringPromotionExternalError(
                "promotion branch could not be verified on the remote"
            )

        try:
            publication = publisher.publish(
                repository_root=root,
                base_branch=self._base_branch,
                head_branch=expected.branch,
                title=title,
                body=body,
            )
        except PullRequestPublicationError:
            raise SoftwareEngineeringPromotionExternalError(
                "pull-request publication failed"
            ) from None
        if (
            publication.base_branch != self._base_branch
            or publication.head_branch != expected.branch
        ):
            raise SoftwareEngineeringPromotionExternalError(
                "pull-request identity did not match trusted promotion branches"
            )
        return SoftwareEngineeringPromotionResult(
            action_id=action_id,
            commit_sha=commit_sha,
            pull_request_number=publication.number,
            pull_request_url=publication.url,
            base_branch=publication.base_branch,
            head_branch=publication.head_branch,
            pull_request_created=publication.created,
        )
