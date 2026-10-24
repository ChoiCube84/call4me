"""Twilio Media Stream WebSocket handler — owned by the `telephony` agent.

Phase 0 stub: accepts the stream and drains frames so Twilio stays connected.
Replace `handle()` with the real handler; `backend/main.py` routes `WS /media` here.
"""

from __future__ import annotations

from fastapi import WebSocket, WebSocketDisconnect


async def handle(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
