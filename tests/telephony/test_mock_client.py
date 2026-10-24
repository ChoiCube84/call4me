"""Mock telephony twin: a full simulated fake-line call, driven like call-engine would."""

from __future__ import annotations

import asyncio

import pytest

from backend.contracts import CallSession, StateCheckLabel
from backend.telephony import get_telephony, mulaw, set_telephony
from backend.telephony.base import CallControl
from backend.telephony.mock_client import MockSegment, MockTelephonyClient
from fakeline import script

SPEECH = [b"\x10" * mulaw.FRAME_BYTES] * 25  # half a second of "agent speech"


def session() -> CallSession:
    return CallSession(
        user_id="u", company="Fake", phone_number="+1555", goal="fee", disclosure_text="AI."
    )


def mock(**kw) -> MockTelephonyClient:
    kw.setdefault("hold_seconds", (1.0, 2.0))
    kw.setdefault("callback_offer", False)
    return MockTelephonyClient(speed=0, connect_delay_s=0, hold_chunk_s=1.0, seed=1, **kw)


async def consume(tel: MockTelephonyClient, sid: str) -> int:
    n = 0
    async for f in tel.audio_in(sid):
        assert len(f) == mulaw.FRAME_BYTES
        n += 1
    return n


async def speak(tel: MockTelephonyClient, sid: str) -> None:
    for f in SPEECH:
        await tel.send_audio(sid, f)


async def run_call(tel: MockTelephonyClient, react) -> tuple[list[MockSegment], int]:
    s = session()
    await tel.place_call(s)
    assert await tel.wait_connected(s.id, timeout=1)
    audio = asyncio.create_task(consume(tel, s.id))
    seen: list[MockSegment] = []
    async with asyncio.timeout(20):
        async for seg in tel.remote_segments(s.id):
            seen.append(seg)
            await react(tel, s.id, seg)
        frames = await audio
    return seen, frames


async def test_full_call_billing_path_to_rep_with_ask_user() -> None:
    tel = mock()
    pressed = {"menu:main": "2", "menu:billing": "2"}

    async def agent(tel, sid, seg: MockSegment) -> None:
        if seg.key in pressed:
            await tel.press_digits(sid, pressed.pop(seg.key))
        elif seg.label is StateCheckLabel.HUMAN and seg.key.startswith("rep:"):
            await speak(tel, sid)  # greeting / holding line / relayed answer

    seen, frames = await run_call(tel, agent)
    keys = [s.key for s in seen]
    assert keys[:3] == ["welcome", "menu:main", "menu:billing"]
    assert "hold:music" in keys and "queue:intro" in keys
    rep_keys = [k for k in keys if k.startswith("rep:")]
    assert rep_keys == ["rep:0", "rep:1", "rep:1:wait", "rep:2", "rep:3", "rep:4"]
    assert seen[-1].text == script.REP_SCRIPT[-1].text
    assert frames > 0
    line = next(iter(tel.lines.values()))
    assert line.digits_pressed == ["2", "2"] and line.ended.is_set()


async def test_orders_message_and_nested_menu_labels() -> None:
    tel = mock()
    pressed = {"menu:main": "1"}

    async def agent(tel, sid, seg) -> None:
        if seg.key in pressed:
            await tel.press_digits(sid, pressed.pop(seg.key))
        if seg.key == "rep:0":
            await tel.hang_up(sid)

    seen, _ = await run_call(tel, agent)
    labels = {s.key: s.label for s in seen}
    assert labels["message:orders"] is StateCheckLabel.RECORDED_MESSAGE
    assert labels["hold:music"] is StateCheckLabel.HOLD_MUSIC
    assert labels["rep:0"] is StateCheckLabel.HUMAN


async def test_menu_loop_without_input_hangs_up() -> None:
    async def silent(tel, sid, seg) -> None:
        pass

    seen, _ = await run_call(mock(), silent)
    assert [s.key for s in seen].count("menu:main") == script.MAX_MENU_TRIES
    assert seen[-1].key == "goodbye"


async def test_callback_offer_accepted() -> None:
    tel = mock(callback_offer=True, hold_seconds=(5.0, 5.0))

    async def agent(tel, sid, seg) -> None:
        if seg.key == "menu:main":
            await tel.press_digits(sid, "0")
        elif seg.key == "callback:offer":
            await tel.press_digits(sid, "1")
        elif seg.key == "callback:ask":
            await tel.press_digits(sid, "4155550100#")

    seen, _ = await run_call(tel, agent)
    keys = [s.key for s in seen]
    assert keys[-3:] == ["callback:offer", "callback:ask", "callback:confirm"]
    assert seen[-3].label is StateCheckLabel.CALLBACK_OFFER


async def test_rep_nudges_when_agent_silent_then_hangs_up() -> None:
    async def agent(tel, sid, seg) -> None:
        if seg.key == "menu:main":
            await tel.press_digits(sid, "0")

    seen, _ = await run_call(mock(), agent)
    assert [s.key for s in seen][-3:] == ["rep:0", "rep:0:nudge", "rep:0:nudge"]


async def test_mark_waits_for_playback_and_clear_cancels() -> None:
    tel = mock()
    s = session()
    await tel.place_call(s)
    await tel.wait_connected(s.id)
    reader = asyncio.create_task(consume(tel, s.id))

    await speak(tel, s.id)
    line = tel.lines[s.id]
    start = line.tick
    assert await tel.mark(s.id, "greeting", timeout=5)
    assert line.tick - start >= len(SPEECH)

    reader.cancel()  # stall the line so the next audio can't finish playing
    for _ in range(4):
        await speak(tel, s.id)
    waiter = asyncio.create_task(tel.mark(s.id, "x", timeout=5))
    await asyncio.sleep(0.02)
    await tel.start_takeover(s.id)  # clears queued audio
    assert await waiter is False and tel.is_taken_over(s.id)
    await tel.end_takeover(s.id)
    assert not tel.is_taken_over(s.id)

    statuses = []
    await tel.hang_up(s.id)
    async for u in tel.status_updates(s.id):
        statuses.append(u.status)
    assert statuses[-1] == "completed"


async def test_realtime_pacing() -> None:
    tel = MockTelephonyClient(speed=1.0, connect_delay_s=0)
    s = session()
    await tel.place_call(s)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    n = 0
    async for _ in tel.audio_in(s.id):
        n += 1
        if n == 25:
            break
    assert loop.time() - t0 >= 0.4  # 25 frames x 20 ms
    await tel.hang_up(s.id)


async def test_press_digits_validates() -> None:
    tel = mock()
    s = session()
    await tel.place_call(s)
    with pytest.raises(ValueError):
        await tel.press_digits(s.id, "12a")
    await tel.hang_up(s.id)


def test_factory_picks_mock(monkeypatch) -> None:
    from backend.config import get_settings

    monkeypatch.setenv("USE_MOCKS", "true")
    get_settings.cache_clear()
    set_telephony(None)
    try:
        tel = get_telephony()
        assert isinstance(tel, MockTelephonyClient) and isinstance(tel, CallControl)
    finally:
        set_telephony(None)
        get_settings.cache_clear()
