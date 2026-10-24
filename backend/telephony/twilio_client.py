"""Real `TelephonyClient` on Twilio Programmable Voice + bidirectional Media Streams.

Flow
----
- `place_call`: REST-create the call with inline TwiML
  ``<Connect><Stream url="wss://<PUBLIC_BASE_URL>/media"><Parameter session_id/>``.
  Twilio opens `WS /media` (see media_ws.py) once the company answers.
- `press_digits`: Twilio has no "send DTMF" on a stream, so we update the live
  call with ``<Play digits="..."/>`` followed by a fresh ``<Connect><Stream>``.
  The old stream stops, the digits play, a new stream opens on the same
  session; `audio_in()` keeps going. Returns once the new stream is up.
- Takeover: always clears queued agent audio and flags the session (call-engine
  must stop initiating speech; typed text still goes out via `send_audio`).
  If `set_takeover_bridge()` gave the user's phone number, the company leg is
  also moved into Conference ``call4me-<session>`` with a listen-only
  ``<Start><Stream>`` so captions keep flowing, and the user's phone is dialed
  into the same room. `end_takeover` reverses it.
- Status callbacks go to ``<PUBLIC_BASE_URL>/twilio/status``; the platform route
  hands the form to `handle_status_form()`.

Caller ID is our agent number, so a company's automatic callback reaches the
agent, not the user (CLAUDE.md section 6). Nothing is recorded.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Mapping

from backend.config import Settings, get_settings
from backend.contracts import CallSession
from backend.telephony.base import (
    DIGITS_PATTERN,
    CallControl,
    CallStatus,
    CallStatusUpdate,
    TelephonyError,
)
from backend.telephony.streams import Leg, StreamRegistry, registry
from backend.telephony.twilio_rest import TwilioRest
from backend.telephony.twiml import Response

log = logging.getLogger("call4me.telephony")

RECONNECT_TIMEOUT_S = 10.0
_DIGITS = re.compile(DIGITS_PATTERN)


def conference_room(session_id: str) -> str:
    return f"call4me-{session_id}"


def media_url(public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/media"


def stream_twiml(public_base_url: str, session_id: str, *, digits: str | None = None) -> str:
    t = Response()
    if digits:
        t.play(digits=digits)
    return t.connect_stream(media_url(public_base_url), {"session_id": session_id}).to_xml()


class TwilioTelephonyClient(CallControl):
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        rest: TwilioRest | None = None,
        streams: StreamRegistry | None = None,
        caller_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.streams = streams or registry
        self._rest = rest
        self.caller_id = caller_id or self.settings.twilio_agent_number

    @property
    def rest(self) -> TwilioRest:
        if self._rest is None:
            self._rest = TwilioRest(
                self.settings.twilio_account_sid, self.settings.twilio_auth_token
            )
        return self._rest

    @property
    def base_url(self) -> str:
        if not self.settings.public_base_url:
            raise TelephonyError("PUBLIC_BASE_URL must be set so Twilio can reach /media")
        return self.settings.public_base_url.rstrip("/")

    def _live_leg(self, session_id: str) -> Leg:
        leg = self.streams.find(session_id)
        if leg is None or leg.call_sid is None:
            raise TelephonyError(f"no live call for session {session_id}")
        return leg

    # ---- contract -------------------------------------------------------------

    async def place_call(self, session: CallSession) -> str:
        if not self.caller_id:
            raise TelephonyError("TWILIO_AGENT_NUMBER must be set")
        self.streams.reopen(session.id)
        call_sid = await self.rest.create_call(
            to=session.phone_number,
            from_=self.caller_id,
            twiml=stream_twiml(self.base_url, session.id),
            status_callback=f"{self.base_url}/twilio/status",
        )
        self.streams.bind_call(session.id, call_sid)
        log.info("placed call %s for session %s", call_sid, session.id)
        return call_sid

    def audio_in(self, session_id: str) -> AsyncIterator[bytes]:
        return self.streams.audio_in(session_id)

    async def send_audio(self, session_id: str, mulaw_frame: bytes) -> None:
        await self.streams.send_audio(session_id, mulaw_frame)

    async def clear_audio(self, session_id: str) -> None:
        await self.streams.clear(session_id)

    async def press_digits(self, session_id: str, digits: str) -> None:
        if not _DIGITS.match(digits):
            raise ValueError("digits must match " + DIGITS_PATTERN)
        leg = self._live_leg(session_id)
        async with leg.control_lock:
            await self._redirect(leg, stream_twiml(self.base_url, session_id, digits=digits))

    async def start_takeover(self, session_id: str) -> None:
        leg = self._live_leg(session_id)
        leg.takeover = True  # set first: callers check this before speaking
        await self.clear_audio(session_id)
        if not leg.bridge_number or leg.user_call_sid:
            return
        async with leg.control_lock:
            room = conference_room(session_id)
            leg.listen_stream_name = f"listen-{session_id}"
            company = (
                Response()
                .start_stream(
                    media_url(self.base_url),
                    {"session_id": session_id, "mode": "listen"},
                    name=leg.listen_stream_name,
                )
                .conference(room, end_on_exit=True)
            )
            await self._redirect(leg, company.to_xml())
            user = Response().say("Connecting you to your call.").conference(room)
            leg.user_call_sid = await self.rest.create_call(
                to=leg.bridge_number, from_=self.caller_id, twiml=user.to_xml()
            )

    async def end_takeover(self, session_id: str) -> None:
        leg = self._live_leg(session_id)
        async with leg.control_lock:
            if leg.user_call_sid:
                t = Response()
                if leg.listen_stream_name:
                    t.stop_stream(leg.listen_stream_name)
                t.connect_stream(media_url(self.base_url), {"session_id": session_id})
                await self._redirect(leg, t.to_xml())
                await self._hang_up_user(leg)
            leg.takeover = False

    async def hang_up(self, session_id: str) -> None:
        leg = self.streams.find(session_id)
        if leg is None:
            return
        await self._hang_up_user(leg)
        if leg.call_sid and not leg.closed:
            try:
                await self.rest.update_call(leg.call_sid, status="completed")
            except TelephonyError as e:
                log.warning("hang up %s: %s", leg.call_sid, e)  # already ended is fine
        self.streams.close(session_id)

    # ---- extras ---------------------------------------------------------------

    async def wait_connected(self, session_id: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        leg = self.streams.leg(session_id)
        try:
            await asyncio.wait_for(leg.connected.wait(), timeout)
        except TimeoutError:
            return False
        return not leg.closed

    async def mark(self, session_id: str, name: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        return await self.streams.mark(session_id, name, timeout)

    def status_updates(self, session_id: str) -> AsyncIterator[CallStatusUpdate]:
        return self.streams.status_updates(session_id)

    def is_taken_over(self, session_id: str) -> bool:
        leg = self.streams.find(session_id)
        return bool(leg and leg.takeover)

    def set_takeover_bridge(self, session_id: str, phone_number: str | None) -> None:
        self.streams.leg(session_id).bridge_number = phone_number or None

    def expect_callback(self, session_id: str) -> None:
        self.streams.expect_callback(session_id)

    # ---- internals ------------------------------------------------------------

    async def _redirect(self, leg: Leg, twiml: str) -> None:
        """Replace the call's TwiML; the current stream stops and a new one opens."""
        assert leg.call_sid
        leg.reconnect_pending = True
        leg.connected.clear()
        try:
            await self.rest.update_call(leg.call_sid, twiml=twiml)
        except TelephonyError:
            leg.reconnect_pending = False
            raise
        try:
            await asyncio.wait_for(leg.connected.wait(), RECONNECT_TIMEOUT_S)
        except TimeoutError:
            leg.reconnect_pending = False
            log.warning("stream did not reconnect for %s", leg.session_id)

    async def _hang_up_user(self, leg: Leg) -> None:
        sid, leg.user_call_sid = leg.user_call_sid, None
        if sid:
            try:
                await self.rest.update_call(sid, status="completed")
            except TelephonyError as e:
                log.warning("hang up user leg %s: %s", sid, e)


def handle_status_form(
    form: Mapping[str, str], streams: StreamRegistry | None = None
) -> CallStatusUpdate | None:
    """Map a Twilio status callback to its session and publish it.

    Returns None for calls we don't know (e.g. the user's bridge leg) or unknown statuses.
    """
    streams = streams or registry
    call_sid = form.get("CallSid", "")
    session_id = streams.session_for_call(call_sid)
    if session_id is None:
        return None
    try:
        status = CallStatus(form.get("CallStatus", ""))
    except ValueError:
        return None
    duration = form.get("CallDuration")
    update = CallStatusUpdate(
        session_id=session_id,
        call_sid=call_sid,
        status=status,
        duration_s=int(duration) if duration and duration.isdigit() else None,
    )
    streams.publish_status(update)
    return update
