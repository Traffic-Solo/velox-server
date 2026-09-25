"""Provider-neutral review and disposition of Software Engineering work products.

A work product is the uncommitted change a coding worker leaves in its per-Action
worktree. VELOX, not the provider, reviews it and applies the operator's
disposition, so any future provider (Claude Code, Codex, ...) gets the same flow.

Identity comes only from the trusted workspace and the Action UUID
(``TrustedGitWorkspace.expected_worktree``). Execution metadata, operator input
or model output never names a path or branch here. Every operation first proves,
through git's own worktree registry, that the derived path is a linked worktree
of the canonical repository on exactly the derived branch, and fails closed on
any mismatch. Review reads only git-generated status/diff output, never files.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from apps.server.src.integrations.software_engineering import (
    IsolatedWorktree,
    TrustedGitWorkspace,
    WorkspaceUnavailableError,
)

DEFAULT_DIFF_LIMIT = 20_000
DIFF_STAT_LIMIT = 4_000


class WorkProductIdentityError(Exception):
    """The derived worktree cannot be proven to be this Action's VELOX worktree."""


class WorkProductDisposition(StrEnum):
    KEEP = "keep"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class WorkProductReview:
    """Bounded, git-generated view of one Action's uncommitted work product."""

    action_id: UUID
    worktree_path: Path
    branch: str
    changed_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    diff_stat: str
    diff: str
    diff_truncated: bool
    dirty: bool
    canonical_clean: bool
    canonical_unchanged: bool


@dataclass(frozen=True, slots=True)
class WorkProductDispositionResult:
    """What a disposition did and what, if anything, still remains."""

    action_id: UUID
    disposition: WorkProductDisposition
    worktree_path: Path
    branch: str
    worktree_present: bool
    branch_present: bool
    canonical_unchanged: bool

    @property
    def remaining(self) -> tuple[str, ...]:
        """Resources a DISCARD failed to remove (always empty for KEEP)."""
        if self.disposition is WorkProductDisposition.KEEP:
            return ()
        left: list[str] = []
        if self.worktree_present:
            left.append("worktree")
        if self.branch_present:
            left.append("branch")
        return tuple(left)

    @property
    def succeeded(self) -> bool:
        return not self.remaining and self.canonical_unchanged


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _registered_worktrees(porcelain: str) -> list[tuple[Path, str | None]]:
    """(path, branch ref) per entry of ``git worktree list --porcelain``."""
    entries: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*porcelain.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = Path(line[len("worktree "):]), None
        elif line.startswith("branch "):
            branch = line[len("branch "):]
        elif not line and path is not None:
            entries.append((path, branch))
            path, branch = None, None
    return entries


class SoftwareEngineeringWorkProductService:
    """Review a per-Action worktree and apply KEEP or DISCARD to it."""

    def __init__(
        self, workspace: TrustedGitWorkspace, *, diff_limit: int = DEFAULT_DIFF_LIMIT,
    ) -> None:
        self._workspace = workspace
        self._diff_limit = diff_limit

    def _git(self, *args: str, cwd: Path) -> str:
        result = self._workspace.run_git(*args, cwd=cwd)
        if result.returncode != 0 or result.timed_out:
            raise WorkProductIdentityError("git command failed during verification")
        return result.stdout

    def _branch_exists(self, root: Path, branch: str) -> bool:
        result = self._workspace.run_git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root,
        )
        return result.returncode == 0 and not result.timed_out

    def _verified(self, action_id: UUID) -> tuple[Path, IsolatedWorktree, str]:
        """Return (canonical root, derived worktree, canonical status) or fail closed."""
        try:
            root = self._workspace.validate()
            canonical_status = self._workspace.status(root)
        except WorkspaceUnavailableError:
            raise WorkProductIdentityError("trusted workspace is unavailable") from None
        worktree = self._workspace.expected_worktree(action_id)
        if not worktree.path.is_dir():
            raise WorkProductIdentityError("isolated worktree does not exist")
        canonical = root.resolve()
        derived = worktree.path.resolve()
        if derived == canonical or canonical in derived.parents:
            raise WorkProductIdentityError("derived worktree overlaps the canonical checkout")
        entries = _registered_worktrees(self._git("worktree", "list", "--porcelain", cwd=root))
        if not entries or entries[0][0].resolve() != canonical:
            raise WorkProductIdentityError("canonical checkout is not the main worktree")
        matches = [branch for path, branch in entries[1:] if path.resolve() == derived]
        if matches != [f"refs/heads/{worktree.branch}"]:
            raise WorkProductIdentityError("derived worktree is not registered on its branch")
        top = self._git("rev-parse", "--show-toplevel", cwd=worktree.path).strip()
        if Path(top).resolve() != derived:
            raise WorkProductIdentityError("derived worktree is not its own checkout")
        return root, worktree, canonical_status

    def review(self, action_id: UUID) -> WorkProductReview:
        """Bounded review built only from git status and git diff output."""
        root, worktree, canonical_before = self._verified(action_id)
        try:
            status = self._workspace.status(worktree.path)
        except WorkspaceUnavailableError:
            raise WorkProductIdentityError("worktree status is unavailable") from None
        untracked = tuple(line[3:] for line in status.splitlines() if line.startswith("?? "))
        diff_options = ("--no-ext-diff", "--no-textconv", "--no-color")
        stat, _ = _clip(
            self._git("diff", *diff_options, "--stat", "HEAD", cwd=worktree.path),
            DIFF_STAT_LIMIT,
        )
        diff, truncated = _clip(
            self._git("diff", *diff_options, "HEAD", cwd=worktree.path), self._diff_limit,
        )
        try:
            canonical_after = self._workspace.status(root)
        except WorkspaceUnavailableError:
            raise WorkProductIdentityError("canonical status is unavailable") from None
        return WorkProductReview(
            action_id=action_id,
            worktree_path=worktree.path,
            branch=worktree.branch,
            changed_files=TrustedGitWorkspace.changed_files(status),
            untracked_files=untracked,
            diff_stat=stat,
            diff=diff,
            diff_truncated=truncated,
            dirty=bool(status.strip()),
            canonical_clean=not canonical_after.strip(),
            canonical_unchanged=canonical_after == canonical_before,
        )

    def apply(
        self, action_id: UUID, disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        """KEEP verifies and changes nothing; DISCARD removes exactly the derived pair."""
        root, worktree, canonical_before = self._verified(action_id)
        if disposition is WorkProductDisposition.KEEP:
            return WorkProductDispositionResult(
                action_id, disposition, worktree.path, worktree.branch,
                worktree_present=True, branch_present=True, canonical_unchanged=True,
            )
        # Workers cannot commit; a branch that did gain commits is not discarded.
        if self._git("rev-list", "--count", f"HEAD..refs/heads/{worktree.branch}",
                     cwd=root).strip() != "0":
            raise WorkProductIdentityError("branch has commits not in the canonical HEAD")
        # Forced because worker changes are intentionally uncommitted.
        self._workspace.run_git("worktree", "remove", "--force", str(worktree.path), cwd=root)
        worktree_present = worktree.path.exists()
        if not worktree_present:
            self._workspace.run_git("branch", "-D", worktree.branch, cwd=root)
        try:
            canonical_after = self._workspace.status(root)
        except WorkspaceUnavailableError:
            canonical_after = None
        return WorkProductDispositionResult(
            action_id, disposition, worktree.path, worktree.branch,
            worktree_present=worktree_present,
            branch_present=self._branch_exists(root, worktree.branch),
            canonical_unchanged=canonical_after == canonical_before,
        )
