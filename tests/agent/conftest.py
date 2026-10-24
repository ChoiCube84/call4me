from __future__ import annotations

import pytest

from backend.agent.ports import AgentRuntime, memory_ports
from backend.contracts import CallLimits, CallSession, CallState


def make_session(**overrides) -> CallSession:
    data = {
        "user_id": "u1",
        "company": "Fake Support Line",
        "phone_number": "+15550000000",
        "goal": "Remove the $40 late fee; no plan changes",
        "limits": CallLimits(notes="no plan changes"),
        "disclosure_text": "Hi, I'm an AI assistant calling on behalf of Sam.",
        "state": CallState.IN_CONVERSATION,
    }
    data.update(overrides)
    return CallSession(**data)


@pytest.fixture
def session() -> CallSession:
    return make_session()


@pytest.fixture
def runtime(session: CallSession) -> AgentRuntime:
    return AgentRuntime(session=session)


@pytest.fixture
def ports():
    return memory_ports()
