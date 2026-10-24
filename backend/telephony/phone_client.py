"""Browser fake phone for offline demos of our own pipeline (TELEPHONY=phone).

The company side is the mock line (`mock_client.MockLine`): the same welcome,
menus, and hold queue as the fake support line, at real time. When the call
reaches a rep, the web page at `/phone` rings, and whoever answers it *is* the
rep: their mic goes to the agent as call audio, and the agent's voice plays on
their speaker. Everything else (STT, Jev, Claude, TTS, guard) runs for real.

Browser protocol on `WS /phone/ws`:
  server -> page  JSON {"type": "idle" | "ringing" | "connected" | "clear" | "ended"}
                  binary: agent audio, 16-bit little-endian PCM at 8 kHz
  page -> server  JSON {"type": "answer" | "hangup"}
                  binary: rep mic audio, 16-bit little-endian PCM at 8 kHz
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.audio.codec import mulaw_to_pcm16, pcm16_to_mulaw
from backend.contracts import CallSession
from backend.telephony import get_telephony, mulaw
from backend.telephony.mock_client import MockLine, MockTelephonyClient, _Hangup

log = logging.getLogger("call4me.telephony.phone")

RING_TIMEOUT_S = 15 * 60  # nobody answers the page: the "company" gives up
MAX_MIC_BACKLOG = 10  # frames (200 ms); older mic audio is dropped to keep latency low


class PhoneLine(MockLine):
    """A mock line whose rep is a person on the `/phone` page."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.company = ""
        self.waiting_for_rep = False
        self.answered = False
        self.rep_hung_up = False
        self.mic: asyncio.Queue[bytes] = asyncio.Queue()
        self.to_page: asyncio.Queue[bytes | dict[str, str]] = asyncio.Queue()
        self._mic_rest = b""

    # ---- the rep is a person ------------------------------------------------------

    async def _rep(self) -> None:
        self.waiting_for_rep = True
        log.info("phone line %s: ringing the rep page", self.session_id)
        rang = 0.0
        while not self.answered:  # hold music until someone answers the page
            if rang > RING_TIMEOUT_S:
                raise _Hangup
            await self._music(1.0)
            rang += 1.0
        self.waiting_for_rep = False
        while not self.rep_hung_up:
            frame = mulaw.SILENCE_FRAME
            with contextlib.suppress(asyncio.QueueEmpty):
                frame = self.mic.get_nowait()
            await self._put(frame)  # paced at real time
        raise _Hangup

    def answer(self) -> bool:
        if self.answered or self.ended.is_set():
            return False
        self.answered = True
        self.to_page.put_nowait({"type": "connected"})
        return True

    def rep_audio(self, pcm: bytes) -> None:
        data = self._mic_rest + pcm16_to_mulaw(pcm[: len(pcm) // 2 * 2])
        n = len(data) // mulaw.FRAME_BYTES * mulaw.FRAME_BYTES
        self._mic_rest = data[n:]
        for i in range(0, n, mulaw.FRAME_BYTES):
            self.mic.put_nowait(data[i : i + mulaw.FRAME_BYTES])
        while self.mic.qsize() > MAX_MIC_BACKLOG:
            self.mic.get_nowait()

    def hang_up_rep(self) -> None:
        self.rep_hung_up = True

    # ---- agent audio goes to the page ---------------------------------------------

    def agent_audio(self, frame: bytes) -> None:
        super().agent_audio(frame)
        if self.answered:
            self.to_page.put_nowait(mulaw_to_pcm16(frame))

    def clear(self) -> None:
        super().clear()
        if self.answered:
            self.to_page.put_nowait({"type": "clear"})

    def finish(self) -> None:
        if not self.ended.is_set():
            self.to_page.put_nowait({"type": "ended"})
        super().finish()


class PhoneLineClient(MockTelephonyClient):
    line_cls = PhoneLine
    echo_cancelled = True  # getUserMedia echoCancellation on the page

    def __init__(self) -> None:
        # A short queue intro, then the page rings; the hold lasts until it's answered.
        super().__init__(speed=1.0, hold_seconds=(0.0, 0.0), callback_offer=False)

    async def place_call(self, session: CallSession) -> str:
        sid = await super().place_call(session)
        line = self.lines[session.id]
        assert isinstance(line, PhoneLine)
        line.company = session.company
        return sid

    def ringing(self) -> PhoneLine | None:
        for line in self.lines.values():
            if isinstance(line, PhoneLine) and line.waiting_for_rep and not line.answered:
                if not line.ended.is_set():
                    return line
        return None


# --------------------------------------------------------------------------- #
# The page's WebSocket
# --------------------------------------------------------------------------- #

router = APIRouter()


@router.websocket("/phone/ws")
async def phone_ws(ws: WebSocket) -> None:
    await ws.accept()
    client = get_telephony()
    if not isinstance(client, PhoneLineClient):
        await ws.send_json(
            {"type": "unavailable", "reason": "server is not in TELEPHONY=phone mode"}
        )
        await ws.close()
        return

    inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def receive() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                inbox.put_nowait(msg)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            inbox.put_nowait(None)

    receiver = asyncio.create_task(receive())
    try:
        while True:
            line = await _wait_for_ring(ws, client, inbox)
            if line is None:
                return
            if await _bridge(ws, line, inbox) is False:
                return
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        receiver.cancel()


def _json_type(msg: dict[str, Any]) -> str | None:
    text = msg.get("text")
    if not text:
        return None
    try:
        return str(json.loads(text).get("type"))
    except (ValueError, AttributeError):
        return None


async def _wait_for_ring(
    ws: WebSocket, client: PhoneLineClient, inbox: asyncio.Queue[dict[str, Any] | None]
) -> PhoneLine | None:
    """Idle, then ringing, until the page answers. None if the page went away."""
    await ws.send_json({"type": "idle"})
    shown: PhoneLine | None = None
    while True:
        line = client.ringing()
        if line is not shown:
            shown = line
            if line is None:
                await ws.send_json({"type": "idle"})
            else:
                await ws.send_json({"type": "ringing", "company": line.company})
        try:
            msg = await asyncio.wait_for(inbox.get(), 0.25)
        except TimeoutError:
            continue
        if msg is None:
            return None
        if _json_type(msg) == "answer" and shown is not None and shown.answer():
            return shown


async def _bridge(
    ws: WebSocket, line: PhoneLine, inbox: asyncio.Queue[dict[str, Any] | None]
) -> bool:
    """Carry audio both ways until either side hangs up. False if the page went away."""

    async def send() -> None:
        while True:
            item = await line.to_page.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_json(item)
                if item["type"] == "ended":
                    return

    sender = asyncio.create_task(send())
    try:
        while not sender.done():
            try:
                msg = await asyncio.wait_for(inbox.get(), 0.25)
            except TimeoutError:
                continue
            if msg is None:
                line.hang_up_rep()
                return False
            if msg.get("bytes"):
                line.rep_audio(msg["bytes"])
            elif _json_type(msg) == "hangup":
                line.hang_up_rep()
        return True
    finally:
        # Let the "ended" message reach the page before we stop sending.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(sender), 2.0)
        sender.cancel()
