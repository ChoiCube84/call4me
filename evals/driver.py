"""The interface between the eval harness and the system under test.

A `CallDriver` is what the simulator talks to. It sees what the product would
see (final transcripts of the company side plus an `AudioLabel`, and user events
like an `ask_user` answer or a takeover) and returns `AgentAction`s from
`backend/contracts.py`. The harness runs every action through the guard before
the line hears it.

`BaselineDriver` is a rule-based reference policy. It exists so the harness can
be tested before the real engine/agent are merged; its scores are NOT product
scores and the runner labels them as such.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from backend.agent.guard import REP_FAREWELL
from backend.contracts import (
    AgentAction,
    AskUserAction,
    AudioLabel,
    AudioLabelKind,
    CallSession,
    CallState,
    EndCallAction,
    PressDigitsAction,
    RequestTakeoverAction,
    SayAction,
    Sensitivity,
    Transcript,
)


@dataclass
class Observation:
    t: float  # virtual seconds since dialing
    audio: AudioLabel
    transcript: Transcript | None = None  # final transcript of the company side, if any


@dataclass
class UserEvent:
    kind: Literal["answer", "answer_timeout", "takeover", "handback", "takeover_declined"]
    request_id: str | None = None
    # The raw ask_user answer. In memory only: drivers must never log or store it.
    answer: str | None = None


@runtime_checkable
class CallDriver(Protocol):
    name: str
    state: CallState

    async def start(self, session: CallSession) -> list[AgentAction]: ...

    async def observe(self, obs: Observation) -> list[AgentAction]: ...

    async def user_event(self, ev: UserEvent) -> list[AgentAction]: ...


# --------------------------------------------------------------------------- #
# Baseline reference policy
# --------------------------------------------------------------------------- #

_OPTION = re.compile(r"(?:for|to)\s+([^,.;]+?),?\s+press\s+(\d)", re.I)
_GREETING = re.compile(
    r"\b(this is|my name is|speaking|how (?:can|may) i help|returning your call)\b", re.I
)
_REFERENCE = re.compile(
    r"\b(?:reference|confirmation|cancellation|ticket|case)\s*(?:number|#)?\s*(?:is)?\s*:?\s*"
    r"([A-Z0-9][A-Z0-9-]{3,})",
    re.I,
)
_HIGH_SENS = re.compile(r"social security|\bssn\b|full card|card number|password", re.I)
_ASKABLE = {
    "pin": ("PIN", Sensitivity.MEDIUM),
    "order number": ("order number", Sensitivity.LOW),
    "account number": ("account number", Sensitivity.MEDIUM),
    "date of birth": ("date of birth", Sensitivity.MEDIUM),
    "zip": ("ZIP code", Sensitivity.LOW),
    "email": ("email address", Sensitivity.LOW),
}
_DEPARTMENTS = {
    "billing": ("fee", "charge", "bill", "payment", "refund"),
    "order": ("order", "delivery", "shipping", "package"),
    "account": ("cancel", "subscription", "account"),
}


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:]


class BaselineDriver:
    name = "baseline"

    def __init__(self) -> None:
        self.state = CallState.DIALING
        self.session: CallSession | None = None
        self._menu_seen: Counter[str] = Counter()
        self._last_remote = ""
        self._awaiting_new_rep = True
        self._known: dict[str, str] = {}  # label -> answer, memory only
        self._pending: dict[str, str] = {}  # label -> rep question (asked, not answered yet)
        self._order: list[str] = []  # labels in the order we asked
        self._reference: str | None = None
        self._rep_info: list[str] = []
        self._wrapped_up = False

    # -- protocol ------------------------------------------------------------

    async def start(self, session: CallSession) -> list[AgentAction]:
        self.session = session
        return []

    async def observe(self, obs: Observation) -> list[AgentAction]:
        if obs.transcript is None:
            if obs.audio.label is AudioLabelKind.MUSIC and self.state not in (
                CallState.TAKEOVER,
                CallState.WAITING_ON_USER,
            ):
                self.state = CallState.HOLD
            return []
        text = obs.transcript.text
        self._last_remote = text
        self._learn(text)
        if self.state is CallState.TAKEOVER:
            return []  # the user is talking; never initiate speech
        return self._react(text)

    async def user_event(self, ev: UserEvent) -> list[AgentAction]:
        if ev.kind == "takeover":
            self.state = CallState.TAKEOVER
            return []
        if ev.kind in ("handback", "takeover_declined"):
            self.state = CallState.IN_CONVERSATION
            return self._react(self._last_remote)
        if ev.kind == "answer_timeout":
            line = "Sorry, I'm still getting that. Could you hold a moment longer?"
            return [SayAction(text=line)]
        if ev.kind == "answer" and ev.answer is not None and self._order:
            label = self._order.pop(0)
            self._pending.pop(label, None)
            self._known[label] = ev.answer
            self.state = CallState.IN_CONVERSATION
            return [SayAction(text=f"Thanks for waiting. The {label} is {ev.answer}.")]
        return []

    # -- policy --------------------------------------------------------------

    def _learn(self, text: str) -> None:
        m = _REFERENCE.search(text)
        if m and re.search(r"\d", m.group(1)):
            self._reference = m.group(1).rstrip(".")
        if "?" not in text and not _GREETING.search(text) and "press" not in text.lower():
            self._rep_info.append(text)

    def _react(self, text: str) -> list[AgentAction]:
        assert self.session is not None
        low = text.lower()

        if "press" in low:
            return self._menu(text)
        if "transfer" in low:
            self.state = CallState.HOLD
            self._awaiting_new_rep = True
            return []
        if self._awaiting_new_rep and _GREETING.search(text):
            self._awaiting_new_rep = False
            self.state = CallState.IN_CONVERSATION
            goal = _lower_first(self.session.goal.rstrip("."))
            return [SayAction(text=f"{self.session.disclosure_text} I'm calling to {goal}.")]
        if self.state is not CallState.IN_CONVERSATION and self.state is not (
            CallState.WAITING_ON_USER
        ):
            self.state = CallState.HOLD  # recorded message or similar
            return []
        return self._converse(text, low)

    def _menu(self, text: str) -> list[AgentAction]:
        self.state = CallState.MENU
        low = text.lower()
        if "callback" in low or "call you back" in low:
            return []  # stay on the line rather than give out any number
        self._menu_seen[text] += 1
        if self._menu_seen[text] > 3:  # menu loop -> ask for a person (CLAUDE.md section 6)
            return [PressDigitsAction(digits="0")]
        assert self.session is not None
        goal = self.session.goal.lower()
        options = _OPTION.findall(text)
        # Words that point at the right option: the department names and keywords
        # for every department the goal is about.
        wanted = {
            w
            for dept, words in _DEPARTMENTS.items()
            if any(w in goal for w in words)
            for w in (dept, *words)
        }
        for label, digit in options:
            if any(w in label.lower() for w in wanted):
                return [PressDigitsAction(digits=digit)]
        for label, digit in options:
            if re.search(r"agent|representative|operator|someone", label, re.I):
                return [PressDigitsAction(digits=digit)]
        return []  # nothing fits; wait for the menu to repeat

    def _converse(self, text: str, low: str) -> list[AgentAction]:
        assert self.session is not None
        if re.search(r"automated|\bai\b|artificial|robot", low) and re.search(
            r"can'?t|cannot|not able|unable|only speak", low
        ):
            return [
                SayAction(text="I understand. I'll bring the account holder on to type to you."),
                RequestTakeoverAction(reason="Rep will only speak with the account holder."),
            ]
        if _HIGH_SENS.search(low):
            return [
                SayAction(
                    text="That's sensitive, so the account holder will type it to you "
                    "directly. One moment."
                ),
                RequestTakeoverAction(reason="Rep asked for high-sensitivity information."),
            ]
        for key, (label, sens) in _ASKABLE.items():
            if re.search(rf"\b{key}\b", low) and ("?" in text or "verify" in low):
                if label in self._known:
                    return [SayAction(text=f"The {label} is {self._known[label]}.")]
                if label in self._pending:
                    return [SayAction(text="I'm still getting that for you, one moment.")]
                self._pending[label] = text
                self._order.append(label)
                self.state = CallState.WAITING_ON_USER
                return [
                    SayAction(text="One moment, let me get that for you."),
                    AskUserAction(
                        question=f"The rep is asking for your {label}. What is it?",
                        sensitivity=sens,
                    ),
                ]
        if "still there" in low:
            if self._pending:
                return [SayAction(text="Yes, I'm here. Still getting that, one moment.")]
            return [SayAction(text="Yes, I'm here.")]
        if re.search(r"upgrade|premium|new plan|a month|per month|add .* to", low) and "?" in text:
            if self.session.limits.allow_plan_changes:
                return [SayAction(text="Yes, please.")]
            goal = _lower_first(self.session.goal.rstrip("."))
            return [SayAction(text=f"No thank you. I only want to {goal}.")]
        if "cancel" in low and "?" in text:
            if self.session.limits.allow_cancellation and "cancel" in self.session.goal.lower():
                return [SayAction(text="Yes, please go ahead.")]
            return [SayAction(text="No, please don't make that change.")]
        if self._wrapped_up or (self._reference and REP_FAREWELL.search(low)):
            return self._finish()  # both sides are done
        if "anything else" in low:
            # Wrap up, then hang up only once the rep has answered (never on them).
            self._wrapped_up = True
            return [SayAction(text="No, that's everything. Thank you for your help.")]
        if "?" in text:
            # A question we can't answer from the goal: never guess (rule 3).
            self._order.append("answer")
            self._pending["answer"] = text
            self.state = CallState.WAITING_ON_USER
            return [
                SayAction(text="One moment, let me check on that."),
                AskUserAction(question=f"The rep asks: {text}", sensitivity=Sensitivity.LOW),
            ]
        if self._reference and self._reference in text:
            return [SayAction(text=f"Just to confirm, the reference is {self._reference}?")]
        return [SayAction(text="Okay, thank you.")]

    def _finish(self) -> list[AgentAction]:
        assert self.session is not None
        summary = f"Goal: {self.session.goal}. Rep said: " + " ".join(self._rep_info[-4:])
        return [
            SayAction(text="Goodbye."),
            EndCallAction(summary=summary, reference_number=self._reference),
        ]
