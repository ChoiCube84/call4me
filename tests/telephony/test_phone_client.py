"""Fake phone line (TELEPHONY=phone): the /phone page protocol and audio bridge."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from backend.contracts import CallSession
from backend.telephony import mulaw, set_telephony
from backend.telephony.phone_client import PhoneLine, PhoneLineClient, phone_ws


class FakeWS:
    """Just enough of starlette's WebSocket for phone_ws."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: asyncio.Queue[Any] = asyncio.Queue()

    async def accept(self) -> None: ...

    async def close(self) -> None: ...

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.sent.put(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.sent.put(data)

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    def page_sends(self, data: dict[str, Any] | bytes) -> None:
        if isinstance(data, bytes):
            self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})
        else:
            self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(data)})

    async def next(self, kind: str | None = None) -> Any:
        while True:
            msg = await asyncio.wait_for(self.sent.get(), 2)
            if kind is None or (isinstance(msg, dict) and msg.get("type") == kind):
                return msg


@pytest.fixture
def client() -> Any:
    c = PhoneLineClient()
    set_telephony(c)
    yield c
    set_telephony(None)


def ringing_line(client: PhoneLineClient) -> PhoneLine:
    session = CallSession(
        user_id="u", company="Acme", phone_number="+15555550100", goal="g", disclosure_text="d"
    )
    line = PhoneLine(
        session.id,
        speed=1.0,
        hold_seconds=(0, 0),
        callback_offer=False,
        rng=random.Random(0),
        hold_chunk_s=1.0,
    )
    line.company = session.company
    line.waiting_for_rep = True
    line.connected.set()
    client.lines[session.id] = line
    return line


async def test_rings_answers_and_bridges_audio(client: PhoneLineClient) -> None:
    line = ringing_line(client)
    ws = FakeWS()
    task = asyncio.create_task(phone_ws(ws))  # type: ignore[arg-type]

    ring = await ws.next("ringing")
    assert ring["company"] == "Acme"
    ws.page_sends({"type": "answer"})
    await ws.next("connected")
    assert line.answered

    # Agent -> page: one mu-law frame arrives as 16-bit PCM.
    line.agent_audio(mulaw.SILENCE_FRAME)
    assert len(await ws.next()) == mulaw.FRAME_BYTES * 2

    # Page -> agent: PCM in, whole mu-law frames out (a partial frame waits for more).
    ws.page_sends(b"\0\0" * 250)
    await asyncio.sleep(0.3)
    assert line.mic.qsize() == 1
    ws.page_sends(b"\0\0" * 70)
    await asyncio.sleep(0.3)
    assert line.mic.qsize() == 2

    ws.page_sends({"type": "hangup"})
    await asyncio.sleep(0.3)
    assert line.rep_hung_up

    line.finish()
    await ws.next("ended")
    ws.incoming.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, 3)


async def test_second_page_cannot_answer_a_taken_call(client: PhoneLineClient) -> None:
    line = ringing_line(client)
    assert line.answer()
    assert not line.answer()
    assert client.ringing() is None


async def test_mic_backlog_is_capped() -> None:
    line = PhoneLine(
        "s",
        speed=1.0,
        hold_seconds=(0, 0),
        callback_offer=False,
        rng=random.Random(0),
        hold_chunk_s=1.0,
    )
    line.rep_audio(b"\0\0" * mulaw.FRAME_BYTES * 50)  # 1 s at once
    assert line.mic.qsize() <= 10


async def test_page_told_when_server_is_not_in_phone_mode() -> None:
    set_telephony(None)
    from backend.telephony.mock_client import MockTelephonyClient

    set_telephony(MockTelephonyClient())
    try:
        ws = FakeWS()
        await phone_ws(ws)  # type: ignore[arg-type]
        assert (await ws.next())["type"] == "unavailable"
    finally:
        set_telephony(None)
