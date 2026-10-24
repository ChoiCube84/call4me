"""Phone menu navigation.

Options are parsed from the menu prompt (Claude via an injected `MenuParser`
from `conversation-agent`, with a regex fallback), then Jev picks the option
that serves the goal. Low confidence or a looping menu falls back to reaching
a person: press 0, then say "representative". Routes that reached a human are
saved to `menu_path` so the next call to the same company can replay them.

Action keys: a DTMF string ("1", "0", "*") or "say:<words>".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from backend.contracts import DecisionClient
from backend.engine import jev_checks

log = logging.getLogger(__name__)

SAY_PREFIX = "say:"
REP_DIGIT = "0"
REP_PHRASE = "representative"

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "star": "*", "pound": "#",
}  # fmt: skip
_KEY = r"(\d|zero|one|two|three|four|five|six|seven|eight|nine|star|pound)"
_LABEL = r"([^,.;?!]+?)"
_END = r"(?=\s*(?:[,.;?!]|$|\bor\b|\bpress\b|\bfor\b))"

# "For billing, press 2" / "To speak with an agent press 0"
_LABEL_THEN_KEY = re.compile(
    rf"\b(?:for|to|if you(?:'d like to| would like to| want to)?)\s+{_LABEL},?\s+"
    rf"(?:please\s+)?(?:press|dial|push|enter)\s+{_KEY}\b"
)
# "Press 1 for orders" / "Press 3 to hear these options again"
_KEY_THEN_LABEL = re.compile(rf"\b(?:press|dial|push)\s+{_KEY}\s+(?:for|to|if)\s+{_LABEL}{_END}")
# "say 'representative'" / "or say agent"
_SAY = re.compile(
    r"\bsay\s+[\"'“]?([a-z]+(?: [a-z]+)?)[\"'”]?"
    r"(?=\s*(?:[,.;?!]|$|\bor\b|\bfor\b|\bto\b))"
)

_OPEN_PROMPT = re.compile(
    r"(in a few words|briefly (tell|describe)|tell (me|us) (what|why)|"
    r"what are you calling about|reason for your call)"
)


def _key(raw: str) -> str:
    return _NUM_WORDS.get(raw, raw)


def parse_menu_options(text: str) -> dict[str, str]:
    """Regex fallback parser: action key -> spoken label."""
    text = text.lower()
    options: dict[str, str] = {}
    label_first = [
        (_key(m.group(2)), m.group(1).strip(), m.start()) for m in _LABEL_THEN_KEY.finditer(text)
    ]
    key_first = [
        (_key(m.group(1)), m.group(2).strip(), m.start()) for m in _KEY_THEN_LABEL.finditer(text)
    ]
    # "press 1 for orders, press 2 for billing" also matches "for orders, press 2"
    # with the wrong pairing, so trust whichever phrasing the menu starts with.
    styles = [label_first, key_first]
    styles.sort(key=lambda found: found[0][2] if found else len(text) + 1)
    for found in styles:
        for key, label, _ in found:
            options.setdefault(key, label)
    for m in _SAY.finditer(text):
        word = m.group(1).strip()
        if word not in {"it", "that", "yes", "no"}:
            options.setdefault(SAY_PREFIX + word, word)
    return options


def decline_callback_options(options: dict[str, str]) -> dict[str, str]:
    """Keys that don't request a callback. Declining by pressing the only key on offer
    ("To get a callback, press 1. Or stay on the line.") would accept it."""
    return {k: v for k, v in options.items() if not jev_checks.CALLBACK_RE.search(v.lower())}


def is_open_prompt(text: str) -> bool:
    return bool(_OPEN_PROMPT.search(text.lower()))


class MenuParser(Protocol):
    """Implemented by `conversation-agent` with Claude; returns key -> label."""

    async def parse(self, text: str, *, goal: str) -> dict[str, str]: ...


class RegexMenuParser:
    async def parse(self, text: str, *, goal: str) -> dict[str, str]:
        return parse_menu_options(text)


# --------------------------------------------------------------------------- #
# Saved routes
# --------------------------------------------------------------------------- #


class MenuPathStore(Protocol):
    async def load(self, company: str) -> list[str] | None: ...

    async def save_success(self, company: str, steps: list[str]) -> None: ...


class MemoryMenuPathStore:
    def __init__(self) -> None:
        self.paths: dict[str, tuple[list[str], int]] = {}

    async def load(self, company: str) -> list[str] | None:
        hit = self.paths.get(company)
        return list(hit[0]) if hit else None

    async def save_success(self, company: str, steps: list[str]) -> None:
        old = self.paths.get(company)
        count = old[1] + 1 if old and old[0] == steps else 1
        self.paths[company] = (list(steps), count)


class DbMenuPathStore:
    async def load(self, company: str) -> list[str] | None:
        from backend.db.store import open_store

        async with open_store() as db:
            return await db.menu(company)

    async def save_success(self, company: str, steps: list[str]) -> None:
        from backend.db.store import open_store

        async with open_store() as db:
            await db.save_menu(company, steps)


# --------------------------------------------------------------------------- #
# Navigator
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MenuAction:
    kind: Literal["dtmf", "say"]
    value: str
    reason: str
    probability: float | None = None
    key: str = ""

    @classmethod
    def from_key(cls, key: str, reason: str, probability: float | None) -> MenuAction:
        if key.startswith(SAY_PREFIX):
            return cls("say", key[len(SAY_PREFIX) :], reason, probability, key)
        return cls("dtmf", key, reason, probability, key)


def menu_signature(options: dict[str, str], transcript: str) -> str:
    """Identify 'the same menu again' so loops can be counted."""
    if options:
        parts = (f"{k}={' '.join(sorted(jev_checks.words(v)))}" for k, v in sorted(options.items()))
        return "|".join(parts)
    return " ".join(sorted(jev_checks.words(transcript)))[:200]


@dataclass
class MenuNavigator:
    decisions: DecisionClient
    goal: str
    saved_path: list[str] | None = None
    confidence_threshold: float = 0.6
    max_repeats: int = 3
    jev_timeout_s: float = 2.0
    steps: list[str] = field(default_factory=list)
    repeats: dict[str, int] = field(default_factory=dict)
    tried: dict[str, set[str]] = field(default_factory=dict)  # menu signature -> keys pressed
    last_sig: str = ""
    escalations: int = 0
    last_decision: jev_checks.JevDecision | None = None

    @property
    def clean_path(self) -> bool:
        """Worth remembering: short, no menu heard twice, no escalation.
        A path with wrong turns would replay the wrong turns next time."""
        return (
            bool(self.steps)
            and self.escalations == 0
            and all(n <= 1 for n in self.repeats.values())
            and len(self.steps) <= 4
        )

    @property
    def loop_exceeded(self) -> bool:
        return any(n > self.max_repeats for n in self.repeats.values())

    def escalate(self, reason: str) -> MenuAction:
        """Alternate press 0 / say "representative" when the menu won't let go."""
        self.escalations += 1
        if self.escalations % 2 == 1:
            action = MenuAction("dtmf", REP_DIGIT, reason, None, REP_DIGIT)
        else:
            action = MenuAction("say", REP_PHRASE, reason, None, SAY_PREFIX + REP_PHRASE)
        self.steps.append(action.key)
        return action

    def _rep_fallback(self, options: dict[str, str], reason: str, p: float | None) -> MenuAction:
        for key, label in options.items():
            if jev_checks.words(label) & jev_checks.REP_WORDS:
                return MenuAction.from_key(key, reason, p)
        if REP_DIGIT in options or not any(k.startswith(SAY_PREFIX) for k in options):
            return MenuAction("dtmf", REP_DIGIT, reason, p, REP_DIGIT)
        return MenuAction("say", REP_PHRASE, reason, p, SAY_PREFIX + REP_PHRASE)

    async def decide(self, options: dict[str, str], transcript: str) -> MenuAction | None:
        """Choose what to do with a fully-heard menu prompt. None means keep listening."""
        sig = menu_signature(options, transcript)
        came_back = sig in self.tried and sig != self.last_sig
        self.last_sig = sig
        self.repeats[sig] = self.repeats.get(sig, 0) + 1
        if self.repeats[sig] > self.max_repeats:
            self.repeats[sig] = 0  # give the escalation a fresh chance to work
            return self.escalate(f"menu repeated more than {self.max_repeats} times")

        if not options:
            if is_open_prompt(transcript):
                action = MenuAction(
                    "say", REP_PHRASE, "open-ended speech menu", None, SAY_PREFIX + REP_PHRASE
                )
                self.steps.append(action.key)
                return action
            return None

        step = len(self.steps)
        if self.saved_path and step < len(self.saved_path) and self.saved_path[step] in options:
            action = MenuAction.from_key(self.saved_path[step], "saved menu path", None)
            self.steps.append(action.key)
            return action

        # Back at a menu after a branch from it didn't reach a person: offer Jev only
        # the untried options. The same menu twice in a row is a missed press; repeat it.
        tried = self.tried.setdefault(sig, set())
        fresh = options
        if came_back:
            fresh = {k: v for k, v in options.items() if k not in tried} or options
        decision = await jev_checks.choose_menu_option(
            self.decisions,
            goal=self.goal,
            menu_options=fresh,
            transcript=transcript,
            timeout_s=self.jev_timeout_s,
        )
        self.last_decision = decision
        if decision.probability < self.confidence_threshold:
            action = self._rep_fallback(
                options,
                f"low confidence ({decision.probability:.2f}) for {decision.choice!r}",
                decision.probability,
            )
        else:
            action = MenuAction.from_key(
                decision.choice, f"jev chose {options[decision.choice]!r}", decision.probability
            )
        tried.add(action.key)
        self.steps.append(action.key)
        return action
