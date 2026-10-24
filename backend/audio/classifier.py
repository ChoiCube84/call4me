"""Music vs speech vs silence over a ~2 s window.

Lightweight fallback for YAMNet (which needs TensorFlow): classic time-domain
features from Scheirer & Slaney (1997). Speech has a syllabic rhythm, so its
loudness swings a lot and many frames sit well below the window mean, and it
alternates voiced (low zero-crossing) with unvoiced (high zero-crossing)
sounds. Hold music is far more stationary. The score is only a hint: Jev makes
the actual state decision using this label plus the transcript.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import pstdev
from typing import Protocol

from backend.contracts import AudioLabel, AudioLabelKind


@dataclass(frozen=True, slots=True)
class FrameFeatures:
    level_db: float
    zcr: float
    active: bool


class AudioClassifier(Protocol):
    def classify(self, frames: list[FrameFeatures]) -> AudioLabel: ...


def _ramp(value: float, music_at: float, speech_at: float) -> float:
    """0 at the music anchor, 1 at the speech anchor, clamped."""
    t = (value - music_at) / (speech_at - music_at)
    return min(1.0, max(0.0, t))


class HeuristicMusicClassifier:
    def __init__(self, *, min_active_ratio: float = 0.2) -> None:
        self.min_active_ratio = min_active_ratio
        self.last_scores: dict[str, float] = {}

    def classify(self, frames: list[FrameFeatures]) -> AudioLabel:
        if not frames:
            return AudioLabel(label=AudioLabelKind.SILENCE, confidence=1.0)
        active_ratio = sum(f.active for f in frames) / len(frames)
        if active_ratio < self.min_active_ratio:
            conf = 0.6 + 0.4 * (1.0 - active_ratio / self.min_active_ratio)
            self.last_scores = {"active_ratio": active_ratio}
            return AudioLabel(label=AudioLabelKind.SILENCE, confidence=round(conf, 3))

        linear = [10 ** (f.level_db / 20.0) for f in frames]
        mean_lin = sum(linear) / len(linear)
        low_energy_ratio = sum(1 for v in linear if v < 0.5 * mean_lin) / len(linear)

        active = [f for f in frames if f.active]
        level_std = pstdev([f.level_db for f in active]) if len(active) > 1 else 0.0
        zcrs = [f.zcr for f in active]
        zcr_mean = sum(zcrs) / len(zcrs)
        zcr_cv = pstdev(zcrs) / zcr_mean if zcr_mean > 0 and len(zcrs) > 1 else 0.0

        score = (
            0.45 * _ramp(low_energy_ratio, 0.10, 0.40)
            + 0.35 * _ramp(level_std, 3.0, 9.0)
            + 0.20 * _ramp(zcr_cv, 0.15, 0.60)
        )
        self.last_scores = {
            "active_ratio": active_ratio,
            "low_energy_ratio": low_energy_ratio,
            "level_std_db": level_std,
            "zcr_cv": zcr_cv,
            "speech_score": score,
        }
        # Map distance from the 0.5 boundary to a confidence in [0.5, 1].
        conf = 0.5 + 0.5 * math.tanh(abs(score - 0.5) * 4)
        label = AudioLabelKind.SPEECH if score >= 0.5 else AudioLabelKind.MUSIC
        return AudioLabel(label=label, confidence=round(conf, 3))
