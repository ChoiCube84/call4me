"""`WS /media`: Twilio Media Streams handler (bidirectional).

Twilio connects here when a call's TwiML runs `<Connect><Stream>` (or
`<Start><Stream>` for the listen-only fork used while the user is bridged in a
conference). We read the session id from the stream's custom parameters, then:

- inbound `media` (8 kHz mu-law, 20 ms / 160-byte frames) -> the session's
  `audio_in()` queue in `streams.registry`;
- `mark` echoes -> resolve `mark()` waiters;
- `stop` / socket close -> detach (the session stays open if a reconnect is
  expected, e.g. after `press_digits()`).

Outbound audio, `clear`, and `mark` messages are sent by the registry on the
socket attached here. Nothing is recorded or logged except counts.

`backend/main.py` routes `WS /media` to `handle()`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from backend.telephony.streams import Leg, StreamRegistry, registry

log = logging.getLogger("call4me.telephony")


async def handle(websocket: WebSocket, reg: StreamRegistry | None = None) -> None:
    reg = reg or registry
    await websocket.accept()
    leg: Leg | None = None
    stream_sid: str | None = None
    try:
        while True:
            try:
                msg = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            if event == "media" and leg is not None:
                media = msg.get("media") or {}
                if media.get("track", "inbound") not in ("inbound", "inbound_track"):
                    continue
                try:
                    frame = base64.b64decode(media.get("payload", ""), validate=True)
                except (binascii.Error, ValueError):
                    continue
                reg.push_inbound(leg, frame)
            elif event == "start":
                start = msg.get("start") or {}
                params = start.get("customParameters") or {}
                stream_sid = start.get("streamSid") or msg.get("streamSid")
                call_sid = start.get("callSid", "")
                session_id = params.get("session_id") or f"inbound-{call_sid}"
                leg = reg.attach(
                    session_id,
                    websocket,
                    stream_sid=stream_sid or "",
                    call_sid=call_sid,
                    bidirectional=params.get("mode", "bidi") != "listen",
                )
            elif event == "mark" and leg is not None:
                reg.mark_played(leg, (msg.get("mark") or {}).get("name", ""))
            elif event == "dtmf":
                # Digits pressed on the far end; not used (and never logged).
                continue
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if leg is not None:
            log.info(
                "stream %s ended for %s (in=%d out=%d frames)",
                stream_sid,
                leg.session_id,
                leg.frames_in,
                leg.frames_out,
            )
            reg.detach(leg.session_id, stream_sid)
