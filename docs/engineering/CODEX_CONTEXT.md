# Codex Engineering Context

## Purpose

This file is the canonical repository handoff for Codex engineering sessions.

## Operating Rule

- One Codex chat equals one completed engineering slice.
- Every new Codex chat must read this file first.
- Do not rely on long previous chat history.
- Never assume file names or repository structure. Search the repository first if uncertain.

## Current Sprint

Sprint 1 - VELOX Core Platform

## Quality Gates

- CI runs on every push and pull request to main: `uv run ruff check apps tests`, `uv run mypy` (strict), `uv run pytest -q`.
- All three gates must pass before any commit is pushed. Run them locally before committing.
- Dev tooling is installed with `uv sync --group dev`.

## Approved Architecture Rules

- Implementation over discussion.
- Architecture is role-based.
- Integrations implement roles, they do not define architecture.
- Every connector must go through Universal Event, Normalizer, Event Inbox and Pipeline.
- Notion is architecture source of truth.
- GitHub is implementation source of truth.
- Runtime belongs to VELOX Server.
- Memory belongs to VELOX Memory Layer.
- No secrets in documentation.
- Small atomic slices only.

## Implemented Core Components

- Universal Event
- Event Normalizer
- Event Repository
- Event Inbox
- Event Lifecycle
- Event Lifecycle Manager
- Event Classifier
- Context Resolver
- Event Processing Pipeline
- Application Container
- Planner
- Rule-based Planner
- Action Model
- Action Executor Role Model
- Action Queue
- Action Queue API
- Action Lifecycle
- Action Lifecycle Manager
- Permission Decision Model
- Permission Engine Contract
- Permission Infrastructure Container Registration
- Permission Engine Runtime
- Worker Runtime Foundation
- Worker Executor Contract
- Worker Executor Runtime Wiring
- Executor Registry
- Executor Registry Explicit Role Resolution
- Worker Runtime Executor Registry Resolution
- NoOp Worker Executor Fallback
- Worker Runtime Invocation API
- Worker Executor Explicit Role Registration
- Worker Runtime In-Memory Invocation Observability
- Worker Runtime Exception Safety
- Worker Executor Failure Contract
- Gmail Worker Executor Bootstrap
- Gmail Capability Contract
- Gmail Read Capability Bootstrap
- Gmail Send Capability Bootstrap
- Gmail Archive Capability Bootstrap
- Gmail Capability Test Consolidation and Fixture Cleanup
- Gmail Provider Boundary Interfaces
- Gmail Fake Transport Bootstrap
- Gmail Fake Credentials Provider Bootstrap
- Gmail Provider Composition Bootstrap
- Google Calendar Integration Bootstrap
- Google Account Context Contract Hardening
- Tooling Baseline (ruff, mypy strict, GitHub Actions CI)
- Action Lifecycle Repository (single source of truth for action status)
- Approval Gate (deny-by-default permission engine, pending approval registry, approve/reject API)
- Honest Execution Statuses (SKIPPED for no-op and unhandled placeholder paths)
- Event Replay and Transient Retry (failed -> processing event transition, bounded transient action retries, honest queue_empty)
- Gmail Explicit message_id Contract (no fallback to action.target)
- Shared Google Provider Boundary (google_provider.py deduplicates Gmail/Calendar fakes)
- Settings Layer and Operational Logging (pydantic-settings, VELOX_ env prefix, structured stdlib logging)

## Current Next Slice

Audit Remediation Sprint (2026-07-10) is in progress. Slices in order:

1. Tooling baseline: ruff + mypy strict + GitHub Actions CI (done).
2. Unify action status: lifecycle repository as single source of truth (done).
3. Approval gate: deny-by-default engine + approve/reject endpoints (done).
4. Honest execution statuses: SKIPPED for no-op/placeholder paths (done).
5. Event replay + transient retry + queue_empty fix (done).
6. Gmail explicit message_id contract (done).
7. Shared Google provider boundary (done).
8. Settings layer + operational logging (done).
9. API hardening: bearer auth, GET /events/{id}, lifecycle endpoint, pagination, duplicate event id rejection.
10. Infra polish: Docker healthcheck, .env.example, README.

After the remediation sprint, continue post-harvest Google integration design without moving directly into OAuth, credentials storage, real HTTP clients or real Google API calls.

## Current Implementation Notes

- Settings live in `apps/server/src/core/config.py` (`Settings` via pydantic-settings, cached `get_settings()`). All env vars use the `VELOX_` prefix and can come from `.env`: `VELOX_API_TOKEN` (bearer token; None disables auth for local dev), `VELOX_LOG_LEVEL`, `VELOX_MAX_TRANSIENT_RETRIES`. Never hardcode these or commit secrets. Logging is configured in `main.py` via `apps/server/src/core/log.py`; permission denials/engine crashes and worker no-executor fallbacks, skips, retries and executor exceptions are logged with action ids. New code paths with operational significance must log.
- Gmail and Calendar share one provider boundary: `apps/server/src/integrations/google_provider.py` defines `GoogleCredentials`, `GoogleProviderRequest/Response/Failure`, `GoogleCredentialsProvider`, `GoogleTransportClient`, `FakeGoogleCredentialsProvider(service=...)`, `FakeGoogleTransportClient(service=...)` and `GoogleProviderComposition(service=...)`. Gmail/Calendar modules keep their public names (`GmailCredentials`, `FakeCalendarTransportClient`, `CalendarProviderComposition`, etc.) as aliases or thin service-bound subclasses. A future Google service integration must reuse this boundary instead of copying it.
- Gmail read and archive require an explicit `payload.message_id`. There is no fallback to `action.target` because the planner stores the source event id in `target`, which is never a Gmail message id; missing message_id maps to a PERMANENT `WorkerExecutionFailure`.
- Failed events can be replayed: the event lifecycle allows failed -> processing, failed events stay in the pending inbox, and POST /events/{id}/process retries them. Transient worker failures are consumed by `WorkerRuntime`: a FAILED lifecycle state with a TRANSIENT failure category is re-queued (FAILED -> QUEUED -> APPROVED, re-using the original approval) with `transient_retry_count` metadata, bounded by `max_transient_retries` (default 3). PERMANENT and INTERNAL failures are terminal. `WorkerInvocationResult.queue_empty` now reports the actual queue emptiness after the batch.
- Execution statuses are honest. `WorkerExecutionStatus.SKIPPED` and `ActionStatus.SKIPPED` exist; `NoOpWorkerExecutor` and the Gmail/Calendar unhandled placeholder branches return SKIPPED with an explanatory reason instead of SUCCEEDED, and `WorkerRuntime` transitions EXECUTING -> SKIPPED for them. SUCCEEDED now always means real work was performed.
- The permission layer is approval-first. `BasePermissionEngine` auto-allows only an explicit safe list of action types (`review_pull_request`, `summarize_email`, `prepare_meeting`, `gmail.read`); every other action type gets `PermissionStatus.REQUIRES_APPROVAL`. Requires-approval actions are held in `PendingApprovalRegistry` (in-memory implementation on the container) with lifecycle QUEUED + `approval_required` metadata, and enter the `ActionQueue` only through `POST /actions/{id}/approve` (QUEUED -> APPROVED). `POST /actions/{id}/reject` transitions QUEUED -> REJECTED with a reason. `GET /actions/pending-approval` lists held actions with lifecycle. Auto-allowed actions are stored as APPROVED at permission time. `WorkerRuntime` executes only APPROVED actions: unapproved queued actions are re-enqueued unprocessed (defense in depth), rejected actions are dropped safely, and legacy stateless actions without approval metadata are auto-approved for standalone runtime usage.
- `Action` no longer carries a `status` field. The single source of truth for action status is `ActionLifecycleState` stored in `ActionLifecycleRepository` (in-memory implementation: `InMemoryActionLifecycleRepository`), keyed by action id. `PermissionEngineRuntime` stores QUEUED/REJECTED states there; `WorkerRuntime` reads the stored state (preserving `created_at`), transitions it, and stores the final COMPLETED/FAILED state back. The `/events/{id}/process` response exposes per-action lifecycle in `permission_decisions[].lifecycle`.
- `Action` carries an explicit first-class `ExecutorRole`.
- `ExecutorRole` values are vendor-neutral and do not include Gmail, Google Calendar, Notion, Slack, or other integration-specific role names.
- `BasePlanner` produces actions with executor roles.
- `WorkerExecutorRegistry` resolves executors by explicit action role.
- `WorkerExecutorRegistry` supports explicit `ExecutorRole` registration and exposes resolution metadata showing the requested role and whether a matching executor was registered.
- Backward compatibility is preserved because actions with no role, or with an unknown role, fall back to `NoOpWorkerExecutor`.
- `WorkerRuntime` records vendor-neutral in-memory execution observations and attaches structured execution metadata to processed actions, including status, start, finish, duration and role resolution details.
- `WorkerRuntime` catches executor exceptions, converts them into explicit failed `WorkerExecutionResult` values, transitions lifecycle state to failed, and finishes execution observations with failure metadata. Dequeued actions are not silently lost and are not requeued by the in-memory queue.
- Worker executors now have an explicit vendor-neutral failure contract via `WorkerExecutionFailure`, classified as transient, permanent or internal, with optional failure message and metadata.
- `WorkerRuntime` consumes the failure contract without adding retries, backoff, durable queues, external logging or vendor-specific exception handling, and surfaces failure classification in execution metadata and in-memory observations.
- A Gmail worker executor bootstrap exists under the integrations package, is registered in `ApplicationContainer` through the existing executor registry using the vendor-neutral `CONTENT_SUMMARY` role, and returns a safe placeholder `WorkerExecutionResult` without credentials, OAuth, HTTP clients or Gmail API calls.
- Gmail capability-level contracts now exist under the Gmail integration module for read, send and archive operations, with shared request/result dataclasses and deterministic in-memory implementations exposed by the existing Gmail worker executor.
- Gmail read capability now has a deterministic in-memory bootstrap behind the Gmail executor boundary. It accepts `GmailReadRequest`, returns fake in-memory message metadata, safely reports no-message cases, and performs no external Gmail, OAuth, credentials, HTTP or API behavior.
- Gmail worker executor can route explicit read actions to the in-memory read capability and maps malformed read requests to the existing `WorkerExecutionFailure` contract.
- Gmail send capability now has a deterministic in-memory bootstrap behind the Gmail executor boundary. It accepts `GmailSendRequest`, returns fake in-memory sent-message metadata, and performs no external Gmail, OAuth, credentials, HTTP or API behavior.
- Gmail worker executor can route explicit send actions to the in-memory send capability and maps malformed send requests to the existing `WorkerExecutionFailure` contract.
- Gmail archive capability now has a deterministic in-memory bootstrap behind the Gmail executor boundary. It accepts `GmailArchiveRequest`, returns fake in-memory archive metadata, safely reports missing-message cases, and performs no external Gmail, OAuth, credentials, HTTP or API behavior.
- Gmail worker executor can route explicit archive actions to the in-memory archive capability and maps malformed archive requests to the existing `WorkerExecutionFailure` contract.
- Gmail executor and capability tests now use small local helpers for repeated content-summary action setup, no-external-execution assertions, socket-call blocking and Gmail failure-contract assertions. This consolidation is test-structure-only and preserves existing behavior.
- ADR-0001 documents the Gmail real adapter and OAuth boundary. Real Gmail API behavior must remain behind the Gmail executor/integration boundary, VELOX core must depend only on roles/contracts, OAuth and credentials must remain separate provider concerns, HTTP transport must be adapter-owned or injected behind the boundary, and real Gmail behavior belongs in opt-in integration tests only.
- Gmail provider-facing boundary interfaces now exist under the Gmail integration module for future real adapter work: `GmailCredentialsProvider`, `GmailTransportClient`, `GmailCredentials`, `GmailProviderRequest`, `GmailProviderResponse` and `GmailProviderFailure`. These are contracts and data shapes only; they do not implement OAuth, credentials storage, HTTP clients or real Gmail API calls, and VELOX core remains independent of Gmail provider details.
- A deterministic `FakeGmailTransportClient` now exists behind the existing Gmail transport boundary. It accepts `GmailProviderRequest`, returns deterministic `GmailProviderResponse` values, can simulate provider failures using `GmailProviderFailure`, and performs no OAuth, credential storage, HTTP client behavior or real Gmail API calls.
- A deterministic `FakeGmailCredentialsProvider` now exists behind the existing Gmail credentials provider boundary. It returns fake `GmailCredentials` for normalized fake principal/account inputs, handles missing principal/account input with `GmailProviderFailure` metadata via `GmailCredentialsProviderError`, can simulate configured provider failures, and performs no OAuth, credential storage, real secret handling, HTTP client behavior or real Gmail API calls.
- A deterministic `GmailProviderComposition` now exists behind the Gmail integration boundary. It obtains fake credentials from `FakeGmailCredentialsProvider`, sends `GmailProviderRequest` through `FakeGmailTransportClient`, returns credential and transport failures safely as `GmailProviderResponse` values, and performs no OAuth, credential storage, real secret handling, HTTP client behavior or real Gmail API calls. Existing in-memory read/send/archive behavior, fake transport behavior and fake credentials provider behavior remain unchanged.
- A Google Calendar integration bootstrap now exists under the integrations package, is registered in `ApplicationContainer` through the existing executor registry using the vendor-neutral `CONTEXT_PREPARATION` role, and returns a safe placeholder `WorkerExecutionResult` without calendar events, OAuth, credential storage, HTTP clients or Google Calendar API calls.
- Google Calendar provider-facing boundary placeholders now exist under the Calendar integration module for future adapter work: `CalendarCredentialsProvider`, `CalendarTransportClient`, `CalendarCredentials`, `CalendarProviderRequest`, `CalendarProviderResponse`, `CalendarProviderFailure` and `CalendarProviderComposition`. These deterministic fake boundaries validate that the Gmail provider composition pattern can be reused for another Google service without implementing OAuth, credential storage, real secrets, HTTP transport or real Google Calendar API calls.
- Gmail and Calendar provider boundary contracts now require explicit principal/account context when resolving fake Google credentials or executing provider composition. Fake credentials carry normalized principal/account fields, fake transport responses echo that context deterministically, missing account context fails safely through provider failure responses, and Gmail and Calendar can execute with separate account identifiers without introducing a hidden global/default Google account.

## Workflow

Codex -> Review -> Commit -> Push -> Notion Sync -> Next Slice

## Slice Execution Protocol

Every engineering slice must follow this protocol:

1. Read `docs/engineering/CODEX_CONTEXT.md`.
2. Discover the target module.
3. Inspect existing public interfaces.
4. Confirm the insertion point.
5. Implement the slice.
6. Run focused tests.
7. Run broader regression tests when appropriate.
8. Update `docs/engineering/CODEX_CONTEXT.md` if engineering state changed.
9. Present:
   - changed files;
   - summary;
   - validation results;
   - blockers.
10. Wait for ChatGPT review before committing.
11. Commit.
12. Push.
13. Sync Notion.

## Definition of Done

- Code compiles.
- Tests pass.
- Public exports are updated if required.
- CODEX_CONTEXT.md is updated if implementation state changes.
- No unrelated refactors.
- No generated/cache/runtime files are touched.

## Update Rule

After every implementation slice, update this file in the same commit if the implemented state, next slice, technical debt or repository conventions changed. If no update is required, explicitly state why.

## Repository Conventions

- Python backend service.
- Python >= 3.12.
- FastAPI.
- Pydantic v2.
- HTTPX.
- Uvicorn.
- Pytest.
- Source code lives under apps/server/src/.
- Tests live under tests/.
- Documentation lives under docs/.
- Engineering ADRs live under docs/engineering/adr/.
- Docker config lives under docker-compose.yml and infrastructure/docker/server.Dockerfile.

## Technical Debt

- Open-source Harvest exists in Notion, but no real repositories have been evaluated yet.
- Apple Ecosystem Strategy references ADRs that are not yet created.
- Engineering Board in Notion may still need reconciliation with current repository state.
- Permission Engine Runtime implementation needs validation in the project virtualenv.
- Action Executor Role Model validation still needs to be run in an environment where `python` is available on PATH.
- Gmail read, send and archive capabilities use deterministic in-memory fake data only. Executor resolution remains role-based and falls back to `NoOpWorkerExecutor` when no registered executor matches.
- Gmail capability tests are consolidated locally in `tests/test_worker_executor.py`; no shared `tests/conftest.py` fixture has been introduced yet.
- Real Gmail adapter, OAuth, credential storage, HTTP transport and real Gmail API calls are not implemented yet.
- Gmail provider boundary interfaces, fake transport bootstrap, fake credentials provider bootstrap and fake provider composition bootstrap are present behind the Gmail integration boundary. No concrete real provider implementation exists yet.
- Google Calendar integration is bootstrap-only with deterministic fake provider composition and a placeholder executor. Calendar events, OAuth, credential storage, HTTP transport and real Google Calendar API calls are not implemented yet.
- Notion sync may still need reconciliation for the latest completed Google integration slices; do not claim Notion is updated unless the sync is explicitly performed.
