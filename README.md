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
Keep `.env` on the safe defaults; live Calendar pilot settings belong in a
separate gitignored `.env.live` (see `.env.live.example`) loaded explicitly with
`uv run --env-file .env.live ...`. Startup logs a warning whenever a live or
non-default Calendar agenda path is enabled.

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
- Gmail and Calendar use deterministic fake composition by default.
  Calendar live reads are explicit/opt-in: set `VELOX_CALENDAR_AGENDA_LIVE=true`
  to execute `POST /calendar/agenda` against Google using existing macOS Keychain
  credentials. The flag defaults to `false`; live mode requires a native macOS
  runtime with those credentials and explicit account context in each request.
  It adds no Calendar writes or OAuth scopes. Other manual/live Calendar read
  tools also require explicit invocation; see the
  [Calendar pilot runbook](docs/engineering/GOOGLE_CALENDAR_PILOT.md).
  Free-form `POST /calendar/agenda/query` uses the bounded resolver by default.
  Set `VELOX_CALENDAR_AGENDA_RESOLVER=ollama` with an explicit
  `VELOX_OLLAMA_MODEL` to opt in to local-only Ollama classification; the
  configured base URL must use a loopback host.

## Development

```bash
uv sync --group dev     # install dependencies + dev tooling
uv run ruff check apps tests
uv run mypy
uv run pytest -q
```

All three checks run in CI on every push and pull request to `main` and must
pass before pushing.
