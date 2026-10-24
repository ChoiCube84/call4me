"""Helpers other areas use: menu parsing, caption summaries, post-call summary."""

from __future__ import annotations

import re

from pydantic import BaseModel

from backend.audit.log import redact
from backend.contracts import CallSession

from .llm import LLMClient
from .prompts import CAPTION_INSTRUCTION, SUMMARY_INSTRUCTION


class MenuOption(BaseModel):
    key: str  # DTMF key ("1", "0", "*") or a spoken keyword ("representative")
    label: str
    spoken: bool = False


_WORD_DIGITS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "star": "*",
    "pound": "#",
}
_KEY = r"(\d|zero|one|two|three|four|five|six|seven|eight|nine|star|pound|\*|#)"
_FOR_PRESS = re.compile(rf"\b(?:for|to) ([^,.;]+?),?\s+(?:press|dial|enter)\s+{_KEY}\b", re.I)
_PRESS_FOR = re.compile(
    rf"(?:press|dial|enter)\s+{_KEY}\s+(?:for|to)\s+([^,.;]+?)(?=[,.;]|\s+(?:press|or)\b|$)", re.I
)
_SAY = re.compile(
    r"(?:say|said)\s+['\"]?([a-z ]+?)['\"]?(?:\s+(?:for|to)\s+([^,.;]+))?(?=[,.;]|$)", re.I
)


def _key(token: str) -> str:
    return _WORD_DIGITS.get(token.lower(), token)


def parse_menu_options(transcript: str) -> list[MenuOption]:
    """Pull "press 1 for billing" / "for billing, press 2" / "say agent" options, in order."""
    found: list[tuple[int, MenuOption]] = []
    seen: set[str] = set()
    for m in _PRESS_FOR.finditer(transcript):
        found.append((m.start(), MenuOption(key=_key(m.group(1)), label=m.group(2).strip())))
    for m in _FOR_PRESS.finditer(transcript):
        found.append((m.start(), MenuOption(key=_key(m.group(2)), label=m.group(1).strip())))
    for m in _SAY.finditer(transcript):
        word = m.group(1).strip()
        found.append(
            (m.start(), MenuOption(key=word, label=(m.group(2) or word).strip(), spoken=True))
        )
    out: list[MenuOption] = []
    for _, opt in sorted(found, key=lambda x: x[0]):
        if opt.key not in seen:
            seen.add(opt.key)
            out.append(opt)
    return out


async def caption_summary(llm: LLMClient | None, rep_text: str) -> str:
    """Plain-language one-liner for captions. Falls back to the redacted text."""
    text = redact(rep_text)
    if llm is None:
        return text
    try:
        out = await llm.complete(CAPTION_INSTRUCTION, text, max_tokens=512)
    except Exception:  # noqa: BLE001 - captions must never break the call
        return text
    return redact(out) or text


async def post_call_summary(
    llm: LLMClient | None, session: CallSession, transcript_lines: list[str]
) -> str:
    """Summary for the user's summary screen. Input and output are redacted."""
    fallback = redact(
        session.outcome
        or (
            "The call ended."
            + (f" Reference: {session.reference_number}." if session.reference_number else "")
        )
    )
    if llm is None:
        return fallback
    prompt = f"Goal: {session.goal}\n\nTranscript:\n" + "\n".join(redact(transcript_lines))
    try:
        out = await llm.complete(SUMMARY_INSTRUCTION, prompt, max_tokens=1024)
    except Exception:  # noqa: BLE001
        return fallback
    return redact(out) or fallback
