"""Offline coverage for the Software Engineering Role and Claude Code provider."""

import io
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import approve_pending_action
from apps.server.src.core.config import Settings, get_settings
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import TaskDelegationRequest, TaskDelegationStatus
from apps.server.src.integrations import software_engineering_pilot
from apps.server.src.integrations import software_engineering_runtime as runtime
from apps.server.src.integrations.claude_code import (
    CLAUDE_CODE_ALLOWED_BASH,
    CLAUDE_CODE_DISALLOWED_BASH_AND_WEB,
    CLAUDE_CODE_PROVIDER,
    CLAUDE_CODE_TOOLS,
    ClaudeCodeSoftwareEngineeringExecutor,
    build_claude_code_argv,
    worktree_file_rules,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    ExecutableNotFoundError,
    ProcessResult,
    SubprocessRunner,
    TrustedGitWorkspace,
    WorkspaceUnavailableError,
)
from apps.server.src.workers.executor import (
    WorkerCapability,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

OBJECTIVE = "Add a docstring to the health endpoint"
REPORT = {
    "summary": "Added the docstring.",
    "files_changed": ["apps/server/src/main.py"],
    "validation": ["uv run pytest -q: passed"],
    "blockers": [],
}
WORKTREE_STATUS = " M apps/server/src/main.py\n?? tests/test_new.py\n"


def result(returncode: int | None = 0, stdout: str = "", stderr: str = "", *,
           timed_out: bool = False) -> ProcessResult:
    return ProcessResult(returncode, stdout, stderr, timed_out, 1.5)


def claude_output(**overrides: Any) -> str:
    envelope: dict[str, Any] = {
        "type": "result", "subtype": "success", "is_error": False, "result": "done",
        "session_id": "session-1", "structured_output": REPORT,
    }
    envelope.update(overrides)
    return json.dumps(envelope)


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    stdin: str | None
    env: dict[str, str] | None
    timeout: float


@dataclass
class FakeRunner:
    """Deterministic stand-in for git and the Claude CLI; never spawns processes."""

    root: Path
    version: str | Exception = "2.1.153 (Claude Code)"
    claude: ProcessResult | Exception = field(
        default_factory=lambda: result(stdout=claude_output()),
    )
    toplevel_rc: int = 0
    toplevel: str | None = None
    worktree_add_rc: int = 0
    canonical_statuses: list[str] = field(default_factory=lambda: ["", ""])
    worktree_status: str = WORKTREE_STATUS
    calls: list[Call] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, cwd: Path, timeout_seconds: float,
            stdin: str | None = None, env: Mapping[str, str] | None = None) -> ProcessResult:
        self.calls.append(Call(list(argv), cwd, stdin, dict(env) if env else None,
                               timeout_seconds))
        if argv[0] == "git":
            if argv[1] == "rev-parse":
                return result(self.toplevel_rc, f"{self.toplevel or self.root}\n")
            if argv[1] == "worktree":
                return result(self.worktree_add_rc)
            if cwd == self.root:
                return result(stdout=self.canonical_statuses.pop(0))
            return result(stdout=self.worktree_status)
        if list(argv[1:]) == ["--version"]:
            if isinstance(self.version, Exception):
                raise self.version
            return result(stdout=self.version)
        if isinstance(self.claude, Exception):
            raise self.claude
        return self.claude

    def claude_calls(self) -> list[Call]:
        return [call for call in self.calls if call.argv[:2] == ["claude", "-p"]]

    def git_calls(self, sub: str) -> list[Call]:
        return [call for call in self.calls if call.argv[:2] == ["git", sub]]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def make_executor(runner: FakeRunner, tmp_path: Path) -> ClaudeCodeSoftwareEngineeringExecutor:
    return ClaudeCodeSoftwareEngineeringExecutor(
        workspace=TrustedGitWorkspace(runner.root, runner, worktrees_root=tmp_path / "wt"),
        runner=runner,
        executable="claude",
        timeout_seconds=5,
        environment={
            "PATH": "/usr/bin", "HOME": "/home/operator", "CLAUDECODE": "1",
            "VELOX_API_TOKEN": "velox-secret-token",
        },
    )


def engineering_action(objective: str = OBJECTIVE) -> Action:
    return Action(
        type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
        payload={"capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY, "objective": objective},
    )


def run(executor: ClaudeCodeSoftwareEngineeringExecutor,
        action: Action | None = None) -> WorkerExecutionResult:
    return executor.execute(
        action or engineering_action(), capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    )


def assert_failed(outcome: WorkerExecutionResult, reason: str,
                  category: WorkerExecutionFailureCategory) -> None:
    assert outcome.status is WorkerExecutionStatus.FAILED
    assert outcome.reason == reason
    assert outcome.failure is not None and outcome.failure.category is category


@pytest.fixture
def opt_in(monkeypatch: pytest.MonkeyPatch, root: Path) -> FakeRunner:
    """Enable the provider for a container and back it with the fake runner."""
    fake = FakeRunner(root)
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_PROVIDER", "claude_code")
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_WORKSPACE", str(root))
    monkeypatch.setattr(runtime, "SubprocessRunner", lambda: fake)
    get_settings.cache_clear()
    return fake


def se_request(**overrides: Any) -> TaskDelegationRequest:
    values: dict[str, Any] = {
        "objective": OBJECTIVE, "target": "velox-server",
        "executor_role": ExecutorRole.SOFTWARE_ENGINEERING,
        "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    }
    values.update(overrides)
    return TaskDelegationRequest(**values)


# --- Role, capability, manifest ------------------------------------------------

def test_software_engineering_role_and_capability_are_vendor_neutral() -> None:
    assert ExecutorRole.SOFTWARE_ENGINEERING.value == "software_engineering"
    assert not any("claude" in role.value or "codex" in role.value for role in ExecutorRole)
    capability = WorkerCapability(
        SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY, ExecutorRole.SOFTWARE_ENGINEERING, "any",
    )
    assert capability.identifier == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY == "code.implement"


def test_claude_code_manifest_declares_one_provider_side_route(
    root: Path, tmp_path: Path,
) -> None:
    manifest = make_executor(FakeRunner(root), tmp_path).provider_manifest
    manifest.validate()
    assert [(c.role, c.identifier, c.provider) for c in manifest.capabilities] == [
        ("software_engineering", "code.implement", CLAUDE_CODE_PROVIDER),
    ]
    assert manifest.account_context is None


def test_delegation_request_cannot_select_a_provider() -> None:
    with pytest.raises(TypeError):
        TaskDelegationRequest(  # type: ignore[call-arg]
            objective=OBJECTIVE, target="t", executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
            capability="code.implement", capability_provider="claude_code",
        )


# --- Composition and opt-in ----------------------------------------------------

def test_disabled_default_registers_no_engineering_worker() -> None:
    container = ApplicationContainer()
    assert container.software_engineering_executor is None
    outcome = container.task_delegator.delegate(se_request())
    assert outcome.status is TaskDelegationStatus.ROUTE_REJECTED
    assert outcome.routing_reason == "no_handler"
    assert container.pending_approval_registry.list_pending() == []


def test_default_container_keeps_exactly_the_existing_gmail_and_calendar_routes() -> None:
    routes = ApplicationContainer().worker_executor_registry.registered_capability_routes()
    assert routes == (
        ("content_summary", "summarize_email", "gmail", "gmail-local-account"),
        ("content_summary", "gmail.read", "gmail", "gmail-local-account"),
        ("content_summary", "gmail.send", "gmail", "gmail-local-account"),
        ("content_summary", "gmail.archive", "gmail", "gmail-local-account"),
        ("context_preparation", "prepare_meeting", "calendar", "calendar-local-account"),
        ("context_preparation", "prepare_calendar_context", "calendar",
         "calendar-local-account"),
        ("context_preparation", "list_calendar_events", "calendar", "calendar-local-account"),
    )


def test_explicit_opt_in_registers_exactly_one_engineering_route(opt_in: FakeRunner) -> None:
    container = ApplicationContainer()
    routes = container.worker_executor_registry.registered_capability_routes()
    assert [route for route in routes if route[0] == "software_engineering"] == [
        ("software_engineering", "code.implement", CLAUDE_CODE_PROVIDER, None),
    ]
    assert len(routes) == 8
    assert opt_in.calls == []


@pytest.mark.parametrize(
    "env",
    [
        {"VELOX_SOFTWARE_ENGINEERING_PROVIDER": "codex"},
        {"VELOX_SOFTWARE_ENGINEERING_PROVIDER": "claude_code"},
        {"VELOX_SOFTWARE_ENGINEERING_PROVIDER": "claude_code",
         "VELOX_SOFTWARE_ENGINEERING_WORKSPACE": "/repo",
         "VELOX_SOFTWARE_ENGINEERING_TIMEOUT_SECONDS": "0"},
    ],
)
def test_invalid_provider_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings()


# --- Delegation, approval and runtime ------------------------------------------

def test_engineering_task_requires_approval_and_never_runs_unapproved(
    opt_in: FakeRunner,
) -> None:
    container = ApplicationContainer()
    outcome = container.task_delegator.delegate(se_request())
    assert outcome.status is TaskDelegationStatus.AWAITING_APPROVAL
    assert outcome.routing_reason == "capability_route"
    assert container.action_queue.list() == []
    invocation = container.worker_runtime_invocation.invoke(max_actions=1)
    assert invocation.processed_count == 0
    assert opt_in.calls == []
    assert container.worker_execution_observer.list() == []


def test_approved_task_reaches_claude_code_through_worker_runtime(opt_in: FakeRunner) -> None:
    container = ApplicationContainer()
    outcome = container.task_delegator.delegate(se_request())
    approve_pending_action(
        outcome.action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
    )
    invocation = container.worker_runtime_invocation.invoke(max_actions=1)
    assert invocation.processed_count == 1
    [observation] = container.worker_execution_observer.list()
    assert observation.requested_provider is None
    assert observation.matched_provider == CLAUDE_CODE_PROVIDER
    assert observation.status == "succeeded"
    assert len(opt_in.claude_calls()) == 1
    lifecycle = container.action_lifecycle_repository.get(outcome.action_id)
    assert lifecycle is not None and lifecycle.status == ActionStatus.COMPLETED


def test_failed_run_is_not_retried_automatically(opt_in: FakeRunner) -> None:
    opt_in.claude = result(returncode=None, timed_out=True)
    container = ApplicationContainer()
    outcome = container.task_delegator.delegate(se_request())
    approve_pending_action(
        outcome.action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
    )
    container.worker_runtime_invocation.invoke(max_actions=3)
    assert len(opt_in.claude_calls()) == 1
    assert container.action_queue.list() == []


# --- Invocation policy -----------------------------------------------------------

def test_argv_is_fixed_and_task_text_travels_only_on_stdin(root: Path, tmp_path: Path) -> None:
    hostile = "--dangerously-skip-permissions; git push --force && rm -rf ../ $(whoami)"
    runner = FakeRunner(root)
    outcome = run(make_executor(runner, tmp_path), engineering_action(hostile))
    assert outcome.status is WorkerExecutionStatus.SUCCEEDED
    [call] = runner.claude_calls()
    assert call.argv == build_claude_code_argv("claude", call.cwd)
    assert all(hostile not in arg for arg in call.argv)
    assert call.stdin is not None and hostile in call.stdin
    assert call.timeout == 5


def option(argv: list[str], flag: str) -> list[str]:
    return argv[argv.index(flag) + 1].split(",")


WORKTREE = Path("/work/.repo-velox-worktrees/se-0f6b4c52-7a3e-4d1f-9b0a-2c8e5d9f1a74")


def test_permission_policy_has_no_bypass_or_git_write_access() -> None:
    argv = build_claude_code_argv("claude", WORKTREE)
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv
    assert "--bare" not in argv
    assert set(CLAUDE_CODE_TOOLS) == {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}
    for verb in ("push", "merge", "reset", "rebase", "commit", "checkout", "branch", "clean"):
        assert not any(f"git {verb}" in rule for rule in CLAUDE_CODE_ALLOWED_BASH)
        assert f"Bash(git {verb} *)" in CLAUDE_CODE_DISALLOWED_BASH_AND_WEB
    assert "WebFetch" in CLAUDE_CODE_DISALLOWED_BASH_AND_WEB


# --- Hardening: no settings-file authority -------------------------------------------

def test_no_settings_file_can_add_permissions_hooks_env_or_mcp() -> None:
    argv = build_claude_code_argv("claude", WORKTREE)
    assert "--setting-sources=" in argv
    assert "project" not in argv
    assert not any(
        arg.startswith("--setting-sources") and arg != "--setting-sources=" for arg in argv
    )
    assert "--settings" not in argv
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" in argv
    assert "--bare" not in argv


# --- Hardening: path-scoped file permissions -------------------------------------------

def test_file_tools_are_never_auto_allowed_bare() -> None:
    allowed = option(build_claude_code_argv("claude", WORKTREE), "--allowedTools")
    for tool in ("Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"):
        assert tool not in allowed
    file_rules = [rule for rule in allowed if not rule.startswith("Bash(")]
    assert file_rules == [f"Read(/{WORKTREE}/**)", f"Edit(/{WORKTREE}/**)"]
    assert all(rule.startswith("Bash(") for rule in CLAUDE_CODE_ALLOWED_BASH)


def test_worker_authority_files_are_explicitly_denied_inside_the_worktree() -> None:
    denied = option(build_claude_code_argv("claude", WORKTREE), "--disallowedTools")
    for protected in (".claude/**", ".mcp.json", ".git", ".git/**"):
        assert f"Edit(/{WORKTREE}/{protected})" in denied


def test_file_rules_are_anchored_at_the_absolute_worktree_path() -> None:
    allow, deny = worktree_file_rules(WORKTREE)
    assert all(rule.split("(", 1)[1].startswith(f"/{WORKTREE}/") for rule in allow + deny)
    assert f"/{WORKTREE}".startswith("//")


@pytest.mark.parametrize(
    "path",
    [Path("relative/wt"), Path("/work/a*b/wt"), Path("/work/a,b/wt"), Path("/work/(x)/wt"),
     Path("/work/[x]/wt"), Path("/work/!x/wt")],
)
def test_unsafe_worktree_paths_cannot_become_permission_rules(path: Path) -> None:
    with pytest.raises(ValueError):
        worktree_file_rules(path)


def test_unsafe_worktrees_root_fails_closed_before_any_worktree(
    root: Path, tmp_path: Path,
) -> None:
    runner = FakeRunner(root)
    executor = ClaudeCodeSoftwareEngineeringExecutor(
        workspace=TrustedGitWorkspace(root, runner, worktrees_root=tmp_path / "a,b"),
        runner=runner, executable="claude", timeout_seconds=5, environment={},
    )
    outcome = run(executor)
    assert_failed(outcome, "workspace_unavailable", WorkerExecutionFailureCategory.PERMANENT)
    assert runner.git_calls("worktree") == []
    assert runner.claude_calls() == []


def test_live_invocation_scopes_file_rules_to_that_actions_worktree(
    root: Path, tmp_path: Path,
) -> None:
    runner = FakeRunner(root)
    action = engineering_action()
    run(make_executor(runner, tmp_path), action)
    [call] = runner.claude_calls()
    worktree = tmp_path / "wt" / f"se-{action.id}"
    assert call.cwd == worktree
    assert f"Edit(/{worktree}/**)" in option(call.argv, "--allowedTools")
    assert f"Edit(/{root}/**)" not in option(call.argv, "--allowedTools")


def test_child_environment_drops_nested_session_marker_and_velox_settings(
    root: Path, tmp_path: Path,
) -> None:
    runner = FakeRunner(root)
    run(make_executor(runner, tmp_path))
    [call] = runner.claude_calls()
    assert call.env == {"PATH": "/usr/bin", "HOME": "/home/operator"}


# --- Workspace and isolation ------------------------------------------------------

def test_worker_runs_in_a_new_isolated_worktree_named_from_the_action(
    root: Path, tmp_path: Path,
) -> None:
    runner = FakeRunner(root)
    action = engineering_action("../../etc work on branch main")
    outcome = run(make_executor(runner, tmp_path), action)
    [add] = runner.git_calls("worktree")
    expected = tmp_path / "wt" / f"se-{action.id}"
    assert add.argv == ["git", "worktree", "add", "-b", f"velox/se-{action.id}",
                        str(expected), "HEAD"]
    assert add.cwd == root
    [call] = runner.claude_calls()
    assert call.cwd == expected != root
    assert outcome.metadata["worktree_path"] == str(expected)
    assert outcome.metadata["worktree_branch"] == f"velox/se-{action.id}"


def test_default_worktrees_root_is_outside_the_canonical_checkout(root: Path) -> None:
    workspace = TrustedGitWorkspace(root, FakeRunner(root))
    assert workspace.worktrees_root() == root.parent / ".repo-velox-worktrees"
    assert root not in workspace.worktrees_root().parents


@pytest.mark.parametrize(
    "runner_overrides",
    [{"toplevel_rc": 128}, {"toplevel": "/somewhere/else"}, {"worktree_add_rc": 1}],
)
def test_unsafe_workspace_fails_closed_before_claude_runs(
    root: Path, tmp_path: Path, runner_overrides: dict[str, Any],
) -> None:
    runner = FakeRunner(root, **runner_overrides)
    outcome = run(make_executor(runner, tmp_path))
    assert_failed(outcome, "workspace_unavailable", WorkerExecutionFailureCategory.PERMANENT)
    assert runner.claude_calls() == []


def test_missing_workspace_directory_fails_closed(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "missing")
    outcome = run(make_executor(runner, tmp_path))
    assert_failed(outcome, "workspace_unavailable", WorkerExecutionFailureCategory.PERMANENT)
    assert runner.calls == []


def test_existing_worktree_for_the_action_is_never_reused(root: Path, tmp_path: Path) -> None:
    runner = FakeRunner(root)
    action = engineering_action()
    (tmp_path / "wt" / f"se-{action.id}").mkdir(parents=True)
    outcome = run(make_executor(runner, tmp_path), action)
    assert_failed(outcome, "workspace_unavailable", WorkerExecutionFailureCategory.PERMANENT)
    assert runner.claude_calls() == []


def test_changes_to_the_canonical_checkout_fail_the_run(root: Path, tmp_path: Path) -> None:
    runner = FakeRunner(root, canonical_statuses=["", " M README.md\n"])
    outcome = run(make_executor(runner, tmp_path))
    assert_failed(outcome, "canonical_workspace_modified",
                  WorkerExecutionFailureCategory.INTERNAL)


# --- Result mapping ---------------------------------------------------------------

def test_valid_structured_report_maps_to_succeeded(root: Path, tmp_path: Path) -> None:
    outcome = run(make_executor(FakeRunner(root), tmp_path))
    assert outcome.status is WorkerExecutionStatus.SUCCEEDED
    assert outcome.failure is None
    metadata = outcome.metadata
    assert metadata["provider"] == CLAUDE_CODE_PROVIDER
    assert metadata["external_execution_performed"] is True
    assert metadata["changed_files"] == ["apps/server/src/main.py", "tests/test_new.py"]
    assert metadata["reported_files_changed"] == REPORT["files_changed"]
    assert metadata["summary"] == REPORT["summary"]
    assert metadata["session_id"] == "session-1"
    assert metadata["duration_seconds"] == 1.5


@pytest.mark.parametrize(
    ("claude", "reason", "category"),
    [
        (result(1, claude_output(is_error=True)), "process_failed", "permanent"),
        (result(0, claude_output(is_error=True)), "process_failed", "permanent"),
        (result(2, claude_output()), "process_failed", "permanent"),
        (result(None, timed_out=True), "timeout", "permanent"),
        (result(0, "not json"), "malformed_output", "internal"),
        (result(0, "[1, 2]"), "malformed_output", "internal"),
        (result(0, claude_output(structured_output=None)), "invalid_result", "internal"),
        (result(0, claude_output(structured_output={**REPORT, "status": "merged"})),
         "invalid_result", "internal"),
        (result(1, claude_output(is_error=True, result="Invalid API key · Please run /login")),
         "authentication_failed", "permanent"),
        (result(1, "", "Not logged in"), "authentication_failed", "permanent"),
    ],
)
def test_failures_map_to_vendor_neutral_categories_without_success(
    root: Path, tmp_path: Path, claude: ProcessResult, reason: str, category: str,
) -> None:
    outcome = run(make_executor(FakeRunner(root, claude=claude), tmp_path))
    assert_failed(outcome, reason, WorkerExecutionFailureCategory(category))
    assert outcome.metadata["external_execution_performed"] is True


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        (ExecutableNotFoundError("missing"), "executable_missing"),
        ("2.0.99 (Claude Code)", "unsupported_cli"),
        ("unknown", "unsupported_cli"),
    ],
)
def test_missing_or_unsupported_cli_fails_before_any_worktree(
    root: Path, tmp_path: Path, version: str | Exception, reason: str,
) -> None:
    runner = FakeRunner(root, version=version)
    outcome = run(make_executor(runner, tmp_path))
    assert_failed(outcome, reason, WorkerExecutionFailureCategory.PERMANENT)
    assert runner.git_calls("worktree") == []
    assert outcome.metadata["external_execution_performed"] is False


@pytest.mark.parametrize(
    ("capability", "objective"),
    [("code.review", OBJECTIVE), ("code.implement", ""), ("code.implement", "x" * 8001)],
)
def test_unsupported_capability_or_invalid_task_does_nothing(
    root: Path, tmp_path: Path, capability: str, objective: str,
) -> None:
    runner = FakeRunner(root)
    outcome = make_executor(runner, tmp_path).execute(
        engineering_action(objective), capability=capability,
    )
    assert outcome.status is WorkerExecutionStatus.FAILED
    assert runner.calls == []


def test_failures_do_not_leak_provider_output_or_secrets(root: Path, tmp_path: Path) -> None:
    leak = "Authorization: Bearer sk-ant-secret-value"
    runner = FakeRunner(root, claude=result(1, claude_output(is_error=True, result=leak), leak))
    outcome = run(make_executor(runner, tmp_path))
    assert outcome.status is WorkerExecutionStatus.FAILED
    rendered = repr(outcome.metadata) + repr(outcome.reason) + repr(outcome.failure)
    assert "sk-ant-secret-value" not in rendered
    assert "velox-secret-token" not in rendered


# --- Real process runner (no Claude; the Python interpreter only) --------------------

def test_subprocess_runner_passes_argv_literally_without_a_shell(tmp_path: Path) -> None:
    payload = "; echo injected && $(whoami) `id`"
    outcome = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.stdin.read())",
         payload],
        cwd=tmp_path, timeout_seconds=10, stdin="stdin text",
    )
    assert outcome.returncode == 0
    assert outcome.stdout.splitlines() == [payload, "stdin text"]


def test_subprocess_runner_terminates_on_timeout(tmp_path: Path) -> None:
    outcome = SubprocessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path, timeout_seconds=0.5,
    )
    assert outcome.timed_out is True
    assert outcome.returncode is None
    assert outcome.duration_seconds < 10


def test_subprocess_runner_reports_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(ExecutableNotFoundError):
        SubprocessRunner().run([f"velox-missing-{uuid4()}"], cwd=tmp_path, timeout_seconds=5)


# --- Operator pilot entrypoint ------------------------------------------------------

def test_pilot_refuses_when_provider_is_disabled() -> None:
    out = io.StringIO()
    code = software_engineering_pilot.main(
        ["--objective", OBJECTIVE, "--target", "velox-server"], out=out,
    )
    assert code == 2
    assert "disabled" in out.getvalue()


def test_pilot_runs_only_after_typed_approval(opt_in: FakeRunner) -> None:
    container = ApplicationContainer()
    out = io.StringIO()

    def approve() -> str:
        [pending] = container.pending_approval_registry.list_pending()
        assert opt_in.claude_calls() == []
        return str(pending.id)

    code = software_engineering_pilot.main(
        ["--objective", OBJECTIVE, "--target", "velox-server"],
        container_factory=lambda: container, read_line=approve, out=out,
    )
    assert code == 0
    assert len(opt_in.claude_calls()) == 1
    assert "Provider: claude_code" in out.getvalue()
    assert "isolated git worktree" in out.getvalue()


def test_pilot_cancellation_executes_nothing(opt_in: FakeRunner) -> None:
    container = ApplicationContainer()
    out = io.StringIO()
    code = software_engineering_pilot.main(
        ["--objective", OBJECTIVE, "--target", "velox-server"],
        container_factory=lambda: container, read_line=lambda: "no", out=out,
    )
    assert code == 1
    assert opt_in.calls == []
    assert len(container.pending_approval_registry.list_pending()) == 1


def test_real_git_worktree_isolation_leaves_the_canonical_checkout_clean(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = SubprocessRunner()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=velox@example.test", "-c", "user.name=VELOX",
         "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        assert runner.run(argv, cwd=repo, timeout_seconds=30).returncode == 0
    workspace = TrustedGitWorkspace(repo, runner, worktrees_root=tmp_path / "wt")
    assert workspace.validate() == repo
    with pytest.raises(WorkspaceUnavailableError):
        TrustedGitWorkspace(tmp_path, runner).validate()

    action_id = uuid4()
    worktree = workspace.create_worktree(action_id)
    assert worktree.path == tmp_path / "wt" / f"se-{action_id}"
    assert worktree.branch == f"velox/se-{action_id}"
    (worktree.path / "change.txt").write_text("worker edit\n")

    assert workspace.status(repo) == ""
    assert TrustedGitWorkspace.changed_files(workspace.status(worktree.path)) == (
        "change.txt",
    )
    with pytest.raises(WorkspaceUnavailableError):
        workspace.create_worktree(action_id)


# --- Hardening: reported blockers ----------------------------------------------------

def test_reported_blockers_are_a_permanent_failure_not_success(
    root: Path, tmp_path: Path,
) -> None:
    blocked = {**REPORT, "blockers": ["Tests fail: missing fixture"]}
    runner = FakeRunner(root, claude=result(stdout=claude_output(structured_output=blocked)))
    outcome = run(make_executor(runner, tmp_path))
    assert_failed(outcome, "worker_reported_blockers", WorkerExecutionFailureCategory.PERMANENT)
    assert outcome.metadata["blockers"] == ["Tests fail: missing fixture"]
    assert outcome.metadata["changed_files"] == ["apps/server/src/main.py", "tests/test_new.py"]
    assert outcome.metadata["external_execution_performed"] is True


def test_reported_blockers_never_complete_and_are_not_retried(opt_in: FakeRunner) -> None:
    blocked = {**REPORT, "blockers": ["Could not finish"]}
    opt_in.claude = result(stdout=claude_output(structured_output=blocked))
    container = ApplicationContainer()
    outcome = container.task_delegator.delegate(se_request())
    approve_pending_action(
        outcome.action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
    )
    container.worker_runtime_invocation.invoke(max_actions=3)
    lifecycle = container.action_lifecycle_repository.get(outcome.action_id)
    assert lifecycle is not None
    assert lifecycle.status == ActionStatus.FAILED  # never COMPLETED
    assert len(opt_in.claude_calls()) == 1
    assert container.action_queue.list() == []
    [observation] = container.worker_execution_observer.list()
    assert observation.status == "failed"
    assert observation.failure_category == "permanent"
