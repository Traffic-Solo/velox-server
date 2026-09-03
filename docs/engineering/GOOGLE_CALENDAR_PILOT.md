# Google Calendar Live Pilot Runbook

Operational runbook for the Sprint 2 controlled local pilot: connect one real Google
account and synchronize one real primary-calendar event through the existing VELOX
event, planning, permission and routing boundaries.

This document is operational. Architecture lives in Notion; implementation state lives
in `docs/engineering/CODEX_CONTEXT.md`.

## Scope of this pilot

In scope:

- one macOS machine;
- one Google account;
- one VELOX `account_identifier`;
- primary calendar only;
- read-only `events.get` for one explicit event ID.

Out of scope (do not attempt through this runbook): `events.list`, background sync,
polling, push/webhooks, Calendar writes, Gmail, multi-account discovery, public OAuth,
UI, durable sync cursors, automatic worker-runtime execution.

## Secret-handling rules

These rules are absolute.

- Refresh-capable credential material lives **only** in the macOS Keychain, under
  namespace `velox.google.oauth`.
- Never place tokens, client secrets, authorization codes or ID tokens in the
  repository, `.env`, Notion, logs, command output, or test fixtures.
- Never commit the downloaded Google client-secret JSON. `.gitignore` blocks
  `client_secret*.json`, `client-secret*.json`, `client-secrets*.json`, `token.json` and
  `.playwright-mcp/`, but keep the file outside the repository anyway. Browsers and
  browser-automation tools may drop a download into the working tree; if that happens,
  move it out immediately rather than relying on the ignore rules.
- Both pilot commands print JSON containing only safe metadata. If any command ever
  prints a token or secret, stop and treat it as a defect.

## Prerequisites

- macOS with a usable login Keychain (`keyring` must select the macOS backend).
- Repository checked out, dependencies installed via `uv sync`.
- A Google account whose primary Calendar contains at least one event.
- A Google Cloud project with a Desktop OAuth client (see below).

## Google Cloud project configuration

1. Open Google Cloud Console and select (or create) the pilot project.
2. Enable the **Google Calendar API** for that project.
3. Configure the OAuth consent screen:
   - User type: **External** is normal for a personal Google account.
   - Add the pilot Google account as a **Test user** while the app is in Testing.
4. Add exactly these scopes and no others:
   - `openid`
   - `email`
   - `https://www.googleapis.com/auth/calendar.events.readonly`
5. Create credentials -> **OAuth client ID** -> Application type: **Desktop app**.
6. Download the client-secret JSON and store it **outside the repository**, for example
   `~/.config/velox/velox-calendar-pilot-client-secret.json`. Restrict it:
   `chmod 700 ~/.config/velox && chmod 600 <path>`. The file's top-level key must be
   `installed` — that is what marks it a Desktop client and what `InstalledAppFlow`
   expects. A `web` key means you created the wrong client type.

### Testing mode vs In production

Google expires refresh tokens for **Testing**-mode apps using Calendar scopes after
**seven days**. For a durable personal pilot, move the OAuth app to **In production**
in the consent-screen settings. For an External app requesting only `openid`, `email`
and `calendar.events.readonly`, publishing does not require Google verification and
does not constitute a public launch — it only stops the seven-day expiry. An unverified
app still shows Google's "Google hasn't verified this app" warning; that is expected.

If the app stays in Testing, expect to rerun the connect command every seven days.

## Approved scopes

The bootstrap requests exactly these three scopes and the stored credential is rejected
at read time if its recorded scopes differ:

```
openid
email
https://www.googleapis.com/auth/calendar.events.readonly
```

Never widen this to `https://www.googleapis.com/auth/calendar` or any read-write scope.

## Step 1 — OAuth bootstrap command

```
uv run python -m apps.server.src.integrations.google_oauth_cli connect \
  --account-identifier <VELOX_ACCOUNT_IDENTIFIER> \
  --expected-google-email <you@example.com> \
  --client-secrets ~/.config/velox/velox-calendar-pilot-client-secret.json
```

Add `--replace` only when you intentionally want to overwrite existing stored material
for that account identifier. Without it, an existing credential is a hard failure.

What happens: the system browser opens on a loopback `127.0.0.1` redirect with a random
port, requests offline access with an explicit consent prompt, and returns. There is no
embedded browser. Select the expected Google account and grant the listed scopes.

If the browser does not open automatically — `webbrowser.open` can fail silently, and a
browser restart mid-flow loses the tab — rerun with `--no-browser`:

```
uv run python -m apps.server.src.integrations.google_oauth_cli connect \
  --account-identifier <VELOX_ACCOUNT_IDENTIFIER> \
  --expected-google-email <you@example.com> \
  --client-secrets ~/.config/velox/velox-calendar-pilot-client-secret.json \
  --no-browser
```

The authorization URL is then printed to **stderr** prefixed with
`VELOX authorization URL: `, and you open it yourself. Stdout still carries only the
single JSON result. The URL is not secret material — it carries the public client id,
the loopback redirect URI, the requested scopes and a CSRF state nonce — but it is
single-use and tied to the loopback port that run is listening on.

Never reuse an authorization URL from an earlier run. Each run binds a fresh random port
(`port=0`), so an old URL redirects to a port nothing is listening on. If a run is
interrupted, kill it and start a new one rather than retrying the old URL.

Safe expected output on success:

```json
{"account_identifier":"<VELOX_ACCOUNT_IDENTIFIER>","command":"connect","credential_namespace":"velox.google.oauth","scopes":["openid","email","https://www.googleapis.com/auth/calendar.events.readonly"],"status":"succeeded","verified_google_email":"<you@example.com>"}
```

Exit code `0`. Nothing else is printed. The authenticated Google identity is verified
against `--expected-google-email` **before** anything is stored; a mismatch stores
nothing and fails with `account_mismatch`.

## Step 2 — Keychain namespace and account mapping

| VELOX concept | Keychain field | Value |
| --- | --- | --- |
| Credential namespace | service | `velox.google.oauth` |
| VELOX account identifier | username | the `--account-identifier` you supplied |
| Credential material | secret | JSON: `client_id`, `client_secret`, `refresh_token`, `scopes` |

The VELOX `principal` is routing identity only and is **not** part of the credential
reference. The Google email is verification-only and is **not** the Keychain username.

Access tokens and ID tokens are never persisted.

## Step 3 — Safe credential verification

```
uv run python -m apps.server.src.integrations.google_oauth_cli verify \
  --account-identifier <VELOX_ACCOUNT_IDENTIFIER>
```

This reports presence and shape only; it never prints the refresh token or the client
secret. Safe expected output:

```json
{"account_identifier":"<VELOX_ACCOUNT_IDENTIFIER>","client_id_present":true,"client_secret_present":true,"command":"verify","credential_namespace":"velox.google.oauth","credential_present":true,"material_parses":true,"persisted_forbidden_fields":[],"refresh_token_present":true,"scopes":["openid","email","https://www.googleapis.com/auth/calendar.events.readonly"],"status":"succeeded"}
```

`"persisted_forbidden_fields":[]` is the assertion that no `access_token`, `id_token` or
`token` was written to the Keychain.

Do not use `security find-generic-password -w` or any other command that dumps the
secret value.

## Step 4 — Obtain one real event ID

`events.list` is intentionally unimplemented, so VELOX cannot discover events. Get the
ID out of band:

- Open the event in Google Calendar in a browser. The URL contains
  `.../eventedit/<base64>` — the event ID is the first space-separated token of the
  base64-decoded value.
- Or read it from any tool you already trust with that calendar.

Use one event you own. Do not enumerate unrelated calendar data.

## Step 5 — Manual single-event sync command

```
uv run python -m apps.server.src.integrations.calendar_manual_sync \
  <VELOX_PRINCIPAL> <VELOX_ACCOUNT_IDENTIFIER> <GOOGLE_EVENT_ID>
```

All three values are positional, required, non-blank and must have no surrounding
whitespace. The `<VELOX_ACCOUNT_IDENTIFIER>` must be the exact same string used during
the OAuth bootstrap, otherwise credential resolution fails closed.

Safe expected output on success:

```json
{"account_identifier":"<VELOX_ACCOUNT_IDENTIFIER>","acceptance_outcome":"accepted","external_execution_performed":true,"google_calendar_event_id":"<GOOGLE_EVENT_ID>","principal":"<VELOX_PRINCIPAL>","processing_outcome":"processed","provider":"calendar","status":"succeeded","universal_event_id":"<uuid>"}
```

Exit code `0`.

## Success criteria

The pilot is successful when all of the following hold:

1. The stored Google credential resolves by the exact VELOX account identifier.
2. Token refresh succeeds against Google.
3. `events.get` succeeds against the primary calendar.
4. Only allowlisted provider fields (event ID, title, start, end, attendees) cross the
   provider boundary.
5. A `UniversalEvent` is created with `payload.calendar_event_id` equal to the real
   Google event ID.
6. The `IntegrationRouteContext` is exactly `provider=calendar` plus the supplied
   principal and account identifier.
7. `EventWorkflowService` processes the event and planner/permission/queue behavior
   matches existing VELOX semantics.
8. The worker runtime is not invoked automatically.
9. No token or secret appears in stdout, stderr, logs, `repr`, event payload, action
   metadata, execution metadata, Notion or any Git diff.

## Safe not-found test

Run the same sync command with a clearly nonexistent event ID:

```
uv run python -m apps.server.src.integrations.calendar_manual_sync \
  <VELOX_PRINCIPAL> <VELOX_ACCOUNT_IDENTIFIER> velox-pilot-nonexistent-event
```

Expected: exit code `1` and

```json
{"external_execution_performed":true,"failure_code":"event_not_found","message":"Google Calendar event was not found","status":"failed"}
```

Google returns HTTP 404, the transport maps it to `found: False`, and manual sync maps
that to `event_not_found`. This is a read-only probe and changes nothing.

Do not test failure handling by revoking credentials or corrupting Keychain entries.

## Reconnect procedure

Reconnect when the refresh token expires (Testing-mode seven-day window), when access is
revoked in the Google account's security settings, or when the sync command returns
`reconnect_required`.

```
uv run python -m apps.server.src.integrations.google_oauth_cli connect \
  --account-identifier <VELOX_ACCOUNT_IDENTIFIER> \
  --expected-google-email <you@example.com> \
  --client-secrets ~/.config/velox/velox-calendar-pilot-client-secret.json \
  --replace
```

`--replace` is required here: without it the existing Keychain entry is protected and
the command fails with `credential_already_exists`.

## Common errors

| Failure code | Command | Meaning and fix |
| --- | --- | --- |
| `credential_store_unavailable` | both | keyring did not select the macOS Keychain backend, or the Keychain is locked. Unlock the login keychain and rerun. |
| `credential_already_exists` | connect | Material already stored for this account identifier. Rerun with `--replace` if you intend to overwrite. |
| `account_mismatch` | connect | You selected the wrong Google account in the browser. Nothing was stored. Rerun and pick the expected account. |
| `oauth_bootstrap_failed` | connect | Consent was denied or the flow did not return refresh-capable material. Confirm offline access and consent, and that the client is a Desktop app. The failure JSON carries `authorizer_error_type` when the flow itself raised, or `authorizer_credential_fields` when consent succeeded but a required field was missing. |
| `authorizer_error_type: Warning` | connect | oauthlib rejecting Google's canonical scope aliases (it returns `email` as `.../auth/userinfo.email`). Handled: the flow relaxes oauthlib's verbatim check and verifies scope equivalence itself. If this reappears, the granted scopes genuinely differ. |
| `authorizer_error_type: GoogleOAuthScopeMismatchError` | connect | The scopes Google actually **granted** are not the three approved scopes, or the token response carried no usable grant list at all. Nothing is stored. Untick nothing on the consent screen; if a checkbox was cleared, rerun and grant all three. |
| `credential_missing` | verify / sync | No credential for that exact account identifier. Check for a typo, then run `connect`. |
| `credential_malformed` | verify | Stored material has the wrong shape or non-approved scopes. Rerun `connect --replace`. |
| `reconnect_required` | sync | Refresh token invalid, revoked or expired. Follow the reconnect procedure. |
| `credential_refresh_failure` | sync | Transient Google refresh problem. Retry later. |
| `transport_failure` | sync | Network or Google transport unavailable. Retry later. |
| `event_not_found` | sync | Event ID does not exist on the primary calendar, or is not visible to this account. |
| `provider_permanent_failure` | sync | Google rejected the request; usually wrong scopes or the Calendar API not enabled. |
| `duplicate_event` | sync | This Calendar event was already ingested into the current VELOX process state. |
| `invalid_input` | both | A supplied value is blank or has surrounding whitespace. |

## Known benign warning on refresh

Every live sync writes this line to stderr before succeeding:

```
Not all requested scopes were granted by the authorization server, missing scopes email.
```

It is emitted by `google-auth` during token refresh and is **not** a failure. It has the
same root cause as the historical bootstrap failure: the stored credential records the
approved scope `email`, while Google's refresh response reports it in canonical form as
`https://www.googleapis.com/auth/userinfo.email`, so a verbatim comparison reports it
missing. Refresh completes and `events.get` succeeds.

Judge a run by its exit code and JSON, not by the presence of this line. Silencing it
would mean either recording canonical scope URLs in stored credentials or filtering a
third-party logger, both of which change approved behavior; it is recorded as technical
debt instead.

## Testing note

No default test opens a browser, contacts Google, uses the real Keychain, or requires
real credentials. `uv run pytest -q` is safe to run at any time. Everything in this
runbook is manual and opt-in.
