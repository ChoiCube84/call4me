"""Cartesia speech (owned by `audio-speech`): streaming STT and Sonic TTS, with mocks.

Use `make_stt_client()` / `make_tts_client()`; they return the mock twins when
USE_MOCKS=true (or when Cartesia credentials are missing).
"""

from backend.speech.playback import speak
from backend.speech.stt import CartesiaSTTClient, MockSTTClient, make_stt_client
from backend.speech.tts import CartesiaTTSClient, MockTTSClient, TTSError, make_tts_client

__all__ = [
    "CartesiaSTTClient",
    "CartesiaTTSClient",
    "MockSTTClient",
    "MockTTSClient",
    "TTSError",
    "make_stt_client",
    "make_tts_client",
    "speak",
]
