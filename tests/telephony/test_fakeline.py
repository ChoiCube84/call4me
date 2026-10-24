"""Fake support line: TwiML flow through menus, hold, callback offer and rep."""

from __future__ import annotations

import re
import time
import wave
from collections.abc import AsyncIterator
from io import BytesIO
from xml.etree import ElementTree as ET

import pytest
from httpx import ASGITransport, AsyncClient

from fakeline import app as fakeline_app
from fakeline import script
from fakeline.config import get_fakeline_settings


@pytest.fixture
async def line(monkeypatch) -> AsyncIterator[AsyncClient]:
    for k in ("REP_NUMBER", "REP_URL", "HOLD_MUSIC_URL"):
        monkeypatch.delenv(f"FAKELINE_{k}", raising=False)
    get_fakeline_settings.cache_clear()
    async with AsyncClient(
        transport=ASGITransport(app=fakeline_app.app), base_url="http://line"
    ) as c:
        yield c
    get_fakeline_settings.cache_clear()


async def twiml(c: AsyncClient, url: str, **form: str) -> ET.Element:
    r = await c.post(url, data=form)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    return ET.fromstring(r.text)


def redirect_of(root: ET.Element) -> str:
    el = root.find("Redirect")
    assert el is not None, ET.tostring(root)
    return el.text or ""


def said(root: ET.Element) -> str:
    return " ".join(e.text or "" for e in root.iter("Say"))


async def test_welcome_then_main_menu(line) -> None:
    root = await twiml(line, "/voice")
    assert "Thank you for calling" in said(root)
    menu = await twiml(line, redirect_of(root))
    gather = menu.find("Gather")
    assert gather is not None and gather.get("numDigits") == "1"
    assert "press 1" in said(menu) and "press 0" in said(menu)


@pytest.mark.parametrize(
    ("path", "digit", "expected"),
    [
        ("/choose/main", "1", "/node/orders"),
        ("/choose/main", "2", "/node/billing"),
        ("/choose/main", "0", "/node/queue"),
        ("/choose/billing", "1", "/node/balance"),
        ("/choose/billing", "2", "/node/queue"),
        ("/choose/billing", "9", "/node/main"),
    ],
)
async def test_menu_routing(line, path: str, digit: str, expected: str) -> None:
    root = await twiml(line, path, Digits=digit)
    assert redirect_of(root) == expected


async def test_nested_billing_menu_and_recorded_message(line) -> None:
    billing = await twiml(line, "/node/billing")
    assert "Billing" in said(billing)
    balance = await twiml(line, "/node/balance")
    assert redirect_of(balance) == "/node/billing"  # recorded message returns to menu


async def test_invalid_option_and_no_input_loop_ends(line) -> None:
    root = await twiml(line, "/choose/main", Digits="7")
    assert script.INVALID_OPTION in said(root)
    assert redirect_of(root) == "/node/main?tries=1"
    root = await twiml(line, f"/node/main?tries={script.MAX_MENU_TRIES}")
    assert root.find("Hangup") is not None


async def test_queue_hold_duration_in_range(line) -> None:
    root = await twiml(line, "/node/queue")
    m = re.search(r"until=(\d+)", redirect_of(root))
    assert m
    wait = int(m.group(1)) - time.time()
    assert 25 <= wait <= 121


async def test_hold_plays_music_then_connects_to_rep(line) -> None:
    root = await twiml(line, f"/hold?until={int(time.time()) + 60}&cb=0&i=0")
    play = root.find("Play")
    assert play is not None and play.text == "/hold.wav" and play.get("loop") == "2"
    assert "/hold?" in redirect_of(root)

    done = await twiml(line, f"/hold?until={int(time.time()) - 1}")
    assert script.CONNECTING in said(done)
    assert redirect_of(done) == "/rep?turn=0"


async def test_callback_offer(line) -> None:
    until = int(time.time()) + 60
    root = await twiml(line, f"/hold?until={until}&cb=1&i=1")
    assert script.CALLBACK_OFFER in said(root)
    # Declining returns to hold; accepting asks for a number then hangs up.
    assert "/hold?" in redirect_of(await twiml(line, f"/callback?until={until}&i=2", Digits="2"))
    ask = await twiml(line, f"/callback?until={until}&i=2", Digits="1")
    assert ask.find("Gather").get("finishOnKey") == "#"
    bye = await twiml(line, "/callback/number", Digits="4155550100")
    assert bye.find("Hangup") is not None and "4155550100" not in ET.tostring(bye).decode()


async def test_forward_to_configured_rep_number(line, monkeypatch) -> None:
    monkeypatch.setenv("FAKELINE_REP_NUMBER", "+14155550123")
    get_fakeline_settings.cache_clear()
    root = await twiml(line, f"/hold?until={int(time.time()) - 1}")
    assert root.find("Dial").text == "+14155550123"


async def test_builtin_rep_waits_for_ask_user_answer(line) -> None:
    pin_turn = next(i for i, r in enumerate(script.REP_SCRIPT) if r.expects_turns == 2)
    root = await twiml(line, f"/rep?turn={pin_turn}")
    assert "PIN" in said(root) and root.find("Gather").get("input") == "speech"

    # Agent's holding line -> rep waits instead of moving on.
    wait = await twiml(
        line, f"/rep/reply?turn={pin_turn}&heard=0", SpeechResult="One moment, let me get that."
    )
    assert script.REP_TAKE_YOUR_TIME in said(wait)
    assert "/rep/reply?turn=" in wait.find("Gather").get("action")
    # Quiet wait while the user types.
    quiet = await twiml(line, f"/rep/wait?turn={pin_turn}&heard=1&w=1")
    assert said(quiet) == ""

    # The relayed answer advances the script.
    nxt = await twiml(line, f"/rep/reply?turn={pin_turn}&heard=1", SpeechResult="It's 4 8 2 1.")
    assert redirect_of(nxt) == f"/rep?turn={pin_turn + 1}"


async def test_rep_last_line_hangs_up(line) -> None:
    root = await twiml(line, f"/rep?turn={len(script.REP_SCRIPT) - 1}")
    assert root.find("Hangup") is not None


async def test_mounted_prefix_is_kept_in_urls() -> None:
    from fastapi import FastAPI

    outer = FastAPI()
    outer.mount("/fakeline", fakeline_app.app)
    async with AsyncClient(transport=ASGITransport(app=outer), base_url="http://x") as c:
        root = await twiml(c, "/fakeline/voice")
        assert redirect_of(root) == "/fakeline/node/main"


async def test_hold_music_is_valid_wav(line) -> None:
    r = await line.get("/hold.wav")
    with wave.open(BytesIO(r.content)) as w:
        assert w.getframerate() == 8000 and w.getnframes() == 80000
