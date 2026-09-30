"""Guarded VELOX-owned promotion of one kept Software Engineering work product."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

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
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringRunRepository,
    SoftwareEngineeringRunState,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
    WorkProductDisposition,
    WorkProductIdentityError,
)


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
        run_repository: SoftwareEngineeringRunRepository,
        workspace: TrustedGitWorkspace | None,
        work_products: SoftwareEngineeringWorkProductService | None,
        pull_request_publisher: PullRequestPublisher | None,
        enabled: bool,
        remote: str,
        base_branch: str,
        author_name: str,
        author_email: str,
    ) -> None:
        self._run_repository = run_repository
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

    def _verify_action(self, action_id: UUID) -> SoftwareEngineeringRunState:
        state = self._run_repository.get(action_id)
        if state is None:
            raise SoftwareEngineeringPromotionNotFoundError(
                "software engineering action was not found"
            )
        if (
            state.executor_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or state.capability != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringPromotionStateError(
                "software engineering durable route evidence is invalid"
            )
        if state.execution_status != "succeeded" or state.execution_finished_at is None:
            raise SoftwareEngineeringPromotionStateError(
                "software engineering action did not complete successfully"
            )
        if (
            state.disposition is not WorkProductDisposition.KEEP
            or state.disposition_succeeded is not True
            or state.worktree_present is not True
            or state.branch_present is not True
            or state.canonical_unchanged is not True
        ):
            raise SoftwareEngineeringPromotionStateError(
                "work product has not been explicitly kept"
            )
        return state

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
        state = self._verify_action(action_id)
        workspace = self._workspace
        work_products = self._work_products
        publisher = self._pull_request_publisher
        if workspace is None or work_products is None or publisher is None:
            raise SoftwareEngineeringPromotionStateError(
                "software engineering promotion is unavailable"
            )

        expected = workspace.expected_worktree(action_id)
        promotion_values = (
            state.promotion_commit_sha,
            state.pull_request_number,
            state.pull_request_url,
            state.promotion_base_branch,
            state.promotion_head_branch,
            state.promotion_finished_at,
        )
        if any(value is not None for value in promotion_values):
            if not all(value is not None for value in promotion_values):
                raise SoftwareEngineeringPromotionStateError(
                    "durable promotion state is incomplete"
                )
            if (
                state.promotion_base_branch != self._base_branch
                or state.promotion_head_branch != expected.branch
            ):
                raise SoftwareEngineeringPromotionStateError(
                    "durable promotion identity does not match trusted branches"
                )
            return SoftwareEngineeringPromotionResult(
                action_id=action_id,
                commit_sha=state.promotion_commit_sha or "",
                pull_request_number=state.pull_request_number or 0,
                pull_request_url=state.pull_request_url or "",
                base_branch=state.promotion_base_branch or "",
                head_branch=state.promotion_head_branch or "",
                pull_request_created=False,
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
        remote_base = self._git(
            "ls-remote",
            "--heads",
            self._remote,
            f"refs/heads/{self._base_branch}",
            cwd=root,
        ).split()
        if not remote_base or remote_base[0] != canonical_head:
            raise SoftwareEngineeringPromotionStateError(
                "trusted remote base does not match canonical HEAD"
            )
        commit_sha = self._create_or_recover_commit(
            action_id=action_id,
            root=root,
            worktree_path=expected.path,
            branch=expected.branch,
            canonical_head=canonical_head,
            dirty=review.dirty,
        )

        if (
            self._git("rev-parse", "HEAD", cwd=root) != canonical_head
            or workspace.status(root).strip()
        ):
            raise SoftwareEngineeringPromotionStateError(
                "canonical checkout changed before promotion push"
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
        self._run_repository.record_promotion(
            action_id=action_id,
            commit_sha=commit_sha,
            pull_request_number=publication.number,
            pull_request_url=publication.url,
            base_branch=publication.base_branch,
            head_branch=publication.head_branch,
            finished_at=datetime.now(UTC),
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
