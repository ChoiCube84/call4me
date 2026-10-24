"""G.711 mu-law <-> 16-bit PCM, in pure Python (no audioop: removed in 3.13).

Phone lines carry 8 kHz mu-law in 20 ms frames (160 bytes). Both
directions use precomputed lookup tables, so per-frame cost is one table walk.
"""

from __future__ import annotations

import sys
from array import array

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES  # one mu-law byte per sample
MULAW_SILENCE = 0xFF  # decodes to 0

_BIAS = 0x84
_CLIP = 32635


def _decode_byte(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sign:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_DECODE: tuple[int, ...] = tuple(_decode_byte(u) for u in range(256))
_DECODE_LE: tuple[bytes, ...] = tuple(s.to_bytes(2, "little", signed=True) for s in _DECODE)
# Indexed by the int16 value reinterpreted as unsigned (s & 0xFFFF).
_ENCODE = bytes(_encode_sample(s - 65536 if s >= 32768 else s) for s in range(65536))


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Decode mu-law bytes to little-endian signed 16-bit PCM."""
    return b"".join(_DECODE_LE[b] for b in data)


def mulaw_to_samples(data: bytes) -> array:
    """Decode mu-law bytes to an int16 sample array."""
    return array("h", (_DECODE[b] for b in data))


def pcm16_to_samples(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":  # pragma: no cover
        samples.byteswap()
    return samples


def samples_to_pcm16(samples: array) -> bytes:
    if sys.byteorder == "big":  # pragma: no cover
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def samples_to_mulaw(samples: array | list[int]) -> bytes:
    return bytes(_ENCODE[s & 0xFFFF] for s in samples)


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Encode little-endian signed 16-bit PCM to mu-law."""
    return samples_to_mulaw(pcm16_to_samples(pcm))


class MulawFramer:
    """Re-chunks an arbitrary mu-law byte stream into exact 20 ms frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        n = len(self._buf) // FRAME_BYTES * FRAME_BYTES
        out = [bytes(self._buf[i : i + FRAME_BYTES]) for i in range(0, n, FRAME_BYTES)]
        del self._buf[:n]
        return out

    def flush(self) -> list[bytes]:
        """Emit any leftover bytes as one final frame padded with silence."""
        if not self._buf:
            return []
        tail = bytes(self._buf) + bytes([MULAW_SILENCE]) * (FRAME_BYTES - len(self._buf))
        self._buf.clear()
        return [tail]


def frame_mulaw(data: bytes) -> list[bytes]:
    """Split mu-law audio into 20 ms frames, padding the tail with silence."""
    framer = MulawFramer()
    return framer.push(data) + framer.flush()
