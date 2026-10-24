"""The in-process fake support line (TELEPHONY=mock, and under the browser phone).

`place_call()` starts a simulated call that walks the same script as the TwiML
fake line (`fakeline/script.py`): welcome, menus that react to `press_digits()`,
recorded messages, hold music with announcements, an optional callback offer,
then the scripted rep, which waits for the agent to speak (audio sent with
`send_audio()` followed by ~0.8 s of quiet counts as one agent turn).

Audio: `audio_in()` yields 8 kHz mu-law 20 ms frames like the real stream.
Prompts are pre-rendered files from `fakeline/audio/` when present, else a
synthesized speech-like placeholder (not intelligible speech). So mock STT can
produce text, `remote_segments()` yields each segment's text and ground-truth
`StateCheckLabel` as it starts playing.

Timing: `speed=1.0` is real time; `speed=0` runs as fast as the `audio_in()`
consumer reads (time is counted in frames), which is what tests want.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from backend.contracts import CallSession, StateCheckLabel
from backend.telephony import mulaw
from backend.telephony.base import (
    DIGITS_PATTERN,
    CallControl,
    CallStatus,
    CallStatusUpdate,
    TelephonyError,
)
from fakeline import script, sounds

log = logging.getLogger("call4me.telephony.mock")

FPS = 1000 // mulaw.FRAME_MS  # frames per second (50)
AGENT_TURN_GAP = int(0.8 * FPS)
_DIGITS = re.compile(DIGITS_PATTERN)


@dataclass(frozen=True)
class MockSegment:
    key: str
    label: StateCheckLabel
    text: str
    at_s: float  # line time (seconds of audio emitted) when it started


class _Hangup(Exception):  # noqa: N818 - control flow, not an error
    pass


class MockLine:
    """One simulated call."""

    def __init__(
        self,
        session_id: str,
        *,
        speed: float,
        hold_seconds: tuple[float, float],
        callback_offer: bool,
        rng: random.Random,
        hold_chunk_s: float,
    ) -> None:
        self.session_id = session_id
        self.speed = speed
        self.hold_seconds = hold_seconds
        self.callback_offer = callback_offer
        self.rng = rng
        self.hold_chunk_s = hold_chunk_s
        realtime = speed > 0
        self.inbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=500 if realtime else 25)
        self.segments: asyncio.Queue[MockSegment | None] = asyncio.Queue()
        self.statuses: asyncio.Queue[CallStatusUpdate | None] = asyncio.Queue()
        self.digits: asyncio.Queue[str] = asyncio.Queue()
        self.connected = asyncio.Event()
        self.ended = asyncio.Event()
        self.tick = 0  # frames emitted so far = line time
        self.agent_play_until = 0  # tick at which queued agent audio finishes playing
        self.agent_spoke = False
        self.clear_count = 0
        self.manual_turns = 0
        self.takeover = False
        self.bridge_number: str | None = None
        self.digits_pressed: list[str] = []
        self.agent_frames = 0
        self.call_sid = f"MOCK{session_id[:28]}"
        self.task: asyncio.Task[None] | None = None
        self._t0 = 0.0

    # ---- output -----------------------------------------------------------------

    async def _put(self, frame: bytes) -> None:
        if self.speed > 0:
            loop = asyncio.get_running_loop()
            delay = self._t0 + self.tick * mulaw.FRAME_MS / 1000 / self.speed - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.inbound.full():
                self.inbound.get_nowait()
            self.inbound.put_nowait(frame)
        else:
            await self.inbound.put(frame)
        self.tick += 1

    async def _play(self, seg: script.Segment) -> None:
        self.segments.put_nowait(MockSegment(seg.key, seg.label, seg.text, self.tick / FPS))
        audio = sounds.recorded_mulaw(seg.key) or sounds.speech_like_mulaw(seg.text)
        for frame in mulaw.frames(audio):
            await self._put(frame)
        await self._silence(0.3)

    async def _music(self, seconds: float) -> None:
        self.segments.put_nowait(
            MockSegment("hold:music", StateCheckLabel.HOLD_MUSIC, "", self.tick / FPS)
        )
        music = sounds.hold_music_mulaw()
        offset = (self.tick * mulaw.FRAME_BYTES) % len(music)
        n = int(seconds * FPS)
        for i in range(n):
            start = (offset + i * mulaw.FRAME_BYTES) % (len(music) - mulaw.FRAME_BYTES)
            await self._put(music[start : start + mulaw.FRAME_BYTES])

    async def _silence(self, seconds: float) -> None:
        for _ in range(int(seconds * FPS)):
            await self._put(mulaw.SILENCE_FRAME)

    # ---- input --------------------------------------------------------------------

    def _drain_digits(self) -> None:
        while not self.digits.empty():
            self.digits.get_nowait()

    async def _wait_digit(self, seconds: float) -> str | None:
        for _ in range(int(seconds * FPS)):
            if not self.digits.empty():
                return self.digits.get_nowait()
            await self._put(mulaw.SILENCE_FRAME)
        return None if self.digits.empty() else self.digits.get_nowait()

    async def _wait_agent_turn(self, seconds: float) -> bool:
        """True once the agent has spoken and gone quiet; False on timeout."""
        start_manual = self.manual_turns
        for _ in range(int(seconds * FPS)):
            if self.manual_turns != start_manual:
                return True
            if self.agent_spoke and self.tick - self.agent_play_until >= AGENT_TURN_GAP:
                self.agent_spoke = False
                return True
            await self._put(mulaw.SILENCE_FRAME)
        return False

    def agent_audio(self, frame: bytes) -> None:
        frames = max(1, len(frame) // mulaw.FRAME_BYTES)
        self.agent_play_until = max(self.agent_play_until, self.tick) + frames
        self.agent_frames += frames
        self.agent_spoke = True

    def clear(self) -> None:
        self.agent_play_until = self.tick
        self.clear_count += 1

    # ---- the script -----------------------------------------------------------------

    async def run(self, connect_delay_s: float) -> None:
        try:
            self._status(CallStatus.INITIATED)
            self._status(CallStatus.RINGING)
            await asyncio.sleep(connect_delay_s / self.speed if self.speed else 0)
            self._t0 = asyncio.get_running_loop().time()
            self._status(CallStatus.IN_PROGRESS)
            self.connected.set()
            await self._play(script.recorded("welcome", script.WELCOME))
            await self._menus()
            await self._hold()
            await self._rep()
        except _Hangup:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mock line crashed for %s", self.session_id)
        finally:
            self.finish()

    async def _menus(self) -> None:
        node, tries = "main", 0
        while node != "queue":
            item = script.NODES[node]
            if isinstance(item, script.Message):
                await self._play(script.message_segment(node))
                node, tries = item.next, 0
                continue
            if tries >= script.MAX_MENU_TRIES:
                await self._play(script.recorded("goodbye", script.NO_INPUT_GOODBYE))
                raise _Hangup
            self._drain_digits()
            await self._play(script.menu_segment(node))
            digit = await self._wait_digit(script.MENU_TIMEOUT_S)
            if digit is not None and digit in item.options:
                node, tries = item.options[digit], 0
            else:
                if digit is not None:
                    await self._play(script.recorded("invalid", script.INVALID_OPTION))
                tries += 1

    async def _hold(self) -> None:
        await self._play(script.recorded("queue:intro", script.QUEUE_INTRO))
        remaining = self.rng.uniform(*self.hold_seconds)
        i = 0
        while remaining > 0:
            chunk = min(remaining, self.hold_chunk_s)
            await self._music(chunk)
            remaining -= chunk
            i += 1
            if remaining <= 0:
                break
            if self.callback_offer and i == 1:
                self._drain_digits()
                seg = script.Segment(
                    "callback:offer", StateCheckLabel.CALLBACK_OFFER, script.CALLBACK_OFFER
                )
                await self._play(seg)
                if await self._wait_digit(5) == "1":
                    await self._take_callback_number()
            else:
                text = script.HOLD_ANNOUNCEMENTS[(i - 1) % len(script.HOLD_ANNOUNCEMENTS)]
                await self._play(script.recorded(f"hold:announce:{i}", text))

    async def _take_callback_number(self) -> None:
        await self._play(
            script.Segment(
                "callback:ask", StateCheckLabel.CALLBACK_OFFER, script.CALLBACK_ASK_NUMBER
            )
        )
        for _ in range(16):  # number, never stored, until '#'
            if await self._wait_digit(15) in (None, "#"):
                break
        await self._play(script.recorded("callback:confirm", script.CALLBACK_CONFIRM))
        raise _Hangup

    async def _rep(self) -> None:
        await self._play(script.recorded("connecting", script.CONNECTING))
        for turn, line in enumerate(script.REP_SCRIPT):
            self.agent_spoke = False
            await self._play(script.rep_segment(turn))
            if line.hangup_after:
                raise _Hangup
            for heard in range(line.expects_turns):
                nudges = 0
                # Later turns may wait on an ask_user answer, so be patient.
                while not await self._wait_agent_turn(15 if heard == 0 else 75):
                    nudges += 1
                    if nudges > 2:
                        raise _Hangup
                    await self._play(
                        script.Segment(f"rep:{turn}:nudge", StateCheckLabel.HUMAN, script.REP_NUDGE)
                    )
                if heard + 1 < line.expects_turns:
                    await self._play(
                        script.Segment(
                            f"rep:{turn}:wait", StateCheckLabel.HUMAN, script.REP_TAKE_YOUR_TIME
                        )
                    )

    # ---- lifecycle -------------------------------------------------------------------

    def _status(self, status: CallStatus) -> None:
        if not self.ended.is_set():
            self.statuses.put_nowait(
                CallStatusUpdate(session_id=self.session_id, call_sid=self.call_sid, status=status)
            )

    def finish(self) -> None:
        if self.ended.is_set():
            return
        self._status(CallStatus.COMPLETED)
        self.ended.set()
        self.connected.clear()
        if self.inbound.full():
            self.inbound.get_nowait()
        self.inbound.put_nowait(None)
        self.segments.put_nowait(None)
        self.statuses.put_nowait(None)


class MockTelephonyClient(CallControl):
    line_cls: type[MockLine] = MockLine

    def __init__(
        self,
        *,
        speed: float = 1.0,
        hold_seconds: tuple[float, float] = (30.0, 120.0),
        callback_offer: bool | float = 0.3,
        seed: int | None = None,
        connect_delay_s: float = 1.0,
        hold_chunk_s: float = 20.0,
    ) -> None:
        self.speed = speed
        self.line_speed = speed if speed > 0 else float("inf")
        self.hold_seconds = hold_seconds
        self.callback_offer = callback_offer
        self.rng = random.Random(seed)
        self.connect_delay_s = connect_delay_s
        self.hold_chunk_s = hold_chunk_s
        self.lines: dict[str, MockLine] = {}
        self.expected_callbacks: list[str] = []

    def line(self, session_id: str) -> MockLine:
        line = self.lines.get(session_id)
        if line is None:
            raise TelephonyError(f"no mock call for session {session_id}")
        return line

    # ---- contract -----------------------------------------------------------------

    async def place_call(self, session: CallSession) -> str:
        offer = self.callback_offer
        if not isinstance(offer, bool):
            offer = self.rng.random() < offer
        line = self.line_cls(
            session.id,
            speed=self.speed,
            hold_seconds=self.hold_seconds,
            callback_offer=offer,
            rng=random.Random(self.rng.random()),
            hold_chunk_s=self.hold_chunk_s,
        )
        old = self.lines.get(session.id)
        if old and old.task:
            old.task.cancel()
        self.lines[session.id] = line
        line.task = asyncio.create_task(line.run(self.connect_delay_s))
        return line.call_sid

    async def audio_in(self, session_id: str) -> AsyncIterator[bytes]:
        line = self.line(session_id)
        while True:
            frame = await line.inbound.get()
            if frame is None:
                return
            yield frame

    async def send_audio(self, session_id: str, mulaw_frame: bytes) -> None:
        line = self.lines.get(session_id)
        if line is not None and line.connected.is_set():
            line.agent_audio(mulaw_frame)

    async def clear_audio(self, session_id: str) -> None:
        if (line := self.lines.get(session_id)) is not None:
            line.clear()

    async def press_digits(self, session_id: str, digits: str) -> None:
        if not _DIGITS.match(digits):
            raise ValueError("digits must match " + DIGITS_PATTERN)
        line = self.line(session_id)
        for d in digits:
            if d not in "wW":
                line.digits_pressed.append(d)
                line.digits.put_nowait(d)

    async def start_takeover(self, session_id: str) -> None:
        line = self.line(session_id)
        line.takeover = True
        line.clear()

    async def end_takeover(self, session_id: str) -> None:
        self.line(session_id).takeover = False

    async def hang_up(self, session_id: str) -> None:
        line = self.lines.get(session_id)
        if line is None:
            return
        if line.task and not line.task.done():
            line.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await line.task
        line.finish()

    # ---- extras ----------------------------------------------------------------------

    async def wait_connected(self, session_id: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        line = self.line(session_id)
        try:
            await asyncio.wait_for(line.connected.wait(), timeout)
        except TimeoutError:
            return False
        return not line.ended.is_set()

    async def mark(self, session_id: str, name: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        line = self.lines.get(session_id)
        if line is None or line.ended.is_set():
            return False
        target, clears = line.agent_play_until, line.clear_count
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while line.tick < target:
            if line.clear_count != clears or line.ended.is_set() or loop.time() > deadline:
                return False
            await asyncio.sleep(0.005)
        return True

    async def status_updates(self, session_id: str) -> AsyncIterator[CallStatusUpdate]:
        line = self.line(session_id)
        while True:
            update = await line.statuses.get()
            if update is None:
                return
            yield update

    def is_taken_over(self, session_id: str) -> bool:
        line = self.lines.get(session_id)
        return bool(line and line.takeover)

    def set_takeover_bridge(self, session_id: str, phone_number: str | None) -> None:
        if (line := self.lines.get(session_id)) is not None:
            line.bridge_number = phone_number

    def expect_callback(self, session_id: str) -> None:
        self.expected_callbacks.append(session_id)

    # ---- mock-only helpers ----------------------------------------------------------

    async def remote_segments(self, session_id: str) -> AsyncIterator[MockSegment]:
        """What the line is saying, with ground-truth labels (feed a mock STT/Jev)."""
        line = self.line(session_id)
        while True:
            seg = await line.segments.get()
            if seg is None:
                return
            yield seg

    def advance_rep(self, session_id: str) -> None:
        """Count one agent turn without audio (for tests driving the rep by hand)."""
        self.line(session_id).manual_turns += 1
