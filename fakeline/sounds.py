"""Synthesized audio for the fake line, so it needs no external media files.

- `hold_music_pcm()`: a 10 s loop of soft chords (served as WAV to Twilio, and
  replayed as mu-law frames by the mock line).
- `speech_like_pcm(text)`: a voiced, syllable-modulated buzz sized to the text's
  length, standing in for a recorded prompt in the mock line. It is not real
  speech; pair it with the segment text side channel (see mock_client).

If `fakeline/audio/<segment key with ':' replaced by '_'>.ulaw` exists (raw 8 kHz
mu-law, e.g. rendered once from TTS), the mock line plays that instead.
"""

from __future__ import annotations

import io
import math
import random
import wave
from functools import lru_cache
from pathlib import Path

from backend.telephony import mulaw

RATE = mulaw.SAMPLE_RATE
HOLD_LOOP_SECONDS = 10
AUDIO_DIR = Path(__file__).parent / "audio"

# I - vi - IV - V in C, as (root, third, fifth) frequencies.
_CHORDS = (
    (261.63, 329.63, 392.00),
    (220.00, 261.63, 329.63),
    (174.61, 220.00, 261.63),
    (196.00, 246.94, 293.66),
)


@lru_cache(maxsize=1)
def hold_music_pcm() -> tuple[int, ...]:
    out: list[int] = []
    per_chord = HOLD_LOOP_SECONDS * RATE // len(_CHORDS)
    for chord in _CHORDS:
        for n in range(per_chord):
            t = n / RATE
            env = min(1.0, n / 400, (per_chord - n) / 400)  # click-free edges
            arp = chord[(n * 4 // per_chord) % 3] * 2  # simple arpeggio on top
            v = sum(math.sin(2 * math.pi * f * t) for f in chord) / 3
            v = 0.7 * v + 0.3 * math.sin(2 * math.pi * arp * t) * math.exp(
                -3 * ((n * 4) % per_chord) / per_chord
            )
            out.append(int(6000 * env * v))
    return tuple(out)


@lru_cache(maxsize=1)
def hold_music_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(s.to_bytes(2, "little", signed=True) for s in hold_music_pcm()))
    return buf.getvalue()


@lru_cache(maxsize=1)
def hold_music_mulaw() -> bytes:
    return mulaw.encode(hold_music_pcm())


def speech_seconds(text: str) -> float:
    return max(0.6, len(text.split()) * 0.33)


@lru_cache(maxsize=256)
def speech_like_mulaw(text: str, f0: float = 140.0) -> bytes:
    rng = random.Random(text)
    n_total = int(speech_seconds(text) * RATE)
    out: list[int] = []
    phase = 0.0
    for n in range(n_total):
        t = n / RATE
        syllable = max(0.0, math.sin(2 * math.pi * 4.0 * t))  # ~4 syllables/s
        pitch = f0 * (1 + 0.08 * math.sin(2 * math.pi * 0.7 * t))
        phase += 2 * math.pi * pitch / RATE
        voiced = sum(math.sin(k * phase) / k for k in range(1, 8))
        v = syllable * (0.8 * voiced + 0.2 * (rng.random() * 2 - 1))
        out.append(int(5000 * v))
    return mulaw.encode(out)


def recorded_mulaw(key: str) -> bytes | None:
    path = AUDIO_DIR / f"{key.replace(':', '_')}.ulaw"
    return path.read_bytes() if path.is_file() else None
