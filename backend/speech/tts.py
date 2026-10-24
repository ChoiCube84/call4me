"""Cartesia Sonic streaming text-to-speech, plus a mock twin.

`CartesiaTTSClient` holds one persistent WebSocket to Cartesia (no handshake
per utterance) and multiplexes requests by `context_id`. It asks Cartesia for
raw 8 kHz mu-law directly, so audio goes to Twilio with no transcoding; chunks
are re-cut into exact 20 ms (160-byte) frames as they stream in.

Latency: time-to-first-audio is measured per utterance, logged, and kept in
`latency_ms` (most recent first) for the evals/dashboard. Call `warmup()` at
call start so the socket is open before the first `say`.

Pre-generated clips (e.g. the greeting) are cached in memory only and can be
played while still generating. `forget()` drops them; `close()` drops all.
Spoken text is never logged raw (it may contain an `ask_user` answer).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from backend.audio.codec import (
    FRAME_MS,
    MulawFramer,
    pcm16_to_mulaw,
    samples_to_mulaw,
)
from backend.contracts import TTSClient

log = logging.getLogger(__name__)

CARTESIA_TTS_URL = "wss://api.cartesia.ai/tts/websocket"
CARTESIA_VERSION = "2025-04-16"
DEFAULT_MODEL = "sonic-2"


class TTSError(RuntimeError):
    pass


@dataclass
class _Clip:
    frames: list[bytes] = field(default_factory=list)
    done: bool = False
    error: BaseException | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class _BaseTTS(TTSClient):
    """Pre-generation cache shared by the real client and the mock."""

    def __init__(self) -> None:
        self._clips: dict[str, _Clip] = {}
        self.latency_ms: deque[float] = deque(maxlen=50)

    async def pregenerate(self, key: str, text: str) -> None:
        await self._fill(self._register(key), text)

    def start_pregenerate(self, key: str, text: str) -> asyncio.Task[None]:
        """Register `key` now and generate in the background.

        `play_pregenerated(key)` works immediately after this returns, streaming
        frames as they arrive, which is what the human-detected path wants.
        """
        clip = self._register(key)
        return asyncio.create_task(self._fill(clip, text))

    def _register(self, key: str) -> _Clip:
        clip = _Clip()
        self._clips[key] = clip
        return clip

    async def _fill(self, clip: _Clip, text: str) -> None:
        try:
            async for frame in self.synthesize(text):
                async with clip.changed:
                    clip.frames.append(frame)
                    clip.changed.notify_all()
        except BaseException as exc:
            clip.error = exc
            raise
        finally:
            async with clip.changed:
                clip.done = True
                clip.changed.notify_all()

    def has_pregenerated(self, key: str) -> bool:
        clip = self._clips.get(key)
        return clip is not None and clip.error is None

    def is_ready(self, key: str) -> bool:
        clip = self._clips.get(key)
        return clip is not None and clip.done and clip.error is None

    async def play_pregenerated(self, key: str) -> AsyncIterator[bytes]:
        """Yield the cached frames; streams along if generation is still running."""
        clip = self._clips.get(key)
        if clip is None:
            raise KeyError(f"no pregenerated clip {key!r}")
        i = 0
        while True:
            async with clip.changed:
                await clip.changed.wait_for(lambda i=i: i < len(clip.frames) or clip.done)
                batch = clip.frames[i:]
                finished = clip.done
            for frame in batch:
                yield frame
            i += len(batch)
            if finished and i >= len(clip.frames):
                break
        if clip.error is not None:
            raise TTSError(f"pregeneration of {key!r} failed") from clip.error

    def forget(self, key: str) -> None:
        self._clips.pop(key, None)

    def _record_latency(self, ms: float) -> None:
        self.latency_ms.appendleft(ms)
        log.info("tts first audio in %.0f ms", ms)


class CartesiaTTSClient(_BaseTTS):
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        model: str = DEFAULT_MODEL,
        language: str = "en",
        url: str = CARTESIA_TTS_URL,
        version: str = CARTESIA_VERSION,
        encoding: str = "pcm_mulaw",  # or "pcm_s16le" (we transcode) if mulaw misbehaves
        connect: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        if not api_key:
            raise ValueError("CartesiaTTSClient needs an API key")
        if not voice_id:
            raise ValueError("CartesiaTTSClient needs a voice id (CARTESIA_VOICE_ID)")
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._language = language
        self._url = url
        self._version = version
        self._encoding = encoding
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        self._connect = connect
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._contexts: dict[str, asyncio.Queue[dict[str, Any] | BaseException]] = {}

    async def warmup(self) -> None:
        await self._ensure_open()

    async def _ensure_open(self) -> Any:
        async with self._lock:
            if self._ws is not None:
                return self._ws
            t0 = time.perf_counter()
            self._ws = await self._connect(
                self._url,
                additional_headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": self._version,
                },
                open_timeout=5,
                max_size=None,
            )
            log.info("tts connected in %.0f ms", (time.perf_counter() - t0) * 1000)
            self._reader = asyncio.create_task(self._read(self._ws))
            return self._ws

    async def _read(self, ws: Any) -> None:
        error: BaseException = TTSError("cartesia tts connection closed")
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                queue = self._contexts.get(msg.get("context_id", ""))
                if queue is not None:
                    queue.put_nowait(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("tts connection lost: %s", type(exc).__name__)
            error = TTSError(f"cartesia tts connection lost: {type(exc).__name__}")
        finally:
            if self._ws is ws:
                self._ws = None
            for queue in self._contexts.values():
                queue.put_nowait(error)

    def _request(self, text: str, context_id: str) -> str:
        return json.dumps(
            {
                "model_id": self._model,
                "transcript": text,
                "voice": {"mode": "id", "id": self._voice_id},
                "language": self._language,
                "context_id": context_id,
                "output_format": {
                    "container": "raw",
                    "encoding": self._encoding,
                    "sample_rate": 8000,
                },
                "continue": False,
            }
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        context_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self._contexts[context_id] = queue
        framer = MulawFramer()
        t0 = time.perf_counter()
        first = True
        finished = False
        ws: Any = None
        try:
            for attempt in range(2):  # one retry if the pooled socket went stale
                ws = await self._ensure_open()
                try:
                    await ws.send(self._request(text, context_id))
                    break
                except Exception:  # noqa: BLE001
                    if self._ws is ws:
                        self._ws = None
                    if attempt:
                        raise
            while True:
                msg = await queue.get()
                if isinstance(msg, BaseException):
                    raise msg
                kind = msg.get("type")
                if kind == "chunk" and msg.get("data"):
                    audio = base64.b64decode(msg["data"])
                    if self._encoding == "pcm_s16le":
                        audio = pcm16_to_mulaw(audio)
                    frames = framer.push(audio)
                    if frames and first:
                        first = False
                        self._record_latency((time.perf_counter() - t0) * 1000)
                    for frame in frames:
                        yield frame
                elif kind == "error":
                    raise TTSError(f"cartesia tts error: {msg.get('error') or msg}")
                if kind == "done" or msg.get("done"):
                    finished = True
                    break
            for frame in framer.flush():
                yield frame
        finally:
            self._contexts.pop(context_id, None)
            if not finished and ws is not None and self._ws is ws:
                # Consumer stopped early (barge-in/takeover): stop paying for audio.
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"context_id": context_id, "cancel": True}))

    async def close(self) -> None:
        self._clips.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader, 2)


class MockTTSClient(_BaseTTS):
    """Offline twin: emits a quiet tone whose length tracks the text.

    `spoken` holds what was synthesized so the scripted bot rep and tests can
    "hear" the agent. It is in memory only and bounded; it's a dev/test tool
    and must never be persisted.
    """

    def __init__(self, *, ms_per_char: float = 60.0, realtime: bool = False) -> None:
        super().__init__()
        self.ms_per_char = ms_per_char
        self.realtime = realtime
        self.spoken: deque[str] = deque(maxlen=100)

    def frames_for(self, text: str) -> int:
        return max(1, math.ceil(len(text.strip()) * self.ms_per_char / FRAME_MS))

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        self.spoken.append(text)
        self._record_latency(0.0)
        n = 0
        for _ in range(self.frames_for(text)):
            samples = [int(2000 * math.sin(2 * math.pi * 440 * (n + i) / 8000)) for i in range(160)]
            n += 160
            yield samples_to_mulaw(samples)
            if self.realtime:
                await asyncio.sleep(FRAME_MS / 1000)
            else:
                await asyncio.sleep(0)

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        self._clips.clear()


def make_tts_client(settings: Any | None = None) -> TTSClient:
    """Real Cartesia client, or the mock when USE_MOCKS=true / no key or voice."""
    if settings is None:
        from backend.config import get_settings

        settings = get_settings()
    if settings.use_mocks:
        return MockTTSClient()
    if not settings.cartesia_api_key or not settings.cartesia_voice_id:
        log.warning("CARTESIA_API_KEY/CARTESIA_VOICE_ID not set; using MockTTSClient")
        return MockTTSClient()
    return CartesiaTTSClient(settings.cartesia_api_key, settings.cartesia_voice_id)
