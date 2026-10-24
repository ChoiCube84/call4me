"""APNs push and Live Activity updates.

Alerts are always visual (banner) plus haptic on the Watch; we never rely on
sound (CLAUDE.md rule 7): every alert carries a title/body, and a push with
nothing to show is dropped rather than sent sound-only.

`APNsNotifier` talks to Apple directly over HTTP/2 with token-based auth (an
ES256 JWT signed with the team's .p8 key). `MockNotifier` records what would
have been sent and is used with USE_MOCKS=true or when APNs isn't configured.

A push is a side channel: failures are logged and swallowed so they can never
stall or crash a live call.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

from backend.audit.log import redact
from backend.config import Settings, get_settings
from backend.contracts import TERMINAL_STATES, CallState, Notifier, UserRequest

log = logging.getLogger(__name__)

ASK_USER_CATEGORY = "ASK_USER"  # iOS category with a text-input action

APNS_PRODUCTION_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# Apple accepts provider tokens for 60 minutes and rejects refreshes more often
# than every 20; 50 minutes leaves headroom for clock skew.
TOKEN_TTL_SECONDS = 50 * 60

# Plain words shown on the Live Activity / Dynamic Island (CLAUDE.md section 11).
LIVE_ACTIVITY_LABELS: dict[CallState, str] = {
    CallState.DIALING: "Calling",
    CallState.MENU: "Navigating menu",
    CallState.HOLD: "On hold",
    CallState.RECORDED_MESSAGE: "On hold",
    CallState.CALLBACK_OFFERED: "On hold",
    CallState.HUMAN_SUSPECTED: "On hold",
    CallState.HUMAN_CONFIRMED: "Person answered",
    CallState.IN_CONVERSATION: "Person answered",
    CallState.WAITING_ON_USER: "Needs your input",
    CallState.TAKEOVER: "You're typing",
    CallState.VOICEMAIL: "Done",
    CallState.FAILED: "Done",
    CallState.ENDED: "Done",
}

# States the user should see right away; others use low priority to stay
# inside Apple's Live Activity update budget.
_URGENT_STATES = frozenset(
    {CallState.HUMAN_CONFIRMED, CallState.WAITING_ON_USER, CallState.TAKEOVER} | TERMINAL_STATES
)

# Stretches of waiting that the Live Activity shows a hold timer for.
HOLD_STATES = frozenset({CallState.HOLD, CallState.RECORDED_MESSAGE, CallState.CALLBACK_OFFERED})


@dataclass
class _CallTimeline:
    started_at: float
    hold_started_at: float | None = None


def _unix_seconds(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


DeviceLookup = Callable[[str], Awaitable[tuple[str | None, str | None]]]


def live_activity_label(state: CallState) -> str:
    return LIVE_ACTIVITY_LABELS.get(state, "Calling")


class MockNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def alert(
        self,
        user_id: str,
        title: str,
        body: str,
        *,
        category: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        msg = {"user_id": user_id, "title": title, "body": body, "category": category, "data": data}
        self.sent.append(msg)
        log.info("mock push %s", redact(msg))

    async def ask_user(self, user_id: str, request: UserRequest) -> None:
        await self.alert(
            user_id,
            "Needs your input",
            request.question,
            category=ASK_USER_CATEGORY,
            data={"request_id": request.id, "session_id": request.session_id},
        )

    async def update_live_activity(
        self, user_id: str, state: CallState, data: dict[str, Any] | None = None
    ) -> None:
        self.sent.append({"user_id": user_id, "live_activity": state, "data": data})


async def _lookup_device_tokens(user_id: str) -> tuple[str | None, str | None]:
    """(apns_token, live_activity_token) for a user, from the `device` table."""
    from backend.db.models import DeviceRow
    from backend.db.session import sessionmaker

    async with sessionmaker()() as db:
        row = await db.get(DeviceRow, user_id)
        if row is None:
            return None, None
        return row.apns_token, row.live_activity_token


class APNsNotifier(Notifier):
    """Token-based APNs over HTTP/2."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        device_lookup: DeviceLookup | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._lookup = device_lookup or _lookup_device_tokens
        self._clock = clock
        self._signing_key: str | None = None
        self._token: str | None = None
        self._token_issued_at = 0.0
        self._timelines: dict[str, _CallTimeline] = {}
        sandbox = bool(getattr(self._settings, "apns_use_sandbox", True))
        self.host = APNS_SANDBOX_HOST if sandbox else APNS_PRODUCTION_HOST

    # -- auth ----------------------------------------------------------- #

    def _provider_token(self) -> str:
        now = self._clock()
        if self._token is None or now - self._token_issued_at >= TOKEN_TTL_SECONDS:
            if self._signing_key is None:
                self._signing_key = _signing_key(self._settings)
            self._token = jwt.encode(
                {"iss": self._settings.apns_team_id, "iat": int(now)},
                self._signing_key,
                algorithm="ES256",
                headers={"kid": self._settings.apns_key_id},
            )
            self._token_issued_at = now
        return self._token

    # -- transport ------------------------------------------------------ #

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send(
        self,
        device_token: str,
        payload: dict[str, Any],
        *,
        push_type: str,
        topic: str,
        priority: int,
        collapse_id: str | None = None,
    ) -> bool:
        """POST one notification. Returns True on 200; never raises."""
        try:
            headers = {
                "authorization": f"bearer {self._provider_token()}",
                "apns-topic": topic,
                "apns-push-type": push_type,
                "apns-priority": str(priority),
            }
            if collapse_id:
                headers["apns-collapse-id"] = collapse_id[:64]
            resp = await self._http().post(
                f"{self.host}/3/device/{device_token}", json=redact(payload), headers=headers
            )
        except Exception as exc:  # noqa: BLE001 - a push must never break the call
            log.warning("apns %s push failed: %s", push_type, type(exc).__name__)
            return False
        if resp.status_code == 200:
            return True
        try:
            reason = resp.json().get("reason", "")
        except ValueError:
            reason = ""
        log.warning("apns %s push rejected: %s %s", push_type, resp.status_code, reason)
        return False

    async def _tokens(self, user_id: str) -> tuple[str | None, str | None]:
        try:
            return await self._lookup(user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("apns device lookup failed: %s", type(exc).__name__)
            return None, None

    # -- Notifier ------------------------------------------------------- #

    async def alert(
        self,
        user_id: str,
        title: str,
        body: str,
        *,
        category: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not (title or body):
            log.warning("apns alert dropped: nothing visible to show (never sound-only)")
            return
        apns_token, _ = await self._tokens(user_id)
        if not apns_token:
            log.info("apns alert skipped: no device token for user")
            return
        aps: dict[str, Any] = {
            "alert": {"title": title, "body": body},
            "interruption-level": "time-sensitive",
            "sound": "default",  # extra cue for hearing companions; the banner is primary
        }
        if category:
            aps["category"] = category
        extra = {k: v for k, v in (data or {}).items() if k != "aps"}
        if session_id := extra.get("session_id"):
            aps["thread-id"] = str(session_id)
        await self._send(
            apns_token,
            {"aps": aps, **extra},
            push_type="alert",
            topic=self._settings.apns_topic,
            priority=10,
        )

    async def ask_user(self, user_id: str, request: UserRequest) -> None:
        # Only the question goes out; answers never leave the backend.
        await self.alert(
            user_id,
            "Needs your input",
            request.question,
            category=ASK_USER_CATEGORY,
            data={"request_id": request.id, "session_id": request.session_id},
        )

    def _timeline(self, session_id: str, state: CallState, now: float) -> _CallTimeline:
        """Track call start and the current hold stretch per session (in memory)."""
        tl = self._timelines.setdefault(session_id, _CallTimeline(started_at=now))
        if state in HOLD_STATES:
            if tl.hold_started_at is None:
                tl.hold_started_at = now
        else:
            tl.hold_started_at = None
        return tl

    async def update_live_activity(
        self, user_id: str, state: CallState, data: dict[str, Any] | None = None
    ) -> None:
        data = data or {}
        now = self._clock()
        session_id = str(data.get("session_id", "") or user_id)
        tl = self._timeline(session_id, state, now)
        if (started := _unix_seconds(data.get("started_at"))) is not None:
            tl.started_at = started
        terminal = state in TERMINAL_STATES
        if terminal:
            self._timelines.pop(session_id, None)

        _, la_token = await self._tokens(user_id)
        if not la_token:
            return
        # Must match the iOS ActivityKit ContentState (Codable):
        # state, startedAt, holdStartedAt?, detail?  (times in Unix seconds)
        content_state: dict[str, Any] = {
            "state": str(state),
            "startedAt": int(tl.started_at),
            "detail": live_activity_label(state),
        }
        if tl.hold_started_at is not None:
            content_state["holdStartedAt"] = int(tl.hold_started_at)
        aps: dict[str, Any] = {
            "timestamp": int(now),
            "event": "end" if terminal else "update",
            "content-state": content_state,
        }
        if terminal:
            aps["dismissal-date"] = int(now) + 15 * 60  # keep "Done" visible for a while
        await self._send(
            la_token,
            {"aps": aps},
            push_type="liveactivity",
            topic=f"{self._settings.apns_topic}.push-type.liveactivity",
            priority=10 if state in _URGENT_STATES else 5,
        )


def _signing_key(settings: Settings) -> str:
    # In Azure the .p8 contents come from Key Vault (`apns-auth-key`); locally a file path.
    content = getattr(settings, "apns_auth_key", "") or ""
    if content.strip():
        return content.replace("\\n", "\n")  # tolerate a one-line secret with literal \n
    return Path(settings.apns_auth_key_path).read_text()


def apns_configured(settings: Settings) -> bool:
    has_key = bool(getattr(settings, "apns_auth_key", "") or settings.apns_auth_key_path)
    return all((settings.apns_key_id, settings.apns_team_id, has_key, settings.apns_topic))


_apns: APNsNotifier | None = None


def get_notifier() -> Notifier:
    """Mock with USE_MOCKS or missing APNs settings; otherwise a shared APNsNotifier."""
    global _apns
    settings = get_settings()
    if settings.use_mocks:
        return MockNotifier()
    if not apns_configured(settings):
        log.warning("APNs settings missing (APNS_KEY_ID/TEAM_ID/AUTH_KEY[_PATH]/TOPIC); using mock")
        return MockNotifier()
    if _apns is None:
        _apns = APNsNotifier(settings)
    return _apns


async def close_notifier() -> None:
    """Close the shared APNs HTTP/2 connection (call from app shutdown)."""
    global _apns
    if _apns is not None:
        await _apns.aclose()
        _apns = None


__all__ = [
    "ASK_USER_CATEGORY",
    "APNsNotifier",
    "MockNotifier",
    "close_notifier",
    "get_notifier",
    "live_activity_label",
]
