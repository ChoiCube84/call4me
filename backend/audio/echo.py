"""Echo suppression: keep our own TTS out of STT.

Two layers, both cheap:
1. Time gate: while our audio is playing (plus a short tail for line delay),
   inbound frames are not fed to STT.
2. Text filter: a transcript that closely matches something we just said is
   treated as echo and dropped (covers echo that slips past the time gate).

Call `clear()` on barge-in/takeover when queued TTS is cut off, so the rep is
heard again right away.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from difflib import SequenceMatcher

_WORDS = re.compile(r"[a-z0-9']+")


def _norm(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


class EchoGate:
    def __init__(
        self,
        *,
        tail_s: float = 0.3,
        text_window_s: float = 6.0,
        similarity: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        gate_audio: bool = True,
    ) -> None:
        # gate_audio=False: the line already cancels echo (the browser phone), so let
        # the rep be heard over our audio; the text filter still runs.
        self.gate_audio = gate_audio
        self.tail_s = tail_s
        self.text_window_s = text_window_s
        self.similarity = similarity
        self._clock = clock
        self._play_until = 0.0
        # (normalized text, time it finishes playing) - kept in memory only.
        self._recent: deque[tuple[str, float]] = deque(maxlen=8)

    def note_playback(self, seconds: float, text: str | None = None) -> None:
        """Record that `seconds` of our audio was queued for the line."""
        now = self._clock()
        self._play_until = max(self._play_until, now) + seconds
        if text:
            self._recent.append((_norm(text), self._play_until))

    def clear(self) -> None:
        """Outgoing audio was cut (barge-in/takeover): stop gating now."""
        self._play_until = min(self._play_until, self._clock() - self.tail_s)

    @property
    def play_until(self) -> float:
        """When our queued audio finishes playing (clock time)."""
        return self._play_until

    @property
    def agent_speaking(self) -> bool:
        return self._clock() < self._play_until

    def suppress_audio(self) -> bool:
        return self.gate_audio and self._clock() < self._play_until + self.tail_s

    def is_echo(self, transcript: str) -> bool:
        heard = _norm(transcript)
        if not heard:
            return False
        now = self._clock()
        for said, ended in self._recent:
            if now - ended > self.text_window_s:
                continue
            # Substring match only for 3+ words so a rep's short "yes" isn't eaten.
            if len(heard.split()) >= 3 and heard in said:
                return True
            if SequenceMatcher(None, heard, said).ratio() >= self.similarity:
                return True
        return False
