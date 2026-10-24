"""Twilio client, /media handler, status callbacks, signatures, and callbacks route."""

from __future__ import annotations

import asyncio
import base64
from xml.etree import ElementTree as ET

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import Settings
from backend.contracts import CallSession
from backend.telephony import media_ws, mulaw, signature
from backend.telephony.streams import StreamRegistry
from backend.telephony.twilio_client import (
    TwilioTelephonyClient,
    handle_status_form,
    media_url,
)
from backend.telephony.twilio_rest import TwilioRest
from tests.telephony.fakes import FakeTwilioApi, FakeTwilioSocket

SETTINGS = Settings(
    _env_file=None,
    twilio_account_sid="AC" + "0" * 32,
    twilio_auth_token="token",
    twilio_agent_number="+15550001111",
    public_base_url="https://abc.ngrok.app",
)


def session(**kw) -> CallSession:
    return CallSession(
        user_id="u",
        company="Fake",
        phone_number="+15550002222",
        goal="remove fee",
        disclosure_text="I'm an AI assistant.",
        **kw,
    )


@pytest.fixture
def reg() -> StreamRegistry:
    return StreamRegistry()


@pytest.fixture
def api(reg) -> FakeTwilioApi:
    return FakeTwilioApi(reg)


@pytest.fixture
def client(reg, api) -> TwilioTelephonyClient:
    rest = TwilioRest(
        SETTINGS.twilio_account_sid, SETTINGS.twilio_auth_token, transport=api.transport()
    )
    return TwilioTelephonyClient(SETTINGS, rest=rest, streams=reg)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def frame(n: int) -> bytes:
    return bytes([n % 256]) * mulaw.FRAME_BYTES


# --- media stream handler -----------------------------------------------------


async def test_media_stream_in_and_out(reg) -> None:
    ws = FakeTwilioSocket()
    task = asyncio.create_task(media_ws.handle(ws, reg))
    ws.start("MZ1", "CA1", session_id="s1")
    for i in range(3):
        ws.media(frame(i))
    await settle()
    assert ws.accepted and reg.session_for_call("CA1") == "s1"

    got = []
    async for f in reg.audio_in("s1"):
        got.append(f)
        if len(got) == 3:
            break
    assert got == [frame(0), frame(1), frame(2)]

    assert await reg.send_audio("s1", frame(9))
    await reg.clear("s1")
    assert ws.sent[0]["event"] == "media" and ws.sent[0]["streamSid"] == "MZ1"
    assert base64.b64decode(ws.sent[0]["media"]["payload"]) == frame(9)
    assert ws.sent[1] == {"event": "clear", "streamSid": "MZ1"}

    # mark resolves when Twilio echoes it back
    waiter = asyncio.create_task(reg.mark("s1", "greeting", timeout=1))
    await settle()
    assert ws.sent[-1]["mark"] == {"name": "greeting"}
    ws.event({"event": "mark", "streamSid": "MZ1", "mark": {"name": "greeting"}})
    assert await waiter is True

    ws.stop()
    await task
    assert [f async for f in reg.audio_in("s1")] == []  # call over -> iterator ends


async def test_unexpected_stop_with_pending_reconnect_keeps_session(reg) -> None:
    ws = FakeTwilioSocket()
    task = asyncio.create_task(media_ws.handle(ws, reg))
    ws.start("MZ1", "CA1", session_id="s1")
    await settle()
    reg.leg("s1").reconnect_pending = True
    ws.stop()
    await task
    assert not reg.leg("s1").closed
    assert not await reg.send_audio("s1", frame(1))  # nothing to send on, dropped


async def test_listen_only_stream_never_sends(reg) -> None:
    ws = FakeTwilioSocket()
    task = asyncio.create_task(media_ws.handle(ws, reg))
    ws.start("MZ1", "CA1", session_id="s1", mode="listen")
    await settle()
    assert not await reg.send_audio("s1", frame(1))
    assert ws.sent == []
    ws.disconnect()
    await task


# --- Twilio client ------------------------------------------------------------


def test_media_url() -> None:
    assert media_url("https://abc.ngrok.app/") == "wss://abc.ngrok.app/media"
    assert media_url("http://localhost:8000") == "ws://localhost:8000/media"


async def test_place_call_opens_stream_with_session_param(client, api, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    path, form = api.requests[0]
    assert path.endswith(f"/Accounts/{SETTINGS.twilio_account_sid}/Calls.json")
    assert form["To"] == ["+15550002222"] and form["From"] == ["+15550001111"]
    assert form["StatusCallback"] == ["https://abc.ngrok.app/twilio/status"]
    assert set(form["StatusCallbackEvent"]) >= {"answered", "completed"}
    stream = ET.fromstring(form["Twiml"][0]).find("Connect/Stream")
    assert stream.get("url") == "wss://abc.ngrok.app/media"
    assert stream.find("Parameter").attrib == {"name": "session_id", "value": s.id}
    assert reg.session_for_call(sid) == s.id

    api.connect(sid, s.id)
    assert await client.wait_connected(s.id, timeout=1)


async def test_press_digits_plays_then_reconnects(client, api, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    first = api.connect(sid, s.id)
    await client.wait_connected(s.id, timeout=1)

    await asyncio.wait_for(client.press_digits(s.id, "2w1"), 2)
    path, form = api.requests[-1]
    assert path.endswith(f"/Calls/{sid}.json")
    root = ET.fromstring(form["Twiml"][0])
    assert root[0].tag == "Play" and root[0].get("digits") == "2w1"
    assert root[1].tag == "Connect"

    # Old stream stopped, new one carries audio for the same session.
    await settle()
    assert reg.leg(s.id).stream_sid != "MZinit1" and not reg.leg(s.id).closed
    api.current[sid].media(frame(7))
    first.media(frame(1))  # old socket is gone; ignored
    async for f in client.audio_in(s.id):
        assert f == frame(7)
        break

    with pytest.raises(ValueError):
        await client.press_digits(s.id, "1; DROP")


async def test_takeover_type_to_talk_keeps_stream(client, api, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    ws = api.connect(sid, s.id)
    await client.wait_connected(s.id, timeout=1)
    n = len(api.requests)

    await client.start_takeover(s.id)
    assert client.is_taken_over(s.id)
    assert ws.sent[-1]["event"] == "clear"
    assert len(api.requests) == n  # no conference without a bridge number
    await client.end_takeover(s.id)
    assert not client.is_taken_over(s.id)


async def test_takeover_with_bridge_uses_conference(client, api, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    api.connect(sid, s.id)
    await client.wait_connected(s.id, timeout=1)
    client.set_takeover_bridge(s.id, "+15550003333")

    await asyncio.wait_for(client.start_takeover(s.id), 2)
    _, company = api.requests[-2]
    root = ET.fromstring(company["Twiml"][0])
    assert root.find("Start/Stream/Parameter[@name='mode']").get("value") == "listen"
    assert root.find("Dial/Conference").text == f"call4me-{s.id}"
    path, user = api.requests[-1]
    assert path.endswith("/Calls.json") and user["To"] == ["+15550003333"]
    assert ET.fromstring(user["Twiml"][0]).find("Dial/Conference").text == f"call4me-{s.id}"
    assert not reg.leg(s.id).bidirectional  # listen-only while bridged

    await asyncio.wait_for(client.end_takeover(s.id), 2)
    _, back = api.requests[-2]
    root = ET.fromstring(back["Twiml"][0])
    assert root.find("Stop/Stream").get("name") == f"listen-{s.id}"
    assert root.find("Connect/Stream") is not None
    assert api.requests[-1][1]["Status"] == ["completed"]  # user's leg hung up
    await settle()
    assert reg.leg(s.id).bidirectional and not client.is_taken_over(s.id)


async def test_hang_up_and_status_callbacks(client, api, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    updates = []

    async def collect() -> None:
        async for u in client.status_updates(s.id):
            updates.append(u.status)

    task = asyncio.create_task(collect())
    assert handle_status_form({"CallSid": sid, "CallStatus": "ringing"}, reg) is not None
    assert handle_status_form({"CallSid": "CAother", "CallStatus": "ringing"}, reg) is None
    await client.hang_up(s.id)
    assert api.requests[-1][1]["Status"] == ["completed"]
    await asyncio.wait_for(task, 1)
    assert updates == ["ringing"]
    assert [f async for f in client.audio_in(s.id)] == []


async def test_completed_status_ends_audio(client, reg) -> None:
    s = session()
    sid = await client.place_call(s)
    update = handle_status_form({"CallSid": sid, "CallStatus": "busy", "CallDuration": "0"}, reg)
    assert update.is_terminal and update.duration_s == 0
    assert [f async for f in client.audio_in(s.id)] == []


# --- signatures and the callback route -------------------------------------------


def test_signature_round_trip() -> None:
    params = {"CallSid": "CA1", "From": "+1555", "Digits": "1"}
    url = "https://abc.ngrok.app/twilio/voice?x=1"
    sig = signature.compute_signature("token", url, params)
    assert signature.is_valid("token", url, params, sig)
    assert not signature.is_valid("token", url, {**params, "Digits": "2"}, sig)
    assert not signature.is_valid("other", url, params, sig)


def test_signature_matches_twilio_docs_example() -> None:
    # https://www.twilio.com/docs/usage/security (classic example)
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert signature.compute_signature("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


async def test_incoming_call_matches_expected_callback(monkeypatch) -> None:
    from fastapi import FastAPI

    from backend.telephony import routes, streams

    reg = StreamRegistry()
    monkeypatch.setattr(streams, "registry", reg)
    monkeypatch.setattr(routes, "registry", reg)
    monkeypatch.setenv("USE_MOCKS", "true")  # skip signature check
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://abc.ngrok.app")
    from backend.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(routes.router)
    reg.expect_callback("sess-1")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
            r = await c.post("/twilio/voice", data={"CallSid": "CA9", "From": "+15550004444"})
            r2 = await c.post("/twilio/voice", data={"CallSid": "CA10", "From": "+1555"})
    finally:
        get_settings.cache_clear()
    p = ET.fromstring(r.text).find("Connect/Stream/Parameter[@name='session_id']")
    assert p.get("value") == "sess-1"
    first = reg.incoming.get_nowait()
    assert first.matched_callback and first.session_id == "sess-1"
    second = reg.incoming.get_nowait()
    assert not second.matched_callback and second.session_id == "inbound-CA10"
    assert reg.session_for_call("CA9") == "sess-1"
    assert "inbound-CA10" in r2.text
