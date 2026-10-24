"""Voice/sound activity detection on 20 ms frames.

`EnergyVAD` is a dependency-free fallback: an adaptive noise floor plus an
absolute loudness gate, with hangover so short pauses inside a word don't
flicker. It detects *sound activity*; telling speech from music is the
classifier's job. Silero VAD can be dropped in behind the same `VAD` protocol
once onnxruntime + the model file are available (see the audio-speech report).
"""

from __future__ import annotations

from array import array
from typing import Protocol

from backend.audio.features import rms_db


class VAD(Protocol):
    def is_active(self, samples: array) -> bool: ...

    def reset(self) -> None: ...


class EnergyVAD:
    def __init__(
        self,
        *,
        abs_threshold_db: float = -50.0,
        margin_db: float = 10.0,
        hangover_frames: int = 8,  # 160 ms
        floor_rise_db: float = 0.05,  # per frame: floor creeps up slowly in loud audio
    ) -> None:
        self.abs_threshold_db = abs_threshold_db
        self.margin_db = margin_db
        self.hangover_frames = hangover_frames
        self.floor_rise_db = floor_rise_db
        self.reset()

    def reset(self) -> None:
        self._floor_db = -70.0
        self._hang = 0

    @property
    def noise_floor_db(self) -> float:
        return self._floor_db

    def is_active(self, samples: array) -> bool:
        level = rms_db(samples)
        # Track the noise floor: fall fast to quiet frames, rise slowly otherwise.
        if level < self._floor_db:
            self._floor_db = level
        else:
            self._floor_db = min(level, self._floor_db + self.floor_rise_db)
        loud = level > self.abs_threshold_db and level > self._floor_db + self.margin_db
        # A stationary loud signal pulls the floor up; never let it gate out audio
        # that is clearly above the absolute threshold by a wide margin.
        loud = loud or level > self.abs_threshold_db + 25.0
        if loud:
            self._hang = self.hangover_frames
            return True
        if self._hang > 0:
            self._hang -= 1
            return True
        return False
