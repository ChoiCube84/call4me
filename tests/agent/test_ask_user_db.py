"""ask_user wired to the real pieces: POST /requests/{id}/answer, AnswerBroker, DB rows."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from backend.agent.ports import (
    AgentPorts,
    AgentRuntime,
    BrokerAnswerSource,
    DbAuditSink,
    DbRequestStore,
    FakeCallControl,
    MemoryUserChannel,
)
from backend.agent.tools import ToolExecutor
from backend.contracts import CallLimits, CallSession, CallState
from backend.db.models import AuditLogRow, UserRequestRow
from backend.db.session import sessionmaker

PIN = "4821"
SPOKEN = " ".join(PIN)  # read to the rep digit by digit


async def test_answer_endpoint_to_relay_never_persists_answer(client) -> None:
    resp = await client.post(
        "/calls",
        json={
            "company": "Fake Support Line",
            "phone_number": "+15550000000",
            "goal": "Remove the $40 late fee",
            "disclosure_text": "Hi, I'm an AI assistant calling on behalf of Sam.",
        },
    )
    session = CallSession(**{**resp.json(), "limits": CallLimits()})
    session.state = CallState.IN_CONVERSATION
    session.disclosure_given = True

    ports = AgentPorts(
        control=FakeCallControl(),
        requests=DbRequestStore(),
        audit=DbAuditSink(),
        user=MemoryUserChannel(),
        answers=BrokerAnswerSource(),
    )
    ex = ToolExecutor(AgentRuntime(session=session), ports, answer_timeout_s=5)

    async def user_answers() -> int:
        for _ in range(500):
            if ports.user.asked:
                break
            await asyncio.sleep(0.005)
        req_id = ports.user.asked[0].id
        r = await client.post(f"/requests/{req_id}/answer", json={"answer": PIN})
        return r.status_code

    outcome, status = await asyncio.gather(
        ex.execute(
            "ask_user",
            {
                "question": "What is your account PIN?",
                "sensitivity": "medium",
                "relay_template": "The PIN is {answer}.",
                "relay_as": "speech",
            },
        ),
        user_answers(),
    )
    assert status == 202 and not outcome.is_error
    assert ports.control.spoken[-1] == f"The PIN is {SPOKEN}."

    async with sessionmaker()() as db:
        reqs = (await db.scalars(select(UserRequestRow))).all()
        audits = (await db.scalars(select(AuditLogRow))).all()
    assert [r.status for r in reqs] == ["answered"]
    dumped = str([(a.action, a.reason, a.diff_json, a.evidence_json) for a in audits])
    assert PIN not in dumped
    assert "user_request.relayed" in dumped
    assert all(PIN not in r.question for r in reqs)
