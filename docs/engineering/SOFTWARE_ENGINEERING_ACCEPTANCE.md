# Software Engineering Live Acceptance

This runbook closes Sprint 4 only after one real Software Engineering Action
passes through the public VELOX HTTP control plane end to end.

## Preconditions

The canonical `velox-server` checkout must be on `main`, clean, and exactly at
the trusted `origin/main` head. Existing Claude Code and GitHub CLI logins must
already work. Do not place provider credentials in `.env.live`.

Configure the local-only live overlay:

```dotenv
VELOX_SOFTWARE_ENGINEERING_PROVIDER=claude_code
VELOX_SOFTWARE_ENGINEERING_WORKSPACE=/absolute/path/to/velox-server
VELOX_SOFTWARE_ENGINEERING_PROMOTION_ENABLED=true
VELOX_SOFTWARE_ENGINEERING_PROMOTION_REMOTE=origin
VELOX_SOFTWARE_ENGINEERING_PROMOTION_BASE_BRANCH=main

# Recommended:
VELOX_API_TOKEN=<local-random-token>
```

The durable SQLite path may be left unset. VELOX will place it outside the git
checkout in the sibling `.velox` directory.

## Terminal 1: server

```bash
uv run --env-file .env.live uvicorn apps.server.src.main:app \
  --host 127.0.0.1 --port 8000
```

Confirm the startup log shows the expected Software Engineering live
configuration and no unexpected integration opt-ins.

## Terminal 2: acceptance client

```bash
uv run --env-file .env.live python -m \
  apps.server.src.integrations.software_engineering_acceptance
```

The client talks only to the public HTTP API. It cannot invoke TaskDelegator,
WorkerRuntime, work-product services or promotion services directly.

It creates one bounded objective: append exactly one unique Markdown bullet to
`docs/engineering/acceptance/SPRINT4_LIVE_ACCEPTANCE.md` and touch no other
file.

Three operator decisions are required:

1. Type the printed Action UUID exactly to approve Claude Code execution.
2. Review the bounded git diff and type `keep`.
3. After KEEP is durable, type `promote` to allow VELOX to commit, non-force
   push the Action-derived branch and create the GitHub pull request.

The harness fails closed if the worker touches any other file, creates an
untracked file, changes the canonical checkout, omits the exact marker line, or
if any durable status transition does not match the expected path.

## Success evidence

A successful run prints only safe identifiers:

- Action UUID
- VELOX promotion commit SHA
- GitHub pull request number and URL

The final durable Action phase must be `promoted` and must carry the same pull
request identity returned by promotion.

Do not merge the acceptance PR merely to satisfy this pilot. The PR itself is
the acceptance artifact and can be reviewed separately.

## Failure handling

If the client stops before execution, the Action remains governed by the normal
approval/recovery path.

If execution has a durable claim but no terminal result, use the Slice 13 status
and reconciliation endpoints. Never manually clear a started claim to force a
retry.

If promotion fails after KEEP, preserve the Action/worktree and diagnose the
trusted git or GitHub publication boundary. Do not manually push the worker
branch as a substitute for VELOX promotion.
