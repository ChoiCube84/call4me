"""The product, as a `CallDriver` for the eval harness.

Runs the real decision code inside the simulator: Jev state checks and menu
navigation (`backend.engine`), the Claude greeting and the Claude tool-use
policy (`backend.agent` system prompt + tool definitions). Each Claude tool call
becomes an `AgentAction`, and the harness puts it through the real guard before
the line hears it, as production does.

The LLM is real Claude when ANTHROPIC_API_KEY is set (and USE_MOCKS is off),
else the rule-based mock; the runner labels which one produced the numbers.
Jev falls back to keyword heuristics without TYPESAFE_API_KEY.

What this does not exercise (covered by tests/integration/test_live_call.py):
audio, STT/TTS, timers, and the async ask_user wait inside the agent.
"""

from __future__ import annotations

from typing import Any

from backend.agent.conversation import FALLBACK_PURPOSE, build_greeting
from backend.agent.llm import LLMClient
from backend.agent.prompts import build_system_prompt
from backend.agent.tools import (
    ANSWER_PLACEHOLDER,
    HOLDING_LINE,
    SAID,
    STILL_WAITING_LINE,
    TOOL_DEFINITIONS,
)
from backend.audit import log as audit
from backend.contracts import (
    AgentAction,
    AskUserAction,
    AudioLabelKind,
    CallSession,
    CallState,
    DecisionClient,
    EndCallAction,
    PressDigitsAction,
    RequestTakeoverAction,
    SayAction,
    Sensitivity,
)
from backend.engine import jev_checks
from backend.engine.call_engine import (
    CALLBACK_DECLINE_GOAL,
    TRANSFER_RE,
)
from backend.engine.menu import (
    MenuAction,
    MenuNavigator,
    decline_callback_options,
    parse_menu_options,
)
from evals.driver import Observation, UserEvent

S = CallState
PRE_HUMAN = frozenset({S.DIALING, S.MENU, S.HOLD, S.RECORDED_MESSAGE, S.CALLBACK_OFFERED})
HUMAN_THRESHOLD = 0.8
STATE_THRESHOLD = 0.6
MAX_TOOL_ROUNDS = 4
MAX_DENIAL_RETRIES = 2  # per rep turn, so a stubborn model can't loop

_LABEL_TO_STATE = {
    "menu": S.MENU,
    "recorded_message": S.RECORDED_MESSAGE,
    "hold_music": S.HOLD,
    "callback_offer": S.CALLBACK_OFFERED,
}


class ProductDriver:
    def __init__(self, llm: LLMClient, decisions: DecisionClient, label: str) -> None:
        self.name = f"product ({label})"
        self.llm = llm
        self.decisions = decisions
        self.state = S.DIALING
        self.session: CallSession | None = None
        self.system = ""
        self.greeting = ""
        self.navigator: MenuNavigator | None = None
        self.messages: list[dict[str, Any]] = []
        self.relays: list[tuple[str, str]] = []  # (template, relay_as) per pending ask_user
        self.last_remote = ""
        self.heard_during_takeover: list[str] = []
        self.retries_left = MAX_DENIAL_RETRIES

    # -- protocol --------------------------------------------------------------

    async def start(self, session: CallSession) -> list[AgentAction]:
        self.session = session
        self.system = build_system_prompt(session)
        self.greeting = await build_greeting(session, self.llm)
        self.navigator = MenuNavigator(decisions=self.decisions, goal=session.goal)
        return []

    async def observe(self, obs: Observation) -> list[AgentAction]:
        if obs.transcript is None:
            if obs.audio.label is AudioLabelKind.MUSIC and self.state in PRE_HUMAN:
                self.state = S.HOLD
            return []
        text = obs.transcript.text
        self.last_remote = text
        self.retries_left = MAX_DENIAL_RETRIES
        if self.state is S.TAKEOVER:
            self.heard_during_takeover.append(audit.redact(text))
            return []  # rule 6: the user is typing
        if self.state in PRE_HUMAN:
            return await self._pre_human(text, obs)
        if TRANSFER_RE.search(text.lower()):
            self.state = S.HOLD
            return []
        if self.state is S.HUMAN_SUSPECTED:
            self.state = S.IN_CONVERSATION
            self._add_user(
                f'[System] You already said to the rep: "{self.greeting}". '
                "Do not repeat the AI disclosure."
            )
        self._add_user(f"Rep: {audit.redact(text)}")
        if self.state is S.WAITING_ON_USER:
            return []  # heard, but the holding line is out; wait for the user's answer
        return await self._claude()

    async def on_denied(self, kind: str, reason: str) -> list[AgentAction]:
        """The guard blocked an action: tell Claude, as the product's tool result does."""
        if self.retries_left <= 0 or self.state is not S.IN_CONVERSATION:
            return []
        self.retries_left -= 1
        self._add_user(f"[System] Your {kind} was blocked: {reason}")
        return await self._claude()

    async def user_event(self, ev: UserEvent) -> list[AgentAction]:
        if ev.kind == "takeover":
            self.state = S.TAKEOVER
            return []
        if ev.kind in ("handback", "takeover_declined"):
            self.state = S.IN_CONVERSATION
            note = (
                "The user handed the call back to you."
                if ev.kind == "handback"
                else "The user could not take over."
            )
            heard = "\n".join(f"Rep: {line}" for line in self.heard_during_takeover)
            self.heard_during_takeover.clear()
            if heard:
                note += f" While the user was typing, the rep said:\n{heard}"
            self._add_user(f"[System] {note} Continue toward the goal.")
            return await self._claude()
        if ev.kind == "answer_timeout":
            return [SayAction(text=STILL_WAITING_LINE)]
        if ev.kind == "answer":
            self.state = S.IN_CONVERSATION
            template, relay_as = self.relays.pop(0) if self.relays else ("{answer}", "speech")
            # The answer goes to the line once and never into Claude's context (rule 2).
            self._add_user("[System] The user answered and it was relayed to the rep.")
            if relay_as == "keypad":
                digits = "".join(c for c in ev.answer or "" if c.isdigit() or c in "*#")
                return [PressDigitsAction(digits=digits)] if digits else []
            return [SayAction(text=template.replace(ANSWER_PLACEHOLDER, ev.answer or ""))]
        return []

    # -- before a person answers ----------------------------------------------

    async def _pre_human(self, text: str, obs: Observation) -> list[AgentAction]:
        d = await jev_checks.classify_state(
            self.decisions, transcript=text, audio_label=obs.audio, current_state=self.state
        )
        if d.choice == "human" and d.probability >= HUMAN_THRESHOLD:
            self.state = S.HUMAN_SUSPECTED
            return [SayAction(text=self.greeting)]  # pre-generated greeting (section 6)
        new = _LABEL_TO_STATE.get(d.choice)
        if new is not None and d.probability >= STATE_THRESHOLD:
            self.state = new
        if self.state is S.CALLBACK_OFFERED:
            return await self._decline_callback(text)
        if self.state is S.MENU:
            assert self.navigator is not None
            options = parse_menu_options(text)
            action = await self.navigator.decide(options, text)
            return [_menu_action(action)] if action else []
        return []

    async def _decline_callback(self, text: str) -> list[AgentAction]:
        # Evals have no agent number to give out, so stay on hold (never the user's number).
        options = decline_callback_options(parse_menu_options(text))
        self.state = S.HOLD
        if not options:
            return []
        d = await jev_checks.choose_menu_option(
            self.decisions, goal=CALLBACK_DECLINE_GOAL, menu_options=options, transcript=text
        )
        if d.probability < STATE_THRESHOLD:
            return []
        return [_menu_action(MenuAction.from_key(d.choice, "callback decline", d.probability))]

    # -- Claude ------------------------------------------------------------------

    def _add_user(self, text: str) -> None:
        block = {"type": "text", "text": text}
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    async def _claude(self) -> list[AgentAction]:
        """One rep turn. Tool results are optimistic: the harness guard decides for real."""
        actions: list[AgentAction] = []
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = await self.llm.respond(self.system, self.messages, TOOL_DEFINITIONS)
            except Exception:  # noqa: BLE001 - the product says a repeat line on LLM errors
                return actions + [SayAction(text="Sorry, could you say that again?")]
            self.messages.append({"role": "assistant", "content": resp.content})
            if not resp.tool_calls:
                return actions
            results: list[dict[str, Any]] = []
            stop = False
            for call in resp.tool_calls:
                out, result, done = self._tool(call.name, call.input)
                actions.extend(out)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": result})
                stop = stop or done
            self.messages.append({"role": "user", "content": results})
            if stop or resp.stop_reason != "tool_use":
                return actions
        return actions

    def _tool(self, name: str, args: dict[str, Any]) -> tuple[list[AgentAction], str, bool]:
        if name == "say":
            text = str(args.get("text", ""))
            return [SayAction(text=text)], SAID, "?" in text  # as the product: asked, so wait
        if name == "press_digits":
            return [PressDigitsAction(digits=str(args.get("digits", "")))], "pressed", False
        if name == "ask_user":
            sens = Sensitivity(args.get("sensitivity", "medium"))
            question = str(args.get("question", ""))
            if sens is not Sensitivity.HIGH:
                self.relays.append(
                    (str(args.get("relay_template", "{answer}")), str(args.get("relay_as")))
                )
                self.state = S.WAITING_ON_USER
                return (
                    [
                        SayAction(text=HOLDING_LINE),
                        AskUserAction(question=question, sensitivity=sens),
                    ],
                    "asked the user; the answer will be relayed",
                    True,
                )
            return (
                [AskUserAction(question=question, sensitivity=sens)],
                "high sensitivity: suggest takeover",
                True,
            )
        if name == "end_call":
            action = EndCallAction(
                summary=str(args.get("summary", "")),
                reference_number=args.get("reference_number") or None,
            )
            return [action], "call ended", True
        if name == "request_takeover":
            reason = str(args.get("reason", "")) or FALLBACK_PURPOSE
            return [RequestTakeoverAction(reason=reason)], "the user was asked to take over", True
        return [], f"unknown tool {name}", False


def _menu_action(action: MenuAction) -> AgentAction:
    if action.kind == "dtmf":
        return PressDigitsAction(digits=action.value)
    return SayAction(text=action.value)


def make_eval_driver(session: CallSession) -> ProductDriver:
    from backend.calls import _decisions, _llm
    from backend.config import get_settings

    settings = get_settings()
    llm = _llm(settings)
    label = "Claude" if type(llm).__name__ == "AnthropicLLMClient" else "mock LLM"
    return ProductDriver(llm, _decisions(settings), label)
