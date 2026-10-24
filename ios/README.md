# call4me iPhone app

SwiftUI app (iOS 17+) plus a Live Activity / Dynamic Island widget extension.
The Xcode project is generated from `project.yml` with XcodeGen and is not committed.

> Status: every Swift file passed `swiftc -parse`, and the Foundation-only files
> (models, networking, settings) pass a full type-check on macOS. The app has **not**
> been built in Xcode yet. Expect to fix a few small compile errors on the first build.

This guide is for a **free Apple ID** (Personal Team). That works for installing on
your own iPhone, with two limits: the app must be re-installed from Xcode every
7 days, and there are **no push notifications**. Keep the app open during a call:
`ask_user` questions, captions, and alerts arrive over the live connection and show
in the app (banner + haptic).

## 1. Install the tools (once)

1. Install **Xcode** from the Mac App Store (large download; start it first).
   Open it once, accept the license, and let it install the iOS platform.
2. `brew install xcodegen`
3. Xcode > Settings > Accounts > **+** > Apple ID. Sign in. Xcode creates your
   **Personal Team**.

## 2. Fill in Config/Secrets.xcconfig

`infra/deploy.sh` writes `ios/Config/Secrets.xcconfig` with the Azure URL
(`CALL4ME_BACKEND_URL`) and access code (`CALL4ME_API_TOKEN`). If you run it again,
it replaces only those two lines. If the file doesn't exist yet:

```sh
cd ios
cp Config/Secrets.example.xcconfig Config/Secrets.xcconfig
```

Then set:

- `CALL4ME_BUNDLE_ID`: a bundle id nobody else uses, e.g. `com.yourname.call4me`.
  Free accounts can't use `com.example...`.
- `CALL4ME_TEAM_ID`: optional. Leave it empty and pick the team in Xcode (step 3).

The file is gitignored: the access code is a password for your server. In
`.xcconfig` files `//` starts a comment, so URLs are written `https:/$()/host`.

## 3. Generate, sign, and run on your iPhone

```sh
cd ios
xcodegen
open Call4me.xcodeproj
```

1. Select the **Call4me** target > **Signing & Capabilities** > Team: your
   **Personal Team**. Do the same for **Call4meWidget**.
2. Plug in the iPhone (or use the same Wi-Fi after pairing once) and pick it as the
   run destination.
3. On the iPhone: Settings > Privacy & Security > **Developer Mode** > On (it restarts).
4. Press **Run** in Xcode. The first launch is blocked until you trust the profile:
   iPhone Settings > General > **VPN & Device Management** > your Apple ID > Trust.
5. Run again. In the app, open Settings (gear) and tap **Test connection**. The
   address and access code come from `Secrets.xcconfig`, and you can also type them there.

Run `xcodegen` again after you add or remove files or change `Secrets.xcconfig`.
Every 7 days, plug in and press Run again to re-sign.

## 4. Backend address and access code

- Default address and access code are baked in from `Secrets.xcconfig`. Without it the
  default is `http://localhost:8000` (Simulator only).
- Settings > **Server address** and **Access code** override them. "Wrong access
  code" means the code doesn't match the server's `API_TOKEN`.
- REST calls send `Authorization: Bearer <code>`. The live WebSocket
  (`/calls/{id}/live`, `wss://` for `https://`) sends it as `?token=<code>`.

## 5. If you later get a paid Apple Developer account

Push notifications let `ask_user` reach the lock screen and Apple Watch while the app
is closed, and keep the Live Activity updated.

1. Add these back to `Call4me/Call4me.entitlements`:
   `aps-environment` = `development`, and
   `com.apple.developer.usernotifications.time-sensitive` = YES. Add
   `remote-notification` back to `UIBackgroundModes` in `Call4me/Info.plist`.
2. Pick the paid team in Signing & Capabilities (Push Notifications is then allowed).
3. On developer.apple.com, create an **APNs Auth Key (.p8)** and give the backend
   `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_AUTH_KEY_PATH`, and `APNS_TOPIC` (= the app
   bundle id). Use `APNS_USE_SANDBOX=true` for Xcode builds and `false` for TestFlight.
4. Real iPhone only: push, Live Activities, and haptics don't fully work in the Simulator.

### What the app sends the backend

`POST /devices` `{user_id, apns_token}` after push registration, and
`{user_id, live_activity_token}` when a Live Activity starts. Tokens are hex strings.
`user_id` is a random id created on first launch.

### What the backend should send

Alert pushes (always a banner + haptic, never sound-only):

```json
{
  "aps": {
    "alert": {"title": "Needs your input", "body": "What is your account PIN?"},
    "category": "ASK_USER",
    "interruption-level": "time-sensitive",
    "sound": "default"
  },
  "request_id": "…",
  "session_id": "…"
}
```

Categories and their buttons (`Call4me/Services/NotificationCategories.swift`):

| Category | Buttons | What the app does |
|---|---|---|
| `ASK_USER` | Answer (text input), Open | `POST /requests/{request_id}/answer` `{answer}`. The answer is never saved. |
| `HUMAN_ANSWERED` | Open, Take over | Opens the call; Take over opens type-to-talk |
| `HOLD_CONTINUE` | Keep waiting, Stop | `POST /calls/{session_id}/hold` `{keep_waiting}` |
| `CALLBACK` | Open | Opens the call |
| `CALL_ENDED` | Open | Opens the summary |
| `TAKEOVER` | Take over, Open | For "Please take over" alerts |

`session_id` / `request_id` may be top-level in the payload or under `"data"`.

Live Activity updates (`apns-push-type: liveactivity`, topic `<bundle id>.push-type.liveactivity`):

```json
{
  "aps": {
    "timestamp": 1761242400,
    "event": "update",
    "content-state": {
      "state": "HOLD",
      "startedAt": 1761242000.0,
      "holdStartedAt": 1761242300.0,
      "detail": "On hold 5 min"
    }
  }
}
```

`state` is the backend `CallState` string. Times are Unix seconds. The app maps states to:
Calling (DIALING) → Navigating menu (MENU) → On hold + timer (HOLD, RECORDED_MESSAGE,
CALLBACK_OFFERED) → Person answered (HUMAN_*, IN_CONVERSATION) → Needs your input
(WAITING_ON_USER) → You're typing (TAKEOVER) → Done (ENDED, FAILED, VOICEMAIL).
Use `"event": "end"` to end it.

## 6. Code map

```
ios/
  project.yml                 XcodeGen spec (app + widget extension)
  Shared/                     Used by app and widget
    CallPhase.swift           CallState string -> plain-language phase
    CallActivityAttributes.swift  Live Activity data
    PhaseColor.swift
  Call4meWidget/              Live Activity + Dynamic Island
  Call4me/
    App/                      App entry, AppDelegate, push handling, router
    Models/                   API models, live messages, loose JSON
    Networking/               REST client, WebSocket client, JSON coding
    Services/                 Settings, haptics, push, Live Activity, categories
    Live/                     LiveCallModel (one call's state)
    Views/                    Onboarding, StartCall, Live (captions, ask sheet,
                              takeover, summary), Settings
```

## Rules the app follows

- Every alert is visual **and** haptic (`Services/Haptics.swift`). Never sound-only.
- `ask_user` answers live only in the sheet's memory or the notification response, are
  sent once, and are never written to UserDefaults, files, or logs.
- Short, plain text. Large fonts that grow with Dynamic Type.
- Dictation: use the mic on the iOS keyboard.
