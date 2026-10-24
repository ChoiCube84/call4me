"""Fakes for the contracts clients the engine depends on.

Telephony and audio-speech are built in parallel, so engine tests run against
these instead of the real modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from backend.contracts import (
    CallLimits,
    CallSession,
    Speaker,
    STTClient,
    TelephonyClient,
    Transcript,
    TTSClient,
)
from backend.engine.call_engine import CallEngine, EngineConfig, EngineDeps
from backend.engine.jev_checks import MockDecisionClient
from backend.engine.menu import MemoryMenuPathStore
from backend.engine.states import MemoryRecorder
from backend.notify.apns import MockNotifier


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTelephony(TelephonyClient):
    def __init__(self) -> None:
        self.digits: list[str] = []
        self.audio: list[bytes] = []
        self.cleared = 0
        self.takeovers = 0
        self.takeovers_ended = 0
        self.hung_up = False

    async def place_call(self, session: CallSession) -> str:
        return "CA-fake"

    async def audio_in(self, session_id: str) -> AsyncIterator[bytes]:  # pragma: no cover
        if False:
            yield b""

    async def send_audio(self, session_id: str, mulaw_frame: bytes) -> None:
        self.audio.append(mulaw_frame)

    async def clear_audio(self, session_id: str) -> None:
        self.cleared += 1

    async def press_digits(self, session_id: str, digits: str) -> None:
        self.digits.append(digits)

    async def start_takeover(self, session_id: str) -> None:
        self.takeovers += 1

    async def end_takeover(self, session_id: str) -> None:
        self.takeovers_ended += 1

    async def hang_up(self, session_id: str) -> None:
        self.hung_up = True


class FakeSTT(STTClient):
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0
        self.stopped = False

    async def start(self, session_id: str) -> None: ...

    async def feed(self, session_id: str, pcm16_frame: bytes) -> None: ...

    async def transcripts(self, session_id: str) -> AsyncIterator[Transcript]:  # pragma: no cover
        if False:
            yield

    async def pause(self, session_id: str) -> None:
        self.paused += 1

    async def resume(self, session_id: str) -> None:
        self.resumed += 1

    async def stop(self, session_id: str) -> None:
        self.stopped = True


class FakeTTS(TTSClient):
    def __init__(self, *, cache_works: bool = True) -> None:
        self.cache: dict[str, str] = {}
        self.cache_works = cache_works
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        yield b"live:" + text.encode()

    async def pregenerate(self, key: str, text: str) -> None:
        if self.cache_works:
            self.cache[key] = text

    async def play_pregenerated(self, key: str) -> AsyncIterator[bytes]:
        if key not in self.cache:
            raise KeyError(key)
        yield b"pre:" + self.cache[key].encode()


def make_session(**overrides: Any) -> CallSession:
    data: dict[str, Any] = {
        "user_id": "u1",
        "company": "Fake Support Line",
        "phone_number": "+15550000000",
        "goal": "Remove the $40 late fee on my billing statement",
        "limits": CallLimits(max_hold_minutes=30, notes="no plan changes"),
        "disclosure_text": "Hi, I'm an AI assistant calling on behalf of Sam.",
    }
    data.update(overrides)
    return CallSession(**data)


class Harness:
    """A CallEngine wired to fakes, plus helpers to drive it."""

    def __init__(
        self,
        *,
        session: CallSession | None = None,
        callback_number: str = "+15551234567",
        tts: FakeTTS | None = None,
        config: EngineConfig | None = None,
        menu_paths: MemoryMenuPathStore | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.session = session or make_session()
        self.telephony = FakeTelephony()
        self.stt = FakeSTT()
        self.tts = tts or FakeTTS()
        self.jev = MockDecisionClient()
        self.notifier = MockNotifier()
        self.recorder = MemoryRecorder()
        self.menu_paths = menu_paths or MemoryMenuPathStore()
        self.conversations: list[str] = []
        self.remote_turns: list[str] = []

        async def on_start(session: CallSession, first: str) -> None:
            self.conversations.append(first)

        async def on_turn(t: Transcript) -> None:
            self.remote_turns.append(t.text)

        self.engine = CallEngine(
            self.session,
            EngineDeps(
                telephony=self.telephony,
                stt=self.stt,
                tts=self.tts,
                decisions=self.jev,
                notifier=self.notifier,
                recorder=self.recorder,
                menu_paths=self.menu_paths,
                callback_number=callback_number,
                on_conversation_start=on_start,
                on_remote_turn=on_turn,
                clock=self.clock,
            ),
            config,
        )

    async def hear(self, text: str, *, final: bool = True) -> None:
        await self.engine.on_transcript(
            Transcript(
                session_id=self.session.id, text=text, is_final=final, speaker=Speaker.REMOTE
            )
        )

    async def settle(self, seconds: float = 3.0) -> None:
        self.clock.advance(seconds)
        await self.engine.tick()

    async def flush(self) -> None:
        """Let background tasks (greeting playback) run."""
        import asyncio

        for _ in range(5):
            await asyncio.sleep(0)
        # Real time passes while the (short, fake) greeting audio plays on the line.
        self.clock.advance(0.5)

    @property
    def path(self) -> list[str]:
        return [t.to_state.value for t in self.recorder.transitions if t.allowed]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.detail for e in self.recorder.events if e.kind == kind]
