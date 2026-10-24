# Convex storage and live updates

This branch moves persistent call data and the mobile web app's live feed to
Convex. The Python worker still owns the audio pipeline, Cartesia callbacks,
call controls, guard, and private answer delivery. Run **one worker replica**:
Convex does not make the in-memory call registry or answer broker distributed.

`STORAGE_BACKEND=sql` remains the default for existing installations. Select
`convex` explicitly. The six existing SQL tables retain their external IDs and
REST response formats. The SQLAlchemy row classes are also used as data transfer
objects in Convex mode; no SQL connection or PostgreSQL server is used in that mode.

## Connect your own project

No deployment URLs, credentials, or account selectors are committed. Each teammate
can deploy this same code to their own Convex project.

1. At the repository root, run `npm ci` and `npx convex dev --once`. Log in and
   select/create the intended **development** project. This writes `.env.local`.
2. Copy `.env.example` to `.env` if it does not already exist. Set
   `STORAGE_BACKEND=convex` and copy the `.convex.cloud` URL from `.env.local` into
   `CONVEX_URL`. Keep `USE_MOCKS=true` and `TELEPHONY=mock` for testing.
3. Generate two different long random secrets and configure this mapping:

   | Python `.env` | Convex environment variable | Purpose |
   |---|---|---|
   | `CONVEX_BACKEND_TOKEN` | `CALL4ME_BACKEND_TOKEN` | Worker reads and writes |
   | `API_TOKEN` | `CALL4ME_VIEWER_TOKEN` | Existing shared access code; browser replay is read-only |

   Set Convex secrets interactively with `npx convex env set CALL4ME_BACKEND_TOKEN`
   and `npx convex env set CALL4ME_VIEWER_TOKEN`. Never put worker credentials in
   a `NEXT_PUBLIC_*` variable. Missing Convex secrets fail closed. The Python
   worker requires a separate nonempty API token in Convex mode.
4. Copy `web/.env.example` to `web/.env.local`, set `NEXT_PUBLIC_CONVEX_URL` to the
   same `.convex.cloud` URL, then run `cd web && npm ci && npm run build`.
5. From the root, run `uv sync` and
   `uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000`.
   Open `http://localhost:8000` and enter the API access code.

The public URL is baked into the static web build. Rebuild after changing projects.
Serving the static build from the worker uses one origin and needs no CORS
configuration. If the web dev server is on another origin, configure a development
proxy or worker CORS before using `NEXT_PUBLIC_API_BASE`.

## Verify

```bash
uv run pytest -q                         # existing SQL regressions + adapter tests
npm run typecheck
npm test                                # Convex transactions, authorization, replay
(cd web && npm run lint && npm run build)
uv run python -m scripts.smoke_convex    # synthetic full call against your dev deployment
node scripts/check_convex_subscription.mjs # actual WebSocket update and reconnect
```

The cloud checks require the settings above. They write clearly labelled synthetic
records. `smoke_convex` forces mock telephony, speech, and models even when `.env`
contains real keys. It checks a completed call, private answer delivery, duplicate
answer rejection, saved events and audit, redacted replay, and denied unauthorized
reads. No real call or paid model invocation is made by either check.

## Existing SQL data

The source must be quiescent: finish calls, stop the old worker, and take a backup.
Set `SOURCE_DATABASE_URL` explicitly. The tool uses a read-only source transaction,
keeps existing external IDs, and copies sessions before dependent records:

```bash
uv run python -m scripts.migrate_to_convex          # count only; no target writes
uv run python -m scripts.migrate_to_convex --apply  # copy to configured Convex target
```

Identical target rows are skipped. A different row with the same ID aborts its
batch rather than overwriting it; earlier batches remain committed and a rerun
resumes safely. This is a maintenance-window copy, not continuous synchronization.
Do not import live calls: private in-memory answers, active phone connections, and
local replay buffers cannot be transferred. We have not copied any team's existing
production database as part of the development deployment.

## Hosting and handoff

Deploy the Convex functions to the intended production deployment with
`npx convex deploy`; configure both secrets **on that deployment** (CLI `--prod`).
Set the worker's `CONVEX_URL` and the frontend's `NEXT_PUBLIC_CONVEX_URL` to that
production URL, not the developer URL.

The Docker image accepts `--build-arg NEXT_PUBLIC_CONVEX_URL=<public URL>`.
`infra/deploy.sh` reads `STORAGE_BACKEND`, `CONVEX_URL`, and `CONVEX_BACKEND_TOKEN`
from its env file, supplies the frontend build argument, and stores the worker
secret in Key Vault. With `STORAGE_BACKEND=convex`, the Bicep template does not
provision PostgreSQL. An existing PostgreSQL resource is not deleted by an
incremental deployment; remove it separately only after deciding how to preserve
its data. The Convex viewer token must match the Azure API token.

Azure deployment and real Cartesia calling must be verified in the team's own
hosting environment. The development checks here exercise the real Convex cloud
and mocked external calling services.

## Data and failure behavior

- Private answers and type-to-talk text still go directly to the Python worker;
  neither is sent to a Convex function or stored as a document field.
- Call transitions, timeline events, and audit changes in one commit are atomic.
  Concurrent changes to the same field fail with HTTP 409. Unrelated fields can
  be updated independently. Audit and timeline rows cannot be edited by the API.
- Browser reads use the existing shared demo access code, not per-user identity.
  A holder can view the demo's calls. This migration does not add user isolation.
- Live messages are redacted before queueing. A bounded ordered writer batches
  them, retries transient errors with stable IDs, and preserves the latest 400
  messages per call. Convex subscriptions deduplicate by sequence on reconnect.
- Cloud replay is persisted for up to 24 hours, then removed by a five-minute
  cleanup job (large backlogs may take longer). This differs from the previous
  process-memory-only replay. Audio recordings and answer values are not stored.
  Call/session/audit retention is unchanged.
- A sustained Convex outage can exhaust replay retries or the 2,000-message
  worker queue. Errors are logged without message contents. The existing worker
  WebSocket feed remains available, but the browser does not silently switch
  between transports. Call control and private answer handling require the worker
  to remain alive.
- Rollback: finish calls, set `STORAGE_BACKEND=sql`, clear
  `NEXT_PUBLIC_CONVEX_URL`, rebuild the web app, and restart the worker. New Convex
  records are not automatically copied back into SQL.

References: [Convex HTTP API](https://docs.convex.dev/http-api),
[reactive JavaScript client](https://docs.convex.dev/client/javascript),
[Convex test library](https://docs.convex.dev/testing/convex-test).
