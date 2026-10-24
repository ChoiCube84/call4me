"""Fake customer service line, as a Twilio TwiML webhook app.

Run standalone::

    uv run uvicorn fakeline.app:app --port 8001

then point the Twilio number in TWILIO_FAKELINE_NUMBER (Voice -> "A call comes
in", HTTP POST) at ``<public url>/voice``. Or mount it inside the backend::

    app.mount("/fakeline", fakeline.app.app)   # webhook: <public url>/fakeline/voice

All action URLs are emitted as host-relative paths that include the mount
prefix, so it works either way. The script lives in `fakeline/script.py`.

Privacy: the line never logs Digits or SpeechResult (they may hold a PIN the
agent relays), and never records.
"""

from __future__ import annotations

import math
import random
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response

from backend.telephony.twiml import REP_VOICE, Verbs
from backend.telephony.twiml import Response as TwiML
from fakeline import script, sounds
from fakeline.config import get_fakeline_settings

app = FastAPI(title="call4me fake support line")

HOLD_CHUNK_S = 20  # music between announcements
REP_MAX_WAITS = 8  # x 8 s gather timeout: how long the rep waits for an ask_user answer
_rng = random.Random(get_fakeline_settings().seed)


def _xml(t: TwiML) -> Response:
    return Response(content=t.to_xml(), media_type="application/xml")


def _url(request: Request, path: str, **query: object) -> str:
    base = request.scope.get("root_path", "") + path
    return f"{base}?{urlencode(query)}" if query else base


async def _form(request: Request) -> dict[str, str]:
    try:
        return {k: str(v) for k, v in (await request.form()).items()}
    except Exception:  # noqa: BLE001 - GET or empty body
        return {}


@app.api_route("/voice", methods=["GET", "POST"])
async def voice(request: Request) -> Response:
    return _xml(TwiML().say(script.WELCOME).redirect(_url(request, "/node/main")))


@app.api_route("/node/{name}", methods=["GET", "POST"])
async def node(request: Request, name: str, tries: int = 0) -> Response:
    if name == "queue":
        return _xml(_enter_queue(request))
    item = script.NODES.get(name)
    if item is None:
        return _xml(TwiML().redirect(_url(request, "/node/main")))
    if isinstance(item, script.Message):
        return _xml(TwiML().say(item.text).redirect(_url(request, f"/node/{item.next}")))
    if tries >= script.MAX_MENU_TRIES:
        return _xml(TwiML().say(script.NO_INPUT_GOODBYE).hangup())
    return _xml(
        TwiML()
        .gather(
            Verbs().say(item.prompt),
            input="dtmf",
            numDigits=1,
            timeout=script.MENU_TIMEOUT_S,
            action=_url(request, f"/choose/{name}", tries=tries),
            method="POST",
        )
        .redirect(_url(request, f"/node/{name}", tries=tries + 1))
    )


@app.post("/choose/{name}")
async def choose(request: Request, name: str, tries: int = 0) -> Response:
    digit = (await _form(request)).get("Digits", "")[:1]
    item = script.NODES.get(name)
    if isinstance(item, script.Menu) and digit in item.options:
        return _xml(TwiML().redirect(_url(request, f"/node/{item.options[digit]}")))
    return _xml(
        TwiML().say(script.INVALID_OPTION).redirect(_url(request, f"/node/{name}", tries=tries + 1))
    )


def _enter_queue(request: Request) -> TwiML:
    cfg = get_fakeline_settings()
    hold = _rng.randint(cfg.hold_min_s, max(cfg.hold_min_s, cfg.hold_max_s))
    offer = 1 if _rng.random() < cfg.callback_probability else 0
    until = int(time.time()) + hold
    return TwiML().say(script.QUEUE_INTRO).redirect(_url(request, "/hold", until=until, cb=offer))


def _music_url(request: Request) -> str:
    return get_fakeline_settings().hold_music_url or _url(request, "/hold.wav")


@app.api_route("/hold", methods=["GET", "POST"])
async def hold(request: Request, until: int, cb: int = 0, i: int = 0) -> Response:
    remaining = until - time.time()
    if remaining <= 0:
        return _xml(_to_rep(request))
    t = TwiML()
    chunk = min(remaining, HOLD_CHUNK_S)
    t.play(_music_url(request), loop=max(1, math.ceil(chunk / sounds.HOLD_LOOP_SECONDS)))
    if cb and i == 1:
        t.gather(
            Verbs().say(script.CALLBACK_OFFER),
            input="dtmf",
            numDigits=1,
            timeout=5,
            action=_url(request, "/callback", until=until, i=i + 1),
            method="POST",
        )
    elif remaining > chunk:
        t.say(script.HOLD_ANNOUNCEMENTS[i % len(script.HOLD_ANNOUNCEMENTS)])
    return _xml(t.redirect(_url(request, "/hold", until=until, cb=cb, i=i + 1)))


@app.post("/callback")
async def callback(request: Request, until: int, i: int = 0) -> Response:
    if (await _form(request)).get("Digits", "")[:1] != "1":
        return _xml(TwiML().redirect(_url(request, "/hold", until=until, cb=0, i=i)))
    return _xml(
        TwiML()
        .gather(
            Verbs().say(script.CALLBACK_ASK_NUMBER),
            input="dtmf",
            finishOnKey="#",
            numDigits=15,
            timeout=15,
            action=_url(request, "/callback/number"),
            method="POST",
        )
        .redirect(_url(request, "/hold", until=until, cb=0, i=i))
    )


@app.post("/callback/number")
async def callback_number(request: Request) -> Response:
    # The number is deliberately not logged or used: this line only simulates the offer.
    return _xml(TwiML().say(script.CALLBACK_CONFIRM).hangup())


def _to_rep(request: Request) -> TwiML:
    cfg = get_fakeline_settings()
    t = TwiML().say(script.CONNECTING)
    if cfg.rep_number:
        return t.dial(cfg.rep_number)
    if cfg.rep_url:
        return t.redirect(cfg.rep_url)
    return t.redirect(_url(request, "/rep", turn=0))


def _rep_gather(request: Request, turn: int, heard: int, say: str | None) -> TwiML:
    return TwiML().gather(
        Verbs().say(say, voice=REP_VOICE) if say else None,
        input="speech",
        speechTimeout="auto",
        timeout=8,
        action=_url(request, "/rep/reply", turn=turn, heard=heard),
        method="POST",
    )


@app.api_route("/rep", methods=["GET", "POST"])
async def rep(request: Request, turn: int = 0, nudged: int = 0) -> Response:
    turn = min(turn, len(script.REP_SCRIPT) - 1)
    line = script.REP_SCRIPT[turn]
    if line.hangup_after:
        return _xml(TwiML().say(line.text, voice=REP_VOICE).hangup())
    if nudged >= 2:
        return _xml(TwiML().say("I'm going to disconnect now. Goodbye.", voice=REP_VOICE).hangup())
    text = line.text if not nudged else script.REP_NUDGE
    return _xml(
        _rep_gather(request, turn, 0, text).redirect(
            _url(request, "/rep", turn=turn, nudged=nudged + 1)
        )
    )


@app.post("/rep/reply")
async def rep_reply(request: Request, turn: int = 0, heard: int = 0) -> Response:
    # SpeechResult may contain the user's PIN: inspect it, never log or store it.
    speech = (await _form(request)).get("SpeechResult", "")
    heard += 1
    line = script.REP_SCRIPT[min(turn, len(script.REP_SCRIPT) - 1)]
    if heard < line.expects_turns and script.is_holding_line(speech):
        return _xml(
            _rep_gather(request, turn, heard, script.REP_TAKE_YOUR_TIME).redirect(
                _url(request, "/rep/wait", turn=turn, heard=heard, w=1)
            )
        )
    return _xml(TwiML().redirect(_url(request, "/rep", turn=turn + 1)))


@app.api_route("/rep/wait", methods=["GET", "POST"])
async def rep_wait(request: Request, turn: int = 0, heard: int = 0, w: int = 0) -> Response:
    """Rep waits quietly while the agent fetches an answer from the user (~60 s max)."""
    if w >= REP_MAX_WAITS:
        return _xml(TwiML().redirect(_url(request, "/rep", turn=turn, nudged=1)))
    return _xml(
        _rep_gather(request, turn, heard, None).redirect(
            _url(request, "/rep/wait", turn=turn, heard=heard, w=w + 1)
        )
    )


@app.get("/hold.wav")
async def hold_wav() -> Response:
    return Response(content=sounds.hold_music_wav(), media_type="audio/wav")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
