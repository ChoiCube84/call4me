"""Claude tool-use loop against the mock LLM: a full fake-line conversation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.agent.conversation import FILLER_LINE, REPEAT_LINE, ConversationAgent, build_greeting
from backend.agent.helpers import parse_menu_options
from backend.agent.llm import LLMClient, LLMResponse, MockLLMClient, ToolCall
from backend.contracts import CallState

from .test_ask_user import answer_when_asked

PIN = "4821"
SPOKEN = " ".join(PIN)  # read to the rep digit by digit


def agent_for(session, ports, llm: LLMClient | None = None, **kw) -> ConversationAgent:
    return ConversationAgent(session, ports, llm or MockLLMClient(), **kw)


def everything_claude_saw(llm: MockLLMClient) -> str:
    return json.dumps([c["messages"] for c in llm.calls], default=str)


async def test_full_mock_call_with_ask_user(session, ports) -> None:
    llm = MockLLMClient()
    agent = agent_for(session, ports, llm)

    await agent.on_rep_turn("Thanks for calling, how can I help you?")
    assert ports.control.spoken[0].startswith("Hi, I'm an AI assistant")  # disclosure first

    await asyncio.gather(
        agent.on_rep_turn("Sure. Can I get the PIN on the account?"),
        answer_when_asked(ports, PIN),
    )
    assert ports.control.spoken.count(f"Sure, the pin is {SPOKEN}.") == 1

    # The rep reads the PIN back: it is redacted before it reaches Claude.
    await agent.on_rep_turn(f"Thanks, I have {PIN}. Would you like to upgrade to premium?")
    assert "No thank you" in ports.control.spoken[-1]

    await agent.on_rep_turn("Okay. I've waived the fee. Your reference number is A7X291.")
    assert "A7X291" in ports.control.spoken[-1]
    await agent.on_rep_turn("Yes, that's correct.")
    assert session.state != CallState.ENDED  # wraps up first; never hangs up on the rep
    assert "Thank you" in ports.control.spoken[-1]
    await agent.on_rep_turn("You're welcome. Have a good one.")

    assert session.state == CallState.ENDED
    assert ports.control.hung_up
    assert session.reference_number == "A7X291"
    assert PIN not in everything_claude_saw(llm)
    assert PIN not in agent.system_prompt
    assert PIN not in str(ports.audit.entries)
    assert any(m.type == "ended" for m in ports.user.live)


class Scripted(LLMClient):
    """Returns given tool calls on the first request of each turn, then ends the turn."""

    def __init__(self, turns: list[list[ToolCall]], delay: float = 0.0) -> None:
        self.turns = turns
        self.delay = delay
        self.seen: list[list[dict[str, Any]]] = []

    async def respond(self, system, messages, tools) -> LLMResponse:
        await asyncio.sleep(self.delay)
        self.seen.append([m.copy() for m in messages])
        last = messages[-1]["content"]
        if any(b.get("type") == "tool_result" for b in last):
            return LLMResponse(content=[{"type": "text", "text": "ok"}])
        calls = self.turns.pop(0)
        return LLMResponse(
            content=[
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
            ],
            tool_calls=calls,
            stop_reason="tool_use",
        )

    async def complete(self, system, prompt, max_tokens=1024) -> str:
        return "I'm calling to remove a $40 late fee."


def tool_results(llm: Scripted) -> list[dict[str, Any]]:
    out = []
    for m in llm.seen[-1]:  # each snapshot is the full history; the last one has everything
        if m["role"] == "user":
            out.extend(b for b in m["content"] if b.get("type") == "tool_result")
    return out


async def test_guard_denial_is_returned_to_claude(session, ports) -> None:
    session.disclosure_given = True
    llm = Scripted([[ToolCall("t1", "say", {"text": "Yes, sign me up for premium!"})], []])
    agent = agent_for(session, ports, llm)
    await agent.on_rep_turn("Want to upgrade to premium for $15 a month?")
    await agent.on_rep_turn("Hello?")
    (result,) = tool_results(llm)
    assert result["is_error"] and "outside the user's goal" in result["content"]
    assert ports.control.spoken == []
    assert any(e["allowed"] is False and e["action"] == "guard.say" for e in ports.audit.entries)


async def test_takeover_stops_agent_speech(session, ports) -> None:
    session.disclosure_given = True
    llm = Scripted([[ToolCall("t1", "say", {"text": "Hello there."})]])
    agent = agent_for(session, ports, llm)
    await agent.takeover()
    assert ports.control.stopped == 1
    assert session.state == CallState.TAKEOVER
    await agent.on_rep_turn("Are you still there?")
    assert ports.control.spoken == [] and llm.seen == []

    await agent.handback()
    assert session.state == CallState.IN_CONVERSATION
    await agent.on_rep_turn("Hi again.")
    assert ports.control.spoken == ["Hello there."]


async def test_slow_claude_gets_filler_line(session, ports) -> None:
    session.disclosure_given = True
    llm = Scripted([[ToolCall("t1", "say", {"text": "Thanks."})]], delay=0.05)
    agent = agent_for(session, ports, llm, filler_after_s=0.01)
    await agent.on_rep_turn("Let me check on that.")
    assert ports.control.spoken[0] == FILLER_LINE


async def test_llm_failure_keeps_call_alive(session, ports) -> None:
    session.disclosure_given = True

    class Broken(Scripted):
        async def respond(self, system, messages, tools):
            raise RuntimeError("boom")

    agent = agent_for(session, ports, Broken([]))
    await agent.on_rep_turn("Hello?")
    assert ports.control.spoken == [REPEAT_LINE]
    assert any(e["action"] == "llm.error" for e in ports.audit.entries)


async def test_every_tool_use_gets_a_result_even_after_stop(session, ports) -> None:
    session.disclosure_given = True
    llm = Scripted(
        [
            [
                ToolCall("t1", "request_takeover", {"reason": "rep refuses AI"}),
                ToolCall("t2", "say", {"text": "More words."}),
            ]
        ]
    )
    agent = agent_for(session, ports, llm)
    await agent.on_rep_turn("I don't talk to robots.")
    results = agent.messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[1]["is_error"]


async def test_greeting_includes_disclosure_and_is_guarded(session) -> None:
    greeting = await build_greeting(session, Scripted([]))
    assert greeting.startswith("Hi, I'm an AI assistant calling on behalf of Sam.")
    assert "$40 late fee" in greeting
    assert (await build_greeting(session)).startswith("Hi, I'm an AI assistant")


def test_parse_menu_options() -> None:
    opts = parse_menu_options(
        "For orders, press 1. Press 2 for billing. To speak to an agent, press 0, "
        "or say representative."
    )
    assert [(o.key, o.label) for o in opts][:3] == [
        ("1", "orders"),
        ("2", "billing"),
        ("0", "speak to an agent"),
    ]
    assert any(o.spoken and o.key == "representative" for o in opts)


async def test_stale_say_dropped_when_rep_spoke_mid_turn() -> None:
    from backend.agent.ports import AgentRuntime, memory_ports
    from backend.agent.tools import ToolExecutor
    from backend.contracts import CallLimits, CallSession, CallState

    session = CallSession(
        user_id="u",
        company="Acme",
        phone_number="+1555",
        goal="Remove the $40 late fee",
        limits=CallLimits(),
        disclosure_text="Hi, I'm an AI assistant calling on behalf of Sam.",
        state=CallState.IN_CONVERSATION,
        disclosure_given=True,
    )
    ports = memory_ports()
    rt = AgentRuntime(session=session)
    tools = ToolExecutor(rt, ports)
    rt.unseen_rep = True
    out = await tools.say("Is that something you can waive today?")
    assert out.is_error and ports.control.spoken == []
    rt.unseen_rep = False
    assert not (await tools.say("Thank you.")).is_error


ORDER = "112-4567890-1234567"


async def test_info_given_before_the_call_is_shared_without_asking(session, ports) -> None:
    from backend.audit import log as audit

    audit.register_secret(session.id, ORDER)  # LiveCall does this at start
    llm = MockLLMClient()
    agent = agent_for(session, ports, llm, known_info={"order number": ORDER})
    assert "order number" in agent.system_prompt and ORDER not in agent.system_prompt

    await agent.on_rep_turn("Thanks for calling, how can I help you?")
    await agent.on_rep_turn("Sure. Can I get the order number, please?")

    # A fixed sentence, digits read one by one in their groups.
    assert ports.control.spoken[-1] == "The order number is 1 1 2, 4 5 6 7 8 9 0, 1 2 3 4 5 6 7."
    assert not any(m.type == "ask_user" for m in ports.user.live)  # no need to ask
    assert ORDER not in everything_claude_saw(llm)
    assert ORDER not in str(ports.audit.entries)
    assert ORDER not in json.dumps([m.data for m in ports.user.live])  # captions redacted


async def test_share_info_with_unknown_label_says_to_ask(session, ports) -> None:
    agent = agent_for(session, ports, known_info={"order number": ORDER})
    out = await agent.tools.execute("share_info", {"label": "date of birth"})
    assert out.is_error and "ask_user" in out.content
    assert ports.control.spoken == []


def test_numbers_are_read_digit_by_digit_in_groups() -> None:
    from backend.agent.tools import for_speech

    assert for_speech("112-4567890-1234567") == "1 1 2, 4 5 6 7 8 9 0, 1 2 3 4 5 6 7"
    assert for_speech("98109") == "9 8 1 0 9"
    assert for_speech("A7X291") == "A 7 X 2 9 1"
    assert for_speech("Jorge Ramos") == "Jorge Ramos"  # words stay words
    assert for_speech("sam@example.com") == "sam@example.com"


def test_markup_never_reaches_speech() -> None:
    from backend.agent.tools import clean_speech

    leaked = 'The order number is {answer}.</parameter> <parameter name="label">'
    assert clean_speech(leaked) == "The order number is ."
    assert clean_speech("Thanks, that's all.") == "Thanks, that's all."
