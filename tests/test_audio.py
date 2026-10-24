"""audio-speech: codec, resampling, VAD, classifier, echo gate, pipeline."""

from __future__ import annotations

import math
from array import array

import pytest

from backend.audio import resample as resample_mod
from backend.audio.codec import (
    FRAME_BYTES,
    MULAW_SILENCE,
    MulawFramer,
    frame_mulaw,
    mulaw_to_pcm16,
    mulaw_to_samples,
    pcm16_to_mulaw,
    pcm16_to_samples,
    samples_to_pcm16,
)
from backend.audio.echo import EchoGate
from backend.audio.pipeline import AudioPipeline
from backend.audio.resample import LinearResampler, make_resampler
from backend.audio.vad import EnergyVAD
from backend.contracts import AudioLabelKind
from tests.audio_fixtures import music, silence, speech_like, to_mulaw

# --- codec -------------------------------------------------------------------


def test_mulaw_known_values() -> None:
    assert mulaw_to_samples(bytes([0xFF, 0x7F, 0x00, 0x80])).tolist() == [0, 0, -32124, 32124]


def test_mulaw_roundtrip_is_stable() -> None:
    every_byte = bytes(range(256))
    pcm = mulaw_to_pcm16(every_byte)
    assert len(pcm) == 512
    # 0x7F and 0xFF both mean zero; everything else round-trips exactly.
    again = pcm16_to_mulaw(pcm)
    assert mulaw_to_pcm16(again) == pcm


def test_mulaw_encode_error_is_small() -> None:
    samples = array("h", [int(10000 * math.sin(i / 7)) for i in range(800)])
    decoded = mulaw_to_samples(pcm16_to_mulaw(samples_to_pcm16(samples)))
    worst = max(abs(a - b) for a, b in zip(samples, decoded, strict=True))
    assert worst < 10000 * 0.04  # mu-law step is ~3% at this level


def test_framer_rechunks_and_pads() -> None:
    framer = MulawFramer()
    assert framer.push(b"\x01" * 100) == []
    frames = framer.push(b"\x02" * 300)
    assert [len(f) for f in frames] == [FRAME_BYTES, FRAME_BYTES]
    tail = framer.flush()
    assert len(tail) == 1 and len(tail[0]) == FRAME_BYTES
    assert tail[0].endswith(bytes([MULAW_SILENCE]))
    assert [len(f) for f in frame_mulaw(b"\x00" * 330)] == [160, 160, 160]


# --- resampling --------------------------------------------------------------


def _sine_pcm(freq: float, rate: int, seconds: float, amp: int = 8000) -> bytes:
    n = int(rate * seconds)
    return samples_to_pcm16(
        array("h", [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)])
    )


def test_linear_resampler_streaming_doubles_length_and_matches_one_shot() -> None:
    pcm = _sine_pcm(440, 8000, 0.2)
    one_shot = LinearResampler(8000, 16000).process(pcm, last=True)
    streamed = LinearResampler(8000, 16000)
    parts = [streamed.process(pcm[i : i + 320]) for i in range(0, len(pcm), 320)]
    parts.append(streamed.process(b"", last=True))
    joined = b"".join(parts)
    assert abs(len(joined) // 2 - 3200) <= 2
    assert joined == one_shot


def test_linear_resampler_preserves_waveform() -> None:
    out = pcm16_to_samples(LinearResampler(8000, 16000).process(_sine_pcm(300, 8000, 0.1)))
    expected = [8000 * math.sin(2 * math.pi * 300 * i / 16000) for i in range(len(out))]
    assert max(abs(a - b) for a, b in zip(out, expected, strict=True)) < 200


@pytest.mark.skipif(not resample_mod.soxr_available(), reason="soxr not installed")
def test_soxr_resampler_streaming_length() -> None:
    rs = make_resampler(8000, 16000)
    pcm = _sine_pcm(440, 8000, 1.0)
    out = b"".join(rs.process(pcm[i : i + 320]) for i in range(0, len(pcm), 320))
    out += rs.process(b"", last=True)
    assert abs(len(out) // 2 - 16000) <= 32


# --- VAD and classifier ------------------------------------------------------


def _frames(samples: list[int]) -> list[array]:
    return [array("h", samples[i : i + 160]) for i in range(0, len(samples) - 159, 160)]


def test_energy_vad_separates_silence_and_sound() -> None:
    vad = EnergyVAD()
    assert not any(vad.is_active(f) for f in _frames(silence(1.0)))
    active = [vad.is_active(f) for f in _frames(music(1.0))]
    assert sum(active) / len(active) > 0.9


async def _labels(mulaw: bytes) -> list:
    pipeline = AudioPipeline("s1")
    labels = []
    for i in range(0, len(mulaw), FRAME_BYTES):
        labels += await pipeline.push(mulaw[i : i + FRAME_BYTES])
    return labels


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (lambda: music(6.0), AudioLabelKind.MUSIC),
        (lambda: speech_like(6.0), AudioLabelKind.SPEECH),
        (lambda: silence(6.0), AudioLabelKind.SILENCE),
    ],
)
async def test_pipeline_labels_every_two_seconds(signal, expected) -> None:
    labels = await _labels(to_mulaw(signal()))
    assert len(labels) == 3
    assert all(lbl.label is expected for lbl in labels), labels
    assert all(0.5 <= lbl.confidence <= 1.0 for lbl in labels)


async def test_pipeline_tracks_change_from_music_to_speech() -> None:
    labels = await _labels(to_mulaw(music(4.0) + speech_like(4.0, seed=7)))
    assert [lbl.label for lbl in labels] == [
        AudioLabelKind.MUSIC,
        AudioLabelKind.MUSIC,
        AudioLabelKind.SPEECH,
        AudioLabelKind.SPEECH,
    ]


class _RecordingSTT:
    def __init__(self) -> None:
        self.fed: list[bytes] = []

    async def feed(self, session_id: str, pcm16_frame: bytes) -> None:
        self.fed.append(pcm16_frame)


async def test_pipeline_feeds_16k_pcm_to_stt() -> None:
    stt = _RecordingSTT()
    pipeline = AudioPipeline("s1", stt=stt)  # type: ignore[arg-type]
    await pipeline.push(to_mulaw(speech_like(1.0)))
    total_samples = sum(len(b) for b in stt.fed) // 2
    # soxr holds back ~25 ms of filter delay until more audio arrives.
    assert 16000 - 800 <= total_samples <= 16000 + 64
    assert pipeline.frames_in == 50


async def test_pipeline_echo_gate_blocks_stt_while_agent_speaks() -> None:
    now = [100.0]
    gate = EchoGate(tail_s=0.3, clock=lambda: now[0])
    stt = _RecordingSTT()
    pipeline = AudioPipeline("s1", stt=stt, echo=gate)  # type: ignore[arg-type]
    gate.note_playback(1.0, "Hi, I'm an AI assistant calling for Sam.")
    await pipeline.push(to_mulaw(speech_like(0.2)))
    assert stt.fed == []
    now[0] += 1.31
    await pipeline.push(to_mulaw(speech_like(0.2)))
    assert stt.fed


# --- echo gate ---------------------------------------------------------------


def test_echo_gate_text_filter_and_clear() -> None:
    now = [0.0]
    gate = EchoGate(clock=lambda: now[0])
    gate.note_playback(2.0, "Hi, I'm an AI assistant calling on behalf of Sam.")
    assert gate.agent_speaking and gate.suppress_audio()
    assert gate.is_echo("I'm an AI assistant calling on behalf")
    assert not gate.is_echo("yes")
    assert not gate.is_echo("Thanks for calling, how can I help you today?")
    gate.clear()
    assert not gate.agent_speaking and not gate.suppress_audio()
    now[0] += 10.0
    assert not gate.is_echo("I'm an AI assistant calling on behalf of Sam")
