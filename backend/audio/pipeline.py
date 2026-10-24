"""Inbound audio pipeline: Twilio mu-law -> PCM -> (VAD, classifier, STT).

Usage (call-engine wiring):

    pipeline = AudioPipeline(session_id, stt=stt_client, echo=echo_gate)
    async for label in pipeline.run(telephony.audio_in(session_id)):
        ...  # an AudioLabel every ~2 s, for the Jev state check

Each 20 ms frame is decoded once. VAD and the music classifier run on the
8 kHz signal; STT gets the same audio resampled to `stt_sample_rate`. Frames
are fed to STT unless our own TTS is playing (echo gate). STT pause/resume
during hold music is the STT client's job; the classifier keeps running.
Audio is processed live and never written anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from backend.audio.classifier import AudioClassifier, FrameFeatures, HeuristicMusicClassifier
from backend.audio.codec import (
    FRAME_MS,
    SAMPLE_RATE,
    MulawFramer,
    mulaw_to_samples,
    samples_to_pcm16,
)
from backend.audio.echo import EchoGate
from backend.audio.features import rms_db, zero_crossing_rate
from backend.audio.resample import make_resampler
from backend.audio.vad import VAD, EnergyVAD
from backend.contracts import AudioLabel, STTClient

log = logging.getLogger(__name__)

STT_SAMPLE_RATE = 16000


class AudioPipeline:
    def __init__(
        self,
        session_id: str,
        stt: STTClient | None = None,
        *,
        stt_sample_rate: int = STT_SAMPLE_RATE,
        window_s: float = 2.0,
        vad: VAD | None = None,
        classifier: AudioClassifier | None = None,
        echo: EchoGate | None = None,
    ) -> None:
        self.session_id = session_id
        self.stt = stt
        self.stt_sample_rate = stt_sample_rate
        self.vad = vad or EnergyVAD()
        self.classifier = classifier or HeuristicMusicClassifier()
        self.echo = echo
        self._window_frames = max(1, round(window_s * 1000 / FRAME_MS))
        self._framer = MulawFramer()
        self._resampler = make_resampler(SAMPLE_RATE, stt_sample_rate)
        self._window: list[FrameFeatures] = []
        self.last_label: AudioLabel | None = None
        self.frames_in = 0
        self.frames_to_stt = 0

    async def push(self, mulaw: bytes) -> list[AudioLabel]:
        """Process any amount of mu-law audio; returns labels completed by it."""
        labels: list[AudioLabel] = []
        for frame in self._framer.push(mulaw):
            label = await self._process_frame(frame)
            if label is not None:
                labels.append(label)
        return labels

    async def _process_frame(self, frame: bytes) -> AudioLabel | None:
        self.frames_in += 1
        samples = mulaw_to_samples(frame)
        self._window.append(
            FrameFeatures(
                level_db=rms_db(samples),
                zcr=zero_crossing_rate(samples),
                active=self.vad.is_active(samples),
            )
        )

        if self.stt is not None and not (self.echo and self.echo.suppress_audio()):
            pcm = self._resampler.process(samples_to_pcm16(samples))
            if pcm:
                await self.stt.feed(self.session_id, pcm)
                self.frames_to_stt += 1

        if len(self._window) >= self._window_frames:
            label = self.classifier.classify(self._window)
            self._window = []
            self.last_label = label
            return label
        return None

    async def run(self, audio_in: AsyncIterator[bytes]) -> AsyncIterator[AudioLabel]:
        """Drive the pipeline from a mu-law frame source, yielding labels."""
        async for chunk in audio_in:
            for label in await self.push(chunk):
                yield label
