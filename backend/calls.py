"""Runs one live call end to end: wires telephony, audio, STT, the call engine,
the conversation agent, and TTS together (CLAUDE.md section 3).

    telephony.audio_in ─► AudioPipeline ─► engine.on_audio_label
                               └─► STT.feed ─► transcripts ─► captions + engine.on_transcript
    engine (IN_CONVERSATION) ─► agent.on_rep_turn ─► guard ─► TTS ─► telephony.send_audio

Each area keeps its own logic; this module only connects them, owns the per-call
tasks, and does the end-of-call bookkeeping (summary, minutes saved, DB row).
Every external client falls back to its mock twin when its credentials are
missing, so a call always runs end to end (against the fake line when mocked).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from backend.agent import conversation
from backend.agent.helpers import caption_summary, post_call_summary
from backend.agent.llm import LLMClient, MockLLMClient
from backend.agent.ports import NotifierUserChannel
from backend.audio.echo import EchoGate
from backend.audio.pipeline import AudioPipeline
from backend.audit import log as audit
from backend.config import Settings, get_settings
from backend.contracts import (
    TERMINAL_STATES,
    CallEvent,
    CallSession,
    CallState,
    DecisionClient,
    LiveMessage,
    Notifier,
    Speaker,
    STTClient,
    Transcript,
    TTSClient,
    utcnow,
)
from backend.engine import states as st
from backend.engine.call_engine import CallEngine, EngineDeps
from backend.engine.menu import DbMenuPathStore
from backend.notify.live import hub
from backend.speech import playback
from backend.telephony.base import CallControl

log = logging.getLogger("call4me.calls")

SPEECH_MARK_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------- #
# Client selection
# --------------------------------------------------------------------------- #


@dataclass
class Clients:
    telephony: CallControl
    stt: STTClient
    tts: TTSClient
    decisions: DecisionClient
    notifier: Notifier
    llm: LLMClient


def _llm(settings: Settings) -> LLMClient:
    if settings.use_mocks:
        return MockLLMClient()
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY not set; using MockLLMClient")
        return MockLLMClient()
    from backend.agent.llm import AnthropicLLMClient

    return AnthropicLLMClient()


def _decisions(settings: Settings) -> DecisionClient:
    from backend.engine.jev_checks import JevDecisionClient, MockDecisionClient

    if settings.use_mocks or not settings.typesafe_api_key:
        if not settings.use_mocks:
            log.warning("TYPESAFE_API_KEY not set; using keyword heuristics for Jev decisions")
        return MockDecisionClient()
    return JevDecisionClient()


def default_clients(settings: Settings | None = None) -> Clients:
    from backend.notify.apns import get_notifier
    from backend.speech import make_stt_client, make_tts_client
    from backend.telephony import get_telephony

    settings = settings or get_settings()
    return Clients(
        telephony=get_telephony(),
        stt=make_stt_client(settings),
        tts=make_tts_client(settings),
        decisions=_decisions(settings),
        notifier=get_notifier(),
        llm=_llm(settings),
    )


# --------------------------------------------------------------------------- #
# The agent's view of the call (backend.agent.ports.CallControl)
# --------------------------------------------------------------------------- #


class AgentControl:
    """Speech, digits, and state changes for the conversation agent.

    State changes are routed to the engine's own commands so every transition
    still goes through `engine/states.py` with the engine's side effects
    (bridging on takeover, shutdown on end).
    """

    def __init__(self, call: LiveCall) -> None:
        self.call = call
        self._speaking: asyncio.Task[int] | None = None
        self._lock = asyncio.Lock()

    async def speak(self, session_id: str, text: str) -> None:
        call = self.call
        async with self._lock:
            if call.session.state == CallState.TAKEOVER and not call.user_speaking:
                return
            self._speaking = asyncio.ensure_future(
                playback.speak(
                    call.clients.tts.synthesize(text),
                    lambda f: call.clients.telephony.send_audio(session_id, f),
                    echo=call.echo,
                    text=text,
                )
            )
            task = self._speaking
            try:
                # wait() rather than await: stop_speaking() cancelling the playback
                # must not look like this caller being cancelled.
                await asyncio.wait({task})
            finally:
                self._speaking = None
            if task.cancelled():
                return
            task.result()
        # Return once the rep has actually heard it, so holding lines land before we wait.
        with contextlib.suppress(Exception):
            await call.clients.telephony.mark(session_id, "say", timeout=SPEECH_MARK_TIMEOUT_S)

    async def stop_speaking(self, session_id: str) -> None:
        task = self._speaking
        if task is not None and not task.done():
            task.cancel()
        await self.call.clients.telephony.clear_audio(session_id)
        self.call.echo.clear()

    async def press_digits(self, session_id: str, digits: str) -> None:
        await self.call.clients.telephony.press_digits(session_id, digits)

    async def hang_up(self, session_id: str) -> None:
        await self.call.clients.telephony.hang_up(session_id)

    async def transition(
        self, session: CallSession, to_state: CallState, reason: str, confidence: float = 1.0
    ) -> None:
        engine = self.call.engine
        if to_state == CallState.WAITING_ON_USER:
            await engine.waiting_on_user(reason)
        elif to_state == CallState.IN_CONVERSATION:
            if engine.state == CallState.TAKEOVER:
                await engine.handback()
            else:
                await engine.user_answered(reason)
        elif to_state == CallState.TAKEOVER:
            await self.call.takeover()
        elif to_state == CallState.ENDED:
            await engine.end(reason, outcome="completed", hang_up=False, actor="agent")
        elif to_state == CallState.FAILED:
            await engine.fail(reason)
        else:
            log.warning("agent asked for unsupported transition to %s", to_state)


# --------------------------------------------------------------------------- #
# One live call
# --------------------------------------------------------------------------- #


@dataclass
class _Timing:
    takeover_started: float | None = None
    takeover_s: float = 0.0
    transcript: list[str] = field(default_factory=list)


class LiveCall:
    def __init__(
        self,
        session: CallSession,
        clients: Clients,
        *,
        callback_number: str = "",
        recorder: st.TransitionRecorder | None = None,
        persist: bool = True,
    ) -> None:
        self.session = session
        self.clients = clients
        self.persist = persist
        self.echo = EchoGate()
        self.control = AgentControl(self)
        self.recorder = recorder or st.DbRecorder()
        self.agent: conversation.ConversationAgent | None = None
        self.user_speaking = False
        self.finished = asyncio.Event()
        self._timing = _Timing()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._finishing = False
        self.engine = CallEngine(
            session,
            EngineDeps(
                telephony=clients.telephony,
                stt=clients.stt,
                tts=clients.tts,
                decisions=clients.decisions,
                notifier=clients.notifier,
                recorder=self.recorder,
                menu_paths=DbMenuPathStore() if persist else _NoMenuPaths(),
                callback_number=callback_number,
                on_conversation_start=self._on_conversation_start,
                on_remote_turn=self._on_remote_turn,
                on_state_change=self._on_state_change,
            ),
        )
        self.pipeline = AudioPipeline(session.id, clients.stt, echo=self.echo)

    @property
    def id(self) -> str:
        return self.session.id

    # ------------------------------------------------------------- lifecycle

    async def start(self, *, place_call: bool = True) -> None:
        tel = self.clients.telephony
        await self.clients.stt.start(self.id)
        # The greeting is written by Claude and pre-synthesized while we dial (section 6).
        self._spawn(self._prepare_greeting())
        if place_call:
            try:
                await tel.place_call(self.session)
            except Exception as e:  # noqa: BLE001 - a dial failure ends the call, not the server
                log.exception("place_call failed for %s", self.id)
                await self.engine.fail(f"could not place the call: {type(e).__name__}")
                await self._finish()
                return
        self._spawn(self._pump_audio())
        self._spawn(self._pump_transcripts())
        self._spawn(self._pump_status())
        self._spawn(self.engine.run())
        if hasattr(tel, "remote_segments") and hasattr(self.clients.stt, "inject"):
            # Mock line + mock STT: the line's script text stands in for recognized speech.
            self._spawn(self._pump_mock_segments())

    async def _prepare_greeting(self) -> None:
        try:
            self.engine.greeting_text = await conversation.build_greeting(
                self.session, self.clients.llm
            )
        except Exception:  # noqa: BLE001 - falls back to the disclosure alone
            log.exception("greeting generation failed")
        await self.engine.start()

    # ------------------------------------------------------------- pumps

    async def _pump_audio(self) -> None:
        async for frame in self.clients.telephony.audio_in(self.id):
            for label in await self.pipeline.push(frame):
                await self.engine.on_audio_label(label)
            if self.engine.done:
                break

    async def _pump_transcripts(self) -> None:
        async for t in self.clients.stt.transcripts(self.id):
            if t.speaker is Speaker.REMOTE and self.echo.is_echo(t.text):
                continue  # our own TTS heard back on the line
            await self._caption(t)
            await self.engine.on_transcript(t)
            if self.engine.done:
                break

    async def _pump_status(self) -> None:
        async for update in self.clients.telephony.status_updates(self.id):
            hub.publish(
                LiveMessage(
                    type="event",
                    session_id=self.id,
                    data={"kind": "call_status", "status": update.status.value},
                )
            )
            await self.engine.on_call_status(update.status.value)
            if self.engine.done:
                break
        # The line is gone (e.g. remote hang-up with no status callback).
        if not self.engine.done:
            await self.engine.end("line closed", outcome="remote_hangup", hang_up=False)

    async def _pump_mock_segments(self) -> None:
        tel: Any = self.clients.telephony
        stt: Any = self.clients.stt
        async for seg in tel.remote_segments(self.id):
            if seg.text:
                stt.inject(self.id, seg.text, partials=False)

    async def _caption(self, t: Transcript) -> None:
        # Captions may contain a PIN the rep read back: redact before anything leaves.
        text = audit.redact(t.text)
        if not text.strip():
            return
        hub.publish(
            LiveMessage(
                type="caption",
                session_id=self.id,
                data={"speaker": t.speaker.value, "text": text, "final": t.is_final},
            )
        )
        if not t.is_final:
            return
        self._timing.transcript.append(f"{t.speaker.value}: {text}")
        if self.persist:
            await self._record_event("caption", {"speaker": t.speaker.value, "text": text})
        if self.session.state in (CallState.IN_CONVERSATION, CallState.WAITING_ON_USER):
            self._spawn(self._plain_summary(text))

    async def _plain_summary(self, text: str) -> None:
        """Plain-language one-liner of the rep's words (English may be a second language)."""
        summary = await caption_summary(self.clients.llm, text)
        if summary and summary != text:
            hub.publish(
                LiveMessage(
                    type="summary", session_id=self.id, data={"text": summary, "kind": "caption"}
                )
            )

    # ------------------------------------------------------------- engine hooks

    async def _on_conversation_start(self, session: CallSession, reply: str) -> None:
        if self.agent is None:
            ports = conversation.default_ports(self.control)
            ports.user = NotifierUserChannel(self.clients.notifier)
            ports.audit = _LiveDenials(ports.audit, session.id)
            self.agent = conversation.start_agent(session, ports, self.clients.llm)
            self.agent.runtime.spoken.append(audit.redact(self.engine.greeting_text))
            if reply:
                self._spawn(self.agent.on_rep_turn(reply))
        else:
            await self.agent.handback()

    async def _on_remote_turn(self, t: Transcript) -> None:
        if self.agent is not None:
            # Don't block the engine: it must keep spotting transfers while Claude thinks.
            self._spawn(self.agent.on_rep_turn(t.text))

    async def _on_state_change(self, before: CallState, after: CallState) -> None:
        loop = asyncio.get_running_loop()
        if after == CallState.TAKEOVER:
            self._timing.takeover_started = loop.time()
        elif before == CallState.TAKEOVER and self._timing.takeover_started is not None:
            self._timing.takeover_s += loop.time() - self._timing.takeover_started
            self._timing.takeover_started = None
        if after in TERMINAL_STATES:
            self._spawn(self._finish())

    # ------------------------------------------------------------- user commands

    async def takeover(self) -> bool:
        """Rule 6: the agent stops speaking at once, then the engine bridges the user."""
        await self.control.stop_speaking(self.id)
        if self.agent is not None:
            self.agent.runtime.takeover.set()
        changed = await self.engine.takeover()
        if self.agent is not None and changed:
            await self.agent.ports.audit(
                actor="user",
                action="agent.takeover",
                subject=self.id,
                reason="agent stopped; user is typing",
            )
        return changed

    async def handback(self) -> bool:
        if self.agent is not None:
            self.agent.runtime.takeover.clear()
        # The engine calls _on_conversation_start, which resumes the agent.
        return await self.engine.handback()

    async def type_text(self, text: str) -> None:
        """Type-to-talk: speak the user's own words verbatim (not the agent, so no guard)."""
        self.user_speaking = True
        try:
            await self.control.speak(self.id, text)
        finally:
            self.user_speaking = False
        hub.publish(
            LiveMessage(
                type="caption",
                session_id=self.id,
                data={"speaker": Speaker.USER.value, "text": audit.redact(text), "final": True},
            )
        )
        self._timing.transcript.append("user: [typed to the rep]")
        if self.agent is not None:
            self.agent.on_user_typed(text)

    async def cancel(self) -> None:
        await self.control.stop_speaking(self.id)
        await self.engine.end("user cancelled", outcome="user_cancelled", actor="user")

    async def continue_hold(self, keep_waiting: bool) -> None:
        await self.engine.continue_hold(keep_waiting)

    # ------------------------------------------------------------- end of call

    async def _finish(self) -> None:
        if self._finishing:
            return
        self._finishing = True
        try:
            await self._wrap_up()
        except Exception:  # noqa: BLE001
            log.exception("wrap-up failed for %s", self.id)
        finally:
            for task in list(self._tasks):
                if task is not asyncio.current_task():
                    task.cancel()
            if self.agent is not None:
                conversation.stop_agent(self.id)
            else:
                audit.forget_session(self.id)
            registry.pop(self.id, None)
            self.finished.set()

    async def _wrap_up(self) -> None:
        s = self.session
        if self._timing.takeover_started is not None:
            loop = asyncio.get_running_loop()
            self._timing.takeover_s += loop.time() - self._timing.takeover_started
        s.ended_at = utcnow()
        total_s = (s.ended_at - s.started_at).total_seconds()
        # Minutes the user didn't have to spend on the phone: all of it except their takeover.
        s.minutes_saved = round(max(0.0, total_s - self._timing.takeover_s) / 60, 1)

        agent_ended = self.agent is not None and self.agent.runtime.ended
        if s.reference_number is None and self.agent is not None:
            # The rep may hang up right after the read-back, before end_call runs.
            s.reference_number = _read_back_reference(self.agent.runtime.spoken)
        if not agent_ended and s.outcome is None:
            code = self.engine.outcome or s.state.value.lower()
            s.outcome = OUTCOME_TEXT.get(code, code.replace("_", " "))
        summary = await post_call_summary(self.clients.llm, s, self._timing.transcript)

        if self.persist:
            await self._save_session()
            await self._record_event(
                "call.ended",
                {"outcome": audit.redact(s.outcome or ""), "minutes_saved": s.minutes_saved},
            )
        data = {
            "summary": summary,
            "outcome": audit.redact(s.outcome or ""),
            "reference_number": s.reference_number,
            "minutes_saved": s.minutes_saved,
            "state": s.state.value,
        }
        hub.publish(LiveMessage(type="summary", session_id=self.id, data={"text": summary, **data}))
        hub.publish(LiveMessage(type="ended", session_id=self.id, data=data))
        if agent_ended:
            # Failures and voicemail already alerted from the engine.
            with contextlib.suppress(Exception):
                await self.clients.notifier.alert(
                    s.user_id,
                    "Call done",
                    summary,
                    category="CALL_ENDED",
                    data={"session_id": self.id, "haptic": True},
                )
        if self.engine.outcome == "callback_scheduled":
            self.clients.telephony.expect_callback(self.id)
        _record_metrics(self)

    async def _save_session(self) -> None:
        from backend.db.models import CallSessionRow
        from backend.db.session import sessionmaker

        s = self.session
        async with sessionmaker()() as db:
            row = await db.get(CallSessionRow, s.id)
            if row is None:
                return
            row.state = s.state
            row.ended_at = s.ended_at
            row.outcome = audit.redact(s.outcome) if s.outcome else None
            row.reference_number = s.reference_number
            row.minutes_saved = s.minutes_saved
            await audit.append(
                db,
                actor="system",
                action="call.end",
                subject=s.id,
                reason=f"call finished in {s.state.value}",
                diff={"outcome": row.outcome, "minutes_saved": s.minutes_saved},
            )
            await db.commit()

    async def _record_event(self, kind: str, detail: dict[str, Any]) -> None:
        from backend.db.session import sessionmaker

        try:
            async with sessionmaker()() as db:
                await audit.record_event(
                    db,
                    CallEvent(
                        session_id=self.id,
                        kind=kind,
                        from_state=self.session.state,
                        to_state=self.session.state,
                        detail=detail,
                    ),
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - the timeline is best effort; the call goes on
            log.exception("could not record %s", kind)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("call task failed for %s", self.id, exc_info=task.exception())


class _LiveDenials:
    """Audit sink that also shows guard denials live (dashboard, demo step 5)."""

    def __init__(self, inner: Any, session_id: str) -> None:
        self.inner = inner
        self.session_id = session_id

    async def __call__(self, **kwargs: Any) -> None:
        await self.inner(**kwargs)
        if kwargs.get("actor") == "guard" and not kwargs.get("allowed", True):
            evidence = kwargs.get("evidence") or {}
            hub.publish(
                LiveMessage(
                    type="event",
                    session_id=self.session_id,
                    data={
                        "kind": "guard_denied",
                        "action": evidence.get("kind"),
                        "reason": kwargs.get("reason", ""),
                        "attempt": audit.redact(evidence.get("attempt", "")),
                    },
                )
            )


class _NoMenuPaths:
    async def load(self, company: str) -> list[str] | None:
        return None

    async def save_success(self, company: str, steps: list[str]) -> None:
        return None


# Plain words for the summary screen (English may be a second language).
OUTCOME_TEXT = {
    "remote_hangup": "The company ended the call.",
    "user_cancelled": "You stopped the call.",
    "callback_scheduled": "They will call back. We will answer for you.",
    "voicemail": "No one answered. We reached voicemail.",
    "failed": "The call could not be completed.",
    "ended": "The call ended.",
}

_REFERENCE_RE = re.compile(r"\breference(?: number)? is ([A-Z0-9-]{4,})", re.I)


def _read_back_reference(spoken: list[str]) -> str | None:
    for line in reversed(spoken):
        if m := _REFERENCE_RE.search(line):
            return m.group(1).upper()
    return None


def _record_metrics(call: LiveCall) -> None:
    try:
        from backend.telemetry import record_metric
    except ImportError:
        return
    s = call.session
    ok = s.state == CallState.ENDED and call.agent is not None and call.agent.runtime.ended
    record_metric("call.completed", 1.0 if ok else 0.0, state=s.state.value)
    if s.minutes_saved is not None:
        record_metric("call.minutes_saved", s.minutes_saved)
    if call.engine.time_to_greet_s is not None:
        record_metric("call.time_to_greet_s", call.engine.time_to_greet_s)


# --------------------------------------------------------------------------- #
# Registry used by the API
# --------------------------------------------------------------------------- #

registry: dict[str, LiveCall] = {}


def get_call(session_id: str) -> LiveCall | None:
    return registry.get(session_id)


async def start_call(
    session: CallSession,
    clients: Clients | None = None,
    *,
    place_call: bool = True,
    persist: bool = True,
) -> LiveCall:
    settings = get_settings()
    call = LiveCall(
        session,
        clients or default_clients(settings),
        callback_number=settings.twilio_agent_number,
        persist=persist,
    )
    registry[session.id] = call
    await call.start(place_call=place_call)
    return call


async def watch_incoming_callbacks() -> None:
    """Answer company callbacks on the agent number (CLAUDE.md section 6).

    A callback matched to a session restarts that session's call as if a person
    picked up: the engine hears the rep, greets, and the agent continues the goal.
    """
    from backend.db.models import CallSessionRow
    from backend.db.session import sessionmaker
    from backend.main import _to_session
    from backend.telephony.streams import registry as streams

    while True:
        incoming = await streams.incoming.get()
        if not incoming.matched_callback or incoming.session_id in registry:
            continue
        try:
            async with sessionmaker()() as db:
                row = await db.get(CallSessionRow, incoming.session_id)
                if row is None:
                    continue
                row.state = CallState.DIALING
                row.ended_at = None
                await audit.append(
                    db,
                    actor="twilio",
                    action="call.callback_answered",
                    subject=row.id,
                    reason="company called back on the agent number",
                )
                await db.commit()
                session = _to_session(row)
            await start_call(session, place_call=False)
        except Exception:  # noqa: BLE001
            log.exception("could not resume session %s for a callback", incoming.session_id)
