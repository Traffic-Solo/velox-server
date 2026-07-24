# Codex Engineering Context

## Purpose

This file is the canonical repository handoff for Codex engineering sessions.

## Operating Rule

- One Codex chat equals one completed engineering slice.
- Every new Codex chat must read this file first.
- Do not rely on long previous chat history.
- Never assume file names or repository structure. Search the repository first if uncertain.

## Current Sprint

Sprint 1 - VELOX Core Platform (completed pending final review and audit commit)

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
- Worker Runtime Executor Registry Resolution
- NoOp Worker Executor Fallback
- Worker Runtime Invocation API
- Worker Capability Provider Routing Contract
- Worker Provider Selection and Account Context Routing Contract
- Provider Adapter Request Construction
- Provider Capability Dispatch
- Capability Registry Normalization
- Legacy Capability Route Removal
- Provider Registry Consolidation
- Provider Manifest Extraction
- Provider Manifest Validation Hardening
- Deterministic In-Memory Calendar Meeting Context Capability
- Explicit Integration Route Context Contract
- Planner Integration Route and Calendar Event-ID Propagation
- Event Workflow Application Service Extraction
- Deterministic Calendar Ingress Adapter
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
- API Hardening (bearer auth, duplicate event rejection, pagination, event/lifecycle GET endpoints, no internal error leaks)
- Infrastructure Polish (Docker healthcheck, .env.example, README rewrite)
- Post-Remediation Verification (independent gate re-run, adversarial approval-bypass tests, handoff contradiction cleanup)

## Current Next Slice

The Audit Remediation Sprint and the subsequent deterministic Google integration
slices are complete.

The Sprint 1 Closure Audit decision is **SPRINT 1 READY TO CLOSE**. Local
validation at audited commit `ea67b97d56d25269d41353aabead828402e89873`
completed with focused regressions (`232 passed, 1 warning`), Ruff
(`All checks passed!`), mypy strict (`32 source files`), and the full test suite
(`434 passed, 1 warning`). See
[`SPRINT_1_CLOSURE_AUDIT.md`](SPRINT_1_CLOSURE_AUDIT.md) for the evidence,
component map, accepted technical debt, and explicit production-integration
exclusions.

Next proposed task: **Sprint 2 Scope and Architecture Selection**.

Do not invent or begin Sprint 2 implementation scope before that selection.

## Current Implementation Notes

- The API is hardened: when `VELOX_API_TOKEN` is set, every route on the events router requires `Authorization: Bearer <token>` (root `/` and `/health` stay open); `POST /events` rejects duplicate event ids with 409 (idempotency guard); `GET /events` is paginated (`limit` <= 1000, `offset`); `GET /events/{id}` and `GET /events/{id}/lifecycle` exist (registered after `/events/pending` and `/events/schema`, so keep static routes above parameterized ones); processing failures return a generic 500 detail and log the real error server-side.
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
- `WorkerExecutorRegistry` resolves executors by explicit capability-provider routes keyed by vendor-neutral executor role, capability and provider. Only `capability_provider` is a routing field; generic payload or metadata `provider` values are ignored by routing.
- `WorkerExecutorRegistry` exposes resolution metadata showing the requested role, capability, provider, matched provider, routing reason and whether a matching executor was registered.
- Explicit capability requests from payload or metadata are fail-closed: if no capability-provider route matches, the registry resolves to `NoOpWorkerExecutor` with `routing_reason="no_handler"`. Capability inference from `action.type` uses the same canonical provider registry. Actions with no role, unknown role, missing handler or ambiguous capability-provider routes resolve to SKIPPED through the no-op fallback and never report SUCCEEDED.
- `WorkerRuntime` records vendor-neutral in-memory execution observations and attaches structured execution metadata to processed actions, including status, start, finish, duration, role resolution details and capability-provider routing details.
- `WorkerRuntime` catches executor exceptions, converts them into explicit failed `WorkerExecutionResult` values, transitions lifecycle state to failed, and finishes execution observations with failure metadata. Dequeued actions are not silently lost. The only re-queueing the runtime performs is the bounded transient retry described below; INTERNAL failures (including executor exceptions) are terminal.
- Worker executors now have an explicit vendor-neutral failure contract via `WorkerExecutionFailure`, classified as transient, permanent or internal, with optional failure message and metadata.
- `WorkerRuntime` consumes the failure contract: TRANSIENT failures are re-queued with a bounded retry budget (`VELOX_MAX_TRANSIENT_RETRIES`, default 3), PERMANENT and INTERNAL failures are terminal. There is still no backoff, no durable queue and no vendor-specific exception handling. Failure classification is surfaced in execution metadata and in-memory observations.
- A Gmail worker executor bootstrap exists under the integrations package, is registered in `ApplicationContainer` through explicit capability-provider routes for the vendor-neutral `CONTENT_SUMMARY` role. Unhandled action types return a SKIPPED placeholder `WorkerExecutionResult` (never SUCCEEDED), without credentials, OAuth, HTTP clients or Gmail API calls.
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
- A Google Calendar integration bootstrap now exists under the integrations package, is registered in `ApplicationContainer` through explicit capability-provider routes for the vendor-neutral `CONTEXT_PREPARATION` role. Unhandled action types return a SKIPPED placeholder `WorkerExecutionResult` (never SUCCEEDED), without calendar events, OAuth, credential storage, HTTP clients or Google Calendar API calls.
- Google Calendar provider-facing boundary placeholders now exist under the Calendar integration module for future adapter work: `CalendarCredentialsProvider`, `CalendarTransportClient`, `CalendarCredentials`, `CalendarProviderRequest`, `CalendarProviderResponse`, `CalendarProviderFailure` and `CalendarProviderComposition`. These deterministic fake boundaries validate that the Gmail provider composition pattern can be reused for another Google service without implementing OAuth, credential storage, real secrets, HTTP transport or real Google Calendar API calls.
- Gmail and Calendar provider boundary contracts now require explicit principal/account context when resolving fake Google credentials or executing provider composition. Fake credentials carry normalized principal/account fields, fake transport responses echo that context deterministically, missing account context fails safely through provider failure responses, and Gmail and Calendar can execute with separate account identifiers without introducing a hidden global/default Google account.
- Worker executor routing now supports an explicit vendor-neutral account context contract through `WorkerAccountContext` and official action `account_context` routing fields. Capability-provider routes can be registered with account identifiers; matching requires the official `capability_provider` and `account_context` fields, fails closed for missing, invalid, unknown or ambiguous account-aware routes, and records requested/matched account context in runtime execution metadata and observations. Generic payload or metadata `provider` fields remain ignored by routing. The application container registers Gmail and Calendar bootstrap routes with separate explicit account identifiers and no hidden default account.
- `WorkerRuntime` dispatches every execution through `WorkerExecutorResolution` and the common `WorkerExecutor.execute` contract, passing the resolved capability and registry-matched `WorkerAccountContext` without inspecting provider names or executor subtypes. It records `account_context_used` in execution metadata and observations. Full principal/account consistency is required; generic payload or metadata fields cannot replace the matched routing context.
- Gmail read/send/archive and Calendar context-preparation worker paths consume the resolved capability and matched account context through the common executor contract, construct deterministic `GoogleProviderRequest` values inside their provider executors and execute them through the existing fake provider compositions. Provider composition rejects conflicting separately supplied context, provider failures map to the worker failure contract, and no OAuth, credential storage, HTTP client or external API behavior is introduced.
- `WorkerCapability` is the canonical provider capability model. It normalizes capability and provider identifiers and carries the vendor-neutral executor role. Gmail and Calendar retain their public `worker_capabilities` interface as a read-only property delegated to `provider_manifest.capabilities`; the manifest remains the single declarative source of registration metadata, and `ApplicationContainer` does not duplicate capability strings.
- `WorkerRuntime` always resolves execution through `WorkerExecutorRegistry`. When callers omit an explicit registry, the supplied legacy worker executor is installed as the registry fallback, preserving standalone behavior without retaining a direct capability-resolution path. Explicit capability requests, provider selection and account-aware matching remain fail-closed.
- The legacy `WorkerCapabilityRoute` model and `register_capability_provider` adapter have been removed. All capability registration now enters the registry through canonical `WorkerCapability` values, while account context remains a separate route binding on `register_capability` or `register_capabilities`. Resolution behavior and metadata are unchanged.
- `WorkerExecutorRegistry` now stores each canonical provider capability, executor and account binding in one provider-registration collection. Role-only `register`, `register_role` and `registered_roles` paths have been removed, and inferred or explicit capabilities use the same provider discovery path. The standalone runtime executor remains the registry fallback rather than a discoverable provider.
- `ProviderManifest` is the canonical frozen, vendor-neutral declaration of a provider's `WorkerCapability` values, `WorkerExecutor` and optional `WorkerAccountContext`. Gmail and Calendar construct and expose their manifests beside their provider-owned capability and account declarations. `ApplicationContainer` only instantiates each provider executor and passes its manifest to `WorkerExecutorRegistry.register_manifest`; it does not repeat capability identifiers, provider identifiers or account bindings.
- `WorkerExecutorRegistry.register_manifest` validates all manifest routes for duplicates before reusing the existing canonical capability registration path, so a rejected manifest cannot be partially registered. Manifest and provider registration order is preserved. Low-level `register_capability` and `register_capabilities` remain available without creating a separate registration store.
- Provider Manifest Extraction preserves provider interface compatibility through read-only manifest-backed `worker_capabilities` properties and does not change runtime behavior, the public `WorkerExecutor` API, `WorkerRuntime` execution flow, capability inference, account-aware routing, routing metadata, fallback behavior or fail-closed semantics. Gmail read/send/archive and Calendar context-preparation behavior remain unchanged.
- Provider Manifest Extraction validation completed with `uv run ruff check apps tests`, `uv run mypy` (30 source files) and `uv run pytest -q` (348 passed, 1 warning: existing Starlette/httpx deprecation warning).
- `ProviderManifest.validate` now enforces manifest-level invariants before registry mutation: capabilities are non-empty canonical `WorkerCapability` values; identifiers and provider identifiers are non-empty and normalized; every capability uses a defined vendor-neutral `ExecutorRole`; all capabilities declare one provider; capability routes are unique within the manifest; the executor structurally implements `WorkerExecutor`; and supplied account context contains a non-empty account identifier and a non-empty principal when principal is provided. A provider executor may expose capabilities across multiple valid executor roles.
- Provider manifest validation uses one `ValueError` contract with deterministic `invalid provider manifest:` diagnostics. Duplicate routes identify the canonical role, capability and provider without including account context or provider credentials. Conflicts with existing registrations are preflighted before any route is added, so manifest registration remains atomic.
- Provider Manifest Validation Hardening does not change runtime behavior, provider interfaces, the public `WorkerExecutor` API, `WorkerRuntime` execution flow, capability inference, account-aware routing, routing metadata, fallback behavior, fail-closed semantics or provider registration order. Gmail read/send/archive and Calendar context-preparation behavior remain unchanged.
- Provider Manifest Validation Hardening validation completed with focused executor/container tests (122 passed), `uv run ruff check apps tests`, `uv run mypy` (30 source files) and `uv run pytest -q` (352 passed, 1 warning: existing Starlette/httpx deprecation warning).
- Calendar now exposes frozen `CalendarEvent`, `CalendarMeetingContextRequest`, `CalendarCapabilityResult` and `CalendarCapabilities` contracts, a runtime-checkable `CalendarMeetingContextCapability` protocol and an injectable `InMemoryCalendarMeetingContextCapability`. Both existing manifest identifiers, `prepare_meeting` and `prepare_calendar_context`, delegate to the same deterministic capability.
- Calendar meeting-context lookup requires an explicit non-blank string in `action.payload["calendar_event_id"]`. `Action.target` remains the internal VELOX source event ID and is never used as a Calendar event ID fallback. Missing, blank and malformed values fail before capability or provider invocation through a PERMANENT `WorkerExecutionFailure` identifying `calendar_event_id`.
- A known deterministic Calendar event returns SUCCEEDED structured context containing event ID, title, start, end and attendees. A valid unknown event ID is also a successful deterministic lookup with `found: False` and no fabricated event.
- Successful Calendar capability results execute the existing account-aware fake provider composition only when official matched account context is present. The provider request encodes the validated explicit event ID as one opaque path segment, preserves the original ID in domain metadata, preserves account context unchanged and merges provider request/response metadata with the in-memory domain metadata. Provider response metadata is reconstructed only from type-validated Calendar service fields (`external_execution_performed`, `integration`, `adapter` and `failed`), so arbitrary response keys and values, credentials and tokens are not exposed. Existing credentials and transport failure classifications are preserved.
- Deterministic In-Memory Calendar Meeting Context Capability leaves `Action`, `WorkerExecutor`, `WorkerExecutionResult`, `WorkerRuntime`, `WorkerExecutorRegistry`, provider manifests, routing metadata, runtime fallback, fail-closed behavior, provider registration order, approval, retry, failure classification, planner and Gmail behavior unchanged. It introduces no socket, OAuth, credentials storage, HTTP or external API behavior.
- Deterministic In-Memory Calendar Meeting Context Capability validation completed with focused Calendar/container tests (67 passed), `uv run ruff check apps tests`, `uv run mypy` (30 source files) and `uv run pytest -q` (367 passed, 1 warning: existing Starlette/httpx deprecation warning).
- `IntegrationRouteContext` is the immutable vendor-neutral core contract for explicit provider/account selection supplied separately through the event processing boundary. It contains provider, optional principal and account identifier values, rejects non-string or blank supplied values, preserves valid submitted text unchanged and does not depend on `WorkerAccountContext`.
- `ProcessedEvent` optionally preserves an explicitly supplied `IntegrationRouteContext`. `EventProcessingPipeline.process` accepts it only as a keyword input, does not infer it from event source, payload or metadata and leaves the original `UniversalEvent`, context resolution and classifier behavior unchanged.
- `POST /events/{event_id}/process` accepts an optional `ProcessEventRequest.integration_route`; the existing bodyless request remains compatible. Arbitrary `account_context`, `capability_provider` and `provider` fields in stored event payload or metadata are not routing authority. Route existence remains the responsibility of `WorkerExecutorRegistry`.
- Explicit Integration Route Context Contract leaves `UniversalEvent`, `Action`, planner, worker account context, worker registry, routing, manifests, integrations, permission, approval, runtime, retry and lifecycle behavior unchanged. It does not yet propagate Calendar event identity or integration route context into actions.
- Explicit Integration Route Context Contract validation completed with focused event pipeline/API/planner tests (63 passed), `uv run ruff check apps tests`, `uv run mypy` (30 source files) and `uv run pytest -q` (386 passed, 1 warning: existing Starlette/httpx deprecation warning).
- `BasePlanner` maps an explicit `ProcessedEvent.integration_route` into the existing official `Action.payload.capability_provider` and `Action.payload.account_context` routing fields for every recognized planned action. Submitted provider, principal and account identifier values are preserved unchanged; the planner performs no normalization, lookup or route validation and writes no duplicate routing fields to action metadata.
- Calendar-classified events producing `prepare_meeting` copy `UniversalEvent.payload["calendar_event_id"]` into the action payload only when the key exists. Values are preserved unchanged, including surrounding whitespace, blank strings, non-string values and explicit `None`; missing keys do not receive a default or an `Action.target` fallback. `Action.target` remains the internal VELOX event UUID.
- Planner-generated Calendar actions with a matching explicit route resolve through the account-aware registry and reach the deterministic in-memory Calendar meeting-context capability. Missing route, unknown account and mismatched provider inputs remain fail-closed, while missing, blank and non-string Calendar IDs reach the existing Calendar executor permanent invalid-field failure.
- Planner Integration Route and Calendar Event-ID Propagation validation completed with focused planner/container tests (64 passed), `uv run ruff check apps tests`, `uv run mypy` (30 source files) and `uv run pytest -q` (407 passed, 1 warning: existing Starlette/httpx deprecation warning).
- `EventWorkflowService` now owns duplicate-safe event acceptance, repository append, inbox enqueue, event lifecycle transitions, pipeline execution, planner invocation, permission evaluation and queueable-action enqueueing. It receives the existing container-owned repository, inbox, lifecycle mapping and manager, pipeline, planner, permission runtime and action queue; no parallel or hidden dependencies are created.
- FastAPI event endpoints delegate acceptance and processing to the container-owned workflow service while retaining request models, validation, bearer authentication, response serialization, lifecycle/action serialization, status-code mapping and server-side processing-error logging. Integrations can reuse the same vendor-neutral service without importing or calling API endpoint functions.
- Explicit `IntegrationRouteContext` remains a separate optional processing input. The service passes it to the pipeline only when supplied and performs no payload or metadata extraction, provider inference or account defaulting. Runtime, planner, permission, approval, lifecycle, replay and routing behavior remain unchanged, and event processing does not execute workers.
- Event Workflow Application Service Extraction adds no Calendar ingress, OAuth, credentials, HTTP client or external API behavior.
- Event Workflow Application Service Extraction validation completed with focused workflow/API/container and affected regression tests (119 passed), `uv run ruff check apps tests`, `uv run mypy` (31 source files) and `uv run pytest -q` (417 passed, 1 warning: existing Starlette/httpx deprecation warning).
- `CalendarEventNormalizer` copies raw Calendar mappings into a new `UniversalEvent` with source `calendar` and type `calendar.event`. When `event_id` is present, its value is copied unchanged to `payload.calendar_event_id`; absence, explicit `None`, blank and whitespace-bearing strings, and non-string values remain distinct.
- `CalendarIngressAdapter` accepts provider/account routing only through its explicit keyword-only `integration_route` argument. Raw route-like fields may remain in payload but never become metadata or routing authority.
- Calendar ingress reuses the container-owned `EventWorkflowService` for acceptance, processing, planning, permission evaluation and queueing. It duplicates no API or workflow orchestration and does not invoke workers.
- Deterministic end-to-end tests prove Calendar ingress, lifecycle processing, planning, explicit account-aware routing, permission approval, queueing and separate invocation of the existing Calendar worker executor. No OAuth, credentials storage, HTTP transport, socket access, real Google Calendar API calls or other external integration behavior was added.
- Deterministic Calendar Ingress Adapter validation completed with focused Calendar ingress/workflow/planner/container regression tests (91 passed), including the Calendar ingress/container subset (62 passed), `uv run ruff check apps tests`, `uv run mypy` (32 source files) and `uv run pytest -q` (434 passed, 1 warning: existing Starlette/httpx deprecation warning).

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
- Gmail read, send and archive capabilities use deterministic in-memory fake data only. Executor resolution supports explicit capability-provider routing and returns `SKIPPED` through `NoOpWorkerExecutor` when no registered handler matches.
- Gmail's unqualified direct-executor aliases (`read`, `send`, `archive`) remain as compatibility inputs. Production provider declarations use canonical `WorkerCapability` values and runtime routing uses their normalized identifiers.
- Shared Google provider composition retains separate principal/account arguments for backward-compatible direct integration tests; worker adapter execution uses only account context embedded from the matched routing result.
- Gmail capability tests are consolidated locally in `tests/test_worker_executor.py`; no shared `tests/conftest.py` fixture has been introduced yet.
- Real Gmail adapter, OAuth, credential storage, HTTP transport and real Gmail API calls are not implemented yet.
- Gmail provider boundary interfaces, fake transport bootstrap, fake credentials provider bootstrap and fake provider composition bootstrap are present behind the Gmail integration boundary. No concrete real provider implementation exists yet.
- Google Calendar meeting context and ingress use deterministic in-memory behavior only. OAuth, credential storage, HTTP transport and real Google Calendar API calls are not implemented.
- Notion sync may still need reconciliation for the latest completed Google integration slices; do not claim Notion is updated unless the sync is explicitly performed.
