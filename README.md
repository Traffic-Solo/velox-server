# VELOX Server

VELOX Server is the always-on runtime for VELOX - a personal operating intelligence platform.

## Current status

Status: MVP Foundation  
Version: 0.0.1

## Run

Build and start the API:

```bash
docker compose up --build
```

The API is available at:

- `GET /`
- `GET /health`
- `GET /events/schema`

## Universal Event Model v0.1

VELOX uses a `UniversalEvent` as the canonical event envelope for future
integrations, workers, and planning flows. The current API exposes read-only
schema introspection at `GET /events/schema`.

This endpoint returns the model name, field list, a serialized sample event, and
a short description of the event normalizer contract. It does not ingest,
persist, or dispatch events.
