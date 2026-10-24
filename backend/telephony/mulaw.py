"""Twilio Media Streams audio format: 8 kHz, mono, G.711 mu-law, 20 ms frames.

Pure-Python codec (stdlib `audioop` is deprecated and gone in 3.13). Decoding for
the real pipeline belongs to `audio-speech`; telephony only needs this to
synthesize mock-line audio and to build silence.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000  # 160 bytes = 160 samples
SILENCE_BYTE = 0xFF  # mu-law encoding of 0
SILENCE_FRAME = bytes([SILENCE_BYTE]) * FRAME_BYTES

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_byte(b: int) -> int:
    b = ~b & 0xFF
    sign = b & 0x80
    exponent = (b >> 4) & 0x07
    mantissa = b & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_ENCODE = bytes(_encode_sample(s) for s in range(-32768, 32768))
_DECODE = tuple(_decode_byte(b) for b in range(256))


def encode(samples: Iterable[int]) -> bytes:
    """16-bit signed PCM samples -> mu-law bytes."""
    return bytes(_ENCODE[max(-32768, min(32767, int(s))) + 32768] for s in samples)


def decode(data: bytes) -> list[int]:
    """mu-law bytes -> 16-bit signed PCM samples."""
    return [_DECODE[b] for b in data]


def frames(data: bytes, pad: bool = True) -> Iterator[bytes]:
    """Split mu-law audio into 20 ms frames, padding the last one with silence."""
    for i in range(0, len(data), FRAME_BYTES):
        chunk = data[i : i + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:
            if not pad:
                return
            chunk = chunk + bytes([SILENCE_BYTE]) * (FRAME_BYTES - len(chunk))
        yield chunk
