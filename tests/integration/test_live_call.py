"""A full call through the real pipeline (backend.calls) on the mock fake line.

Runs at 3x line speed (~25 s each). This is the end-to-end check that every
area is wired together: dialing, menu, hold, human detection + greeting,
Claude (mock) conversation, ask_user, guard, takeover, and wrap-up.
"""

from __future__ import annotations

import json

import pytest

from backend.config import get_settings
from evals.live_call import run_live_call

PIN = "8642"


@pytest.fixture
def mocks(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_MOCKS", "true")
    get_settings.cache_clear()
    yield f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    get_settings.cache_clear()


def _dump(result: dict) -> str:
    return (
        json.dumps(result["call"])
        + json.dumps(result["audit"])
        + "".join(m.model_dump_json() for m in result["messages"])
    )


async def test_full_call_with_ask_user(mocks, caplog) -> None:
    caplog.set_level("DEBUG")
    result = await run_live_call(hold_s=2, pin=PIN, speed=3, verbose=False, db_url=mocks)
    session = result["call"]["session"]
    assert session["state"] == "ENDED"
    assert session["reference_number"] == "A7X291"
    assert session["minutes_saved"] is not None

    states = [m.data["to"] for m in result["messages"] if m.type == "state"]
    for expected in ("MENU", "HUMAN_SUSPECTED", "IN_CONVERSATION", "WAITING_ON_USER", "ENDED"):
        assert expected in states

    agent_lines = [
        m.data["text"]
        for m in result["messages"]
        if m.type == "caption" and m.data["speaker"] == "agent"
    ]
    assert any("[REDACTED]" in line for line in agent_lines)  # relayed once, shown redacted
    assert any("No thank you" in line for line in agent_lines)  # upsell declined

    # Rule 2: the PIN is nowhere — API, audit log, live channel, logs.
    assert PIN not in _dump(result)
    assert PIN not in caplog.text
    assert any(a["action"] == "user_request.answered" for a in result["audit"])


async def test_takeover_and_type_to_talk(mocks) -> None:
    result = await run_live_call(
        hold_s=2, pin=PIN, speed=3, verbose=False, db_url=mocks, takeover_on_ask=True
    )
    msgs = result["messages"]
    states = [m.data["to"] for m in msgs if m.type == "state"]
    assert "TAKEOVER" in states
    i = states.index("TAKEOVER")
    assert "IN_CONVERSATION" in states[i:]  # handed back

    # Rule 6: no agent speech between takeover and handback.
    seq = [m for m in msgs if m.type in ("state", "caption")]
    start = next(n for n, m in enumerate(seq) if m.type == "state" and m.data["to"] == "TAKEOVER")
    end = next(
        n
        for n, m in enumerate(seq)
        if n > start and m.type == "state" and m.data["from"] == "TAKEOVER"
    )
    during = [m for m in seq[start:end] if m.type == "caption"]
    assert all(m.data["speaker"] != "agent" for m in during)
    assert any(m.data["speaker"] == "user" for m in during)
    assert result["call"]["session"]["state"] == "ENDED"
