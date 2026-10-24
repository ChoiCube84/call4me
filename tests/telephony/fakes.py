"""Test doubles: a fake Twilio Media Stream socket and a recording Twilio REST API."""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qsl

import httpx
from fastapi import WebSocketDisconnect

from backend.telephony.streams import StreamRegistry


class FakeTwilioSocket:
    """Plays Twilio's side of WS /media: we push events in, read what the server sent."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        msg = await self.incoming.get()
        if msg is None:
            raise WebSocketDisconnect(1000)
        return msg

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    # helpers for the test (Twilio -> us)
    def event(self, payload: dict) -> None:
        self.incoming.put_nowait(json.dumps(payload))

    def start(self, stream_sid: str, call_sid: str, **params: str) -> None:
        self.event({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        self.event(
            {
                "event": "start",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                    "callSid": call_sid,
                    "tracks": ["inbound"],
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                    "customParameters": params,
                },
            }
        )

    def media(self, frame: bytes) -> None:
        self.event(
            {
                "event": "media",
                "media": {"track": "inbound", "payload": base64.b64encode(frame).decode()},
            }
        )

    def stop(self) -> None:
        self.event({"event": "stop", "stop": {}})

    def disconnect(self) -> None:
        self.incoming.put_nowait(None)


class FakeTwilioApi:
    """httpx transport recording Calls API requests. Optionally reconnects a stream
    when a call is updated with TwiML, as Twilio would."""

    def __init__(self, registry: StreamRegistry | None = None) -> None:
        self.requests: list[tuple[str, dict[str, list[str]]]] = []
        self.registry = registry
        self.sockets: list[FakeTwilioSocket] = []
        self.tasks: list[asyncio.Task] = []
        self.current: dict[str, FakeTwilioSocket] = {}  # call_sid -> live stream
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        form: dict[str, list[str]] = {}
        for k, v in parse_qsl(request.content.decode()):
            form.setdefault(k, []).append(v)
        self.requests.append((request.url.path, form))
        self._n += 1
        if "Twiml" in form and "/Calls/" in request.url.path and self.registry is not None:
            self._reconnect(request.url.path, form["Twiml"][0])
        return httpx.Response(201, json={"sid": f"CA{self._n:032d}"})

    def connect(self, call_sid: str, session_id: str) -> FakeTwilioSocket:
        """Twilio answering a fresh call and opening its first stream."""
        from backend.telephony import media_ws

        ws = FakeTwilioSocket()
        ws.start(f"MZinit{self._n}", call_sid, session_id=session_id)
        self.current[call_sid] = ws
        self.sockets.append(ws)
        self.tasks.append(asyncio.ensure_future(media_ws.handle(ws, self.registry)))
        return ws

    def _reconnect(self, path: str, twiml: str) -> None:
        from backend.telephony import media_ws

        call_sid = path.rsplit("/", 1)[-1].removesuffix(".json")
        session_id = self.registry.session_for_call(call_sid)
        if session_id is None or "<Stream" not in twiml:
            return
        if (old := self.current.get(call_sid)) is not None:
            old.stop()  # Twilio ends the running stream when the TwiML changes
        ws = FakeTwilioSocket()
        self.current[call_sid] = ws
        mode = "listen" if "<Start>" in twiml else "bidi"
        ws.start(f"MZ{self._n}", call_sid, session_id=session_id, mode=mode)
        self.sockets.append(ws)
        self.tasks.append(asyncio.ensure_future(media_ws.handle(ws, self.registry)))
