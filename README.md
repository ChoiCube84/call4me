# call4me

**Deaf people can't use phone-only customer service. Now they can, by typing.**

call4me is a mobile web app and a voice agent that makes a customer service call for
you, start to finish. It goes through the phone menu, waits on hold, talks to the
rep, and asks you only for what only you know (an order number, a PIN, a yes/no).
You read the whole call as live captions and can take over by typing at any time.
Built at Cal Hacks 13.0 for Deaf and hard-of-hearing adults. Full design: `CLAUDE.md`.

## How it works

```
Phone browser: web app (/)  ──REST + WebSocket (token)──▶  Backend (FastAPI) on Azure Container Apps
Judges view (/dashboard/)                                   ├─ serves the web app (static Next.js export)
                                                            ├─ Call state engine ── Jev (TypeSafe) decisions
                                                            ├─ Claude: conversation, greeting, summaries
                                                            ├─ Guard: disclosure, privacy, limits (in code)
                                                            ├─ Convex (or PostgreSQL) · Key Vault · Application Insights
                                                            ▼
                                   Cartesia: voice agent that dials the company (TELEPHONY=cartesia)
                                   or our own pipeline + fake line (TELEPHONY=phone)
```

Two ways to place a call, chosen with `TELEPHONY`:

| Mode | What dials | Who is the rep | Use it for |
|---|---|---|---|
| `cartesia` (default on Azure) | A Cartesia agent calls the real number from a Cartesia phone number. Our backend gets every utterance by webhook and answers the agent's tool calls (ask the user, check an offer against the limits, record the outcome). | A real company, or your own cell for testing | Real calls |
| `phone` | Our own pipeline: Cartesia speech-to-text and voice, Jev, Claude, guard | Whoever opens `/phone` in a browser; it plays the fake company's menu and hold music first | Backup demo, no phone network |

With `USE_MOCKS=true` every service is mocked and calls go to an in-process fake line.

Safety rules enforced in code (CLAUDE.md section 2): the agent always says it is an AI
calling for you; anything you answer is spoken once and then redacted everywhere; it
never makes up facts about you; it never relays a full SSN, card number, or password;
it can't agree to anything outside your goal and limits; you can take over at any time;
every decision goes into an append-only audit log.

How strictly these hold depends on the mode. In `phone` mode the guard checks every line
before it is spoken. In `cartesia` mode Cartesia's agent speaks for itself: the disclosure,
the limits, and the no-SSN rule are in its instructions, and offers go through our
`check_offer` tool. Our side redacts everything before it is logged, stored, or shown, and
deletes the call data at Cartesia once the call is wrapped up. The guard still can't block
a sentence before Cartesia's agent says it.

## Convex migration

The `codex/convex-migration` branch supports Convex for call records, questions,
audit logs, devices, saved menus, and reactive live captions. Python remains the
single call worker; private answers are delivered in memory only. Existing SQL
installations keep working until `STORAGE_BACKEND=convex` is selected.

See [setup, tests, SQL data copy, and teammate handoff](docs/convex-migration.md).
Each teammate can connect their own Convex deployment without changing source.

## Run it locally

```bash
uv sync
uv run pytest -q                                       # unit + integration + full-call tests
USE_MOCKS=true uv run python -m evals.live_call        # one full call on mocks, printed live
docker compose up -d                                   # local Postgres
(cd web && npm ci && npm run build)                    # the web app -> web/out, served at /
USE_MOCKS=true uv run uvicorn backend.main:app --reload
open http://localhost:8000/                            # web app; /dashboard/ = judges view
```

For web app development with hot reload: `cd web && NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev`
(see `web/README.md`).

Real services: copy `.env.example` to `.env` and fill in the keys.

- **Phone mode:** `TELEPHONY=phone uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000`.
  Open `http://localhost:8000/phone` (the rep; tap **Get ready**) and the web app (the
  user). `uv run python -m evals.phone_rep` plays a scripted rep instead.
- **Cartesia mode** needs a public https URL for Cartesia's webhooks: run
  `cloudflared tunnel --url http://localhost:8000` and set `PUBLIC_BASE_URL` to it. On
  start the server creates or updates the Cartesia agent, tools, webhook, and phone number
  (`uv run python -m backend.cartesia.setup` does it by hand and prints them).

## Deploy to Azure

```bash
az login                      # the subscription with the credits
./infra/deploy.sh             # resource group call4me-rg in northcentralus
```

This creates Container Apps, PostgreSQL, Key Vault, Application Insights, and a container
registry. It copies the secrets from `.env` into Key Vault, then builds the image (web app
included) and rolls it out. It prints the web app URL.
Azure for Students only allows a few regions, and the script checks for that. Details and
troubleshooting: `infra/README.md`.

On Azure every API call needs the access token (`API_TOKEN`, generated by `deploy.sh`).
`POST /calls` dials real numbers, so the API is never open. The web app asks for the
token once. To print it:
`az keyvault secret show --vault-name <vault> --name api-token --query value -o tsv`.

## The web app (on your phone)

Open the Azure URL on your phone. iPhone: Safari > Share > **Add to Home Screen**. Android:
Chrome > menu > **Install app**. Enter the access code once. During a call:

- Captions are large, and the banner shows where the call is (menu, hold with a timer, a
  person, needs your answer).
- When the agent needs something only you know, the whole screen flashes and the phone
  vibrates where supported. Nothing relies on sound. Type the answer. For private items (full
  card number, SSN, password), tap **Say it myself** and type it to the rep.
- **Take over** at any time and type. call4me speaks your words. Hand back with one tap.
- The screen stays awake (Wake Lock), and the app reconnects by itself if the network drops.

Keep the app open during a call. Web push notifications are a later stretch goal. The judges
view is at `/dashboard/`. The native iOS app in `ios/` is shelved.

## Evals

```bash
USE_MOCKS=false uv run python -m evals.run --driver product --guard real
```

11 scripted scenarios on the fake line: cancellation, fee waiver, order status, callback
offer, transfer back to hold, menu loop, rep asks for a PIN, paid upgrade offer, rep
refuses to talk to an AI, user takeover, rep asks for an SSN. The scored driver is our own
pipeline (phone mode) with real Claude and Jev and the real guard:
`evals/results/product-claude.json` passed **11/11**, with 0 safety violations.
`product-mock-llm.json` used the mock LLM.

## What's where

| Path | What |
|---|---|
| `backend/main.py` | REST + WebSocket API (`/calls`, `/requests/{id}/answer`, `/devices`, `/metrics`, ...) |
| `backend/auth.py` | Access token check on every route (when `API_TOKEN` is set) |
| `backend/cartesia/` | Cartesia mode: agent setup, webhook + tools, one call (`call.py`) |
| `backend/calls.py` | Phone/mock mode: telephony → audio → STT → engine → Claude → guard → TTS |
| `backend/engine/` | Call states, Jev checks, menu navigation, human detection |
| `backend/agent/` | Claude tool-use loop, `ask_user`, guard layer |
| `backend/telephony/`, `fakeline/` | Phone-mode line (`/phone` page) and mock line; the fake company's menu and hold audio |
| `backend/audio/`, `backend/speech/` | VAD, music classifier; Cartesia STT/TTS |
| `backend/config.py`, `backend/telemetry.py` | Settings from env or Key Vault; Application Insights (redacted) |
| `web/` | Mobile web app (Next.js + Tailwind, static export served at `/`; judges view at `/dashboard/`) |
| `dashboard/` | Backup console (`/console/`) and the phone-mode rep page (`/phone`) |
| `evals/` | Scenarios, simulator, scoring, product driver |
| `ios/` | Shelved native iOS app (replaced by `web/`) |
| `infra/` | Azure Bicep + `deploy.sh` |
