"""Tests for the settings layer and operational logging."""

import logging

import pytest
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.core.config import Settings
from apps.server.src.core.permission import (
    PermissionDecision,
    PermissionEngineRuntime,
)
from apps.server.src.workers.executor import NoOpWorkerExecutor, WorkerExecutorRegistry
from apps.server.src.workers.runtime import WorkerRuntime


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.api_token is None
    assert settings.log_level == "INFO"
    assert settings.max_transient_retries == 3


def test_settings_read_velox_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOX_API_TOKEN", "secret-token")
    monkeypatch.setenv("VELOX_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("VELOX_MAX_TRANSIENT_RETRIES", "5")

    settings = Settings(_env_file=None)

    assert settings.api_token == "secret-token"
    assert settings.log_level == "DEBUG"
    assert settings.max_transient_retries == 5


def test_permission_engine_exception_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            raise RuntimeError("engine crashed")

    runtime = PermissionEngineRuntime(
        permission_engine=ExplodingEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
    )

    with caplog.at_level(logging.ERROR, logger="apps.server.src.core.permission"):
        evaluations = runtime.evaluate([Action(type="summarize_email", target="e-1")])

    assert evaluations[0].decision.status.value == "denied"
    assert any(
        "permission engine raised" in record.message for record in caplog.records
    )


def test_unregistered_role_fallback_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = ActionQueue()
    queue.enqueue(
        Action(type="summarize_email", target="e-1", executor_role="unknown_role")
    )
    runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=NoOpWorkerExecutor(),
        executor_registry=WorkerExecutorRegistry(),
    )

    with caplog.at_level(logging.WARNING, logger="apps.server.src.workers.runtime"):
        result = runtime.process_next()

    assert result.processed is True
    assert any(
        "no executor registered for role 'unknown_role'" in record.message
        for record in caplog.records
    )
