# call4me

An iPhone app and voice agent that makes customer service calls for Deaf and
hard-of-hearing users: it navigates the phone menu, waits on hold, talks to the
rep, and asks the user only for what only they know. Full design: `CLAUDE.md`.

## Run it now (no API keys)

Every external service has a mock twin, so the whole call runs locally against
an in-process fake support line.

```bash
uv sync
USE_MOCKS=true uv run python -m evals.live_call      # one full call, printed live (~1 min)
uv run pytest -q                                     # unit + integration + 2 full-call e2e tests
USE_MOCKS=true uv run python -m evals.run --driver product --guard real   # 11-scenario eval table
```

Server + dashboard:

```bash
docker compose up -d                                  # Postgres (or set DATABASE_URL=sqlite+aiosqlite:///./dev.db)
USE_MOCKS=true uv run uvicorn backend.main:app --reload
open http://localhost:8000/dashboard/
```

Start a call from the dashboard, then watch the menu, hold, rep, the `ask_user` box
(type a PIN), takeover/type-to-talk, the guard blocking the upsell, the summary,
and the audit log with the PIN shown as `[REDACTED]`.

## What's where

| Path | What |
|---|---|
| `backend/calls.py` | Runs one live call: telephony → audio → STT → engine → Claude agent → guard → TTS |
| `backend/main.py` | REST + WebSocket API (CLAUDE.md §10, plus `/devices`, `/calls/{id}/hold`, `/calls/{id}/audit`, `/metrics`) |
| `backend/engine/` | State machine, Jev checks, menu navigation, human detection |
| `backend/agent/` | Claude tool-use loop, `ask_user`, guard layer |
| `backend/telephony/`, `fakeline/` | Twilio client + media stream; fake support line (mounted at `/fakeline`) |
| `backend/audio/`, `backend/speech/` | VAD, music classifier; Cartesia STT/TTS |
| `backend/notify/apns.py`, `backend/telemetry.py` | APNs push + Live Activities; Application Insights |
| `evals/` | Scenarios, simulator, scoring; `product_driver.py` (the product in the sim); `live_call.py` (real pipeline) |
| `dashboard/` | Demo dashboard (static, served at `/dashboard/`) |
| `ios/` | SwiftUI app + Live Activity widget (see `ios/README.md`) |
| `infra/` | Azure Bicep + `deploy.sh` (see `infra/README.md`) |

## Going live: what you need to provide

Put these in `.env` (template: `.env.example`) and set `USE_MOCKS=false`. Any
service whose key is missing falls back to its mock with a warning.

| Service | Variables | Unlocks |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | Real Claude conversation, greeting, summaries |
| Cartesia | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` | Real speech-to-text and voice |
| Twilio (upgraded account) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_AGENT_NUMBER`, `TWILIO_FAKELINE_NUMBER` | Real phone calls |
| Public URL | `PUBLIC_BASE_URL` (ngrok in dev, Azure URL in prod) | Twilio webhooks + media stream |
| TypeSafe | `TYPESAFE_API_KEY` | Jev decisions (else keyword heuristics) |
| Apple | `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_AUTH_KEY_PATH`, `APNS_TOPIC` + signing team in Xcode | Push, lock-screen answers, Live Activities |
| Azure | `az login`, then `infra/deploy.sh` | Hosting, Postgres, Key Vault, App Insights |

Twilio console webhooks (shown by `infra/deploy.sh`):
- Agent number → Voice "A call comes in": `POST {PUBLIC_BASE_URL}/twilio/voice`
- Fake line number → Voice "A call comes in": `POST {PUBLIC_BASE_URL}/fakeline/voice`
- Status callbacks go to `{PUBLIC_BASE_URL}/twilio/status` (set per call by the backend)
- Optional: `FAKELINE_REP_NUMBER` = a teammate's phone to play the rep (else a scripted rep answers)

Then: `USE_MOCKS=false uv run python -m evals.run --driver product --guard real`
scores real Claude on the 11 scenarios. Do this before quoting numbers: the
saved report in `evals/results/` was produced with the mock LLM.
