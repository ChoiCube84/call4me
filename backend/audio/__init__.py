"""Audio pipeline (owned by `audio-speech`): decode, resample, VAD, music classifier."""

from backend.audio.classifier import HeuristicMusicClassifier
from backend.audio.codec import (
    FRAME_BYTES,
    FRAME_MS,
    MULAW_SILENCE,
    SAMPLE_RATE,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)
from backend.audio.echo import EchoGate
from backend.audio.pipeline import STT_SAMPLE_RATE, AudioPipeline
from backend.audio.vad import EnergyVAD

__all__ = [
    "FRAME_BYTES",
    "FRAME_MS",
    "MULAW_SILENCE",
    "SAMPLE_RATE",
    "STT_SAMPLE_RATE",
    "AudioPipeline",
    "EchoGate",
    "EnergyVAD",
    "HeuristicMusicClassifier",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
]
