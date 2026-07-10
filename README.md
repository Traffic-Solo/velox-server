# VELOX Server

VELOX Server is the always-on runtime for VELOX - a personal operating intelligence platform.

## Current status

Status: Sprint 1 - VELOX Core Platform (post-audit hardening complete)
Version: 0.0.1

## Run

Build and start the API:

```bash
docker compose up --build
```

Configuration is read from environment variables with the `VELOX_` prefix,
or from a local `.env` file (see `.env.example`). Never commit `.env`.

When `VELOX_API_TOKEN` is set, every API route (except `/` and `/health`)
requires an `Authorization: Bearer <token>` header.

## API

Service:

- `GET /` - service status
- `GET /health` - health check (used by the Docker healthcheck)

Events:

- `POST /events` - accept a UniversalEvent (409 on duplicate id)
- `GET /events?limit=&offset=` - stored events, paginated
- `GET /events/pending` - pending inbox events
- `GET /events/schema` - Universal Event Model contract
- `GET /events/{id}` - one stored event
- `GET /events/{id}/lifecycle` - event lifecycle state
- `POST /events/{id}/process` - classify, resolve context, plan and
  permission-check one event; failed events can be re-processed (replay)

Actions:

- `GET /actions/queue` - actions queued for execution
- `GET /actions/pending-approval` - actions held for explicit approval
- `POST /actions/{id}/approve` - approve a held action (moves it to the queue)
- `POST /actions/{id}/reject` - reject a held action

## Safety model

- Deny-by-default permissions: only an explicit safe list of action types is
  auto-approved; everything else requires explicit approval via the API.
- Action status has one source of truth: the action lifecycle repository.
- No-op execution paths report `skipped`, never `succeeded`.
- Transient worker failures are retried a bounded number of times;
  permanent failures are terminal.
- No integration performs external calls yet: Gmail/Calendar are
  deterministic in-memory fakes behind provider boundaries (see ADR-0001).

## Development

```bash
uv sync --group dev     # install dependencies + dev tooling
uv run ruff check apps tests
uv run mypy
uv run pytest -q
```

All three checks run in CI on every push and pull request to `main` and must
pass before pushing.
