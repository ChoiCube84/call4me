"""Shared types and client interfaces for every area of call4me.

Owned by `platform`. Changing anything here affects all agents: announce it to
the lead and affected teammates before merging (see TEAM.md).
"""

from __future__ import annotations

import abc
import enum
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Call state (CLAUDE.md section 6)
# --------------------------------------------------------------------------- #


class CallState(enum.StrEnum):
    DIALING = "DIALING"
    MENU = "MENU"
    HOLD = "HOLD"
    RECORDED_MESSAGE = "RECORDED_MESSAGE"
    CALLBACK_OFFERED = "CALLBACK_OFFERED"
    HUMAN_SUSPECTED = "HUMAN_SUSPECTED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    IN_CONVERSATION = "IN_CONVERSATION"
    WAITING_ON_USER = "WAITING_ON_USER"
    TAKEOVER = "TAKEOVER"
    VOICEMAIL = "VOICEMAIL"
    FAILED = "FAILED"
    ENDED = "ENDED"


TERMINAL_STATES: frozenset[CallState] = frozenset(
    {CallState.VOICEMAIL, CallState.FAILED, CallState.ENDED}
)


class CallLimits(BaseModel):
    """What the agent may and may not agree to. Enforced by the guard layer."""

    model_config = ConfigDict(extra="allow")

    max_hold_minutes: int = 60
    max_spend_usd: float = 0.0
    allow_plan_changes: bool = False
    allow_cancellation: bool = False
    notes: str = ""  # free-text limits, e.g. "no plan changes"


class CallSession(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    company: str
    phone_number: str
    goal: str
    limits: CallLimits = Field(default_factory=CallLimits)
    disclosure_text: str
    state: CallState = CallState.DIALING
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    outcome: str | None = None
    reference_number: str | None = None
    minutes_saved: float | None = None
    disclosure_given: bool = False


class CallEvent(BaseModel):
    """One entry on the call timeline. Payloads must be redacted before storage."""

    id: str = Field(default_factory=new_id)
    session_id: str
    at: datetime = Field(default_factory=utcnow)
    kind: str  # e.g. "transition", "caption", "jev_decision", "tool_call"
    from_state: CallState | None = None
    to_state: CallState | None = None
    confidence: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Audio / speech
# --------------------------------------------------------------------------- #


class AudioLabelKind(enum.StrEnum):
    MUSIC = "music"
    SPEECH = "speech"
    SILENCE = "silence"


class AudioLabel(BaseModel):
    """Emitted by the audio pipeline every ~2 s."""

    label: AudioLabelKind
    confidence: float
    at: datetime = Field(default_factory=utcnow)


class Speaker(enum.StrEnum):
    REMOTE = "remote"  # the company side (menu, recordings, rep)
    AGENT = "agent"  # our TTS
    USER = "user"  # type-to-talk text during takeover


class Transcript(BaseModel):
    session_id: str
    text: str
    is_final: bool
    speaker: Speaker = Speaker.REMOTE
    at: datetime = Field(default_factory=utcnow)
    confidence: float | None = None


# --------------------------------------------------------------------------- #
# Decisions (Jev)
# --------------------------------------------------------------------------- #


class StateCheckLabel(enum.StrEnum):
    MENU = "menu"
    RECORDED_MESSAGE = "recorded_message"
    HOLD_MUSIC = "hold_music"
    SILENCE = "silence"
    HUMAN = "human"
    VOICEMAIL = "voicemail"
    CALLBACK_OFFER = "callback_offer"
    CALL_ENDED = "call_ended"


class JevDecision(BaseModel):
    """A typed answer plus probability from Jev."""

    question: str  # e.g. "state_check", "menu_choice", "human_detection"
    choice: str
    probability: float
    alternatives: dict[str, float] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Agent actions (CLAUDE.md section 7)
# --------------------------------------------------------------------------- #


class Sensitivity(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SayAction(BaseModel):
    kind: Literal["say"] = "say"
    text: str


class AskUserAction(BaseModel):
    kind: Literal["ask_user"] = "ask_user"
    question: str
    sensitivity: Sensitivity = Sensitivity.MEDIUM


class PressDigitsAction(BaseModel):
    kind: Literal["press_digits"] = "press_digits"
    digits: str = Field(pattern=r"^[0-9*#wW]+$")


class EndCallAction(BaseModel):
    kind: Literal["end_call"] = "end_call"
    summary: str
    reference_number: str | None = None


class RequestTakeoverAction(BaseModel):
    kind: Literal["request_takeover"] = "request_takeover"
    reason: str


AgentAction = Annotated[
    SayAction | AskUserAction | PressDigitsAction | EndCallAction | RequestTakeoverAction,
    Field(discriminator="kind"),
]


class GuardResult(BaseModel):
    """Outcome of `guard.check(action, session)`."""

    allowed: bool
    reason: str = ""
    # The guard may rewrite an action (e.g. prepend the disclosure).
    action: AgentAction | None = None
    trigger_takeover: bool = False


# --------------------------------------------------------------------------- #
# ask_user requests — the answer is NEVER part of any stored model
# --------------------------------------------------------------------------- #


class UserRequestStatus(enum.StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    TAKEOVER_SUGGESTED = "takeover_suggested"


class UserRequest(BaseModel):
    id: str = Field(default_factory=new_id)
    session_id: str
    question: str
    sensitivity: Sensitivity
    status: UserRequestStatus = UserRequestStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Live channel to the app / dashboard (WS /calls/{id}/live)
# --------------------------------------------------------------------------- #


class LiveMessage(BaseModel):
    type: Literal["caption", "state", "summary", "ask_user", "event", "ended"]
    session_id: str
    at: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Client interfaces. Every external service sits behind one of these, with a
# real implementation and a mock twin selected by USE_MOCKS.
# --------------------------------------------------------------------------- #


class TelephonyClient(abc.ABC):
    """The phone line for our own pipeline. Audio is 8 kHz mu-law, 20 ms frames."""

    @abc.abstractmethod
    async def place_call(self, session: CallSession) -> str:
        """Dial the company and open a Media Stream. Returns the provider call id."""

    @abc.abstractmethod
    def audio_in(self, session_id: str) -> AsyncIterator[bytes]:
        """Raw mu-law frames from the company side."""

    @abc.abstractmethod
    async def send_audio(self, session_id: str, mulaw_frame: bytes) -> None:
        """Play mu-law audio to the company side."""

    @abc.abstractmethod
    async def clear_audio(self, session_id: str) -> None:
        """Stop any queued outgoing audio immediately (used on takeover/barge-in)."""

    @abc.abstractmethod
    async def press_digits(self, session_id: str, digits: str) -> None: ...

    @abc.abstractmethod
    async def start_takeover(self, session_id: str) -> None:
        """Move the call into a Conference and bridge the user."""

    @abc.abstractmethod
    async def end_takeover(self, session_id: str) -> None: ...

    @abc.abstractmethod
    async def hang_up(self, session_id: str) -> None: ...


class STTClient(abc.ABC):
    """Cartesia streaming speech-to-text (owned by `audio-speech`)."""

    @abc.abstractmethod
    async def start(self, session_id: str) -> None: ...

    @abc.abstractmethod
    async def feed(self, session_id: str, pcm16_frame: bytes) -> None:
        """Feed 16-bit PCM audio (sample rate per implementation)."""

    @abc.abstractmethod
    def transcripts(self, session_id: str) -> AsyncIterator[Transcript]:
        """Partial and final transcripts."""

    @abc.abstractmethod
    async def pause(self, session_id: str) -> None:
        """Pause during confirmed hold music (cost)."""

    @abc.abstractmethod
    async def resume(self, session_id: str) -> None: ...

    @abc.abstractmethod
    async def stop(self, session_id: str) -> None: ...


class TTSClient(abc.ABC):
    """Cartesia Sonic streaming text-to-speech (owned by `audio-speech`)."""

    @abc.abstractmethod
    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream phone-line mu-law frames (8 kHz, 20 ms) for `text`."""

    @abc.abstractmethod
    async def pregenerate(self, key: str, text: str) -> None:
        """Synthesize ahead of time and cache under `key` (e.g. the greeting)."""

    @abc.abstractmethod
    def play_pregenerated(self, key: str) -> AsyncIterator[bytes]: ...


class DecisionClient(abc.ABC):
    """Jev via typesafe-sdk (used by `call-engine`)."""

    @abc.abstractmethod
    async def choose(
        self, question: str, options: list[str], context: dict[str, Any]
    ) -> JevDecision: ...


class Notifier(abc.ABC):
    """APNs push + Live Activity updates (owned by `platform`).

    Every alert must be visual or haptic — never sound-only (CLAUDE.md rule 7).
    """

    @abc.abstractmethod
    async def alert(
        self,
        user_id: str,
        title: str,
        body: str,
        *,
        category: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def ask_user(self, user_id: str, request: UserRequest) -> None:
        """Time-sensitive push with a text-input action."""

    @abc.abstractmethod
    async def update_live_activity(
        self, user_id: str, state: CallState, data: dict[str, Any] | None = None
    ) -> None: ...
