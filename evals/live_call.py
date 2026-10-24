"""Run one full call through the real backend pipeline against the mock fake line.

Unlike `evals.run` (a fast simulation scored over many scenarios), this goes
through everything production uses: `POST /calls`, `backend.calls.LiveCall`,
the audio pipeline, the call engine, the conversation agent, the guard, TTS,
and `POST /requests/{id}/answer`, with every external service mocked. It prints
the live feed the app and dashboard would see, and answers `ask_user` like a
user would.

    uv run python -m evals.live_call                 # real time (~1-2 min)
    uv run python -m evals.live_call --hold 5 --pin 4321
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


async def run_live_call(
    *,
    hold_s: float = 5.0,
    pin: str = "4321",
    speed: float = 1.0,
    timeout_s: float = 240.0,
    verbose: bool = True,
    db_url: str | None = None,
    takeover_on_ask: bool = False,
) -> dict[str, Any]:
    """Returns the final session, live messages, and audit rows for checks."""
    from httpx import ASGITransport, AsyncClient

    from backend import calls
    from backend.db import session as db_session
    from backend.main import app
    from backend.notify.live import hub
    from backend.telephony import set_telephony
    from backend.telephony.mock_client import MockTelephonyClient

    db_session.configure(db_url or f"sqlite+aiosqlite:///{Path(tempfile.mkdtemp()) / 'live.db'}")
    await db_session.init_db()
    set_telephony(
        MockTelephonyClient(speed=speed, hold_seconds=(hold_s, hold_s), callback_offer=False)
    )
    app.state.start_calls = True

    messages: list[Any] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://live") as client:
        r = await client.post(
            "/calls",
            json={
                "company": "Fake Support Line (test)",
                "phone_number": "+15550100000",
                "goal": "Remove the $40 late fee",
                "limits": {"notes": "no plan changes"},
                "disclosure_text": "Hi, I'm an AI assistant calling on behalf of Sam Lee.",
            },
        )
        r.raise_for_status()
        call_id = r.json()["id"]
        q = hub.subscribe(call_id)

        async def feed() -> None:
            while True:
                msg = await q.get()
                messages.append(msg)
                if verbose:
                    _print(msg)
                if msg.type == "ask_user":
                    await asyncio.sleep(1.0)
                    if takeover_on_ask:
                        # The user takes the call, types the PIN to the rep, and hands back.
                        (await client.post(f"/calls/{call_id}/takeover")).raise_for_status()
                        (
                            await client.post(f"/calls/{call_id}/type", json={"text": pin})
                        ).raise_for_status()
                        await asyncio.sleep(2.0)
                        (await client.post(f"/calls/{call_id}/handback")).raise_for_status()
                    else:
                        # The user reads the push and types the answer on the lock screen.
                        await client.post(
                            f"/requests/{msg.data['request_id']}/answer", json={"answer": pin}
                        )
                # The agent's end_call sends "ended" first; the wrap-up adds minutes saved.
                if msg.type == "ended" and "minutes_saved" in msg.data:
                    return

        try:
            await asyncio.wait_for(feed(), timeout_s)
        except TimeoutError:
            if verbose:
                print("!! timed out waiting for the call to end", file=sys.stderr)
        finally:
            hub.unsubscribe(call_id, q)
            live = calls.get_call(call_id)
            if live is not None:
                await asyncio.wait_for(live.finished.wait(), 10)
        detail = (await client.get(f"/calls/{call_id}")).json()
        audit_rows = (await client.get(f"/calls/{call_id}/audit")).json()
    set_telephony(None)
    return {"call": detail, "messages": messages, "audit": audit_rows}


def _print(msg: Any) -> None:
    d = msg.data
    if msg.type == "caption":
        if d.get("final", True):
            print(f"  [{d['speaker']:>6}] {d['text']}")
    elif msg.type == "state":
        print(f"== {d.get('from')} -> {d.get('to')}  ({d.get('reason')})")
    elif msg.type == "event":
        kind = d.get("kind")
        if kind in ("jev_decision", "call_status", "caption"):
            return
        extra = {k: v for k, v in d.items() if k not in ("kind", "options")}
        print(f"   * {kind}: {extra}")
    elif msg.type == "ask_user":
        print(f"!! ask_user: {d.get('question')}")
    elif msg.type in ("summary", "ended"):
        print(f"## {msg.type}: {d}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--hold", type=float, default=5.0, help="seconds of hold music")
    p.add_argument("--pin", default="4321", help="what the 'user' types for the PIN")
    p.add_argument("--speed", type=float, default=1.0, help="line speed (1 = real time)")
    args = p.parse_args()
    os.environ["USE_MOCKS"] = "true"
    result = asyncio.run(run_live_call(hold_s=args.hold, pin=args.pin, speed=args.speed))
    s = result["call"]["session"]
    print(f"\nfinal state={s['state']} outcome={s['outcome']!r} ref={s['reference_number']}")
    print(f"minutes saved={s['minutes_saved']}  audit rows={len(result['audit'])}")
    denials = [a for a in result["audit"] if not a["allowed"]]
    print(f"guard/state denials logged: {len(denials)}")


if __name__ == "__main__":
    main()
