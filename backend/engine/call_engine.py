"""Per-call engine: drives the state machine from transcripts, audio labels and Jev.

Inputs (wired at merge):
- `on_transcript(Transcript)` from the STT client (remote side only matters),
- `on_audio_label(AudioLabel)` from the audio pipeline (~every 2 s),
- `tick()` every `check_interval_s` (`run()` loops it),
- user / agent commands: `takeover()`, `handback()`, `waiting_on_user()`,
  `user_answered()`, `continue_hold()`, `end()`, `fail()`, `on_call_status()`.

Outputs go through the contracts clients only (TelephonyClient, STTClient,
TTSClient, DecisionClient, Notifier) plus hooks for the conversation agent.
Every state change goes through `states.transition()`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from backend.contracts import (
    TERMINAL_STATES,
    AudioLabel,
    AudioLabelKind,
    CallEvent,
    CallSession,
    CallState,
    DecisionClient,
    JevDecision,
    Notifier,
    Speaker,
    STTClient,
    TelephonyClient,
    Transcript,
    TTSClient,
)
from backend.engine import jev_checks
from backend.engine import states as st
from backend.engine.menu import (
    MemoryMenuPathStore,
    MenuAction,
    MenuNavigator,
    MenuParser,
    MenuPathStore,
    RegexMenuParser,
    decline_callback_options,
    parse_menu_options,
)
from backend.engine.timers import CallTimers, TimerEvent

log = logging.getLogger(__name__)

S = CallState

# States where we are still waiting to reach a person; Jev state checks run here.
PRE_HUMAN_STATES = frozenset({S.DIALING, S.MENU, S.HOLD, S.RECORDED_MESSAGE, S.CALLBACK_OFFERED})
CONVERSATION_STATES = frozenset({S.IN_CONVERSATION, S.WAITING_ON_USER})
WAITING_STATES = frozenset({S.HOLD, S.RECORDED_MESSAGE})

_LABEL_TO_STATE: dict[str, CallState] = {
    "menu": S.MENU,
    "recorded_message": S.RECORDED_MESSAGE,
    "hold_music": S.HOLD,
    "callback_offer": S.CALLBACK_OFFERED,
    "voicemail": S.VOICEMAIL,
    "call_ended": S.ENDED,
}

TRANSFER_RE = re.compile(
    r"(transfer(ring)? you|put you (on|through)|connect(ing)? you (to|with)|"
    r"please hold (while|for a moment|one moment)|let me get (my|a) (supervisor|manager|colleague)|"
    r"one moment while i)"
)
NUMBER_PROMPT_RE = re.compile(
    r"((enter|say|tell us|key in|provide).{0,40}(phone|telephone|callback|call back) number|"
    r"number (where|at which) (we|you) (can|may|should))"
)
CALLBACK_ACCEPT_GOAL = "accept the offer to receive a call back instead of waiting"
CALLBACK_DECLINE_GOAL = "decline the offer: stay on the line and keep holding for a live person"

GREETING_KEY_PREFIX = "greeting:"

ConversationStart = Callable[[CallSession, str], Awaitable[None]]
RemoteTurn = Callable[[Transcript], Awaitable[None]]
StateChange = Callable[[CallState, CallState], Awaitable[None]]


@dataclass
class EngineConfig:
    check_interval_s: float = 2.5
    window_s: float = 10.0
    state_threshold: float = 0.6  # minimum Jev probability to change state
    # Greeting fast path. Moderate on purpose: the rep must still answer the greeting
    # (confirm step), so a false alarm only costs one greeting over a recording.
    human_threshold: float = 0.6
    human_streak_threshold: float = 0.45  # two "human" checks in a row at this level also count
    live_person_threshold: float = 0.55  # yes/no "live person?" on each utterance while waiting
    terminal_threshold: float = 0.8  # voicemail / call_ended end the call, so be sure
    confirm_threshold: float = 0.6
    # Replaying a saved route presses keys without listening, and STT words each menu a
    # bit differently, so a looping route can look clean when saved. Off: Jev picks
    # each option from what it hears. Routes are still saved (insight, future hints).
    replay_menu_paths: bool = False
    menu_settle_s: float = 2.5  # wait this long after the last menu words before acting
    confirm_timeout_s: float = 8.0  # no reply to the greeting -> back to hold
    hold_music_pause_threshold: float = 0.8
    hold_extend_s: float = 15 * 60
    jev_timeout_s: float = 2.0
    callback_policy: str = "accept"  # "accept" (give the agent's number) or "decline"


@dataclass
class EngineDeps:
    telephony: TelephonyClient
    stt: STTClient
    tts: TTSClient
    decisions: DecisionClient
    notifier: Notifier
    recorder: st.TransitionRecorder
    menu_parser: MenuParser = field(default_factory=RegexMenuParser)
    menu_paths: MenuPathStore = field(default_factory=MemoryMenuPathStore)
    callback_number: str = ""  # number to give for a callback; none on our pipeline
    on_conversation_start: ConversationStart | None = None
    on_remote_turn: RemoteTurn | None = None
    on_state_change: StateChange | None = None
    clock: Callable[[], float] = time.monotonic


class CallEngine:
    def __init__(
        self,
        session: CallSession,
        deps: EngineDeps,
        config: EngineConfig | None = None,
        *,
        greeting_text: str | None = None,
    ) -> None:
        self.session = session
        self.deps = deps
        self.config = config or EngineConfig()
        self.greeting_text = greeting_text or session.disclosure_text
        self.greeting_key = GREETING_KEY_PREFIX + session.id
        self.clock = deps.clock
        self.timers = CallTimers(
            max_hold_s=session.limits.max_hold_minutes * 60.0, clock=self.clock
        )
        self.navigator = MenuNavigator(
            decisions=deps.decisions, goal=session.goal, jev_timeout_s=self.config.jev_timeout_s
        )
        self.decision_count = 0
        self.audio_label: AudioLabel | None = None
        self.stt_paused = False
        self.callback_accepted = False
        self.outcome: str | None = None
        self.time_to_greet_s: float | None = None

        self._finals: deque[tuple[float, str]] = deque()
        self._human_streak = 0  # consecutive moderate "human" state checks
        self._partial = ""
        self._last_remote_at: float | None = None
        self._menu_buffer: list[str] = []
        self._menu_last_at: float | None = None
        self._number_given = False
        self._check_lock = asyncio.Lock()
        self._greeting_task: asyncio.Task[None] | None = None
        self._greeting_started_at: float | None = None
        self._greeting_ends_at: float | None = None
        self._suspected_from: CallState = S.HOLD
        self._confirm_buffer: list[str] = []
        self._pre_takeover: CallState | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CallState:
        return self.session.state

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    async def start(self) -> None:
        """Before the call connects: load a saved route and pre-generate the greeting."""
        if self.config.replay_menu_paths:
            try:
                self.navigator.saved_path = await self.deps.menu_paths.load(self.session.company)
            except Exception:  # noqa: BLE001 - menu memory is polish, never block a call on it
                log.exception("could not load menu path")
        try:
            await self.deps.tts.pregenerate(self.greeting_key, self.greeting_text)
        except Exception:  # noqa: BLE001 - we fall back to live synthesis at greet time
            log.exception("greeting pre-generation failed")

    async def run(self) -> None:
        """Run periodic checks until the call reaches a terminal state."""
        while not self.done:
            await self.tick()
            await asyncio.sleep(self.config.check_interval_s)

    async def consume_transcripts(self, stream: AsyncIterator[Transcript]) -> None:
        async for t in stream:
            await self.on_transcript(t)
            if self.done:
                break

    async def consume_audio_labels(self, stream: AsyncIterator[AudioLabel]) -> None:
        async for label in stream:
            await self.on_audio_label(label)
            if self.done:
                break

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #

    async def on_audio_label(self, label: AudioLabel) -> None:
        if self.done:
            return
        self.audio_label = label
        if label.label in (AudioLabelKind.SPEECH, AudioLabelKind.MUSIC):
            self.timers.heard_sound()
        # Pause STT during confirmed hold music (cost); the label keeps flowing.
        if self.state is S.HOLD and self._confirmed_music():
            await self._pause_stt()
        elif label.label is AudioLabelKind.SPEECH:
            await self._resume_stt()

    def _confirmed_music(self) -> bool:
        label = self.audio_label
        return (
            label is not None
            and label.label is AudioLabelKind.MUSIC
            and label.confidence >= self.config.hold_music_pause_threshold
        )

    async def on_transcript(self, t: Transcript) -> None:
        if self.done or t.speaker is not Speaker.REMOTE:
            return
        now = self.clock()
        self.timers.heard_sound()
        self._last_remote_at = now
        if not t.is_final:
            self._partial = t.text
            return
        self._partial = ""
        text = t.text.strip()
        if not text:
            return
        self._finals.append((now, text))

        if (
            self.callback_accepted
            and not self._number_given
            and NUMBER_PROMPT_RE.search(text.lower())
        ):
            await self._give_callback_number()
            return

        if self.state is S.HUMAN_SUSPECTED:
            await self._on_greeting_reply(text)
            return
        if self.state in CONVERSATION_STATES:
            if TRANSFER_RE.search(text.lower()):
                await self._go(S.HOLD, "transfer announced", 0.9, {"phrase": text})
                return
            if self.deps.on_remote_turn is not None:
                await self.deps.on_remote_turn(t)
            return
        if self.state is S.MENU:
            self._menu_buffer.append(text)
            self._menu_last_at = now
        if self.state in WAITING_STATES and await self._check_live_person(text):
            return
        if self.state in PRE_HUMAN_STATES:
            # Don't wait for the next tick: a rep's "hello" must be answered fast.
            await self.check_state(reason="new speech")

    async def tick(self) -> None:
        if self.done:
            return
        for event in self.timers.due():
            if event is TimerEvent.SILENCE_RECHECK:
                await self.check_state(reason="silence > 30 s re-check", force=True)
            elif event is TimerEvent.MAX_HOLD:
                await self._max_hold_reached()
        if self.done:
            return
        if self.state is S.HUMAN_SUSPECTED:
            await self._check_confirm_timeout()
        elif self.state in PRE_HUMAN_STATES:
            await self.check_state(reason="periodic")
            if self.state is S.MENU:
                await self._maybe_act_on_menu()

    # ------------------------------------------------------------------ #
    # Jev state check
    # ------------------------------------------------------------------ #

    def window_text(self) -> str:
        cutoff = self.clock() - self.config.window_s
        while self._finals and self._finals[0][0] < cutoff:
            self._finals.popleft()
        parts = [text for _, text in self._finals]
        if self._partial:
            parts.append(self._partial)
        return " ".join(parts)

    async def check_state(self, *, reason: str, force: bool = False) -> JevDecision | None:
        if self.state not in PRE_HUMAN_STATES:
            return None
        if self._check_lock.locked():
            if not force:
                return None
        async with self._check_lock:
            if self.state not in PRE_HUMAN_STATES:
                return None
            decision = await jev_checks.classify_state(
                self.deps.decisions,
                transcript=self.window_text(),
                latest=self._partial or (self._finals[-1][1] if self._finals else ""),
                audio_label=self.audio_label,
                current_state=self.state,
                silence_s=self.timers.silence_seconds,
                timeout_s=self.config.jev_timeout_s,
            )
            await self._record_decision(decision, reason)
            await self._apply_state_decision(decision)
            return decision

    async def _check_live_person(self, latest: str) -> bool:
        """True if a person just started talking (and the greeting is under way)."""
        window = self.window_text()
        previous = window[: max(0, len(window) - len(latest))].strip()
        d = await jev_checks.live_person(
            self.deps.decisions,
            latest=latest,
            previous=previous,
            timeout_s=self.config.jev_timeout_s,
        )
        await self._record_decision(d, "new speech: live person?")
        if self.state not in WAITING_STATES:
            return False
        if d.choice == "live_person" and d.probability >= self.config.live_person_threshold:
            await self._human_detected(d)
            return True
        return False

    async def _apply_state_decision(self, d: JevDecision) -> None:
        label, p = d.choice, d.probability
        if label == "human":
            # One confident call, or two moderate ones in a row: the rep's first words
            # often share the window with the recording before them, which lowers p.
            if p >= self.config.human_threshold or (
                p >= self.config.human_streak_threshold and self._human_streak
            ):
                self._human_streak = 0
                await self._human_detected(d)
            elif p >= self.config.human_streak_threshold:
                self._human_streak += 1
            return
        if label != "silence":
            self._human_streak = 0
        if label == "silence" or p < self.config.state_threshold:
            return
        target = _LABEL_TO_STATE[label]
        reason = f"jev: {label}"
        if target is S.RECORDED_MESSAGE and TRANSFER_RE.search(self.window_text().lower()):
            target, reason = S.HOLD, "transfer announced"
        if target in (S.VOICEMAIL, S.ENDED):
            if p < self.config.terminal_threshold:
                return
            if target is S.ENDED and self.callback_accepted:
                await self.end("callback scheduled", outcome="callback_scheduled", confidence=p)
            elif target is S.VOICEMAIL:
                await self._reached_voicemail(p)
            else:
                await self.end(
                    "remote side ended the call",
                    outcome="remote_hangup",
                    confidence=p,
                    hang_up=False,
                )
            return
        if target is S.CALLBACK_OFFERED and self.callback_accepted:
            return  # already handled this offer; the follow-up prompts are just noise
        if target is S.MENU and self.state is S.CALLBACK_OFFERED:
            return  # the offer's own "press 1" prompt; handled by _handle_callback_offer
        changed = await self._go(target, reason, p)
        if changed and target is S.CALLBACK_OFFERED:
            await self._handle_callback_offer()

    async def _record_decision(self, d: JevDecision, reason: str) -> None:
        self.decision_count += 1
        await self._event(
            "jev_decision",
            {
                "question": d.question,
                "choice": d.choice,
                "probability": round(d.probability, 3),
                "count": self.decision_count,
                "state": self.state,
                "reason": reason,
                "fallback": "fallback" in d.alternatives,
            },
            confidence=d.probability,
            audit_action="jev.decision",
        )

    # ------------------------------------------------------------------ #
    # Menu navigation
    # ------------------------------------------------------------------ #

    async def _maybe_act_on_menu(self) -> None:
        if not self._menu_buffer or self._menu_last_at is None:
            return
        if self._partial or self.clock() - self._menu_last_at < self.config.menu_settle_s:
            return  # still talking: the best option may not have been read yet
        text = " ".join(self._menu_buffer)
        try:
            options = await self.deps.menu_parser.parse(text, goal=self.session.goal)
        except Exception:  # noqa: BLE001 - Claude parse failed; regex still works
            log.exception("menu parser failed; using regex")
            options = {}
        if not options:
            options = parse_menu_options(text)
        action = await self.navigator.decide(options, text)
        if self.navigator.last_decision is not None:
            await self._record_decision(self.navigator.last_decision, "menu choice")
            self.navigator.last_decision = None
        self._menu_buffer.clear()
        self._menu_last_at = None
        if action is not None:
            # The prompt we just answered is stale; don't let it colour the next checks.
            self._finals.clear()
            await self._perform(action, options)

    async def _perform(self, action: MenuAction, options: dict[str, str]) -> None:
        await self._event(
            "menu_action",
            {
                "action": action.kind,
                "value": action.value,
                "reason": action.reason,
                "options": options,
                "step": len(self.navigator.steps),
            },
            confidence=action.probability,
            audit_action="menu.press" if action.kind == "dtmf" else "menu.say",
        )
        if action.kind == "dtmf":
            await self.deps.telephony.press_digits(self.session.id, action.value)
        else:
            await self._speak(action.value)

    async def _speak(self, text: str) -> None:
        async for frame in self.deps.tts.synthesize(text):
            await self.deps.telephony.send_audio(self.session.id, frame)

    # ------------------------------------------------------------------ #
    # Callback offers
    # ------------------------------------------------------------------ #

    async def _handle_callback_offer(self) -> None:
        accept = self.config.callback_policy == "accept" and bool(self.deps.callback_number)
        text = self.window_text()
        options = parse_menu_options(text)
        if not accept:
            options = decline_callback_options(options)
        await self._event(
            "callback_offer",
            {
                "decision": "accept" if accept else "decline",
                "options": options,
                "reason": "policy"
                if accept or not self.deps.callback_number
                else "no agent number",
            },
            audit_action="callback.decide",
        )
        if options:
            d = await jev_checks.choose_menu_option(
                self.deps.decisions,
                goal=CALLBACK_ACCEPT_GOAL if accept else CALLBACK_DECLINE_GOAL,
                menu_options=options,
                transcript=text,
                timeout_s=self.config.jev_timeout_s,
            )
            await self._record_decision(d, "callback choice")
            if d.probability >= self.config.state_threshold:
                await self._perform(
                    MenuAction.from_key(
                        d.choice, f"callback {'accept' if accept else 'decline'}", d.probability
                    ),
                    options,
                )
            else:
                accept = False  # unsure which key accepts: keep waiting rather than guess
        if accept and options:
            self.callback_accepted = True
            await self._notify(
                "Callback requested",
                "They will call back. We will answer for you.",
                category="CALLBACK",
            )
        else:
            await self._go(S.HOLD, "callback declined; keep holding", None)

    async def _give_callback_number(self) -> None:
        digits = re.sub(r"\D", "", self.deps.callback_number)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]  # US IVRs ask for the 10-digit number
        self._number_given = True
        await self._event(
            "callback_number",
            {"reason": "IVR asked for a callback number"},
            audit_action="callback.give_number",
        )
        await self.deps.telephony.press_digits(self.session.id, digits + "#")

    # ------------------------------------------------------------------ #
    # Human detection fast path
    # ------------------------------------------------------------------ #

    async def _human_detected(self, d: JevDecision) -> None:
        detected_at = self.clock()
        self._suspected_from = self.state if self.state in WAITING_STATES else S.HOLD
        if not await self._go(S.HUMAN_SUSPECTED, "jev: human", d.probability):
            return
        await self._resume_stt()
        self._confirm_buffer.clear()
        self._greeting_started_at = self.clock()
        self._greeting_ends_at = None
        self._greeting_task = self._spawn(self._play_greeting(detected_at))

    async def _play_greeting(self, detected_at: float) -> None:
        sent = False
        first_at = 0.0
        seconds = 0.0

        async def stream(frames: AsyncIterator[bytes], source: str) -> None:
            nonlocal sent, first_at, seconds
            async for frame in frames:
                if not sent:
                    sent = True
                    first_at = self.clock()
                    await self._greeting_first_frame(detected_at, source)
                await self.deps.telephony.send_audio(self.session.id, frame)
                # 8 kHz mu-law: one byte per sample.
                seconds += len(frame) / 8000 / getattr(self.deps.telephony, "line_speed", 1.0)
                # Frames are queued faster than real time; the rep hears them until here.
                self._greeting_ends_at = first_at + seconds

        try:
            try:
                await stream(self.deps.tts.play_pregenerated(self.greeting_key), "pregenerated")
            except Exception:
                if sent:
                    raise
                # Cache miss or TTS hiccup before any audio: synthesize live instead.
                log.warning("pre-generated greeting unavailable; synthesizing live")
            if not sent:
                await stream(self.deps.tts.synthesize(self.greeting_text), "live")
            if sent:
                self.session.disclosure_given = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken greeting must not kill the engine
            log.exception("greeting playback failed")

    async def _greeting_first_frame(self, detected_at: float, source: str) -> None:
        self.time_to_greet_s = self.clock() - detected_at
        await self._event(
            "greeting_played",
            {
                "source": source,
                "time_to_greet_s": round(self.time_to_greet_s, 3),
                "reason": "human suspected",
                # Shown as call4me's first caption (the disclosure; never private data).
                "text": self.greeting_text,
            },
            audit_action="greeting.play",
        )

    def _greeting_playing(self) -> bool:
        task = self._greeting_task
        if task is not None and not task.done():
            return True
        return self._greeting_ends_at is not None and self.clock() < self._greeting_ends_at

    async def _on_greeting_reply(self, text: str) -> None:
        if self._greeting_playing():
            # Talk-over: the rep finishing their own sentence, not a reply to us. STT
            # often splits "This is Alex. How can I help?" across the greeting.
            return
        self._confirm_buffer.append(text)
        reply = " ".join(self._confirm_buffer)
        d = await jev_checks.confirm_human(
            self.deps.decisions,
            greeting=self.greeting_text,
            response=reply,
            timeout_s=self.config.jev_timeout_s,
        )
        await self._record_decision(d, "confirm human")
        if self.state is not S.HUMAN_SUSPECTED:
            return
        # Asymmetric on purpose: missing a real person loses the call, while a recording
        # mistaken for one is caught by the next state checks and the confirm timeout.
        p_human = max(d.probability, d.alternatives.get("human", 0.0))
        if d.choice == "human" and p_human >= self.config.confirm_threshold:
            await self._human_confirmed(reply, p_human)
        elif d.choice == "recording" and d.probability >= self.config.terminal_threshold:
            await self._false_human("recording ignored the greeting", d.probability)

    async def _human_confirmed(self, reply: str, p: float) -> None:
        if not await self._go(S.HUMAN_CONFIRMED, "rep responded to greeting", p):
            return
        await self._notify(
            "A person answered", "Open the app to read the call.", category="HUMAN_ANSWERED"
        )
        try:
            if self.navigator.clean_path:
                await self.deps.menu_paths.save_success(self.session.company, self.navigator.steps)
        except Exception:  # noqa: BLE001
            log.exception("could not save menu path")
        await self._go(S.IN_CONVERSATION, "handing off to conversation agent", p)
        if self.deps.on_conversation_start is not None:
            await self.deps.on_conversation_start(self.session, reply)

    async def _false_human(self, reason: str, p: float | None) -> None:
        await self._stop_greeting()
        await self._go(self._suspected_from, reason, p)

    async def _check_confirm_timeout(self) -> None:
        if self._greeting_started_at is None or self._greeting_playing():
            return
        since = self._greeting_ends_at or self._greeting_started_at
        if self.clock() - since > self.config.confirm_timeout_s:
            await self._false_human("no reply to greeting", None)

    async def _stop_greeting(self) -> None:
        task, self._greeting_task = self._greeting_task, None
        if task is not None and not task.done():
            task.cancel()
            await self.deps.telephony.clear_audio(self.session.id)

    # ------------------------------------------------------------------ #
    # Timers
    # ------------------------------------------------------------------ #

    async def _max_hold_reached(self) -> None:
        minutes = round(self.timers.hold_seconds / 60)
        await self._event(
            "max_hold",
            {"hold_minutes": minutes, "reason": "hold > user max"},
            audit_action="hold.max_reached",
        )
        await self._notify(
            "Still on hold",
            f"On hold {minutes} min. Keep waiting?",
            category="HOLD_CONTINUE",
            data={"hold_minutes": minutes},
        )

    async def continue_hold(self, keep_waiting: bool) -> None:
        """User's answer to the max-hold alert."""
        await self._event(
            "hold_continue",
            {"keep_waiting": keep_waiting, "reason": "user choice"},
            audit_action="hold.user_choice",
        )
        if keep_waiting:
            self.timers.extend_hold(self.config.hold_extend_s)
        else:
            await self.end("user stopped waiting on hold", outcome="user_cancelled")

    async def _reached_voicemail(self, p: float) -> None:
        await self._go(S.VOICEMAIL, "jev: voicemail", p)
        await self._shutdown(hang_up=True)
        await self._notify(
            "Reached voicemail", "No one answered. The call has ended.", category="CALL_ENDED"
        )

    # ------------------------------------------------------------------ #
    # Commands from the agent / API
    # ------------------------------------------------------------------ #

    async def waiting_on_user(self, reason: str = "ask_user") -> bool:
        return await self._go(S.WAITING_ON_USER, reason, None, actor="agent")

    async def user_answered(self, reason: str = "user answered") -> bool:
        return await self._go(S.IN_CONVERSATION, reason, None, actor="agent")

    async def takeover(self) -> bool:
        """Rule 6: stop agent speech immediately, then bridge the user."""
        prior = self.state
        await self._stop_greeting()
        await self.deps.telephony.clear_audio(self.session.id)
        if not await self._go(S.TAKEOVER, "user took over", None, actor="user"):
            return False
        self._pre_takeover = prior
        await self.deps.telephony.start_takeover(self.session.id)
        return True

    async def handback(self) -> bool:
        if self.state is not S.TAKEOVER:
            return False
        prior = self._pre_takeover or S.IN_CONVERSATION
        if prior in (S.IN_CONVERSATION, S.WAITING_ON_USER, S.HUMAN_CONFIRMED, S.HUMAN_SUSPECTED):
            target = S.IN_CONVERSATION
        elif prior is S.MENU:
            target = S.MENU
        else:
            target = S.HOLD
        await self.deps.telephony.end_takeover(self.session.id)
        changed = await self._go(target, "user handed back", None, actor="user")
        if changed and target is S.IN_CONVERSATION and self.deps.on_conversation_start:
            await self.deps.on_conversation_start(self.session, "")
        return changed

    async def end(
        self,
        reason: str,
        *,
        outcome: str | None = None,
        confidence: float | None = None,
        hang_up: bool = True,
        actor: str = "engine",
    ) -> bool:
        if self.done:
            return False
        await self._stop_greeting()
        changed = await self._go(S.ENDED, reason, confidence, actor=actor)
        self.outcome = outcome or self.outcome
        self.session.outcome = self.session.outcome or self.outcome
        await self._shutdown(hang_up=hang_up)
        if outcome == "callback_scheduled":
            await self._notify(
                "Call ended", "They will call back. We will answer for you.", category="CALL_ENDED"
            )
        return changed

    async def fail(self, reason: str) -> bool:
        if self.done:
            return False
        await self._stop_greeting()
        changed = await self._go(S.FAILED, reason, None)
        await self._shutdown(hang_up=True)
        await self._notify("Call failed", reason, category="CALL_ENDED")
        return changed

    async def on_call_status(self, status: str) -> None:
        """Line status values: completed, busy, failed, no-answer, canceled."""
        if self.done:
            return
        if status == "completed":
            outcome = "callback_scheduled" if self.callback_accepted else "remote_hangup"
            await self.end(f"line: {status}", outcome=outcome, hang_up=False)
        elif status in ("busy", "failed", "no-answer", "canceled"):
            await self.fail(f"line: {status}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _go(
        self,
        to: CallState,
        reason: str,
        confidence: float | None,
        evidence: dict[str, Any] | None = None,
        *,
        actor: str = "engine",
    ) -> bool:
        before = self.state
        try:
            changed = await st.transition(
                self.session,
                to,
                reason=reason,
                recorder=self.deps.recorder,
                confidence=confidence,
                actor=actor,
                evidence=evidence,
            )
        except st.IllegalTransition:
            return False  # already audited as denied; the engine keeps running
        if not changed:
            return False
        await self._on_state_changed(before, to)
        return True

    async def _on_state_changed(self, before: CallState, after: CallState) -> None:
        if after in WAITING_STATES:
            self.timers.hold_started()
        elif before in WAITING_STATES:
            self.timers.hold_stopped()
        if before is S.HOLD:
            await self._resume_stt()
        if after is S.HOLD and self._confirmed_music():
            await self._pause_stt()
        if after is S.MENU:
            self._menu_buffer = [self._finals[-1][1]] if self._finals else []
            self._menu_last_at = self._last_remote_at
        try:
            await self.deps.notifier.update_live_activity(
                self.session.user_id, after, {"session_id": self.session.id}
            )
        except Exception:  # noqa: BLE001 - a push failure must not stall the call
            log.exception("live activity update failed")
        if self.deps.on_state_change is not None:
            await self.deps.on_state_change(before, after)

    async def _pause_stt(self) -> None:
        if not self.stt_paused:
            self.stt_paused = True
            await self.deps.stt.pause(self.session.id)

    async def _resume_stt(self) -> None:
        if self.stt_paused:
            self.stt_paused = False
            await self.deps.stt.resume(self.session.id)

    async def _shutdown(self, *, hang_up: bool) -> None:
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()
        if hang_up:
            try:
                await self.deps.telephony.hang_up(self.session.id)
            except Exception:  # noqa: BLE001
                log.exception("hang up failed")
        try:
            await self.deps.stt.stop(self.session.id)
        except Exception:  # noqa: BLE001
            log.exception("stt stop failed")
        st.forget_session(self.session.id)

    async def _notify(
        self, title: str, body: str, *, category: str, data: dict[str, Any] | None = None
    ) -> None:
        # Rule 7: these become visual banners + haptics on the phone/Watch, never sound.
        try:
            await self.deps.notifier.alert(
                self.session.user_id,
                title,
                body,
                category=category,
                data={"session_id": self.session.id, "haptic": True, **(data or {})},
            )
        except Exception:  # noqa: BLE001
            log.exception("notify failed")

    async def _event(
        self,
        kind: str,
        detail: dict[str, Any],
        *,
        confidence: float | None = None,
        audit_action: str | None = None,
    ) -> None:
        try:
            await self.deps.recorder.event(
                CallEvent(
                    session_id=self.session.id,
                    kind=kind,
                    from_state=self.state,
                    to_state=self.state,
                    confidence=confidence,
                    detail=detail,
                ),
                audit_action=audit_action,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to record %s event", kind)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
