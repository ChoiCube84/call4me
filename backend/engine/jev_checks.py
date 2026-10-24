"""Jev decisions: call state, menu option choice, human confirmation.

`JevDecisionClient` calls Jev through `typesafe-sdk` (a `Choice` question per
call). `MockDecisionClient` answers the same questions with keyword
heuristics so the engine runs without network (`USE_MOCKS=true`), and can be
scripted by tests. The `classify_state` / `choose_menu_option` /
`confirm_human` helpers add a hard timeout: if Jev is slow or down, the call
keeps moving on the heuristic answer at reduced confidence.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque
from typing import Any

from backend.config import get_settings
from backend.contracts import (
    AudioLabel,
    AudioLabelKind,
    CallState,
    DecisionClient,
    JevDecision,
    StateCheckLabel,
)

log = logging.getLogger(__name__)

STATE_CHECK = "state_check"
MENU_CHOICE = "menu_choice"
HUMAN_CONFIRMATION = "human_confirmation"

STATE_OPTIONS: list[str] = [label.value for label in StateCheckLabel]
HUMAN_OPTIONS: list[str] = ["human", "recording"]

# Probability is scaled by this when we fall back to heuristics after a Jev
# failure, so fallback answers never clear the high-confidence bars on their own.
FALLBACK_DISCOUNT = 0.6

_INSTRUCTIONS: dict[str, str] = {
    STATE_CHECK: (
        "You are monitoring an automated customer-service phone call from the caller's side. "
        "Given the last few seconds of what the company side said and the audio label, "
        "what is happening on the line right now?"
    ),
    MENU_CHOICE: (
        "A phone menu just listed options. Which option best moves the caller toward their "
        "goal? Prefer reaching a live representative when no option clearly fits."
    ),
    HUMAN_CONFIRMATION: (
        "The caller's assistant just greeted the other side. Is this reply a live person "
        "responding naturally to the greeting, or a recording that ignores it?"
    ),
}

_STATE_DESCRIPTIONS: dict[str, str] = {
    "menu": "an automated menu listing options to press or say",
    "recorded_message": "a recorded announcement (e.g. 'your call is important to us')",
    "hold_music": "hold music is playing",
    "silence": "nothing is being said",
    "human": "a live person is speaking (e.g. introduces themselves, asks how to help)",
    "voicemail": "a voicemail box asking to leave a message",
    "callback_offer": "the system offers to call the caller back instead of waiting",
    "call_ended": "the call was disconnected or the other side said goodbye and hung up",
}


# --------------------------------------------------------------------------- #
# Heuristics (shared by the mock and the fallback path)
# --------------------------------------------------------------------------- #

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("call_ended", re.compile(r"\b(goodbye|good bye|disconnect(ed|ing)?|hang(ing)? up)\b")),
    ("voicemail", re.compile(r"(leave (a|your) message|after the (tone|beep)|voicemail)")),
    (
        "callback_offer",
        re.compile(r"(call you back|callback|call back|return your call|keep your place in line)"),
    ),
    (
        "human",
        re.compile(
            r"(my name is|this is \w+ (speaking|with)|how (can|may) i (help|assist)|"
            r"who am i speaking|thank you for (holding|waiting)[,.]? (this is|my name)|"
            r"hello\?|are you there|can you hear me)"
        ),
    ),
    (
        "menu",
        re.compile(r"(\bpress\b|\bsay\b \w+|\bfor\b [\w ]+,? (press|say|dial)|main menu)"),
    ),
    (
        "recorded_message",
        re.compile(
            r"(your call is important|please (continue to )?hold|please stay on the line|"
            r"all of our (agents|representatives)|wait time|calls? (may be|are) recorded|"
            r"transfer(ring)? you|next available)"
        ),
    ),
]


def heuristic_state(context: dict[str, Any]) -> tuple[str, float]:
    text = str(context.get("transcript", "")).lower().strip()
    label = context.get("audio_label")
    audio_p = float(context.get("audio_confidence") or 0.0)
    for choice, pattern in _PATTERNS:
        if text and pattern.search(text):
            return choice, 0.9
    if label == AudioLabelKind.MUSIC:
        return "hold_music", max(0.6, audio_p)
    if label == AudioLabelKind.SILENCE or not text:
        return "silence", max(0.6, audio_p)
    # Unlabelled speech: a person is more likely than not, but not confidently.
    return "human", 0.55


_WORD = re.compile(r"[a-z]+")
_STOP = frozenset({"the", "a", "an", "to", "for", "my", "of", "and", "or", "on", "in", "is", "i"})
REP_WORDS = frozenset({"agent", "representative", "operator", "person", "someone", "associate"})


def words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def heuristic_menu_choice(options: list[str], context: dict[str, Any]) -> tuple[str, float]:
    """Pick the option whose label overlaps the goal most; else a rep option."""
    labels: dict[str, str] = context.get("menu_options", {}) or {}
    goal = words(str(context.get("goal", "")))
    best, best_score = None, 0
    for key in options:
        score = len(goal & words(labels.get(key, "")))
        if score > best_score:
            best, best_score = key, score
    if best is not None:
        return best, min(0.95, 0.6 + 0.15 * best_score)
    for key in options:
        if words(labels.get(key, "")) & REP_WORDS:
            return key, 0.6
    return options[0], 0.3


_RECORDING_HINTS = re.compile(
    r"(your call is important|please (continue to )?hold|press \d|para español|"
    r"calls? (may be|are) recorded|next available)"
)


def heuristic_human(context: dict[str, Any]) -> tuple[str, float]:
    reply = str(context.get("response", "")).lower()
    if not reply.strip():
        return "recording", 0.5
    if _RECORDING_HINTS.search(reply):
        return "recording", 0.85
    if re.search(
        r"(\byes\b|\bsure\b|\bokay\b|\bok\b|how can i|what can i|sorry|\bhi\b|hello|"
        r"go ahead|account|name)",
        reply,
    ):
        return "human", 0.9
    return "human", 0.6


def heuristic(question: str, options: list[str], context: dict[str, Any]) -> tuple[str, float]:
    if question == STATE_CHECK:
        choice, p = heuristic_state(context)
    elif question == MENU_CHOICE:
        choice, p = heuristic_menu_choice(options, context)
    elif question == HUMAN_CONFIRMATION:
        choice, p = heuristic_human(context)
    else:
        choice, p = options[0], 1.0 / max(1, len(options))
    if choice not in options:
        choice, p = options[0], 0.3
    return choice, p


def _alternatives(options: list[str], choice: str, p: float) -> dict[str, float]:
    rest = (1.0 - p) / max(1, len(options) - 1)
    return {o: (p if o == choice else rest) for o in options}


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


class MockDecisionClient(DecisionClient):
    """Keyword-heuristic Jev twin. Tests can queue exact answers with `script()`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, Any]]] = []
        self._scripted: dict[str, deque[tuple[str, float]]] = defaultdict(deque)

    def script(self, question: str, choice: str, probability: float = 0.95) -> None:
        self._scripted[question].append((choice, probability))

    async def choose(
        self, question: str, options: list[str], context: dict[str, Any]
    ) -> JevDecision:
        self.calls.append((question, list(options), dict(context)))
        if self._scripted[question]:
            choice, p = self._scripted[question].popleft()
        else:
            choice, p = heuristic(question, options, context)
        return JevDecision(
            question=question,
            choice=choice,
            probability=p,
            alternatives=_alternatives(options, choice, p),
        )


class JevDecisionClient(DecisionClient):
    """Jev via `typesafe-sdk` (`AsyncTypeSafeClient.system_one` with one `Choice`)."""

    def __init__(self, api_key: str | None = None, model: str = "jev-latest") -> None:
        self._api_key = api_key or get_settings().typesafe_api_key or None
        self._model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            # Imported lazily so mock runs and tests don't need the SDK installed.
            from typesafe_sdk import AsyncTypeSafeClient

            self._client = AsyncTypeSafeClient(api_key=self._api_key, model=self._model)
        return self._client

    async def choose(
        self, question: str, options: list[str], context: dict[str, Any]
    ) -> JevDecision:
        from typesafe_sdk import Choice

        descriptions: dict[str, str] = context.get("option_descriptions", {}) or {}
        criteria = {o: descriptions.get(o) for o in options}
        state = {k: v for k, v in context.items() if k != "option_descriptions"}
        response = await self._get_client().system_one(
            state=state,
            questions={
                question: Choice(
                    instructions=_INSTRUCTIONS.get(question, question), criteria=criteria
                )
            },
        )
        answer = response.choices[question]
        return JevDecision(
            question=question,
            choice=answer.choice,
            probability=float(answer.confidence),
            alternatives=dict(answer.probabilities),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def get_decision_client() -> DecisionClient:
    return MockDecisionClient() if get_settings().use_mocks else JevDecisionClient()


# --------------------------------------------------------------------------- #
# Engine-facing checks
# --------------------------------------------------------------------------- #


async def _choose_with_fallback(
    client: DecisionClient,
    question: str,
    options: list[str],
    context: dict[str, Any],
    timeout_s: float,
) -> JevDecision:
    try:
        decision = await asyncio.wait_for(client.choose(question, options, context), timeout_s)
        if decision.choice in options:
            return decision
        log.warning("jev returned unknown choice %r for %s", decision.choice, question)
    except Exception as exc:  # noqa: BLE001 - any Jev failure falls back, the call continues
        log.warning("jev %s failed (%s); using heuristic", question, exc)
    choice, p = heuristic(question, options, context)
    p *= FALLBACK_DISCOUNT
    return JevDecision(
        question=question,
        choice=choice,
        probability=p,
        alternatives={"fallback": 1.0, **_alternatives(options, choice, p)},
    )


async def classify_state(
    client: DecisionClient,
    *,
    transcript: str,
    audio_label: AudioLabel | None,
    current_state: CallState,
    silence_s: float = 0.0,
    timeout_s: float = 2.0,
) -> JevDecision:
    context = {
        "transcript": transcript,
        "audio_label": audio_label.label if audio_label else None,
        "audio_confidence": audio_label.confidence if audio_label else None,
        "current_state": current_state,
        "silence_seconds": round(silence_s, 1),
        "option_descriptions": _STATE_DESCRIPTIONS,
    }
    return await _choose_with_fallback(client, STATE_CHECK, STATE_OPTIONS, context, timeout_s)


async def choose_menu_option(
    client: DecisionClient,
    *,
    goal: str,
    menu_options: dict[str, str],
    transcript: str,
    timeout_s: float = 2.0,
) -> JevDecision:
    """`menu_options` maps an action key ("1", "say:representative") to its label."""
    options = list(menu_options)
    context = {
        "goal": goal,
        "menu_options": menu_options,
        "transcript": transcript,
        "option_descriptions": menu_options,
    }
    return await _choose_with_fallback(client, MENU_CHOICE, options, context, timeout_s)


async def confirm_human(
    client: DecisionClient,
    *,
    greeting: str,
    response: str,
    timeout_s: float = 1.5,
) -> JevDecision:
    context = {
        "greeting": greeting,
        "response": response,
        "option_descriptions": {
            "human": "a live person answering or reacting to the greeting",
            "recording": "a recording or menu that continues regardless of the greeting",
        },
    }
    return await _choose_with_fallback(
        client, HUMAN_CONFIRMATION, HUMAN_OPTIONS, context, timeout_s
    )
