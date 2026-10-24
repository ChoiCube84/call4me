# call4me iPhone app

SwiftUI app (iOS 17+) plus a Live Activity / Dynamic Island widget extension.
The Xcode project is generated from `project.yml` with XcodeGen and is not committed.

> Status: every Swift file passed `swiftc -parse`. The Foundation-only files
> (models, networking, settings) also passed a full type-check on macOS, and
> JSON decoding was tested against sample backend payloads. The app has **not**
> been built in Xcode yet (no Xcode on the machine where it was written).
> Expect to fix a few small compile errors on the first build.

## 1. Generate the project

```sh
brew install xcodegen
cd ios
xcodegen
open Call4me.xcodeproj
```

Run `xcodegen` again after you add or remove files.

## 2. Signing and bundle ids

1. In Xcode, select the **Call4me** target, then **Signing & Capabilities**, and pick your **Team**.
   Do the same for the **Call4meWidget** target. (Or set `DEVELOPMENT_TEAM` in `project.yml`.)
2. Change the bundle ids to ones your team owns. Edit them in `project.yml` and run `xcodegen` again:
   - app: `com.example.call4me`
   - widget: `com.example.call4me.widget` (must start with the app id)
3. The backend's `APNS_TOPIC` must equal the **app** bundle id.

## 3. Backend URL

- Default is `http://localhost:8000` (`Call4meDefaultBackendURL` in `Call4me/Info.plist`).
  This works only in the Simulator.
- On a real iPhone: open the app, tap the **gear** on the New call screen, and set the
  **Server address** to your Mac's LAN address (`http://192.168.x.x:8000`) or the ngrok
  `https://…` URL. Tap **Test connection**.
- The WebSocket URL comes from the same address (`http`→`ws`, `https`→`wss`).

## 4. Push notifications and Live Activities

1. The **Push Notifications** capability comes from `Call4me/Call4me.entitlements`
   (`aps-environment = development`). Xcode adds it to your App ID when you pick a team.
   If not, add it in Signing & Capabilities.
2. **Time Sensitive Notifications** is in the entitlements too
   (`com.apple.developer.usernotifications.time-sensitive`). Keep it so alerts get through Focus.
3. In the Apple Developer site, create an **APNs Auth Key (.p8)**. Give the backend
   `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_AUTH_KEY_PATH`, `APNS_TOPIC` (see CLAUDE.md section 12).
4. Use a **real iPhone**. Push, Live Activities, and haptics do not work fully in the Simulator.
5. On the phone: Settings > call4me > Notifications on, and Live Activities on.

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

## 5. Code map

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
