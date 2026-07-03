# ADR-0001: Gmail Real Adapter and OAuth Boundary

## Status

Accepted

## Context

VELOX currently has Gmail read, send and archive capability bootstraps implemented with deterministic in-memory behavior only. Before adding real Gmail API calls, OAuth, credential storage or HTTP clients, the Gmail integration boundary must be explicit.

The core worker architecture is role-based. Gmail-specific code must remain behind the executor and integration boundary so VELOX core continues to depend only on worker roles, capability contracts and failure contracts.

## Decision

The real Gmail adapter will live behind the existing Gmail executor/integration boundary.

VELOX core must not import Gmail API clients, OAuth libraries, credential models or Gmail-specific request behavior. Core code may depend only on existing roles, executor contracts, Gmail capability contracts exposed through the integration boundary and the Worker Executor Failure Contract.

The real Gmail adapter should implement the existing Gmail capabilities:

- read;
- send;
- archive.

OAuth and credentials are separate provider concerns. They must not be embedded into executor dispatch logic. The executor may receive or resolve a provider abstraction, but OAuth flows, token persistence and credential refresh behavior belong behind the Gmail boundary.

The HTTP client must be adapter-owned or injected behind the Gmail boundary. No HTTP client or Gmail API transport should leak into VELOX core.

All Gmail API, OAuth, credential and transport failures must be mapped to the existing Worker Executor Failure Contract. Failure mapping must preserve the current transient, permanent and internal classification model and should include safe metadata only.

## Testing Strategy

Unit tests should continue to use fake or in-memory Gmail capability implementations. Unit tests must not require OAuth, credentials, network access or real Gmail API calls.

Real Gmail adapter behavior belongs behind explicit integration tests only. Integration tests must be opt-in and isolated from the default unit test path.

## Proposed Implementation Order

1. Define provider-facing Gmail boundary interfaces for credentials and transport without implementing OAuth.
2. Add a real Gmail adapter skeleton that implements the existing read, send and archive capabilities.
3. Add failure mapping tests using fake provider and transport errors.
4. Add HTTP transport integration behind the Gmail boundary.
5. Add OAuth and credential provider implementation behind the provider boundary.
6. Add opt-in integration tests for real Gmail API behavior.

## Consequences

Real Gmail API implementation remains blocked until adapter, OAuth, credential and HTTP concerns are separated behind the Gmail boundary.

This preserves the current VELOX core architecture and keeps Gmail-specific behavior inside the integration layer.
