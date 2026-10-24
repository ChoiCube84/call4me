"""Synthetic test signals (generated in memory; no recordings are stored)."""

from __future__ import annotations

import math
import random

from backend.audio.codec import SAMPLE_RATE, samples_to_mulaw


def _clip(x: float) -> int:
    return max(-32768, min(32767, int(x)))


def music(seconds: float, amp: float = 6000.0) -> list[int]:
    """Sustained chords that change every half second, like hold music."""
    chords = [(262, 330, 392), (220, 277, 330), (196, 247, 294), (175, 220, 262)]
    out = []
    for n in range(int(seconds * SAMPLE_RATE)):
        t = n / SAMPLE_RATE
        chord = chords[int(t / 0.5) % len(chords)]
        tremolo = 1.0 + 0.1 * math.sin(2 * math.pi * 5 * t)
        v = sum(math.sin(2 * math.pi * f * t) for f in chord) / len(chord)
        out.append(_clip(amp * tremolo * v))
    return out


def speech_like(seconds: float, amp: float = 9000.0, seed: int = 1) -> list[int]:
    """Syllables at ~4 Hz: voiced vowel, short fricative, gap; word pauses."""
    rng = random.Random(seed)
    out = []
    n_total = int(seconds * SAMPLE_RATE)
    syl = int(0.25 * SAMPLE_RATE)
    n = 0
    k = 0
    while n < n_total:
        f0 = 110 + 30 * rng.random()
        pause = k % 5 == 4  # every fifth syllable is a pause between words
        for i in range(syl):
            if n >= n_total:
                break
            t = i / SAMPLE_RATE
            if pause:
                v = rng.gauss(0, 30)
            elif i < int(0.15 * SAMPLE_RATE):
                env = math.sin(math.pi * i / (0.15 * SAMPLE_RATE))
                v = env * amp * sum(math.sin(2 * math.pi * f0 * h * t) / h for h in range(1, 8)) / 2
            elif i < int(0.20 * SAMPLE_RATE):
                v = rng.gauss(0, amp * 0.15)
            else:
                v = rng.gauss(0, 30)
            out.append(_clip(v))
            n += 1
        k += 1
    return out


def silence(seconds: float, noise: float = 20.0, seed: int = 2) -> list[int]:
    rng = random.Random(seed)
    return [_clip(rng.gauss(0, noise)) for _ in range(int(seconds * SAMPLE_RATE))]


def to_mulaw(samples: list[int]) -> bytes:
    return samples_to_mulaw(samples)
