"""audio-speech: Cartesia STT/TTS clients (against local fake servers) and mocks."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from backend.audio.codec import FRAME_BYTES, MULAW_SILENCE
from backend.audio.echo import EchoGate
from backend.contracts import STTClient, TTSClient
from backend.speech import (
    CartesiaSTTClient,
    CartesiaTTSClient,
    MockSTTClient,
    MockTTSClient,
    TTSError,
    make_stt_client,
    make_tts_client,
    speak,
)


async def _collect(it: AsyncIterator, n: int | None = None, limit_s: float = 2.0) -> list:
    out = []

    async def run() -> None:
        async for item in it:
            out.append(item)
            if n is not None and len(out) >= n:
                break

    await asyncio.wait_for(run(), limit_s)
    return out


# --- fake Cartesia STT server -------------------------------------------------


class FakeSTTServer:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.headers: list = []
        self.audio_bytes = 0
        self.connections = 0

    async def handler(self, ws) -> None:
        self.connections += 1
        self.paths.append(ws.request.path)
        self.headers.append(ws.request.headers)
        pending = 0
        async for msg in ws:
            if isinstance(msg, bytes):
                self.audio_bytes += len(msg)
                pending += len(msg)
                if pending >= 3200:  # every 100 ms of audio, a partial
                    pending = 0
                    await ws.send(
                        json.dumps({"type": "transcript", "text": "thanks for", "is_final": False})
                    )
            elif msg == "finalize":
                await ws.send(
                    json.dumps(
                        {"type": "transcript", "text": "thanks for calling", "is_final": True}
                    )
                )
                await ws.send(json.dumps({"type": "flush_done"}))
            elif msg == "done":
                await ws.send(json.dumps({"type": "done"}))
                return


@pytest.fixture
async def stt_server():
    fake = FakeSTTServer()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, f"ws://127.0.0.1:{port}/stt/websocket"


async def test_cartesia_stt_streams_partials_and_finals(stt_server) -> None:
    fake, url = stt_server
    stt = CartesiaSTTClient("test-key", url=url)
    await stt.start("s1")
    for _ in range(10):
        await stt.feed("s1", b"\x00\x00" * 320)  # 20 ms at 16 kHz
    await stt.finalize("s1")
    got = await _collect(stt.transcripts("s1"), n=3)
    assert [t.is_final for t in got] == [False, False, True]
    assert got[-1].text == "thanks for calling"
    assert got[-1].session_id == "s1"
    assert "model=ink-whisper" in fake.paths[0] and "sample_rate=16000" in fake.paths[0]
    assert "encoding=pcm_s16le" in fake.paths[0]
    assert fake.headers[0]["X-API-Key"] == "test-key"
    assert "Cartesia-Version" in fake.headers[0]
    await stt.stop("s1")
    assert fake.audio_bytes == 6400


async def test_cartesia_stt_pause_closes_socket_and_replays_backlog(stt_server) -> None:
    fake, url = stt_server
    stt = CartesiaSTTClient("k", url=url, resume_backlog_s=0.1)  # keep 3200 bytes
    await stt.start("s1")
    await stt.pause("s1")
    assert stt.is_paused("s1")
    for _ in range(20):  # 400 ms of hold music: only the last 100 ms is kept
        await stt.feed("s1", b"\x01\x00" * 320)
    assert fake.audio_bytes == 0
    await stt.resume("s1")
    await asyncio.sleep(0.05)
    assert fake.connections == 2
    assert 3200 <= fake.audio_bytes <= 3200 + 640
    await stt.stop("s1")


async def test_cartesia_stt_drops_echo_of_our_tts(stt_server) -> None:
    _, url = stt_server
    stt = CartesiaSTTClient("k", url=url)
    gate = EchoGate()
    gate.note_playback(0.5, "Thanks for calling")
    stt.set_echo_gate("s1", gate)
    await stt.start("s1")
    await stt.feed("s1", b"\x00\x00" * 1600)
    await stt.finalize("s1")
    await asyncio.sleep(0.1)
    await stt.stop("s1")
    got = await _collect(stt.transcripts("s1"))
    # The fake echoes our own words back; all of it is suppressed.
    assert all(t.text not in ("thanks for calling",) for t in got)


# --- fake Cartesia TTS server ---------------------------------------------------


class FakeTTSServer:
    def __init__(self, chunk_sizes=(100, 250, 70), fail: bool = False) -> None:
        self.requests: list[dict] = []
        self.cancels: list[str] = []
        self.chunk_sizes = chunk_sizes
        self.fail = fail
        self.connections = 0

    async def handler(self, ws) -> None:
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("cancel"):
                self.cancels.append(msg["context_id"])
                continue
            self.requests.append(msg)
            ctx = msg["context_id"]
            if self.fail:
                await ws.send(
                    json.dumps(
                        {"type": "error", "error": "bad voice", "context_id": ctx, "done": True}
                    )
                )
                continue
            for size in self.chunk_sizes:
                data = base64.b64encode(bytes([0x10]) * size).decode()
                await ws.send(
                    json.dumps({"type": "chunk", "data": data, "done": False, "context_id": ctx})
                )
            await ws.send(json.dumps({"type": "done", "done": True, "context_id": ctx}))


async def _tts_server(fake: FakeTTSServer):
    server = await serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/tts/websocket"


async def test_cartesia_tts_streams_mulaw_frames_and_measures_latency() -> None:
    fake = FakeTTSServer()
    server, url = await _tts_server(fake)
    try:
        tts = CartesiaTTSClient("k", "voice-1", url=url)
        frames = await _collect(tts.synthesize("Hello there"))
        # 420 bytes -> two full frames + one padded tail.
        assert [len(f) for f in frames] == [FRAME_BYTES] * 3
        assert frames[-1].endswith(bytes([MULAW_SILENCE]) * (480 - 420))
        req = fake.requests[0]
        assert req["transcript"] == "Hello there"
        assert req["voice"] == {"mode": "id", "id": "voice-1"}
        assert req["output_format"] == {
            "container": "raw",
            "encoding": "pcm_mulaw",
            "sample_rate": 8000,
        }
        assert len(tts.latency_ms) == 1 and tts.latency_ms[0] >= 0
        # A second utterance reuses the same socket.
        await _collect(tts.synthesize("Again"))
        assert fake.connections == 1
        await tts.close()
    finally:
        server.close()


async def test_cartesia_tts_cancels_when_consumer_stops_early() -> None:
    fake = FakeTTSServer(chunk_sizes=(160,) * 20)
    server, url = await _tts_server(fake)
    try:
        tts = CartesiaTTSClient("k", "v", url=url)
        gen = tts.synthesize("A long sentence")
        await gen.__anext__()
        await gen.aclose()
        await asyncio.sleep(0.05)
        assert fake.cancels == [fake.requests[0]["context_id"]]
        await tts.close()
    finally:
        server.close()


async def test_cartesia_tts_raises_on_error() -> None:
    server, url = await _tts_server(FakeTTSServer(fail=True))
    try:
        tts = CartesiaTTSClient("k", "v", url=url)
        with pytest.raises(TTSError):
            await _collect(tts.synthesize("hi"))
        await tts.close()
    finally:
        server.close()


async def test_cartesia_tts_pregenerated_greeting() -> None:
    server, url = await _tts_server(FakeTTSServer())
    try:
        tts = CartesiaTTSClient("k", "v", url=url)
        await tts.pregenerate("greeting", "Hi, I'm an AI assistant calling for Sam.")
        assert tts.is_ready("greeting")
        frames = await _collect(tts.play_pregenerated("greeting"))
        assert len(frames) == 3
        again = await _collect(tts.play_pregenerated("greeting"))  # replayable
        assert again == frames
        tts.forget("greeting")
        with pytest.raises(KeyError):
            await _collect(tts.play_pregenerated("greeting"))
        await tts.close()
    finally:
        server.close()


# --- mocks ----------------------------------------------------------------------


def test_mocks_implement_contracts() -> None:
    assert isinstance(MockSTTClient(), STTClient)
    assert isinstance(MockTTSClient(), TTSClient)


def test_factories_pick_mocks() -> None:
    mocks = SimpleNamespace(use_mocks=True, cartesia_api_key="k", cartesia_voice_id="v")
    assert isinstance(make_stt_client(mocks), MockSTTClient)
    assert isinstance(make_tts_client(mocks), MockTTSClient)
    real = SimpleNamespace(use_mocks=False, cartesia_api_key="k", cartesia_voice_id="v")
    assert isinstance(make_stt_client(real), CartesiaSTTClient)
    assert isinstance(make_tts_client(real), CartesiaTTSClient)
    nokey = SimpleNamespace(use_mocks=False, cartesia_api_key="", cartesia_voice_id="")
    assert isinstance(make_stt_client(nokey), MockSTTClient)
    assert isinstance(make_tts_client(nokey), MockTTSClient)


async def test_mock_stt_inject_pause_stop() -> None:
    stt = MockSTTClient()
    await stt.start("s1")
    await stt.feed("s1", b"\x00\x00" * 16000)
    assert stt.seconds_fed("s1") == 1.0
    stt.inject("s1", "press one for billing")
    await stt.pause("s1")
    stt.inject("s1", "this is hold music talking")  # deaf while paused
    await stt.feed("s1", b"\x00\x00" * 16000)
    assert stt.seconds_fed("s1") == 1.0
    await stt.resume("s1")
    stt.inject("s1", "hello", partials=False)
    await stt.stop("s1")
    got = await _collect(stt.transcripts("s1"))
    assert [(t.text, t.is_final) for t in got] == [
        ("press", False),
        ("press one", False),
        ("press one for", False),
        ("press one for billing", True),
        ("hello", True),
    ]


async def test_mock_tts_frames_and_pregenerate_streams_while_generating() -> None:
    tts = MockTTSClient(ms_per_char=20.0)
    frames = await _collect(tts.synthesize("hello"))
    assert len(frames) == 5 and all(len(f) == FRAME_BYTES for f in frames)
    task = tts.start_pregenerate("greeting", "Hi there, this is an AI")
    played = await _collect(tts.play_pregenerated("greeting"))
    await task
    assert len(played) == tts.frames_for("Hi there, this is an AI")
    assert list(tts.spoken) == ["hello", "Hi there, this is an AI"]


async def test_speak_sends_frames_and_updates_echo_gate() -> None:
    now = [0.0]
    gate = EchoGate(clock=lambda: now[0])
    tts = MockTTSClient(ms_per_char=20.0)
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    n = await speak(tts.synthesize("one two three"), send, echo=gate, text="one two three")
    assert n == len(sent) == 13
    assert gate.agent_speaking
    assert gate.is_echo("one two three")
    now[0] += 0.26 + 0.3 + 0.01
    assert not gate.suppress_audio()
