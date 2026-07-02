# Codex Engineering Context

## Purpose

This file is the canonical repository handoff for Codex engineering sessions.

## Operating Rule

- One Codex chat equals one completed engineering slice.
- Every new Codex chat must read this file first.
- Do not rely on long previous chat history.
- Never assume repository structure. Discover it first if uncertain.

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

## Current Next Slice

Permission Engine Runtime

## Workflow

Codex -> Review -> Commit -> Push -> Notion Sync -> Next Slice

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
