"""Scripted rep on the fake phone: a full call over real services, no humans.

Plays the person holding `/phone`: answers when it rings, speaks the rep's lines
(Cartesia TTS, streamed as 8 kHz PCM like the page's mic), waits for the agent
to finish each reply, and answers the user's ask_user prompt like the dashboard.

    TELEPHONY=phone uv run uvicorn backend.main:app --port 8000   # in one shell
    uv run python -m evals.phone_rep                              # in another
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import httpx
import websockets

from backend.audio.codec import mulaw_to_pcm16
from backend.speech.tts import make_tts_client
from backend.telephony import mulaw

REP_LINES = [
    "Thanks for calling Amazon, this is Alex. How can I help you today?",
    "I'm sorry to hear that. I can help. First, can I get the order number, please?",
    "Thank you. And to verify the account, what's the ZIP code on file?",
    "Thanks, you're verified. While I have you, would you like to try Prime? It's free for "
    "thirty days, then fourteen ninety nine a month.",
    "No problem. I've issued a full refund of forty nine ninety nine to your original payment "
    "method. Your reference number is A 7 X 2 9 1. Is there anything else I can help with?",
    "Thanks for calling Amazon. Have a great day. Goodbye.",
]
AGENT_DONE_QUIET_S = 2.0  # agent audio finished playing this long ago = their turn is over


async def run(base: str, answer: str, go_quiet_after: int | None = None) -> int:
    ws_base = base.replace("http", "ws", 1)
    tts = make_tts_client()
    rendered = []
    for line in REP_LINES:  # render up front so the rep answers promptly
        pcm = b"".join([mulaw_to_pcm16(f) async for f in tts.synthesize(line)])
        rendered.append(pcm)

    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        r = await http.post(
            "/calls",
            json={
                "company": "Amazon Customer Service",
                "phone_number": "+15550100100",
                "goal": "Get a refund for wireless headphones that arrived damaged.",
                "limits": {"notes": "No subscriptions or new charges."},
                # Given before the call: shared without asking. The ZIP code is not,
                # so the agent asks the user for it (ask_user).
                "known_info": [{"label": "order number", "value": "112-4567890-1234567"}],
                "disclosure_text": "Hi, I'm an AI assistant calling on behalf of my client, "
                "who is Deaf. They are following this call by text.",
            },
        )
        r.raise_for_status()
        call_id = r.json()["id"]
        print(f"call {call_id}")
        t0 = time.monotonic()

        def stamp(msg: str) -> None:
            print(f"{time.monotonic() - t0:6.1f}s  {msg}", flush=True)

        async def watch_live() -> None:
            async with websockets.connect(f"{ws_base}/calls/{call_id}/live") as live:
                async for raw in live:
                    m: dict[str, Any] = json.loads(raw)
                    kind, data = m.get("type"), m.get("data", {})
                    if kind == "caption":
                        stamp(f"[{data.get('speaker')}] {data.get('text')}")
                    elif kind == "state":
                        stamp(f"== {data.get('from')} -> {data.get('to')}  ({data.get('reason')})")
                    elif kind == "ask_user":
                        stamp(f"!! ask_user: {data.get('question')}  -> answering")
                        await asyncio.sleep(2)
                        await http.post(
                            f"/requests/{data['request_id']}/answer", json={"answer": answer}
                        )
                    elif kind == "ended" and "minutes_saved" in data:
                        stamp(f"## ended: {data}")
                        return

        live_task = asyncio.create_task(watch_live())

        async with websockets.connect(f"{ws_base}/phone/ws", max_size=None) as phone:
            while json.loads(await phone.recv()).get("type") != "ringing":
                pass
            stamp("phone ringing -> answer")
            await phone.send(json.dumps({"type": "answer"}))

            speech = bytearray()
            agent = {"last_frame": 0.0, "plays_until": 0.0, "spoke": False}
            ended = asyncio.Event()

            async def mic() -> None:  # continuous 20 ms frames, like a real mic
                frame = mulaw.FRAME_BYTES * 2
                nxt = time.monotonic()
                while not ended.is_set():
                    chunk = bytes(speech[:frame]) or b"\0" * frame
                    del speech[:frame]
                    await phone.send(chunk.ljust(frame, b"\0"))
                    nxt += 0.02
                    await asyncio.sleep(max(0, nxt - time.monotonic()))

            async def speaker() -> None:
                async for raw in phone:
                    if isinstance(raw, bytes):
                        now = time.monotonic()
                        agent["plays_until"] = max(agent["plays_until"], now) + len(raw) / 2 / 8000
                        agent["last_frame"], agent["spoke"] = now, True
                    elif json.loads(raw).get("type") == "ended":
                        stamp("phone: call ended")
                        ended.set()
                        return

            tasks = [asyncio.create_task(mic()), asyncio.create_task(speaker())]
            for i, pcm in enumerate(rendered):
                if ended.is_set():
                    break
                if i == go_quiet_after:
                    stamp("rep goes quiet (stays on the line)")
                    await asyncio.wait_for(ended.wait(), 120)
                    break
                agent["spoke"] = False
                speech.extend(pcm)
                while speech and not ended.is_set():  # noqa: ASYNC110 - mic task drains it
                    await asyncio.sleep(0.05)
                if i == len(rendered) - 1:
                    await asyncio.sleep(1.0)
                    await phone.send(json.dumps({"type": "hangup"}))
                    break
                # Wait for the agent's reply to finish playing (may include an ask_user pause).
                deadline = time.monotonic() + 90
                while not ended.is_set() and time.monotonic() < deadline:
                    quiet = time.monotonic() - agent["plays_until"]
                    if agent["spoke"] and quiet > AGENT_DONE_QUIET_S:
                        break
                    await asyncio.sleep(0.05)
            await asyncio.wait_for(ended.wait(), 15)
            for t in tasks:
                t.cancel()
        try:
            await asyncio.wait_for(live_task, 60)
        except TimeoutError:
            stamp("!! no wrap-up within 60 s")
            return 1
        call = (await http.get(f"/calls/{call_id}")).json()["session"]
        print(f"\noutcome={call['outcome']!r} ref={call['reference_number']!r}")
        return 0 if call["reference_number"] or go_quiet_after is not None else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--answer", default="98109", help="what the 'user' types when asked (ZIP)")
    p.add_argument(
        "--go-quiet-after",
        type=int,
        help="stop talking after this many rep lines, without hanging up (tests 'nobody there')",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.base, args.answer, args.go_quiet_after)))


if __name__ == "__main__":
    main()
