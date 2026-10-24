"""Streaming resampler: soxr when installed, linear interpolation otherwise.

Phone audio arrives at 8 kHz; Cartesia STT is fed 16 kHz. Both implementations
keep state across 20 ms chunks so frame boundaries don't click.
"""

from __future__ import annotations

import logging
from array import array
from typing import Protocol

from backend.audio.codec import pcm16_to_samples, samples_to_pcm16

log = logging.getLogger(__name__)

try:  # optional: numpy + soxr give proper band-limited resampling
    import numpy as _np
    import soxr as _soxr
except ImportError:  # pragma: no cover - exercised when deps are absent
    _np = None
    _soxr = None


class Resampler(Protocol):
    in_rate: int
    out_rate: int

    def process(self, pcm: bytes, last: bool = False) -> bytes: ...


def soxr_available() -> bool:
    return _soxr is not None


class LinearResampler:
    """Dependency-free streaming linear-interpolation resampler for int16 mono.

    Good enough for 8k -> 16k upsampling (no aliasing risk going up). Not for
    downsampling speech-quality audio; use soxr for that.
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._step = in_rate / out_rate
        self._pos = 0.0  # next output position, in input samples, relative to src[0]
        self._prev: int | None = None  # last input sample of the previous chunk

    def process(self, pcm: bytes, last: bool = False) -> bytes:
        if self.in_rate == self.out_rate:
            return pcm
        samples = pcm16_to_samples(pcm)
        src = [self._prev, *samples] if self._prev is not None else list(samples)
        if not src:
            return b""
        out = array("h")
        pos = self._pos
        limit = len(src) - 1
        while pos < limit:
            i = int(pos)
            frac = pos - i
            out.append(int(round(src[i] + (src[i + 1] - src[i]) * frac)))
            pos += self._step
        if last and pos <= limit + 1e-9:
            out.append(src[limit])
        # Carry the final sample forward; positions become relative to it.
        self._prev = src[limit]
        self._pos = pos - limit
        return samples_to_pcm16(out)


class SoxrResampler:
    def __init__(self, in_rate: int, out_rate: int, quality: str = "MQ") -> None:
        if _soxr is None or _np is None:
            raise RuntimeError("soxr/numpy not installed")
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._stream = _soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality=quality)

    def process(self, pcm: bytes, last: bool = False) -> bytes:
        x = _np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(_np.int16)
        y = self._stream.resample_chunk(x, last=last)
        return y.astype("<i2").tobytes()


def make_resampler(in_rate: int, out_rate: int) -> Resampler:
    """soxr "MQ" keeps added latency low; linear is the no-deps fallback."""
    if _soxr is not None:
        return SoxrResampler(in_rate, out_rate)
    log.info("soxr not installed; using linear resampler %s->%s Hz", in_rate, out_rate)
    return LinearResampler(in_rate, out_rate)
