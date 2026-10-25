# CLAUDE.md — call4me

This file gives Claude Code the full context for this repo. Read it before every task. When something here conflicts with a request, ask before deviating.

It describes the project **as built**. The plan changed during the hackathon (Twilio → Cartesia agents, native iOS → mobile web app, Convex added as an optional store); section 20 lists what changed and why.

---

## 1. What we're building

**call4me** is a mobile web app plus a voice agent that makes a customer service phone call for the user, start to finish. It navigates the phone menu, waits on hold, talks to the human rep to reach the user's goal, and pulls the user in only when it needs something only they know (order number, PIN, a decision).

**Who it's for (named community):** Deaf and hard-of-hearing adults who face phone-only customer service. Relay services exist but involve waits, interpreters, and staying present through hold. call4me lets them handle the whole call by reading and typing.

**One-line pitch:** "Deaf people can't use phone-only customer service. Now they can, by typing."

**Context:** built at Cal Hacks 13.0 (Oct 23–25, 2026, San Francisco). Submission deadline: 8:30 AM Sunday. Judging is a 3-minute pitch + 2-minute Q&A at our table.

### Target tracks (and what each one judges)
- **Anthropic — Claude for Public Benefit:** named community; a real problem (we talked to people who have it); it works (one thing done well, usable Monday); thoughtful use of Claude, including where it could go wrong for these users.
- **Microsoft — Built for the Real World:** customer understanding; measurable usefulness; path from prototype to production; responsible design; end-to-end functionality. Hosted on Azure (Container Apps, Key Vault, Application Insights, PostgreSQL).
- **Cartesia:** voice is the core of the product. Real calls are placed and run by a Cartesia Managed Agent; phone mode uses Cartesia speech-to-text and Sonic text-to-speech.
- Secondary: Cal Hacks social impact prize (if offered), Figma Best Design (if screens are designed in Figma).

---

## 2. Non-negotiable rules (product and safety)

1. **AI disclosure:** the agent always identifies itself as an AI assistant calling on behalf of the user, using the user's chosen wording, at the start of any human interaction.
2. **Private info is never stored.** Anything the user provides via `ask_user` (PINs, account numbers, DOB, etc.) is spoken once, then redacted from transcripts, logs, the database, and telemetry. Store only that a request happened, never the answer.
3. **The agent never invents user facts.** It only speaks information the user provided or the session goal contains. Otherwise it must call `ask_user`.
4. **High-sensitivity items** (full SSN, full card number, passwords): do not relay via the agent. The user provides them another way.
5. **Limits are enforced outside the model.** The agent cannot agree to anything outside the user's stated goal and limits (new plans, fees, cancellations not requested). Blocked attempts are logged.
6. **User can take over at any time.**
7. **All user alerts are visual or haptic,** never sound-only (the users are Deaf/HoH).
8. **No recordings stored by default.** Process audio live. California requires all-party consent for recording.
9. **Append-only audit log.** Every state change, decision, denial, and tool call is logged with reason and confidence. Fixes are new events, never edits.

### How each rule is enforced, by mode

| Rule | `phone` mode (our pipeline) | `cartesia` mode (real calls) |
|---|---|---|
| 1 Disclosure | Guard prepends it if missing | In the agent's instructions; the agent speaks it |
| 2 Redaction | `audit/log.py` before every log, DB write, live message, telemetry | Same on our side; Cartesia sees answers (its agent must say them), and we delete the call data at Cartesia once the call wraps up |
| 3 No invented facts | Prompt + `ask_user` tool | Prompt + `call4me_ask_user` tool |
| 4 High sensitivity | Guard blocks relay; `request_takeover` | Instructions + `ask_user` with `sensitivity=high` returns no value; the web app shows **Say it myself** |
| 5 Limits | Guard checks every outgoing line in code | Agent is told to call `call4me_check_offer`, which decides in code; nothing can block a sentence before Cartesia's agent says it |
| 6 Takeover | Agent stops speaking immediately; the user's typed lines are spoken verbatim | A Cartesia agent can't be muted from outside: typed lines are queued and the agent says them word for word on its next tool call |
| 9 Audit | Append-only `audit_log` | Same |

Be honest about this split in the pitch and in code comments: the strongest guarantees are in phone mode.

---

## 3. Architecture

```
Phone browser: mobile web app (/)          Judges view (/dashboard/)
  │  REST + WebSocket (Bearer API_TOKEN)        ▲
  │  or Convex subscription (optional)          │
  ▼                                             │
Backend (Python, FastAPI) on Azure Container Apps, exactly 1 replica
  ├─ serves the web app (static Next.js export) at /
  ├─ Session API + live feed (WS /calls/{id}/live, replay on reconnect)
  ├─ Storage: PostgreSQL (default) or Convex (STORAGE_BACKEND=convex)
  ├─ Guard + audit log + redaction (in code)
  ├─ Key Vault (secrets) · Application Insights (redacted logs)
  │
  ├─ TELEPHONY=cartesia (default, real calls)
  │    Cartesia Managed Agent dials the company from our Cartesia number,
  │    listens, thinks (Claude via Cartesia), speaks, presses keys.
  │    We get every utterance by webhook (POST /cartesia/webhook) and answer
  │    its tools (POST /cartesia/tools/{name}): ask_user, check_offer,
  │    menu_choice, report_state, finish.
  │    Jev classifies each company utterance (call state) and picks the
  │    menu key when the agent calls menu_choice.
  │
  └─ TELEPHONY=phone (backup demo, no phone network)
       Our own pipeline against the fake support line:
       audio → VAD + music classifier → Cartesia STT → state engine (Jev)
       → Claude tool-use loop → guard → Cartesia TTS.
       Whoever opens /phone in a browser is the rep.
```

With `USE_MOCKS=true` every external service is mocked and calls go to an in-process fake line (tests, evals, local dev).

### Who does what
- **Cartesia:** in `cartesia` mode, the whole call (telephony, listening, speaking, DTMF) via a Managed Agent whose model is Claude (`CARTESIA_AGENT_MODEL`, default `claude-haiku-5.5`). In `phone` mode, ears (STT) and voice (Sonic TTS).
- **Claude:** the conversation with the rep in both modes (through Cartesia's agent, or our own tool-use loop via the Anthropic API in phone mode, default `claude-sonnet-5-5`), the greeting, caption summaries, and the post-call summary.
- **Jev (TypeSafe):** fast typed decisions with probabilities, in both modes. Falls back to keyword heuristics without `TYPESAFE_API_KEY` (or if it doesn't answer within 2 s).
  - `cartesia` mode: classifies every company utterance (menu, recorded message, hold, person, callback offer…) and moves the call state on a confident answer, until a person answers; picks the menu key when Haiku calls `call4me_menu_choice`. Haiku does the talking and still reports state; where its report contradicts Jev's last confident label, a `jev_disagreement` event is logged.
  - `phone` mode: call state every 2–3 s, menu option choice, human detection and confirmation.
- **Convex (optional):** stored call data and the live caption feed when `STORAGE_BACKEND=convex`. See section 9.
- **Azure:** hosting, PostgreSQL, Key Vault, Application Insights. `AZURE_FOUNDRY_*` settings exist but Claude is not routed through Foundry.

---

## 4. Tech stack

| Layer | Tool |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn, `uv` for deps |
| Storage | PostgreSQL + SQLAlchemy (default; Docker locally, Azure Flexible Server in prod) or Convex (`convex/`, optional) |
| Telephony | Cartesia Managed Agents (real calls); `/phone` browser page + mock line (backup) |
| Speech | Cartesia STT and Sonic TTS (phone mode); Cartesia agent voice (`CARTESIA_VOICE_ID`) |
| Decisions | Jev via `typesafe-sdk` (model `jev-latest`) |
| Conversation | Claude via the Anthropic SDK (tool use) |
| Audio | numpy, soxr; energy VAD and a time-domain music/speech classifier (lightweight stand-ins for Silero and YAMNet) |
| Web app | Next.js (App Router, TypeScript) + Tailwind, static export in `web/out` |
| Hosting | Azure Container Apps (WebSockets, 1 replica), Key Vault, Application Insights, ACR |
| Dev | cloudflared tunnel for Cartesia webhooks, Docker Compose, pytest, vitest |

**Never deploy the backend to serverless (e.g., Vercel).** Calls can last an hour; webhooks, the live WebSocket and in-memory call state must all reach one long-lived process.

---

## 5. Repo layout

```
call4me-team/
├─ CLAUDE.md · README.md · TEAM.md
├─ .env.example                # every setting, with comments
├─ docker-compose.yml          # local Postgres
├─ Dockerfile                  # Node stage builds web/, Python stage runs the API
├─ backend/
│  ├─ main.py                  # FastAPI app, routes, serves web/out at /
│  ├─ config.py                # env + Key Vault loading
│  ├─ auth.py                  # API_TOKEN check
│  ├─ contracts.py             # shared types
│  ├─ calls.py                 # starts calls; LiveCall = phone/mock pipeline
│  ├─ cartesia/                # cartesia mode: agent spec + setup, webhook/tool routes, CartesiaCall
│  ├─ db/                      # models, SQL session, store.py (SQL or Convex), convex_client.py
│  ├─ telephony/               # phone_client (/phone page), mock_client (in-process line)
│  ├─ audio/                   # codec, resample, VAD, music classifier, echo
│  ├─ speech/                  # Cartesia STT/TTS, playback
│  ├─ engine/                  # states.py, call_engine.py, jev_checks.py, menu.py, timers.py
│  ├─ agent/                   # Claude tool-use loop, tools, prompts, guard
│  ├─ notify/                  # live hub, Convex live writer, APNs (unused without a paid Apple account)
│  ├─ audit/log.py             # append-only audit log, redaction
│  └─ telemetry.py             # Application Insights (redacted)
├─ web/                        # mobile web app + judges view
├─ convex/                     # Convex schema, storage, live replay, cleanup cron
├─ fakeline/                   # fake company script, menus, hold audio
├─ dashboard/                  # backup console (/console/) and rep page (/phone)
├─ evals/                      # scenarios, simulator, bot rep, scoring, results/
├─ scripts/                    # Convex smoke test, subscription check, SQL → Convex copy
├─ infra/                      # Bicep + deploy.sh
├─ docs/convex-migration.md
├─ tests/
└─ ios/                        # shelved SwiftUI app (unmaintained)
```

Keep one responsibility per file. Put every external service behind a small client module with a mock twin (`USE_MOCKS=true`).

---

## 6. Call state machine

```
DIALING → MENU → HOLD ⇄ RECORDED_MESSAGE
            │        │
            │        ├→ CALLBACK_OFFERED
            │        └→ HUMAN_SUSPECTED → HUMAN_CONFIRMED → IN_CONVERSATION
            │                                                   │  ⇄ WAITING_ON_USER
            │                                                   ├→ TAKEOVER (user typing)
            │                                                   └→ ENDED
            └→ VOICEMAIL / FAILED / ENDED
```

All transitions go through `engine/states.py` and are logged (from, to, reason, confidence) to `call_event` and `audit_log`.

### Phone mode (`engine/call_engine.py`)
- Every 2–3 s, send the last transcript window + audio label to Jev (Choice): `menu | recorded_message | hold_music | silence | human | voicemail | callback_offer | call_ended`.
- **Timers:** menu loop > 3 repeats → press 0 / say "representative"; silence > 30 s → re-check; hold > user's max → ask the user whether to keep waiting (`POST /calls/{id}/hold`).
- **Transfers** ("please hold while I transfer you") return to HOLD.
- Pause STT during confirmed hold music (cost); keep the music classifier and Jev checks running.
- Working menu routes are saved per company (`menu_path`) and reused.

**Human detection (latency-critical):** Jev flags `human` with high confidence → play the **pre-generated** greeting → confirm by a natural response from the rep (recordings don't respond) → visual alert on the user's screen. Target: agent speaks within ~1.5 s of the rep's greeting.

### Cartesia mode (`cartesia/call.py`)
- **Jev watches:** each company utterance from the webhook (already redacted) goes to Jev's state check with the last few utterances, off the webhook's path. A confident answer (≥0.6; ≥0.75 for `human`) moves the state. Jev stops once a person answers. Voicemail and call-ended labels are only logged; Cartesia ends the call itself.
- **Jev picks menu keys:** after the whole menu, Haiku calls `call4me_menu_choice` with the options as `key: label` lines (our caption parse is the backup). Jev chooses; callback keys are never offered. Haiku presses what Jev returns; below 0.5 it may pick a clearly better option.
- **Haiku reports:** it stays silent on hold (`skip_turn`) and calls `call4me_report_state` with `menu | hold | person` and a note, which also moves the state. The menu note ("Pressed 2: returns and refunds…") is shown to the user, never spoken.
- Every Jev decision is a `jev_decision` event + audit entry, same shape as phone mode. `call_completed` / `call_failed` webhooks end the call.

---

## 7. Conversation agent

### Phone mode: our Claude tool-use loop (`agent/`)
Runs in `IN_CONVERSATION`. Each rep turn: final STT transcript → Claude → tool call → guard → action.

| Tool | Purpose |
|---|---|
| `say(text)` | Speak to the rep (passes through guard, then Cartesia TTS) |
| `ask_user(question, sensitivity)` | Pause and ask the user; `sensitivity ∈ {low, medium, high}` |
| `press_digits(digits)` | Send DTMF |
| `end_call(summary, reference_number)` | Finish: confirm details, thank rep, hang up |
| `request_takeover(reason)` | Ask the user to take over (e.g., high-sensitivity info) |

### Cartesia mode: webhook tools (`cartesia/spec.py`)
The agent's instructions carry the goal, limits, disclosure wording, info the user chose to share, and the rules above. Its tools call back into our server:

| Tool | Purpose |
|---|---|
| `call4me_ask_user(question, sensitivity)` | Ask the user; waits up to ~95 s for the typed answer |
| `call4me_check_offer(kind, description, amount_usd)` | Decide in code whether the limits allow a charge, plan change, cancellation, or anything outside the goal |
| `call4me_menu_choice(options)` | Jev picks the menu key (or word to say) for the goal |
| `call4me_report_state(state, note)` | Update the user's screen: menu, hold, or person |
| `call4me_finish(summary, …)` | Record the outcome for the summary screen (silent) |
| built-in `send_dtmf`, `skip_turn`, `end_call` | Keys, staying silent on hold, hanging up |

Any tool result may carry a queued message the user typed; the agent says it word for word.

### `ask_user` flow (both modes)
1. Agent says a holding line: "One moment, let me check with my client."
2. State → `WAITING_ON_USER`; create `user_request` (question only).
3. The web app gets the question over the live feed: full-screen color flash + vibration where supported, big question, text input.
4. User answers → `POST /requests/{id}/answer` → spoken once → **redacted** everywhere.
5. No answer in time → ask the rep to hold briefly or offer to call back.
6. `sensitivity=high` → no answer box, only **Say it myself**.

### Prompting guidance
- Keep spoken turns short and natural. Never think out loud (in cartesia mode everything the agent writes is spoken).
- Confirm key details once before ending ("Just to confirm, the $40 fee is waived and the reference is A7X291?").
- Never state facts about the user's account that weren't provided.

---

## 8. Guard layer (`agent/guard.py`)

In phone mode every outgoing action passes through `guard.check(action, session)` before execution:
- Disclosure already given? If not, prepend it.
- Does the text contain stored private data? Block.
- Does it agree to anything outside goal/limits? Block, log denial, return reason to Claude.
- High-sensitivity relay? Block; trigger `request_takeover`.

In cartesia mode `CartesiaCall._check_offer` answers `call4me_check_offer` from the same limits (plan changes, cancellations, max spend), and the guard's `looks_high_sensitivity` screens `ask_user` questions. Denied attempts are logged to `audit_log` with `allowed=false` in both modes.

---

## 9. Data model and storage

Six tables (same IDs and REST shapes in both backends):

- `call_session(id, user_id, company, phone_number, goal, limits_json, disclosure_text, state, started_at, ended_at, outcome, reference_number, minutes_saved)`
- `call_event(id, session_id, at, kind, from_state, to_state, confidence, detail_json)` — timeline
- `menu_path(id, company, steps_json, success_count, last_used_at)` — reuse working routes
- `user_request(id, session_id, question, sensitivity, status, created_at, answered_at)` — **never store the answer**
- `audit_log(id, at, actor, action, subject, allowed, reason, diff_json, evidence_json)` — append-only
- `device(user_id, apns_token, live_activity_token)` — kept for the shelved iOS app

**Backend selection:** `backend/db/store.py` picks SQL or Convex from `STORAGE_BACKEND` (default `sql`). Business logic uses `open_store()`; keep raw SQL in the SQL adapter.

**Convex mode** (`STORAGE_BACKEND=convex`, see `docs/convex-migration.md`):
- All six tables live in Convex; no PostgreSQL is used or provisioned.
- The web app subscribes to Convex for live captions and state (`NEXT_PUBLIC_CONVEX_URL` at build time) instead of the worker WebSocket. The last 400 redacted messages per call are kept up to 24 h for replay.
- A state change and its timeline/audit rows commit atomically; audit and timeline rows cannot be edited.
- Python still owns calls, call controls, the guard, and private answers. Answers and typed text go straight to the worker and are never sent to Convex.
- Still one worker replica: the in-memory call registry and answer broker are not distributed.

The deployed Azure app currently runs `STORAGE_BACKEND=sql`.

---

## 10. API endpoints (backend)

- `GET /health`
- `POST /calls` — start a call `{company, phone_number, goal, limits, disclosure_text, known_info}` · `GET /calls` · `GET /calls/{id}` · `GET /calls/{id}/audit`
- `WS /calls/{id}/live` — captions, state, summaries, questions; replays recent messages on connect
- `POST /calls/{id}/takeover` · `POST /calls/{id}/handback` · `POST /calls/{id}/cancel`
- `POST /calls/{id}/type` — type-to-talk text (spoken verbatim)
- `POST /calls/{id}/hold` — keep waiting or stop after the max-hold alert
- `POST /requests/{id}/answer` — user's answer to `ask_user`
- `GET /companies?q=` — company directory
- `POST /devices` — APNs tokens (iOS app, shelved)
- `GET /metrics` — latest eval report (JSON; no longer shown on the dashboards)
- `POST /cartesia/webhook` · `POST /cartesia/tools/{name}` — Cartesia callbacks (tool-secret auth)
- `GET /phone` + `WS /phone/ws` — phone-mode rep page
- `/` — the web app; `/dashboard/` judges view; `/console/` backup console

On Azure every route except health, the Cartesia callbacks and static pages needs `Authorization: Bearer <API_TOKEN>` (or `?token=` on WebSockets).

---

## 11. Mobile web app (`web/`) — Plan C

The native iOS app is shelved (`ios/` stays in the repo, unmaintained): a free Apple ID can't do push, installs expire every 7 days, and judges can't install it. Instead the user's phone runs a **mobile web app**, served by the Azure backend over HTTPS. Any phone opens it from a link, and "Add to Home Screen" makes it look like an app.

- **Stack:** Next.js (App Router, TypeScript) + Tailwind, built as a static export (`web/out`) that FastAPI serves at `/`. No Node server in production. Pages use query params (`/live/?id=…`), not dynamic segments.
- **Start (`/`):** company (from `GET /companies`), phone number, goal, limits, info the agent may share (values used once, never saved in the browser), AI introduction → `POST /calls`.
- **Live (`/live/?id=`):** large high-contrast captions and a state banner (with a hold timer) over `WS /calls/{id}/live` (or Convex). It reconnects automatically and rebuilds from the replay. Also shows plain-language summaries (including why a menu key was pressed) and Hang up.
- **Question sheet (`ask_user`):** full-screen color flash (a single color change with reduced motion) plus vibration where supported, a big question, and a text input → `POST /requests/{id}/answer`. The input clears at once. For `sensitivity=high` there is no answer box, only **"Say it myself"** → takeover. It closes on the `ask_user_answered` live event.
- **Take over (type-to-talk):** `POST /calls/{id}/takeover`, then `POST /calls/{id}/type`. Quick phrases. Hand back with one tap. (Cartesia mode: see the table in section 2.)
- **Summary (`/summary/?id=`):** outcome, reference number (large, copyable), minutes saved.
- **Judges view (`/dashboard/`):** recent calls, a live call with its timeline and audit log (denials highlighted, private values shown as `[REDACTED]`).
- **Phone behavior:** Screen Wake Lock on the Live screen, a web app manifest for Add to Home Screen, large text (adjustable), big touch targets, plain short English.
- **Alerts are visual (+ vibration), never sound-only** (rule 7). Web push is a later stretch goal, so the app must be open during a call.
- **Access:** on Azure the API needs the access token (`API_TOKEN`). The app asks for it once, keeps it in localStorage, and sends it as `Authorization: Bearer` (or `?token=` on the WebSocket).

---

## 12. Environment variables

`.env.example` is the source of truth, with a comment on every setting. The main ones:

```
TELEPHONY=cartesia            # cartesia | phone | (empty: mock with USE_MOCKS, else cartesia)
PUBLIC_BASE_URL=              # public https URL for Cartesia webhooks (Azure URL or cloudflared)
CARTESIA_API_KEY=  CARTESIA_VOICE_ID=  CARTESIA_AGENT_MODEL=claude-haiku-5.5
TYPESAFE_API_KEY=             # Jev (both modes; keyword heuristics without it)
ANTHROPIC_API_KEY=            # Claude, phone mode
API_TOKEN=                    # empty = open (local dev); deploy.sh generates one
STORAGE_BACKEND=sql           # or convex (+ CONVEX_URL, CONVEX_BACKEND_TOKEN)
DATABASE_URL=  APPLICATIONINSIGHTS_CONNECTION_STRING=  AZURE_KEY_VAULT_URL=
USE_MOCKS=false
APNS_*                        # paid Apple account only; leave empty
```

Never commit secrets. In prod they are loaded from Azure Key Vault.

---

## 13. Development workflow

1. `uv sync` · `uv run pytest -q` · `(cd web && npm ci && npm run build)`
2. All mocks: `USE_MOCKS=true uv run uvicorn backend.main:app --reload`, open `http://localhost:8000/`.
3. Phone mode: `docker compose up -d`, `TELEPHONY=phone uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000`, open `/phone` (rep) and `/` (user). `uv run python -m evals.phone_rep` plays a scripted rep.
4. Cartesia mode: `cloudflared tunnel --url http://localhost:8000`, set `PUBLIC_BASE_URL`, start the server. On start it creates or updates the Cartesia agent, tools, webhook and phone number (`uv run python -m backend.cartesia.setup` does it by hand). Test by calling your own cell before any real company.

The fake support line (`fakeline/`): menu (1 orders, 2 billing, 0 agent), nested billing menu, recorded messages, hold music, occasional callback offer, then a rep (person on `/phone`, or the scripted bot rep).

---

## 14. Testing and evals

- **Unit tests:** `uv run pytest -q` (state transitions, guard rules, redaction, limits, full mock calls); `npm test` (Convex functions).
- **Eval suite (`evals/`):** 11 scripted scenarios on the fake line with a scripted bot rep — cancellation, fee waiver, order status, callback offer, transfer back to hold, menu loop, rep asks for PIN, rep offers paid upgrade, rep refuses to talk to AI, user takeover, rep asks for an SSN.
- `USE_MOCKS=false uv run python -m evals.run --driver product --guard real` runs our phone-mode pipeline with real Claude and Jev: `evals/results/product-claude.json` passed **11/11** with 0 safety violations. This measures phone mode, not the Cartesia agent.
- **Metrics:** success rate, avg minutes saved, # times asked user, errors, guard denials, time-to-greet. Saved in `evals/results/`; also served as JSON at `GET /metrics`.

---

## 15. Deployment (Azure)

`./infra/deploy.sh` (safe to re-run; details in `infra/README.md`):
- Resource group `call4me-rg` in `northcentralus` (Azure for Students allows only a few regions; the script checks).
- Container Apps (WebSockets, exactly 1 replica, no cold start), PostgreSQL Flexible Server (skipped when `STORAGE_BACKEND=convex`), Key Vault, Application Insights, ACR.
- Copies secrets from `.env` into Key Vault; generates `API_TOKEN` on first deploy.
- ACR Tasks are blocked on student subscriptions, so it falls back to a local `docker buildx` (amd64) build and push.
- `TELEPHONY=phone ./infra/deploy.sh` switches the deployed app to the backup demo.
- `az containerapp logs show` crashes (CLI bug); query `ContainerAppConsoleLogs_CL` in Log Analytics instead.

**Production path (for the pitch):** shared call state (Redis) to scale past one replica, per-user sign-in instead of one shared token, Postgres on a private network, telephony we control end to end (Azure Communication Services) so the guard can check every line in real calls too, per-call cost tracking.

---

## 16. Latency budget (rep speaks → agent replies)

- Cartesia mode: handled inside Cartesia's agent; keep tool webhooks fast (`report_state` 5 s timeout, others 10 s, `ask_user` up to ~110 s).
- Phone mode: STT final ~300–500 ms · Claude turn: keep prompts lean · Guard <10 ms · TTS first audio as low as Cartesia allows (streamed). Greeting on human detection is pre-generated: target <1.5 s total.

---

## 17. Coding conventions

- Python: type hints everywhere, `async` for all I/O, small modules, `ruff` formatting.
- Each external service has a client module + mock twin; never call SDKs directly from business logic.
- All state changes go through `engine/states.py` (one `transition()` function) — no ad-hoc state writes.
- Redact before logging: run every log/event payload through `audit/log.py` redaction.
- Storage goes through `open_store()`; no raw SQL outside the SQL adapter.
- Prefer clear names over clever code. Comment the "why," not the "what."

---

## 18. Status

Done: fake support line, phone-mode pipeline (audio, STT, Jev engine, menu navigation, Claude agent, guard, TTS), `ask_user` loop, takeover, audit log + redaction, evals (11/11), mobile web app + judges view, Azure deploy, real calls through Cartesia with Jev state checks and menu choices, optional Convex storage.

Shelved: native iOS app, Live Activities, APNs push (needs a paid Apple account). Stretch: web push.

Before demoing a Cartesia call, test one by calling your own cell. Keep a recorded backup video of a full call.

---

## 19. Demo (3 minutes)

1. Problem: Deaf users vs phone-only support (quote from someone we interviewed).
2. On a phone, start a call ("Remove the $40 late fee; no plan changes") → the Live screen shows menu navigation (and why each key was pressed), then hold with a timer.
3. A rep answers → the agent discloses it's an AI and starts.
4. Rep asks for the account PIN → the screen flashes → the user types → the agent relays it; it shows as `[REDACTED]` everywhere.
5. Rep offers a paid upgrade → `check_offer` / guard says no → the agent declines.
6. Fee waived, reference number → summary "34 minutes saved."
7. Proof: eval results (11/11), Azure deployment, audit log with the PIN redacted.

---

## 20. What changed from the original plan

| Original plan | As built | Why |
|---|---|---|
| Twilio Programmable Voice + Media Streams | Cartesia Managed Agents place real calls; `/phone` page is the backup | Cartesia's agent handles the phone line, listening, voice and keypad tones in one service |
| SwiftUI iPhone app, Live Activities, APNs | Mobile web app (`web/`) served at `/` | Free Apple ID: no push, 7-day installs, judges can't install it |
| PostgreSQL only | PostgreSQL by default, Convex optional | Teammate's Convex migration (merged PR #1) for reactive live updates |
| Silero VAD, YAMNet | Energy VAD + time-domain classifier behind the same interfaces | No onnxruntime/TensorFlow in the image |
| Claude via Foundry (maybe) | Anthropic API directly; Claude via Cartesia in cartesia mode | Foundry settings exist but aren't wired |
| Guard checks every line | True in phone mode; cartesia mode relies on instructions + `check_offer` | Cartesia's agent speaks for itself |

---

## 21. Multi-agent team setup

The repo was built by several Claude Code agents in parallel; see **TEAM.md** and `.claude/agents/`.

- Each specialist agent **owns specific directories** (table in TEAM.md). Only edit files you own.
- Shared interfaces live in `backend/contracts.py` (owned by `platform`). Don't change another area's interface: message its owner or the lead with the change you need.
- Before marking a task done: run `uv run pytest` for your area and commit with a clear message on your own branch.
