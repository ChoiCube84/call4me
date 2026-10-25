# call4me web app

The phone app for call4me (CLAUDE.md section 11, Plan C): a mobile web app that Deaf and
hard-of-hearing users open on their phone. It starts a call, shows live captions and the call
state, flashes full screen when call4me needs an answer, lets the user take over by typing, and
shows a summary. `/dashboard/` is a desktop view for judges.

Next.js (App Router, TypeScript) + Tailwind, built as a **static export**. In production FastAPI
serves `out/` at the site root, on the same origin as the API, so there is no Node server.

| Page | What |
|---|---|
| `/` | Start a call: company, number, goal, rules, info call4me may share → `POST /calls` |
| `/live/?id=…` | State banner + hold timer, large captions, question sheet, take over, hang up |
| `/summary/?id=…` | Outcome, reference number (copy), minutes saved |
| `/dashboard/` | Judges: recent calls, live captions, timeline, audit log |

Pages take the call id as a query param because a static export can't have dynamic segments.

## Develop

```bash
npm install
# backend on :8000 (from the repo root): USE_MOCKS=true uv run uvicorn backend.main:app
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

With `NEXT_PUBLIC_API_BASE` set, the dev server calls the backend cross-origin, so the browser
may block API calls (CORS). The simplest full test: `npm run build`, then open the backend's
own URL (`http://localhost:8000/`), which serves `out/`.

```bash
npm run lint
npm run build          # → out/ (served by FastAPI at /)
node scripts/make-icons.mjs   # regenerate the PWA icons in public/
```

## How it talks to the backend

- `lib/api.ts`: REST calls and typed models. When the server has `API_TOKEN`, a 401 opens the
  access-code dialog. The code is kept in localStorage (`call4me.token`) and sent as
  `Authorization: Bearer`.
- `lib/live.ts`: `useLiveCall(id)` on `WS /calls/{id}/live` (`?token=` in the URL). It
  reconnects with backoff (1, 2, 5, 10 s) and right away when the phone wakes up. The server
  replays the call's recent messages on every connect, so the view is rebuilt from scratch each
  time.
- Privacy: `ask_user` answers, typed lines, and "info call4me may share" values are read
  straight from the inputs, and the inputs are cleared when sent. They are never kept in React
  state, storage, or the console.

## On the phone

- **Add to Home Screen.** iPhone (Safari): Share → *Add to Home Screen*. Android (Chrome): ⋮ →
  *Add to Home screen* / *Install app*. It then opens full screen like an app (web app
  manifest + icons).
- **Screen stays on** during a call (Screen Wake Lock API: iOS 16.4+, Android Chrome). Where
  it isn't supported, the app shows a tip to keep the screen on.
- **Alerts are visual, never sound-only.** A question flashes the whole screen yellow/black
  (a single color change with *Reduce Motion*) and vibrates where the browser allows it
  (Android; iPhone browsers can't vibrate).
- Text is large and high-contrast, captions can be made bigger (A−/A+), and touch targets are
  at least 48 px.

## Later (stretch goals)

- TODO: **Web push** (service worker + VAPID; iOS 16.4+ for home-screen apps), so a question
  reaches the user when the app is closed. Until then, keep the app open during a call.
- Offline shell (service worker cache) and a reconnect sound-free status in the home-screen
  icon badge.

## Convex live updates

Set `NEXT_PUBLIC_CONVEX_URL` at build time to subscribe to Convex replay instead of
the worker WebSocket. The always-on deployment serves this static app and its call
controls from Convex HTTP actions on the same `.convex.site` origin. Private answers
are encrypted temporarily in Convex until the waiting Cartesia tool receives them.
See [Convex always-on deployment](../docs/convex-always-on.md). Leave the URL empty
for legacy local installations.
