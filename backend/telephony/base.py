"""Telephony-side extensions to the shared `TelephonyClient` contract.

The mock line and the browser phone implement `CallControl`, so
call-engine can rely on these extras in either mode. Everything in
`backend/contracts.py` stays the primary interface.
"""

from __future__ import annotations

import abc
import enum
from collections.abc import AsyncIterator
from datetime import datetime

from pydantic import BaseModel, Field

from backend.contracts import TelephonyClient, utcnow

DIGITS_PATTERN = r"^[0-9*#wW]+$"


class CallStatus(enum.StrEnum):
    """Call status values (the names telephony providers commonly use)."""

    QUEUED = "queued"
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    ANSWERED = "answered"  # sent as CallStatus for the 'answered' event on some legs
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.BUSY,
        CallStatus.NO_ANSWER,
        CallStatus.FAILED,
        CallStatus.CANCELED,
    }
)


class CallStatusUpdate(BaseModel):
    session_id: str
    call_sid: str
    status: CallStatus
    duration_s: int | None = None
    at: datetime = Field(default_factory=utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class IncomingCall(BaseModel):
    """A call to our agent number (e.g. a company calling back)."""

    session_id: str  # the session it was matched to, or a fresh "inbound-<CallSid>" id
    call_sid: str
    from_number: str
    matched_callback: bool


class TelephonyError(RuntimeError):
    pass


class CallControl(TelephonyClient, abc.ABC):
    """`TelephonyClient` plus the extras call-engine needs.

    Audio contract (both directions): 8 kHz mu-law, 20 ms frames of 160 bytes.
    `audio_in()` has a single consumer per session; it ends when the call ends.
    """

    # Seconds of audio played per real second. Only the mock line runs faster
    # (sped-up tests); the engine uses this to know when sent audio has been heard.
    line_speed: float = 1.0
    # True when the far end removes our own audio from what it sends back (the
    # browser phone's echo canceller), so inbound audio needn't be muted while we talk.
    echo_cancelled: bool = False

    @abc.abstractmethod
    async def wait_connected(self, session_id: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        """True once the company side has answered and audio is flowing."""

    @abc.abstractmethod
    async def mark(self, session_id: str, name: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        """Wait until all audio sent before this call has finished playing on the
        line (True) or was cleared / timed out (False). Use to know when the
        greeting or a holding line has actually been heard."""

    @abc.abstractmethod
    def status_updates(self, session_id: str) -> AsyncIterator[CallStatusUpdate]:
        """Provider call-status changes (ringing, in-progress, busy, completed...)."""

    @abc.abstractmethod
    def is_taken_over(self, session_id: str) -> bool: ...

    @abc.abstractmethod
    def set_takeover_bridge(self, session_id: str, phone_number: str | None) -> None:
        """Opt in to bridging the user's own phone into a Conference on takeover
        (for hard-of-hearing users who want to speak themselves). Without it,
        takeover is type-to-talk on the existing stream."""

    @abc.abstractmethod
    def expect_callback(self, session_id: str) -> None:
        """The next incoming call to our agent number belongs to this session."""
