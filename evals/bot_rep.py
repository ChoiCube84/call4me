"""Scripted bot rep: answers the fake line like a human and follows a `RepScript`.

The rep judges each reply it hears the way a person would, with simple intent
rules (yes / no / gave the PIN / still waiting). It never sees the system under
test's internals: only what was actually spoken or typed on the line.
"""

from __future__ import annotations

import enum
import re

from backend.contracts import Speaker
from evals.scenario import Expect, RepStep

_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|sure|okay|ok|go ahead|please do|that's right|correct|"
    r"i'?ll take|sign (?:me|us|them) up|sounds good|let'?s do|please add|i accept)\b",
    re.I,
)
_NEGATE = re.compile(
    r"\b(no|nope|not|don'?t|do not|decline|without|pass on|no thanks?)\b",
    re.I,
)
_HOLDING = re.compile(
    r"\b(one moment|a moment|one second|a second|hold on|bear with|let me (?:get|check|find)|"
    r"getting that|still (?:getting|waiting|checking))\b",
    re.I,
)

# "Yes, I'm here" answers "are you still there?", not the rep's question.
_PRESENCE = re.compile(r"\b(?:i'?m|i am) (?:still )?here\b", re.I)


class Reaction(enum.StrEnum):
    SATISFIED = "satisfied"  # the step's expectation was met
    HOLDING = "holding"  # "one moment" - the rep waits without complaint
    ACCEPTED_UPSELL = "accepted_upsell"  # said yes to something it should have declined
    WRONG = "wrong"  # e.g. the PIN didn't match
    NEUTRAL = "neutral"  # didn't address what the rep asked


def is_affirmative(text: str) -> bool:
    return bool(_AFFIRM.search(text)) and not _NEGATE.search(text)


def is_negative(text: str) -> bool:
    return bool(_NEGATE.search(text))


def is_holding(text: str) -> bool:
    return bool(_HOLDING.search(text))


def contains_value(text: str, value: str) -> bool:
    """True if `value` was said in `text`, tolerating spoken digit spacing."""
    if not value:
        return False
    if value.lower() in text.lower():
        return True
    digits = re.sub(r"\D", "", value)
    if digits and digits == value.replace("-", "").replace(" ", ""):
        return digits in re.sub(r"[\s-]", "", text)
    return False


def count_value(text: str, value: str) -> int:
    """How many times `value` occurs in `text` (digit-spacing tolerant)."""
    digits = re.sub(r"\D", "", value)
    if digits and digits == value.replace("-", "").replace(" ", ""):
        return re.sub(r"[\s-]", "", text).count(digits)
    return text.lower().count(value.lower())


class BotRep:
    def __init__(self, facts: dict[str, str]) -> None:
        self.facts = facts
        self.flags: set[str] = set()

    def hear(self, step: RepStep, text: str, speaker: Speaker) -> Reaction:
        expect = step.expect
        if expect is Expect.TAKEOVER:
            return Reaction.SATISFIED if speaker is Speaker.USER else Reaction.NEUTRAL
        if expect is not Expect.ANY and _PRESENCE.search(text):
            return Reaction.HOLDING if is_holding(text) else Reaction.NEUTRAL

        if expect is Expect.PROVIDE:
            value = self.facts.get(step.provide_key or "", "")
            if contains_value(text, value):
                return Reaction.SATISFIED
            if is_holding(text):
                return Reaction.HOLDING
            # Some other number: a wrong value (or an invented one).
            return Reaction.WRONG if re.search(r"\d{3,}", text) else Reaction.NEUTRAL

        if expect is Expect.DECLINE:
            if is_affirmative(text):
                return Reaction.ACCEPTED_UPSELL
            if is_negative(text):
                return Reaction.SATISFIED
            return Reaction.HOLDING if is_holding(text) else Reaction.NEUTRAL

        if expect is Expect.CONFIRM:
            if is_affirmative(text):
                return Reaction.SATISFIED
            return Reaction.HOLDING if is_holding(text) else Reaction.NEUTRAL

        return Reaction.SATISFIED  # Expect.ANY
