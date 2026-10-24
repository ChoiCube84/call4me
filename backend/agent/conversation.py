"""Claude tool-use loop for IN_CONVERSATION (CLAUDE.md section 7).

Each final rep transcript -> `ConversationAgent.on_rep_turn()` -> Claude ->
tool calls -> guard -> action. The engine owns *when* this runs; the agent
owns *what* is said.

Privacy: rep text is redacted before it enters Claude's context (the rep may
read a PIN back), and ask_user answers never enter it at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.audit import log as audit_log
from backend.contracts import CallSession, CallState, LiveMessage, SayAction

from . import guard
from .llm import LLMClient, LLMResponse, get_llm_client
from .ports import (
    AgentPorts,
    AgentRuntime,
    BrokerAnswerSource,
    DbAuditSink,
    DbRequestStore,
    NotifierUserChannel,
)
from .prompts import GREETING_INSTRUCTION, build_system_prompt
from .tools import TOOL_DEFINITIONS, ToolExecutor

log = logging.getLogger(__name__)

# Not "Sure, ...": an affirmative filler right after an upsell would sound like agreement.
FILLER_LINE = "One moment, please."
FALLBACK_PURPOSE = "I'm calling for help with their account."
REPEAT_LINE = "Sorry, could you say that again?"
MAX_TOOL_ROUNDS = 6
MAX_CONSECUTIVE_FAILURES = 3


class ConversationAgent:
    def __init__(
        self,
        session: CallSession,
        ports: AgentPorts,
        llm: LLMClient | None = None,
        *,
        filler_after_s: float = 1.2,
        answer_timeout_s: float | None = None,
        answer_grace_s: float | None = None,
    ) -> None:
        self.runtime = AgentRuntime(session=session)
        self.ports = ports
        self.llm = llm or get_llm_client()
        kwargs: dict[str, float] = {}
        if answer_timeout_s is not None:
            kwargs["answer_timeout_s"] = answer_timeout_s
        if answer_grace_s is not None:
            kwargs["answer_grace_s"] = answer_grace_s
        self.tools = ToolExecutor(self.runtime, ports, **kwargs)
        self.system_prompt = build_system_prompt(session)
        self.messages: list[dict[str, Any]] = []
        self.filler_after_s = filler_after_s
        self._lock = asyncio.Lock()
        self._failures = 0

    @property
    def session(self) -> CallSession:
        return self.runtime.session

    # ------------------------------------------------------------------ #
    # Events from the engine / API
    # ------------------------------------------------------------------ #

    async def on_rep_turn(self, text: str) -> None:
        """Handle one final transcript from the rep. Serialized per call."""
        redacted = audit_log.redact(text.strip())
        if not redacted:
            return
        async with self._lock:
            self.runtime.remote_history.append(redacted)
            if self.runtime.taken_over or self.runtime.ended:
                return  # the user is typing; the agent stays silent
            self._append_user_text(f"Rep: {redacted}")
            await self._run_turn()

    async def takeover(self) -> None:
        """User took over: stop agent speech immediately (rule 6)."""
        self.runtime.takeover.set()
        await self.ports.control.stop_speaking(self.session.id)
        if self.session.state != CallState.TAKEOVER:
            await self.ports.control.transition(self.session, CallState.TAKEOVER, "user took over")
        await self.ports.audit(
            actor="user",
            action="agent.takeover",
            subject=self.session.id,
            reason="agent stopped; user is typing",
        )

    async def handback(self) -> None:
        self.runtime.takeover.clear()
        if self.session.state == CallState.TAKEOVER:
            await self.ports.control.transition(
                self.session, CallState.IN_CONVERSATION, "user handed back"
            )
        self._append_user_text(
            "[System] The user handed the call back to you. Continue toward the goal."
        )

    def on_user_typed(self, text: str) -> None:
        """Type-to-talk text the user sent during takeover. Kept out of Claude's context,
        since the user may have typed private details; only that it happened is noted."""
        self.runtime.remote_history.append("[the user typed to the rep directly]")

    def close(self) -> None:
        """Call is over: drop in-memory secrets for this session."""
        audit_log.forget_session(self.session.id)

    # ------------------------------------------------------------------ #
    # Claude loop
    # ------------------------------------------------------------------ #

    def _append_user_text(self, text: str) -> None:
        block = {"type": "text", "text": text}
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    async def _run_turn(self) -> None:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._call_claude()
            if resp is None:
                return
            self.messages.append({"role": "assistant", "content": resp.content})
            if not resp.tool_calls:
                return
            results: list[dict[str, Any]] = []
            stop = False
            for call in resp.tool_calls:
                if stop:  # keep the history valid: every tool_use needs a result
                    results.append(_result(call.id, "skipped: the call moved on", True))
                    continue
                outcome = await self.tools.execute(call.name, call.input)
                results.append(_result(call.id, outcome.content, outcome.is_error))
                stop = stop or outcome.stop
            self.messages.append({"role": "user", "content": results})
            if stop or resp.stop_reason != "tool_use":
                return
        log.warning("tool round limit reached for %s", self.session.id)

    async def _call_claude(self) -> LLMResponse | None:
        task = asyncio.ensure_future(
            self.llm.respond(self.system_prompt, self.messages, TOOL_DEFINITIONS)
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=self.filler_after_s)
            if not done:
                await self._filler()
            resp = await task
        except Exception as e:  # noqa: BLE001 - any LLM failure must not kill the call
            self._failures += 1
            log.warning("Claude call failed (%s) for %s", type(e).__name__, self.session.id)
            await self.ports.audit(
                actor="agent",
                action="llm.error",
                subject=self.session.id,
                allowed=False,
                reason=type(e).__name__,
            )
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                await self.tools.request_takeover("The assistant is having trouble right now.")
            else:
                await self.tools.say(REPEAT_LINE)
            # History still ends with a user turn, so the next rep turn just appends to it.
            return None
        self._failures = 0
        if resp.stop_reason == "refusal":
            await self.tools.request_takeover("The assistant could not continue this request.")
            return None
        return resp

    async def _filler(self) -> None:
        """Claude is slow: cover the gap with a short line (section 16)."""
        if self.session.disclosure_given and not self.runtime.taken_over:
            await self.tools.say(FILLER_LINE)


def _result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


# --------------------------------------------------------------------------- #
# Greeting (pre-generated before a human answers; section 6)
# --------------------------------------------------------------------------- #


async def build_greeting(session: CallSession, llm: LLMClient | None = None) -> str:
    """Disclosure + one-line purpose. Guard-checked; falls back to a fixed line."""
    purpose = ""
    if llm is not None:
        try:
            purpose = await llm.complete(
                build_system_prompt(session), GREETING_INSTRUCTION, max_tokens=512
            )
        except Exception:  # noqa: BLE001
            purpose = ""
    if not purpose:
        purpose = FALLBACK_PURPOSE
    disclosure = guard.effective_disclosure(session)
    candidate = f"{disclosure} {purpose.strip()}"
    if not guard.check(SayAction(text=candidate), session).allowed:
        candidate = f"{disclosure} {FALLBACK_PURPOSE}"
    return candidate


# --------------------------------------------------------------------------- #
# Registry so the API / engine can find the agent for a call
# --------------------------------------------------------------------------- #

_agents: dict[str, ConversationAgent] = {}


def default_ports(control: Any) -> AgentPorts:
    """Production wiring. `control` comes from the engine (speech, DTMF, transition)."""
    return AgentPorts(
        control=control,
        requests=DbRequestStore(),
        audit=DbAuditSink(),
        user=NotifierUserChannel(),
        answers=BrokerAnswerSource(),
    )


def start_agent(
    session: CallSession, ports: AgentPorts, llm: LLMClient | None = None
) -> ConversationAgent:
    agent = ConversationAgent(session, ports, llm)
    _agents[session.id] = agent
    return agent


def get_agent(session_id: str) -> ConversationAgent | None:
    return _agents.get(session_id)


def stop_agent(session_id: str) -> None:
    agent = _agents.pop(session_id, None)
    if agent is not None:
        agent.close()


def publish_summary(ports: AgentPorts, session: CallSession, text: str) -> None:
    ports.user.publish(
        LiveMessage(type="summary", session_id=session.id, data={"text": audit_log.redact(text)})
    )
