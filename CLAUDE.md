# CLAUDE.md — call4me

This file gives Claude Code the full context for this repo. Read it before every task. When something here conflicts with a request, ask before deviating.

---

## Convex migration branch

Read `docs/convex-migration.md` for the storage path added in this branch.
`backend/db/store.py` selects SQL or Convex. Business logic uses `open_store()`;
keep raw SQL queries in the SQL adapter. Python still owns long-running calls and
private answers. Convex stores redacted records and bounded live replay. The
historical architecture below describes the SQL/default deployment.

## 1. What we're building

**call4me** is an iPhone app plus a voice agent that makes a customer service phone call for the user, start to finish. It navigates the phone menu, waits on hold, talks to the human rep to reach the user's goal, and pulls the user in only when it needs something only they know (order number, PIN, a decision).

**Who it's for (named community):** Deaf and hard-of-hearing adults who face phone-only customer service. Relay services exist but involve waits, interpreters, and staying present through hold. call4me lets them handle the whole call by reading and typing.

**One-line pitch:** "Deaf people can't use phone-only customer service. Now they can, by typing."

**Context:** built at Cal Hacks 13.0 (Oct 23–25, 2026, San Francisco). Submission deadline: 8:30 AM Sunday. Judging is a 3-minute pitch + 2-minute Q&A at our table.

### Target tracks (and what each one judges)
- **Anthropic — Claude for Public Benefit:** named community; a real problem (we talked to people who have it); it works (one thing done well, usable Monday); thoughtful use of Claude, including where it could go wrong for these users.
- **Microsoft — Built for the Real World:** customer understanding; measurable usefulness; path from prototype to production; responsible design; end-to-end functionality. We use Azure credits where they don't hurt performance.
- **Cartesia:** voice is the core of the product (speech-to-text and text-to-speech).
- Secondary: Cal Hacks social impact prize (if offered), Figma Best Design (if screens are designed in Figma).

---

## 2. Non-negotiable rules (product and safety)

These are enforced **in code**, never only in prompts.

1. **AI disclosure:** the agent always identifies itself as an AI assistant calling on behalf of the user, using the user's chosen wording, at the start of any human interaction.
2. **Private info is never stored.** Anything the user provides via `ask_user` (PINs, account numbers, DOB, etc.) is spoken once, then redacted from transcripts, logs, the database, and telemetry. Store only that a request happened, never the answer.
3. **The agent never invents user facts.** It only speaks information the user provided or the session goal contains. Otherwise it must call `ask_user`.
4. **High-sensitivity items** (full SSN, full card number, passwords): do not relay via the agent. Suggest type-to-talk takeover so the user provides it directly.
5. **Limits are enforced by the guard layer.** The agent cannot agree to anything outside the user's stated goal and limits (new plans, fees, cancellations not requested). Blocked attempts are logged.
6. **User can take over at any time.** Takeover must stop the agent from initiating speech immediately.
7. **All user alerts are visual or haptic,** never sound-only (the users are Deaf/HoH).
8. **No recordings stored by default.** Process audio live. California requires all-party consent for recording.
9. **Append-only audit log.** Every state change, decision, denial, and tool call is logged with reason and confidence. Fixes are new events, never edits.

---

## 3. Architecture

```
iPhone app (SwiftUI)
  │  REST + WebSocket (captions, state)        ▲ APNs push (Live Activity, alerts, ask_user)
  ▼                                            │
Backend (Python, FastAPI) on Azure Container Apps
  ├─ Session API            ── PostgreSQL (sessions, events, menu paths, user_requests, audit_log)
  ├─ Call state engine      ── Jev (fast state + menu decisions)
  ├─ Conversation agent     ── Claude (tool-use loop)
  ├─ Guard layer            ── checks every outgoing action
  ├─ Audio pipeline         ── VAD + music classifier
  ├─ STT client             ── Cartesia streaming speech-to-text
  └─ TTS client             ── Cartesia Sonic text-to-speech
  │
  │  Twilio Media Streams (bidirectional WebSocket, 8 kHz μ-law, 20 ms frames)
  ▼
Twilio Programmable Voice ── outbound call ── company support line
                         └── Conference (takeover / bridging)
```

Secrets in **Azure Key Vault**. Logs/metrics in **Application Insights**.

### Who does what
- **Claude:** understands menus, runs the conversation with the rep, decides when to ask the user, writes the greeting, plain-language caption summaries, and the post-call summary.
- **Jev (TypeSafe):** fast, frequent typed decisions — call state every 2–3 s, menu option choice, human detection. Returns a typed answer + probability.
- **Cartesia:** ears (STT) and voice (TTS). Latency-critical.
- **Twilio:** the phone line, audio streaming, DTMF, conferences.
- **Azure:** hosting, database, secrets, monitoring. Optionally Claude via Microsoft Foundry (check availability; else Anthropic API directly).

---

## 4. Tech stack

| Layer | Tool |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn, `uv` for deps |
| DB | PostgreSQL (Docker locally; Azure Database for PostgreSQL in prod), SQLAlchemy |
| Telephony | Twilio Programmable Voice, Media Streams (bidirectional), Conference |
| Speech | Cartesia streaming STT; Cartesia Sonic TTS (streaming) |
| Decisions | Jev via `typesafe-sdk` (model `jev-latest`) |
| Conversation | Claude via Anthropic SDK (tool use), or via Microsoft Foundry |
| Audio | numpy, soxr (resampling), Silero VAD, YAMNet (music vs speech) |
| iOS | SwiftUI, ActivityKit (Live Activities), APNs, App Intents, UserNotifications (text-input actions), WatchKit haptics |
| Hosting | Azure Container Apps (WebSockets enabled), Key Vault, Application Insights |
| Dev | ngrok for public URLs, Docker Compose, pytest |
| Dashboard | Small web page (Next.js or plain HTML + WebSocket) |

**Never deploy the media WebSocket to serverless (e.g., Vercel).** Calls can last an hour; connections must stay open.

---

## 5. Repo layout

```
call4me/
├─ CLAUDE.md
├─ .env.example
├─ docker-compose.yml          # local Postgres
├─ backend/
│  ├─ main.py                  # FastAPI app, routes
│  ├─ config.py                # env + Key Vault loading
│  ├─ db/models.py             # SQLAlchemy models
│  ├─ telephony/twilio_client.py
│  ├─ telephony/media_ws.py    # /media WebSocket handler
│  ├─ audio/pipeline.py        # decode, resample, VAD, music classifier
│  ├─ speech/stt.py            # Cartesia STT client
│  ├─ speech/tts.py            # Cartesia TTS client
│  ├─ engine/states.py         # state machine
│  ├─ engine/jev_checks.py     # Jev state + menu decisions
│  ├─ agent/conversation.py    # Claude tool-use loop
│  ├─ agent/tools.py           # say, ask_user, press_digits, end_call, request_takeover
│  ├─ agent/guard.py           # limits, disclosure, privacy checks
│  ├─ notify/apns.py           # push + Live Activity updates
│  └─ audit/log.py             # append-only audit log, redaction
├─ fakeline/                   # fake support line (TwiML app) for testing
├─ evals/                      # scripted scenarios + scoring
├─ dashboard/                  # demo panel
└─ ios/Call4me/                # SwiftUI app
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

- Every 2–3 s, send the last transcript window + audio label to Jev (Choice): `menu | recorded_message | hold_music | silence | human | voicemail | callback_offer | call_ended`.
- **Timers:** menu loop > 3 repeats → press 0 / say "representative"; silence > 30 s → re-check; hold > user's max → notify and ask whether to continue.
- **Transfers** ("please hold while I transfer you") return to HOLD.
- **Callbacks:** give the agent's own Twilio number (not the user's), so the agent answers the callback. (Tentative decision; confirm.)
- Pause STT during confirmed hold music (cost); keep the music classifier and Jev checks running.
- Log every transition (from, to, reason, confidence) to `call_event` and `audit_log`.

### Human detection (latency-critical)
1. Jev flags `human` with high confidence → immediately play the **pre-generated** greeting (generate it before reaching a human).
2. Confirm by a natural response from the rep (recordings don't respond).
3. Notify user with haptic alert; open caption view.
Target: agent speaks within **~1.5 s** of the rep's greeting.

---

## 7. Conversation agent (Claude)

Runs in `IN_CONVERSATION`. Each rep turn: final STT transcript → Claude → tool call → guard → action.

### Tools
| Tool | Purpose |
|---|---|
| `say(text)` | Speak to the rep (passes through guard, then Cartesia TTS) |
| `ask_user(question, sensitivity)` | Pause and ask the user; `sensitivity ∈ {low, medium, high}` |
| `press_digits(digits)` | Send DTMF |
| `end_call(summary, reference_number)` | Finish: confirm details, thank rep, hang up |
| `request_takeover(reason)` | Ask the user to take over (e.g., high-sensitivity info) |

### `ask_user` flow
1. Agent says a holding line: "One moment, let me get that for you."
2. State → `WAITING_ON_USER`; create `user_request` (question only).
3. APNs push with **text-input action** + Apple Watch haptic.
4. User answers → `POST /requests/{id}/answer` → speak once via TTS → **redact** everywhere.
5. No answer in 60 s → ask the rep to hold briefly or offer to call back.
6. `sensitivity=high` → do not relay; suggest takeover.

### Prompting guidance
- System prompt includes: user's goal, limits, chosen disclosure wording, and the rules in Section 2.
- Keep spoken turns short and natural. Read back key details before ending ("Just to confirm, the $40 fee is waived and the reference is A7X291?").
- Never state facts about the user's account that weren't provided.

---

## 8. Guard layer (`agent/guard.py`)

Every outgoing action passes through `guard.check(action, session)` before execution:
- Disclosure already given? If not, prepend it.
- Does the text contain stored private data? Block.
- Does it agree to anything outside goal/limits? Block, log denial, return reason to Claude.
- High-sensitivity relay? Block; trigger `request_takeover`.
Denied attempts are logged to `audit_log` with `allowed=false`.

---

## 9. Data model (PostgreSQL)

- `call_session(id, user_id, company, phone_number, goal, limits_json, disclosure_text, state, started_at, ended_at, outcome, reference_number, minutes_saved)`
- `call_event(id, session_id, at, kind, from_state, to_state, confidence, detail_json)` — timeline
- `menu_path(id, company, steps_json, success_count, last_used_at)` — reuse working routes
- `user_request(id, session_id, question, sensitivity, status, created_at, answered_at)` — **never store the answer**
- `audit_log(id, at, actor, action, subject, allowed, reason, diff_json, evidence_json)` — append-only
- `device(user_id, apns_token, live_activity_token)`

---

## 10. API endpoints (backend)

- `POST /calls` — start a call `{company, phone_number, goal, limits, disclosure_text}`
- `GET /calls/{id}` — state, timeline
- `WS /calls/{id}/live` — captions, state, summaries to the app/dashboard
- `POST /calls/{id}/takeover` · `POST /calls/{id}/handback` · `POST /calls/{id}/cancel`
- `POST /calls/{id}/type` — type-to-talk text during takeover (spoken verbatim)
- `POST /requests/{id}/answer` — user's answer to `ask_user`
- `POST /twilio/status` — Twilio status callbacks
- `WS /media` — Twilio Media Stream
- `GET /companies?q=` — company directory

---

## 11. Mobile web app (`web/`) — Plan C

The native iOS app is shelved (`ios/` stays in the repo, unmaintained): a free Apple ID can't do push,
installs expire every 7 days, and judges can't install it. Instead the user's phone runs a **mobile web
app**, served by the Azure backend over HTTPS. Any phone opens it from a link, and "Add to Home
Screen" makes it look like an app.

- **Stack:** Next.js (App Router, TypeScript) + Tailwind, built as a static export (`web/out`) that
  FastAPI serves at `/`. No Node server in production. Pages use query params (`/live/?id=…`), not
  dynamic segments.
- **Start (`/`):** company (from `GET /companies`), phone number, goal, limits, info the agent may
  share (values used once, never saved in the browser), AI introduction → `POST /calls`.
- **Live (`/live/?id=`):** large high-contrast captions and a state banner (with a hold timer) over
  `WS /calls/{id}/live`. It reconnects automatically, and the server replays the call's recent
  messages on every connect, so a phone that slept or changed network rebuilds the screen. Also
  shows plain-language summaries and Hang up.
- **Question sheet (`ask_user`):** full-screen color flash (a single color change with reduced
  motion) plus vibration where supported, a big question, and a text input →
  `POST /requests/{id}/answer`. The input clears at once. For `sensitivity=high` there is no answer
  box, only **"Say it myself"** → takeover. It closes on the `ask_user_answered` live event.
- **Take over (type-to-talk):** `POST /calls/{id}/takeover`, then `POST /calls/{id}/type` (spoken
  verbatim). Quick phrases. Hand back with one tap.
- **Summary (`/summary/?id=`):** outcome, reference number (large, copyable), minutes saved.
- **Judges view (`/dashboard/`):** recent calls, eval metrics, a live call with its timeline and audit
  log (denials highlighted, private values shown as `[REDACTED]`).
- **Phone behavior:** Screen Wake Lock on the Live screen, a web app manifest for Add to Home Screen,
  large text (adjustable), big touch targets, plain short English.
- **Alerts are visual (+ vibration), never sound-only** (rule 7). Web push is a later stretch goal,
  so the app must be open during a call.
- **Access:** on Azure the API needs the access token (`API_TOKEN`). The app asks for it once, keeps it
  in localStorage, and sends it as `Authorization: Bearer` (or `?token=` on the WebSocket).
- The old plain-JS dashboard lives at `/console/` (backup, plus the phone-mode rep page `/phone`).

---

## 12. Environment variables (`.env.example`)

```
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_AGENT_NUMBER=          # number the agent calls from / receives callbacks on
TWILIO_FAKELINE_NUMBER=
USER_VERIFIED_CALLER_ID=      # optional: user's verified number for caller ID
PUBLIC_BASE_URL=              # ngrok URL in dev, Azure URL in prod
CARTESIA_API_KEY=
CARTESIA_VOICE_ID=
TYPESAFE_API_KEY=
ANTHROPIC_API_KEY=            # or Foundry settings below
AZURE_FOUNDRY_ENDPOINT=
AZURE_FOUNDRY_KEY=
DATABASE_URL=
APNS_KEY_ID=
APNS_TEAM_ID=
APNS_AUTH_KEY_PATH=
APNS_TOPIC=                   # app bundle id
APPLICATIONINSIGHTS_CONNECTION_STRING=
USE_MOCKS=false
```

Never commit secrets. In prod, load from Azure Key Vault.

---

## 13. Development workflow

1. `docker compose up -d` (Postgres) · `uv run uvicorn backend.main:app --reload`
2. `ngrok http 8000` → set `PUBLIC_BASE_URL`; point Twilio webhooks there.
3. Test against the **fake support line** first (`fakeline/`): menu (1 orders, 2 billing, 0 agent), nested billing menu, recorded messages, hold music 30–120 s, occasional callback offer, then forwards to a teammate as the rep (or a scripted bot rep).
4. Real company lines only after the fake line works end to end.

**Twilio notes (verify in docs):** trial accounts can only call verified numbers and play a trial message — use an upgraded account. To send DTMF mid-stream, update the call with `<Play digits>` then reconnect the stream (Telnyx has a direct send-DTMF command if this is painful).

---

## 14. Testing and evals

- **Unit tests (pytest):** state transitions, guard rules, redaction, `can`/limits checks.
- **Eval suite (`evals/`):** ≥10 scripted scenarios on the fake line with a scripted bot rep — cancellation, fee waiver, order status, callback offer, transfer back to hold, menu loop, rep asks for PIN, rep offers paid upgrade, rep refuses to talk to AI, user takeover.
- **Metrics reported:** success rate, avg minutes saved, # times asked user, errors, guard denials, time-to-greet after human detected.
- Send metrics to Application Insights; show them on the dashboard.

---

## 15. Deployment (Azure)

- Containerize backend → **Azure Container Apps** (WebSockets on, min replicas ≥1 to avoid cold starts).
- **Azure Database for PostgreSQL**, **Key Vault** for secrets, **Application Insights** for logs/traces/metrics.
- Point Twilio webhooks and the iOS app at the Azure URL.
- Production path (for the pitch): Azure Communication Services for telephony, multi-region, per-call cost tracking.

---

## 16. Latency budget (rep speaks → agent replies)

- STT final: ~300–500 ms · Claude turn: keep prompts lean · Guard: <10 ms · TTS first audio: as low as Cartesia allows (stream it).
- Greeting on human detection is pre-generated: target <1.5 s total.
- If Claude latency is too high mid-call, use a short filler line ("Sure, one second") while it thinks.

---

## 17. Coding conventions

- Python: type hints everywhere, `async` for all I/O, small modules, `ruff` formatting.
- Each external service has a client module + mock twin; never call SDKs directly from business logic.
- All state changes go through `engine/states.py` (one `transition()` function) — no ad-hoc state writes.
- Redact before logging: run every log/event payload through `audit/log.py` redaction.
- Prefer clear names over clever code. Comment the "why," not the "what."

---

## 18. Build order (follow in sequence)

1. Repo + this file · 2. Local Postgres + models · 3. Fake support line · 4. Outbound call + media stream
5. Cartesia STT · 6. VAD + music classifier · 7. State engine + Jev checks
8. Menu navigation · 9. Cartesia TTS + pre-generated greeting · 10. Claude conversation agent · 11. `ask_user` loop (test web page first)
12. iOS app basics · 13. Live Activity + APNs · 14. Answer from lock screen · 15. Takeover (type-to-talk; conference)
16. Guardrails + audit log · 17. Evals · 18. Azure deploy · 19. One real call (record as backup) · 20. Demo dashboard

**Gates:** Gate 1 (Fri night): agent calls fake line, audio reaches server. Gate 2 (Sat ~1 PM): full call on fake line incl. `ask_user`. Gate 3 (Sat ~7 PM): feature freeze, app end to end. If a gate slips >2 h, cut polish (Live Activity extras, menu-path memory), not core.

---

## 19. Demo (3 minutes)

1. Problem: Deaf users vs phone-only support (quote from someone we interviewed).
2. Start a call ("Remove the $40 late fee; no plan changes") → dashboard shows menu navigation, then hold.
3. Rep answers (teammate) → agent discloses, starts.
4. Rep asks for account PIN → presenter's lock screen/Watch alerts → types → agent relays.
5. Rep offers paid upgrade → guard blocks → agent declines.
6. Fee waived, reference number → summary "34 minutes saved."
7. Proof: eval table, Azure deployment, audit log with PIN redacted.

Always keep a recorded backup video of a full call.

---

## 20. Open questions

- Callbacks: give the agent's Twilio number (current plan) vs always decline?
- Claude via Microsoft Foundry available with our credits? (Ask at Microsoft booth.)
- Team: teammate as rep for demo, or scripted bot rep?

---

## 21. Multi-agent team setup

This repo is built by several Claude Code agents in parallel. See **TEAM.md** for how to run them.

- Each specialist agent is defined in `.claude/agents/` and **owns specific directories** (table in TEAM.md). Only edit files you own.
- Shared interfaces live in `backend/contracts.py` (owned by `platform`). Don't change another area's interface: message its owner or the lead with the change you need.
- Before marking a task done: run `uv run pytest` for your area and commit with a clear message on your own branch.
