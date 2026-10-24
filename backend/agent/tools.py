"""The agent's tools: say, ask_user, press_digits, end_call, request_takeover.

`TOOL_DEFINITIONS` is what Claude sees. `ToolExecutor.execute()` validates the
input, runs it through the guard, performs it via the ports, and returns the
tool_result text for Claude.

The ask_user answer never reaches Claude: the executor speaks it once itself
(using Claude's `relay_template`), then drops it. Claude's tool_result only
says that the answer was relayed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from backend.audit import log as audit_log
from backend.contracts import (
    AgentAction,
    AskUserAction,
    CallState,
    EndCallAction,
    LiveMessage,
    PressDigitsAction,
    RequestTakeoverAction,
    SayAction,
    Sensitivity,
    UserRequest,
    UserRequestStatus,
    utcnow,
)

from . import guard
from .ports import AgentPorts, AgentRuntime

log = logging.getLogger(__name__)

HOLDING_LINE = "One moment, let me get that for you."
STILL_WAITING_LINE = (
    "Thanks for your patience, I'm still getting that information. Could you hold a "
    "little longer? If you'd rather, we can call back."
)
TAKEOVER_LINE = "One moment please. The customer will type their answer to you directly."
CLOSING_LINE = "Thank you for your help. Goodbye."
ANSWER_PLACEHOLDER = "{answer}"

ASK_USER_TIMEOUT_S = 60.0
ASK_USER_GRACE_S = 60.0


def _strict(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _strict(
        "say",
        "Speak to the representative. Keep it to one or two short, natural sentences.",
        {"text": {"type": "string", "description": "Exactly what to say out loud."}},
        ["text"],
    ),
    _strict(
        "ask_user",
        "Ask the user for information only they know (PIN, account number, a decision). "
        "The rep hears a holding line while the user types. You will NOT see the answer: "
        "the system speaks it to the rep once using relay_template, then forgets it. "
        "Never use this for a full SSN, full card number, or password; use "
        "request_takeover for those.",
        {
            "question": {
                "type": "string",
                "description": "Short, plain question shown on the user's phone.",
            },
            "sensitivity": {"type": "string", "enum": ["low", "medium", "high"]},
            "relay_template": {
                "type": "string",
                "description": "Sentence spoken to the rep with the answer in place of "
                "{answer}, e.g. 'Sure, the PIN is {answer}.'",
            },
            "relay_as": {
                "type": "string",
                "enum": ["speech", "keypad"],
                "description": "speech: say it to the rep. keypad: enter digits by DTMF.",
            },
        },
        ["question", "sensitivity", "relay_template", "relay_as"],
    ),
    _strict(
        "press_digits",
        "Send keypad tones (DTMF). Digits, * and # only; w is a half-second pause.",
        {"digits": {"type": "string"}},
        ["digits"],
    ),
    _strict(
        "end_call",
        "Finish the call after reading back key details with say. Hangs up.",
        {
            "summary": {
                "type": "string",
                "description": "Plain-language summary for the user: outcome and what was agreed.",
            },
            "reference_number": {
                "type": ["string", "null"],
                "description": "Reference/confirmation number the rep gave, or null.",
            },
        },
        ["summary", "reference_number"],
    ),
    _strict(
        "request_takeover",
        "Ask the user to take over and type to the rep directly (high-sensitivity info, "
        "rep refuses to talk to an AI, or you are stuck).",
        {"reason": {"type": "string", "description": "Short reason shown to the user."}},
        ["reason"],
    ),
]

_action_adapter: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    # Stop the Claude loop after this tool (call ended, or the user is taking over).
    stop: bool = False


class ToolExecutor:
    def __init__(
        self,
        runtime: AgentRuntime,
        ports: AgentPorts,
        *,
        answer_timeout_s: float = ASK_USER_TIMEOUT_S,
        answer_grace_s: float = ASK_USER_GRACE_S,
    ) -> None:
        self.rt = runtime
        self.ports = ports
        self.answer_timeout_s = answer_timeout_s
        self.answer_grace_s = answer_grace_s

    @property
    def session(self):  # noqa: ANN201 - CallSession
        return self.rt.session

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def execute(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        extra = {k: tool_input.get(k) for k in ("relay_template", "relay_as")}
        payload = {k: v for k, v in tool_input.items() if k not in extra}
        try:
            action = _action_adapter.validate_python({"kind": name, **payload})
        except ValidationError as e:
            return ToolOutcome(f"invalid input for {name}: {e.errors()[0]['msg']}", is_error=True)

        await self.ports.audit(
            actor="agent",
            action=f"tool.{name}",
            subject=self.session.id,
            reason="tool call from Claude",
            evidence={"input": _loggable_input(name, tool_input)},
        )

        if isinstance(action, SayAction):
            return await self.say(action.text)
        if isinstance(action, AskUserAction):
            return await self.ask_user(
                action,
                relay_template=extra["relay_template"] or ANSWER_PLACEHOLDER,
                relay_as=extra["relay_as"] or "speech",
            )
        if isinstance(action, PressDigitsAction):
            return await self.press_digits(action)
        if isinstance(action, EndCallAction):
            return await self.end_call(action)
        if isinstance(action, RequestTakeoverAction):
            return await self.request_takeover(action.reason)
        return ToolOutcome(f"unknown tool {name}", is_error=True)

    # ------------------------------------------------------------------ #
    # say
    # ------------------------------------------------------------------ #

    async def say(self, text: str) -> ToolOutcome:
        result = await guard.enforce(SayAction(text=text), self.rt, self.ports.audit)
        if not result.allowed:
            if result.trigger_takeover:
                await self._suggest_takeover(result.reason)
                return ToolOutcome(
                    f"blocked: {result.reason}. Takeover suggested to the user.",
                    is_error=True,
                    stop=True,
                )
            return ToolOutcome(f"blocked: {result.reason}", is_error=True)
        assert isinstance(result.action, SayAction)
        await self._speak(result.action.text)
        return ToolOutcome("said")

    async def _speak(self, text: str) -> None:
        """Speak guard-approved text. Callers must have run the guard."""
        if self.rt.taken_over:  # takeover may land between guard and speech
            return
        await self.ports.control.speak(self.session.id, text)
        self.session.disclosure_given = True
        redacted = audit_log.redact(text)
        self.rt.spoken.append(redacted)
        self.ports.user.publish(
            LiveMessage(
                type="caption",
                session_id=self.session.id,
                data={"speaker": "agent", "text": redacted},
            )
        )

    # ------------------------------------------------------------------ #
    # ask_user
    # ------------------------------------------------------------------ #

    async def ask_user(
        self,
        action: AskUserAction,
        *,
        relay_template: str,
        relay_as: Literal["speech", "keypad"] | str,
    ) -> ToolOutcome:
        result = await guard.enforce(action, self.rt, self.ports.audit)
        checked = result.action if isinstance(result.action, AskUserAction) else action
        request = UserRequest(
            session_id=self.session.id,
            question=checked.question,
            sensitivity=checked.sensitivity,
        )

        if not result.allowed:
            if result.trigger_takeover:
                request.status = UserRequestStatus.TAKEOVER_SUGGESTED
                await self.ports.requests.create(request)
                await self._suggest_takeover(checked.question)
                return ToolOutcome(
                    f"not relayed: {result.reason}. The user has been asked to take over; "
                    "wait for them.",
                    is_error=True,
                    stop=True,
                )
            return ToolOutcome(f"blocked: {result.reason}", is_error=True)

        if relay_as not in ("speech", "keypad"):
            return ToolOutcome("relay_as must be 'speech' or 'keypad'", is_error=True)
        if relay_as == "speech":
            if relay_template.count(ANSWER_PLACEHOLDER) != 1:
                return ToolOutcome(
                    "relay_template must contain {answer} exactly once", is_error=True
                )
            # Claude wrote the template, so it gets the full say() checks (minus the answer).
            probe = guard.check(
                SayAction(text=relay_template.replace(ANSWER_PLACEHOLDER, "")),
                self.session.model_copy(update={"disclosure_given": True}),
                self.rt,
            )
            if not probe.allowed:
                return ToolOutcome(f"relay_template blocked: {probe.reason}", is_error=True)

        # 1. Holding line to the rep.
        holding = await self.say(HOLDING_LINE)
        if holding.is_error:
            return holding
        # 2. WAITING_ON_USER + user_request (question only).
        await self.ports.control.transition(
            self.session, CallState.WAITING_ON_USER, "ask_user: waiting for the user"
        )
        future = self.ports.answers.expect(request.id)
        self.rt.pending_request_id = request.id
        try:
            await self.ports.requests.create(request)
            await self.ports.audit(
                actor="agent",
                action="user_request.created",
                subject=request.id,
                reason="asked the user",
                evidence={"session_id": self.session.id, "sensitivity": request.sensitivity},
            )
            # 3. Push with text-input action (visual + haptic) and the live channel.
            await self.ports.user.ask(self.session.user_id, request)
            self.ports.user.publish(
                LiveMessage(
                    type="ask_user",
                    session_id=self.session.id,
                    data={
                        "request_id": request.id,
                        "question": request.question,
                        "sensitivity": request.sensitivity.value,
                        "timeout_s": self.answer_timeout_s,
                    },
                )
            )
            # 4/5. Wait; on timeout ask the rep to hold or offer a callback, then wait a bit more.
            answer = await self._wait_for_answer(future, self.answer_timeout_s)
            if answer is None and not self.rt.taken_over:
                await self.say(STILL_WAITING_LINE)
                await self.ports.user.alert(
                    self.session.user_id, "Still waiting", "The rep is waiting for your answer."
                )
                answer = await self._wait_for_answer(future, self.answer_grace_s)
        finally:
            self.ports.answers.release(request.id)
            self.rt.pending_request_id = None

        if self.rt.taken_over:
            await self.ports.requests.set_status(request.id, UserRequestStatus.CANCELLED)
            return ToolOutcome("the user took over the call; stop", stop=True)

        if answer is None:
            await self.ports.requests.set_status(request.id, UserRequestStatus.TIMED_OUT)
            await self.ports.audit(
                actor="agent",
                action="user_request.timed_out",
                subject=request.id,
                allowed=True,
                reason="no answer from the user",
            )
            await self._back_to_conversation("ask_user timed out")
            return ToolOutcome(
                "The user did not answer in time. The rep was asked to hold. Offer to call "
                "back later, or continue without it.",
                is_error=True,
            )

        try:
            return await self._relay(request, answer, relay_template, relay_as)
        finally:
            answer = ""  # drop our reference; nothing else holds it
            self.rt.one_shot_relay = None

    async def _wait_for_answer(self, future: asyncio.Future[str], wait_s: float) -> str | None:
        if future.done():
            return None if future.cancelled() else future.result()
        takeover = asyncio.ensure_future(self.rt.takeover.wait())
        try:
            done, _ = await asyncio.wait(
                {future, takeover}, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            takeover.cancel()
        if future in done and not future.cancelled():
            return future.result()
        return None

    async def _relay(
        self, request: UserRequest, answer: str, template: str, relay_as: str
    ) -> ToolOutcome:
        answer = answer.strip()
        # Register before anything else so every later log/caption/transcript is scrubbed.
        audit_log.register_secret(self.session.id, answer)
        await self.ports.requests.set_status(request.id, UserRequestStatus.ANSWERED)

        if guard.looks_high_sensitivity(answer, request.question):
            await self.ports.audit(
                actor="guard",
                action="guard.relay",
                subject=request.id,
                allowed=False,
                reason="answer looks like a full SSN or card number; not relayed",
            )
            await self._suggest_takeover("This needs to be typed by you directly.")
            return ToolOutcome(
                "The answer was high-sensitivity and was NOT relayed. The user has been asked "
                "to take over.",
                is_error=True,
                stop=True,
            )

        self.rt.one_shot_relay = answer
        if relay_as == "keypad":
            digits = "".join(ch for ch in answer if ch.isdigit() or ch in "*#")
            if not digits:
                return await self._relay_failed(request, "the answer has no keypad digits")
            action: AgentAction = PressDigitsAction(digits=digits)
        else:
            action = SayAction(text=template.replace(ANSWER_PLACEHOLDER, answer))

        result = await guard.enforce(action, self.rt, self.ports.audit)
        if not result.allowed:
            if result.trigger_takeover:
                await self._suggest_takeover(result.reason)
                return ToolOutcome(f"not relayed: {result.reason}", is_error=True, stop=True)
            return await self._relay_failed(request, result.reason)

        if isinstance(result.action, PressDigitsAction):
            await self.ports.control.press_digits(self.session.id, result.action.digits)
        elif isinstance(result.action, SayAction):
            await self._speak(result.action.text)
        self.rt.one_shot_relay = None  # spoken once; from here on it is blocked and redacted

        await self.ports.audit(
            actor="agent",
            action="user_request.relayed",
            subject=request.id,
            reason=f"answer relayed once by {relay_as}",
        )
        await self._back_to_conversation("ask_user answered and relayed")
        return ToolOutcome(
            "The user answered and it was relayed to the rep once. You do not know the "
            "answer and must not repeat or guess it; if the rep needs it again, call "
            "ask_user again."
        )

    async def _relay_failed(self, request: UserRequest, reason: str) -> ToolOutcome:
        await self._back_to_conversation("ask_user relay blocked")
        return ToolOutcome(f"the user's answer could not be relayed: {reason}", is_error=True)

    async def _back_to_conversation(self, reason: str) -> None:
        if self.session.state == CallState.WAITING_ON_USER and not self.rt.taken_over:
            await self.ports.control.transition(self.session, CallState.IN_CONVERSATION, reason)

    # ------------------------------------------------------------------ #
    # press_digits / end_call / request_takeover
    # ------------------------------------------------------------------ #

    async def press_digits(self, action: PressDigitsAction) -> ToolOutcome:
        result = await guard.enforce(action, self.rt, self.ports.audit)
        if not result.allowed:
            if result.trigger_takeover:
                await self._suggest_takeover(result.reason)
                return ToolOutcome(f"blocked: {result.reason}", is_error=True, stop=True)
            return ToolOutcome(f"blocked: {result.reason}", is_error=True)
        await self.ports.control.press_digits(self.session.id, action.digits)
        return ToolOutcome("pressed")

    async def end_call(self, action: EndCallAction) -> ToolOutcome:
        result = await guard.enforce(action, self.rt, self.ports.audit)
        if not result.allowed:
            return ToolOutcome(f"blocked: {result.reason}", is_error=True)
        assert isinstance(result.action, EndCallAction)
        final = result.action

        last = self.rt.spoken[-1].lower() if self.rt.spoken else ""
        if "thank" not in last and "bye" not in last:
            closing = await guard.enforce(SayAction(text=CLOSING_LINE), self.rt, self.ports.audit)
            if closing.allowed and isinstance(closing.action, SayAction):
                await self._speak(closing.action.text)

        self.session.outcome = final.summary
        self.session.reference_number = final.reference_number
        self.rt.ended = True
        await self.ports.control.hang_up(self.session.id)
        await self.ports.control.transition(self.session, CallState.ENDED, "agent ended the call")
        self.ports.user.publish(
            LiveMessage(
                type="ended",
                session_id=self.session.id,
                data={"summary": final.summary, "reference_number": final.reference_number},
            )
        )
        return ToolOutcome("call ended", stop=True)

    async def request_takeover(self, reason: str) -> ToolOutcome:
        result = await guard.enforce(
            RequestTakeoverAction(reason=reason), self.rt, self.ports.audit
        )
        if not result.allowed:
            return ToolOutcome(f"blocked: {result.reason}", is_error=True, stop=True)
        await self._suggest_takeover(reason)
        return ToolOutcome("the user was asked to take over; wait for them", stop=True)

    async def _suggest_takeover(self, reason: str) -> None:
        """Hold the rep and alert the user (visual + haptic). The user decides to take over."""
        if not self.rt.taken_over:
            line = guard.check(SayAction(text=TAKEOVER_LINE), self.session, self.rt)
            if line.allowed and isinstance(line.action, SayAction):
                await self._speak(line.action.text)
        await self.ports.user.alert(
            self.session.user_id, "Please take over", audit_log.redact(reason)
        )
        self.ports.user.publish(
            LiveMessage(
                type="event",
                session_id=self.session.id,
                data={"kind": "takeover_suggested", "reason": reason, "at": utcnow().isoformat()},
            )
        )
        await self.ports.audit(
            actor="agent",
            action="takeover.suggested",
            subject=self.session.id,
            reason=reason,
        )


def _loggable_input(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Tool input for the audit trail. Digits may be private, so only their length is kept."""
    if name == "press_digits":
        return {"digits_len": len(str(tool_input.get("digits", "")))}
    return dict(tool_input)


__all__ = [
    "ANSWER_PLACEHOLDER",
    "HOLDING_LINE",
    "STILL_WAITING_LINE",
    "TOOL_DEFINITIONS",
    "Sensitivity",
    "ToolExecutor",
    "ToolOutcome",
]
