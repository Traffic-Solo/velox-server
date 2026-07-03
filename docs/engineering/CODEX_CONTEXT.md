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

## Current Next Slice

Gmail Fake Credentials Provider Bootstrap

Recommended next implementation step after Gmail Fake Credentials Provider Bootstrap:
continue post-harvest Gmail provider design without moving directly into OAuth, credentials storage, real HTTP clients or real Gmail API calls.

## Current Implementation Notes

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
- Gmail provider boundary interfaces, fake transport bootstrap and fake credentials provider bootstrap are present behind the Gmail integration boundary. No concrete real provider implementation exists yet.
