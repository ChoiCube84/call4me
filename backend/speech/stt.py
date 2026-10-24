"""Cartesia streaming speech-to-text, plus a mock twin.

`CartesiaSTTClient` keeps one WebSocket per call session to Cartesia's STT
endpoint (model `ink-whisper`), streams 16 kHz PCM16 audio as binary messages,
and turns `transcript` messages into partial/final `Transcript` events.

Cost and latency details:
- `pause()` (confirmed hold music) closes the socket and stops sending audio.
  While paused, the last `resume_backlog_s` of audio is kept *in memory only*
  and replayed on `resume()`, so a rep's first words aren't lost while we
  reconnect. Nothing is written to disk.
- `finalize()` asks Cartesia to flush the current utterance now, for callers
  that know the speaker stopped (cuts final-transcript latency).
- An optional `EchoGate` per session drops transcripts of our own TTS.

Transcript text is never logged raw; only `audit.redact()`ed text at DEBUG.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from backend.audio.echo import EchoGate
from backend.audit.log import redact
from backend.contracts import Speaker, STTClient, Transcript

log = logging.getLogger(__name__)

CARTESIA_STT_URL = "wss://api.cartesia.ai/stt/websocket"
CARTESIA_VERSION = "2025-04-16"
STT_SAMPLE_RATE = 16000


@dataclass
class _Session:
    queue: asyncio.Queue[Transcript | None] = field(default_factory=asyncio.Queue)
    paused: bool = False
    stopped: bool = False
    echo: EchoGate | None = None
    backlog: deque[bytes] = field(default_factory=deque)
    backlog_bytes: int = 0
    # Real-client only.
    ws: Any = None
    reader: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finalize_sent_at: float | None = None


class _BaseSTT(STTClient):
    """Per-session bookkeeping shared by the real client and the mock."""

    def __init__(self, *, sample_rate: int = STT_SAMPLE_RATE, resume_backlog_s: float = 1.5):
        self.sample_rate = sample_rate
        self._backlog_limit = int(resume_backlog_s * sample_rate * 2)
        self._sessions: dict[str, _Session] = {}

    def _session(self, session_id: str) -> _Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = self._sessions[session_id] = _Session()
        return sess

    def _fresh_session(self, session_id: str) -> _Session:
        """For start(): reuse a session (e.g. one with an echo gate set) unless stopped."""
        sess = self._sessions.get(session_id)
        if sess is None or sess.stopped:
            echo = sess.echo if sess is not None else None
            sess = self._sessions[session_id] = _Session(echo=echo)
        return sess

    def set_echo_gate(self, session_id: str, gate: EchoGate | None) -> None:
        self._session(session_id).echo = gate

    def is_paused(self, session_id: str) -> bool:
        return self._session(session_id).paused

    def _keep_backlog(self, sess: _Session, pcm: bytes) -> None:
        sess.backlog.append(pcm)
        sess.backlog_bytes += len(pcm)
        while sess.backlog and sess.backlog_bytes - len(sess.backlog[0]) >= self._backlog_limit:
            sess.backlog_bytes -= len(sess.backlog.popleft())

    def _take_backlog(self, sess: _Session) -> list[bytes]:
        chunks = list(sess.backlog)
        sess.backlog.clear()
        sess.backlog_bytes = 0
        return chunks

    def _emit(
        self,
        session_id: str,
        text: str,
        is_final: bool,
        confidence: float | None = None,
    ) -> None:
        sess = self._sessions.get(session_id)
        if sess is None or sess.stopped or not text.strip():
            return
        if sess.echo is not None and sess.echo.is_echo(text):
            log.debug("stt %s: dropped echo of our own TTS", session_id)
            return
        if log.isEnabledFor(logging.DEBUG):
            log.debug("stt %s final=%s: %s", session_id, is_final, redact(text))
        sess.queue.put_nowait(
            Transcript(
                session_id=session_id,
                text=text.strip(),
                is_final=is_final,
                speaker=Speaker.REMOTE,
                confidence=confidence,
            )
        )

    async def transcripts(self, session_id: str) -> AsyncIterator[Transcript]:
        sess = self._session(session_id)
        while True:
            item = await sess.queue.get()
            if item is None:
                # Ended by stop(); forget the session once its consumer is done.
                if self._sessions.get(session_id) is sess:
                    del self._sessions[session_id]
                return
            yield item

    def _close_session(self, session_id: str) -> None:
        """Mark stopped and end the transcript stream (queued items still drain)."""
        sess = self._sessions.get(session_id)
        if sess is not None and not sess.stopped:
            sess.stopped = True
            sess.backlog.clear()
            sess.queue.put_nowait(None)


class CartesiaSTTClient(_BaseSTT):
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "ink-whisper",
        language: str = "en",
        sample_rate: int = STT_SAMPLE_RATE,
        url: str = CARTESIA_STT_URL,
        version: str = CARTESIA_VERSION,
        extra_params: dict[str, str] | None = None,
        resume_backlog_s: float = 1.5,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(sample_rate=sample_rate, resume_backlog_s=resume_backlog_s)
        if not api_key:
            raise ValueError("CartesiaSTTClient needs an API key")
        self._api_key = api_key
        self._version = version
        params = {
            "model": model,
            "language": language,
            "encoding": "pcm_s16le",
            "sample_rate": str(sample_rate),
            **(extra_params or {}),
        }
        self._url = f"{url}?{urlencode(params)}"
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        self._connect = connect

    async def _ensure_open(self, session_id: str, sess: _Session) -> Any:
        async with sess.lock:
            if sess.ws is not None:
                return sess.ws
            t0 = time.perf_counter()
            sess.ws = await self._connect(
                self._url,
                additional_headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": self._version,
                },
                open_timeout=5,
                max_size=None,
            )
            log.info("stt %s: connected in %.0f ms", session_id, (time.perf_counter() - t0) * 1000)
            sess.reader = asyncio.create_task(self._read(session_id, sess, sess.ws))
            return sess.ws

    async def _read(self, session_id: str, sess: _Session, ws: Any) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "transcript":
                    self._emit(
                        session_id,
                        msg.get("text", ""),
                        bool(msg.get("is_final")),
                        msg.get("confidence"),
                    )
                elif kind == "flush_done":
                    if sess.finalize_sent_at is not None:
                        ms = (time.perf_counter() - sess.finalize_sent_at) * 1000
                        log.info("stt %s: finalize flushed in %.0f ms", session_id, ms)
                        sess.finalize_sent_at = None
                elif kind == "error":
                    log.warning("stt %s: cartesia error: %s", session_id, msg.get("message"))
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001 - connection loss; reconnect on next feed
            log.warning("stt %s: connection lost: %s", session_id, type(exc).__name__)
        finally:
            if sess.ws is ws:
                sess.ws = None

    async def start(self, session_id: str) -> None:
        sess = self._fresh_session(session_id)
        await self._ensure_open(session_id, sess)

    async def feed(self, session_id: str, pcm16_frame: bytes) -> None:
        sess = self._sessions.get(session_id)
        if sess is None or sess.stopped or not pcm16_frame:
            return
        if sess.paused:
            self._keep_backlog(sess, pcm16_frame)
            return
        ws = await self._ensure_open(session_id, sess)
        try:
            await ws.send(pcm16_frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("stt %s: send failed (%s); will reconnect", session_id, type(exc).__name__)
            if sess.ws is ws:
                sess.ws = None

    async def finalize(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None or sess.ws is None:
            return
        sess.finalize_sent_at = time.perf_counter()
        with contextlib.suppress(Exception):
            await sess.ws.send("finalize")

    async def pause(self, session_id: str) -> None:
        sess = self._session(session_id)
        if sess.paused:
            return
        sess.paused = True
        await self._close_ws(sess)
        log.info("stt %s: paused (hold music)", session_id)

    async def resume(self, session_id: str) -> None:
        sess = self._session(session_id)
        if not sess.paused:
            return
        sess.paused = False
        backlog = self._take_backlog(sess)
        ws = await self._ensure_open(session_id, sess)
        for chunk in backlog:
            await ws.send(chunk)
        log.info("stt %s: resumed, replayed %d backlog chunks", session_id, len(backlog))

    async def _close_ws(self, sess: _Session, wait_s: float = 2.0) -> None:
        ws, reader = sess.ws, sess.reader
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send("done")  # flush the last words before closing
        if reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader, wait_s)
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        sess.ws = None
        sess.reader = None

    async def stop(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        await self._close_ws(sess)
        self._close_session(session_id)


class MockSTTClient(_BaseSTT):
    """In-memory twin. Tests and the mock call loop push text with `inject()`.

    Behaves like the real client: nothing is emitted while paused (we'd be
    deaf), echo of our own TTS is dropped, and audio fed is only counted.
    """

    def __init__(self, *, sample_rate: int = STT_SAMPLE_RATE) -> None:
        super().__init__(sample_rate=sample_rate)
        self.bytes_fed: dict[str, int] = {}
        self.started: set[str] = set()

    async def start(self, session_id: str) -> None:
        self._fresh_session(session_id)
        self.started.add(session_id)

    async def feed(self, session_id: str, pcm16_frame: bytes) -> None:
        sess = self._sessions.get(session_id)
        if sess is None or sess.stopped or sess.paused:
            return
        self.bytes_fed[session_id] = self.bytes_fed.get(session_id, 0) + len(pcm16_frame)

    def seconds_fed(self, session_id: str) -> float:
        return self.bytes_fed.get(session_id, 0) / (2 * self.sample_rate)

    def inject(self, session_id: str, text: str, *, partials: bool = True) -> None:
        """Pretend the remote side said `text`: word-by-word partials, then a final."""
        sess = self._session(session_id)
        if sess.paused or sess.stopped:
            return
        words = text.split()
        if partials:
            for i in range(1, len(words)):
                self._emit(session_id, " ".join(words[:i]), False)
        self._emit(session_id, text, True, 0.99)

    async def finalize(self, session_id: str) -> None:
        return None

    async def pause(self, session_id: str) -> None:
        self._session(session_id).paused = True

    async def resume(self, session_id: str) -> None:
        self._session(session_id).paused = False

    async def stop(self, session_id: str) -> None:
        self.started.discard(session_id)
        self._close_session(session_id)


def make_stt_client(settings: Any | None = None) -> STTClient:
    """Real Cartesia client, or the mock when USE_MOCKS=true / no API key."""
    if settings is None:
        from backend.config import get_settings

        settings = get_settings()
    if settings.use_mocks:
        return MockSTTClient()
    if not settings.cartesia_api_key:
        log.warning("CARTESIA_API_KEY not set; using MockSTTClient")
        return MockSTTClient()
    return CartesiaSTTClient(settings.cartesia_api_key)
