"""ask_user flow: holding line, WAITING_ON_USER, question-only request, timeout,
speak once then redact, high sensitivity -> takeover."""

from __future__ import annotations

import asyncio

from backend.agent.ports import AgentRuntime
from backend.agent.tools import HOLDING_LINE, STILL_WAITING_LINE, ToolExecutor
from backend.contracts import CallState, UserRequestStatus

PIN = "4821"


def executor(runtime: AgentRuntime, ports, timeout: float = 1.0, grace: float = 1.0):
    runtime.session.disclosure_given = True
    return ToolExecutor(runtime, ports, answer_timeout_s=timeout, answer_grace_s=grace)


async def answer_when_asked(ports, answer: str, delay: float = 0.0) -> None:
    for _ in range(500):
        if ports.answers.waiting:
            await asyncio.sleep(delay)
            (req_id,) = list(ports.answers.waiting)
            assert ports.answers.deliver(req_id, answer)
            return
        await asyncio.sleep(0.002)
    raise AssertionError("agent never waited for an answer")


def ask_input(**kw):
    data = {
        "question": "What is your account PIN?",
        "sensitivity": "medium",
        "relay_template": "Sure, the PIN is {answer}.",
        "relay_as": "speech",
    }
    data.update(kw)
    return data


async def test_full_flow_speaks_once_and_never_stores_answer(runtime, ports) -> None:
    ex = executor(runtime, ports)
    runtime.remote_history.append("Can I get your PIN to verify the account?")
    outcome, _ = await asyncio.gather(
        ex.execute("ask_user", ask_input()), answer_when_asked(ports, PIN)
    )

    control = ports.control
    assert control.spoken == [HOLDING_LINE, f"Sure, the PIN is {PIN}."]  # spoken exactly once
    states = [s for s, _ in control.transitions]
    assert states == [CallState.WAITING_ON_USER, CallState.IN_CONVERSATION]
    assert runtime.session.state == CallState.IN_CONVERSATION

    # Claude never sees the answer.
    assert not outcome.is_error and PIN not in outcome.content

    # The stored request holds the question only.
    (req,) = ports.requests.requests.values()
    assert req.question == "What is your account PIN?"
    assert req.status == UserRequestStatus.ANSWERED
    assert PIN not in req.model_dump_json()

    # Nothing persisted or published contains it.
    assert PIN not in str(ports.audit.entries)
    assert PIN not in str([m.model_dump() for m in ports.user.live])
    assert PIN not in str(runtime.spoken)
    assert runtime.one_shot_relay is None

    # The push carried only the question.
    (pushed,) = ports.user.asked
    assert pushed.question == req.question

    # Saying it a second time is blocked.
    again = await ex.execute("say", {"text": f"Again, it is {PIN}."})
    assert again.is_error and control.spoken[-1] != f"Again, it is {PIN}."


async def test_timeout_asks_rep_to_hold_then_times_out(runtime, ports) -> None:
    ex = executor(runtime, ports, timeout=0.02, grace=0.02)
    outcome = await ex.execute("ask_user", ask_input())
    assert outcome.is_error and "did not answer" in outcome.content
    assert ports.control.spoken == [HOLDING_LINE, STILL_WAITING_LINE]
    assert any(t == "Still waiting" for t, _ in ports.user.alerts)
    (req,) = ports.requests.requests.values()
    assert req.status == UserRequestStatus.TIMED_OUT
    assert runtime.session.state == CallState.IN_CONVERSATION
    assert not ports.answers.waiting  # released


async def test_answer_during_grace_period_is_relayed(runtime, ports) -> None:
    ex = executor(runtime, ports, timeout=0.02, grace=1.0)
    outcome, _ = await asyncio.gather(
        ex.execute("ask_user", ask_input()), answer_when_asked(ports, PIN, delay=0.05)
    )
    assert not outcome.is_error
    assert ports.control.spoken == [HOLDING_LINE, STILL_WAITING_LINE, f"Sure, the PIN is {PIN}."]


async def test_high_sensitivity_never_asked_suggests_takeover(runtime, ports) -> None:
    ex = executor(runtime, ports)
    outcome = await ex.execute(
        "ask_user", ask_input(question="What is your full card number?", sensitivity="low")
    )
    assert outcome.is_error and outcome.stop
    assert not ports.user.asked  # no push asking to relay it
    assert ports.user.alerts and ports.user.alerts[0][0] == "Please take over"
    (req,) = ports.requests.requests.values()
    assert req.status == UserRequestStatus.TAKEOVER_SUGGESTED
    assert CallState.WAITING_ON_USER not in [s for s, _ in ports.control.transitions]
    assert any(e["allowed"] is False for e in ports.audit.entries)


async def test_ssn_answer_to_medium_question_not_relayed(runtime, ports) -> None:
    ex = executor(runtime, ports)
    outcome, _ = await asyncio.gather(
        ex.execute("ask_user", ask_input(question="What number is on the account?")),
        answer_when_asked(ports, "123-45-6789"),
    )
    assert outcome.is_error and outcome.stop
    assert all("123-45-6789" not in s for s in ports.control.spoken)
    assert "123-45-6789" not in str(ports.audit.entries)


async def test_takeover_during_wait_cancels_request(runtime, ports) -> None:
    ex = executor(runtime, ports, timeout=5.0)

    async def take_over() -> None:
        for _ in range(500):
            if ports.answers.waiting:
                break
            await asyncio.sleep(0.002)
        runtime.takeover.set()

    outcome, _ = await asyncio.gather(ex.execute("ask_user", ask_input()), take_over())
    assert outcome.stop
    (req,) = ports.requests.requests.values()
    assert req.status == UserRequestStatus.CANCELLED
    assert ports.control.spoken == [HOLDING_LINE]


async def test_keypad_relay(runtime, ports) -> None:
    ex = executor(runtime, ports)
    outcome, _ = await asyncio.gather(
        ex.execute("ask_user", ask_input(relay_as="keypad")), answer_when_asked(ports, PIN)
    )
    assert not outcome.is_error
    assert ports.control.digits == [PIN]
    assert ports.control.spoken == [HOLDING_LINE]


async def test_bad_template_rejected_before_asking(runtime, ports) -> None:
    ex = executor(runtime, ports)
    outcome = await ex.execute("ask_user", ask_input(relay_template="It is 1234 {answer}"))
    assert outcome.is_error
    assert not ports.user.asked and not ports.control.spoken


async def test_template_missing_placeholder_rejected(runtime, ports) -> None:
    ex = executor(runtime, ports)
    outcome = await ex.execute("ask_user", ask_input(relay_template="Here you go."))
    assert outcome.is_error and "{answer}" in outcome.content
