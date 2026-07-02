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
- Action Queue
- Action Queue API
- Action Lifecycle
- Action Lifecycle Manager
- Permission Decision Model
- Permission Engine Contract
- Permission Infrastructure Container Registration

## Current Next Slice

Permission Engine Runtime

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
- ADRs live under docs/adr/.
- Docker config lives under docker-compose.yml and infrastructure/docker/server.Dockerfile.

## Technical Debt

- Open-source Harvest exists in Notion, but no real repositories have been evaluated yet.
- Apple Ecosystem Strategy references ADRs that are not yet created.
- Engineering Board in Notion may still need reconciliation with current repository state.
- Workers Runtime must not start before Permission Engine Runtime is verified.
