"""Cheap per-frame features on 8 kHz int16 audio (pure Python)."""

from __future__ import annotations

import math
from array import array

FULL_SCALE = 32768.0
MIN_DB = -100.0


def rms_db(samples: array) -> float:
    """Frame loudness in dBFS."""
    if not samples:
        return MIN_DB
    mean_sq = sum(s * s for s in samples) / len(samples)
    if mean_sq <= 0:
        return MIN_DB
    return max(MIN_DB, 10.0 * math.log10(mean_sq / (FULL_SCALE * FULL_SCALE)))


def zero_crossing_rate(samples: array) -> float:
    """Fraction of adjacent sample pairs that change sign."""
    if len(samples) < 2:
        return 0.0
    crossings = sum(1 for a, b in zip(samples, samples[1:], strict=False) if (a < 0) != (b < 0))
    return crossings / (len(samples) - 1)
