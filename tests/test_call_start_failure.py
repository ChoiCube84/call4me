"""A voice service that refuses to start ends the call with a plain reason."""

from __future__ import annotations

from backend import calls
from backend.agent.llm import MockLLMClient
from backend.contracts import CallSession, CallState
from backend.engine.jev_checks import MockDecisionClient
from backend.notify.apns import MockNotifier
from backend.speech.stt import MockSTTClient
from backend.speech.tts import MockTTSClient
from backend.telephony.mock_client import MockTelephonyClient


class OutOfCreditsSTT(MockSTTClient):
    async def start(self, session_id: str) -> None:
        raise RuntimeError("server rejected WebSocket connection: HTTP 402 (quota_exceeded)")


async def test_stt_out_of_credits_fails_the_call_with_a_reason(db) -> None:
    session = CallSession(
        user_id="u",
        company="Amazon Customer Service",
        phone_number="+15550100100",
        goal="Refund",
        disclosure_text="Hi, I'm an AI assistant.",
    )
    tel = MockTelephonyClient(speed=0)
    clients = calls.Clients(
        telephony=tel,
        stt=OutOfCreditsSTT(),
        tts=MockTTSClient(),
        decisions=MockDecisionClient(),
        notifier=MockNotifier(),
        llm=MockLLMClient(),
    )
    call = await calls.start_call(session, clients, persist=False)
    assert session.state == CallState.FAILED
    assert call.engine.done
    assert session.id not in tel.lines  # never dialed


def test_reference_found_in_full_transcript_after_history_rolled_over() -> None:
    from collections import deque

    from backend.calls import _read_back_reference

    recent = deque([f"line {i}" for i in range(12)], maxlen=12)  # reference line pushed out
    transcript = ["remote: Your reference number is A7X291.", "user: [typed to the rep]"]
    assert _read_back_reference([], recent, transcript) == "A7X291"
