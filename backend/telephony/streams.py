"""Per-session media stream state, shared by the `/media` handler and the Twilio client.

A session's Twilio call may open several Media Streams over its life (each
`press_digits()` or takeover redirect replaces the stream), so the session's
inbound audio queue outlives any one WebSocket: `audio_in()` keeps yielding
across reconnects and ends only when the call ends.

Audio is processed live and never stored (CLAUDE.md rule 8).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from backend.telephony.base import CallStatusUpdate, IncomingCall

log = logging.getLogger("call4me.telephony")

INBOUND_QUEUE_FRAMES = 500  # 10 s; if the consumer stalls we drop the oldest audio
CALLBACK_WINDOW_S = 2 * 60 * 60


class Sender(Protocol):
    async def send_text(self, data: str) -> None: ...


@dataclass
class Leg:
    session_id: str
    call_sid: str | None = None
    stream_sid: str | None = None
    ws: Sender | None = None
    bidirectional: bool = False
    takeover: bool = False
    bridge_number: str | None = None
    user_call_sid: str | None = None  # user's phone leg while bridged in a conference
    listen_stream_name: str | None = None
    reconnect_pending: bool = False
    closed: bool = False
    status: str | None = None
    frames_in: int = 0
    frames_out: int = 0
    inbound: asyncio.Queue[bytes | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=INBOUND_QUEUE_FRAMES)
    )
    statuses: asyncio.Queue[CallStatusUpdate | None] = field(default_factory=asyncio.Queue)
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    marks: dict[str, asyncio.Future[bool]] = field(default_factory=dict)


class StreamRegistry:
    def __init__(self) -> None:
        self._legs: dict[str, Leg] = {}
        self._by_call_sid: dict[str, str] = {}
        self._expected_callbacks: deque[tuple[str, float]] = deque()
        self.incoming: asyncio.Queue[IncomingCall] = asyncio.Queue()

    # ---- lookup -------------------------------------------------------------

    def leg(self, session_id: str) -> Leg:
        leg = self._legs.get(session_id)
        if leg is None:
            leg = self._legs[session_id] = Leg(session_id=session_id)
        return leg

    def find(self, session_id: str) -> Leg | None:
        return self._legs.get(session_id)

    def session_for_call(self, call_sid: str) -> str | None:
        return self._by_call_sid.get(call_sid)

    def bind_call(self, session_id: str, call_sid: str) -> Leg:
        leg = self.leg(session_id)
        leg.call_sid = call_sid
        self._by_call_sid[call_sid] = session_id
        return leg

    def reopen(self, session_id: str) -> Leg:
        """Fresh state for a session whose earlier call ended (e.g. a callback)."""
        old = self.find(session_id)
        if old is not None and old.closed:
            self.forget(session_id)
        return self.leg(session_id)

    def forget(self, session_id: str) -> None:
        leg = self._legs.pop(session_id, None)
        if leg and leg.call_sid:
            self._by_call_sid.pop(leg.call_sid, None)

    # ---- stream lifecycle (driven by media_ws) ---------------------------------

    def attach(
        self, session_id: str, ws: Sender, stream_sid: str, call_sid: str, bidirectional: bool
    ) -> Leg:
        leg = self.bind_call(session_id, call_sid) if call_sid else self.leg(session_id)
        leg.ws, leg.stream_sid, leg.bidirectional = ws, stream_sid, bidirectional
        leg.reconnect_pending = False
        leg.connected.set()
        log.info(
            "stream %s attached to session %s (bidi=%s)", stream_sid, session_id, bidirectional
        )
        return leg

    def detach(self, session_id: str, stream_sid: str | None) -> None:
        leg = self.find(session_id)
        if leg is None or (stream_sid and leg.stream_sid != stream_sid):
            return  # a newer stream already replaced this one
        leg.ws = None
        leg.stream_sid = None
        leg.connected.clear()
        self._fail_marks(leg)
        if not leg.reconnect_pending:
            self.close(session_id)

    def push_inbound(self, leg: Leg, frame: bytes) -> None:
        if leg.closed:
            return
        if leg.inbound.full():
            leg.inbound.get_nowait()
        leg.inbound.put_nowait(frame)
        leg.frames_in += 1

    def close(self, session_id: str) -> None:
        leg = self.find(session_id)
        if leg is None or leg.closed:
            return
        leg.closed = True
        leg.connected.clear()
        self._fail_marks(leg)
        if leg.inbound.full():
            leg.inbound.get_nowait()
        leg.inbound.put_nowait(None)
        leg.statuses.put_nowait(None)

    # ---- audio --------------------------------------------------------------

    async def audio_in(self, session_id: str) -> AsyncIterator[bytes]:
        leg = self.leg(session_id)
        while True:
            frame = await leg.inbound.get()
            if frame is None:
                return
            yield frame

    async def _send(self, leg: Leg, msg: dict) -> bool:
        ws = leg.ws
        if ws is None or not leg.bidirectional or leg.stream_sid is None:
            return False
        msg["streamSid"] = leg.stream_sid
        async with leg.send_lock:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:  # noqa: BLE001 - socket gone; stop event will follow
                log.debug("send on closed stream for %s", leg.session_id)
                return False
        return True

    async def send_audio(self, session_id: str, mulaw: bytes) -> bool:
        leg = self.find(session_id)
        if leg is None or leg.closed:
            return False
        payload = base64.b64encode(mulaw).decode()
        sent = await self._send(leg, {"event": "media", "media": {"payload": payload}})
        if sent:
            leg.frames_out += 1
        return sent

    async def clear(self, session_id: str) -> None:
        leg = self.find(session_id)
        if leg is not None:
            await self._send(leg, {"event": "clear"})

    async def mark(self, session_id: str, name: str, timeout: float) -> bool:  # noqa: ASYNC109
        leg = self.find(session_id)
        if leg is None or leg.closed:
            return False
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        leg.marks[name] = fut
        if not await self._send(leg, {"event": "mark", "mark": {"name": name}}):
            leg.marks.pop(name, None)
            return False
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return False
        finally:
            leg.marks.pop(name, None)

    def mark_played(self, leg: Leg, name: str) -> None:
        fut = leg.marks.get(name)
        if fut and not fut.done():
            fut.set_result(True)

    @staticmethod
    def _fail_marks(leg: Leg) -> None:
        for fut in leg.marks.values():
            if not fut.done():
                fut.set_result(False)

    # ---- status + callbacks ---------------------------------------------------

    def publish_status(self, update: CallStatusUpdate) -> None:
        leg = self.leg(update.session_id)
        leg.status = update.status
        if not leg.closed:
            leg.statuses.put_nowait(update)
        if update.is_terminal:
            self.close(update.session_id)

    async def status_updates(self, session_id: str) -> AsyncIterator[CallStatusUpdate]:
        leg = self.leg(session_id)
        while True:
            update = await leg.statuses.get()
            if update is None:
                return
            yield update

    def expect_callback(self, session_id: str) -> None:
        self._expected_callbacks.append((session_id, time.monotonic()))

    def claim_callback(self) -> str | None:
        now = time.monotonic()
        while self._expected_callbacks:
            session_id, at = self._expected_callbacks.popleft()
            if now - at < CALLBACK_WINDOW_S:
                return session_id
        return None


registry = StreamRegistry()
