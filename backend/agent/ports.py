"""What the conversation agent needs from the rest of the system.

The agent never touches Twilio, Cartesia, the state machine, or the database
directly. It talks to these small ports, so other areas can wire in their real
implementations (engine `transition()`, TTS + telephony speech, DB rows) while
tests use the in-memory twins at the bottom of this file.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.audit import log as audit
from backend.contracts import (
    CallSession,
    CallState,
    LiveMessage,
    UserRequest,
    UserRequestStatus,
    utcnow,
)

# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


class CallControl(Protocol):
    """Speech, DTMF, and state changes. Implemented by engine + telephony + TTS."""

    async def speak(self, session_id: str, text: str) -> None:
        """Synthesize `text` and play it to the rep. Returns when queued/played."""

    async def stop_speaking(self, session_id: str) -> None:
        """Drop any queued agent audio right now (takeover)."""

    async def press_digits(self, session_id: str, digits: str) -> None: ...

    async def hang_up(self, session_id: str) -> None: ...

    async def transition(
        self, session: CallSession, to_state: CallState, reason: str, confidence: float = 1.0
    ) -> None:
        """Change state through engine/states.py `transition()`; must update session.state."""


class RequestStore(Protocol):
    """Persists `user_request` rows. Question only — there is no answer parameter."""

    async def create(self, request: UserRequest) -> None: ...

    async def set_status(self, request_id: str, status: UserRequestStatus) -> None: ...


class AuditSink(Protocol):
    async def __call__(
        self,
        *,
        actor: str,
        action: str,
        subject: str | None = None,
        allowed: bool = True,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None: ...


class UserChannel(Protocol):
    """Reaching the user: push (visual + haptic) and the live WebSocket."""

    async def ask(self, user_id: str, request: UserRequest) -> None: ...

    async def alert(self, user_id: str, title: str, body: str) -> None: ...

    def publish(self, msg: LiveMessage) -> None: ...


class AnswerSource(Protocol):
    """In-memory hand-off of `ask_user` answers (see notify/live.AnswerBroker)."""

    def expect(self, request_id: str) -> asyncio.Future[str]: ...

    def release(self, request_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# Per-call runtime state the guard and tools share. In memory only.
# --------------------------------------------------------------------------- #


@dataclass
class AgentRuntime:
    session: CallSession
    takeover: asyncio.Event = field(default_factory=asyncio.Event)
    # The single ask_user answer that may be spoken right now. Set just before
    # the relay and cleared right after, so it can be spoken exactly once.
    one_shot_relay: str | None = None
    # Recent rep utterances (already redacted). Used for limits and fact checks.
    remote_history: deque[str] = field(default_factory=lambda: deque(maxlen=12))
    # What the agent has said (redacted). Used for the end-of-call read-back check.
    spoken: list[str] = field(default_factory=list)
    pending_request_id: str | None = None
    ended: bool = False

    @property
    def last_remote_text(self) -> str:
        return self.remote_history[-1] if self.remote_history else ""

    @property
    def taken_over(self) -> bool:
        return self.takeover.is_set() or self.session.state == CallState.TAKEOVER


@dataclass
class AgentPorts:
    control: CallControl
    requests: RequestStore
    audit: AuditSink
    user: UserChannel
    answers: AnswerSource


# --------------------------------------------------------------------------- #
# Default wiring to platform pieces that already exist
# --------------------------------------------------------------------------- #


class BrokerAnswerSource:
    """Adapter over notify.live.AnswerBroker.

    We hold the future ourselves (instead of `broker.wait`) so an answer that
    arrives while we are speaking the 60 s "please hold" line is not lost.
    """

    def __init__(self, broker: Any | None = None) -> None:
        if broker is None:
            from backend.notify.live import answers

            broker = answers
        self._broker = broker

    def expect(self, request_id: str) -> asyncio.Future[str]:
        return self._broker.expect(request_id)

    def release(self, request_id: str) -> None:
        # TODO(platform): replace with a public AnswerBroker.cancel(request_id).
        fut = self._broker._waiting.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.cancel()


class DbAuditSink:
    """Writes to the append-only audit log in its own short transaction."""

    async def __call__(
        self,
        *,
        actor: str,
        action: str,
        subject: str | None = None,
        allowed: bool = True,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        from backend.db.session import sessionmaker

        async with sessionmaker()() as db:
            await audit.append(
                db,
                actor=actor,
                action=action,
                subject=subject,
                allowed=allowed,
                reason=reason,
                evidence=evidence,
            )
            await db.commit()


class DbRequestStore:
    async def create(self, request: UserRequest) -> None:
        from backend.db.models import UserRequestRow
        from backend.db.session import sessionmaker

        async with sessionmaker()() as db:
            db.add(
                UserRequestRow(
                    id=request.id,
                    session_id=request.session_id,
                    question=audit.redact(request.question),
                    sensitivity=request.sensitivity,
                    status=request.status,
                    created_at=request.created_at,
                )
            )
            await db.commit()

    async def set_status(self, request_id: str, status: UserRequestStatus) -> None:
        from backend.db.models import UserRequestRow
        from backend.db.session import sessionmaker

        async with sessionmaker()() as db:
            row = await db.get(UserRequestRow, request_id)
            # Never downgrade an answer the endpoint already recorded.
            if row is not None and row.status == UserRequestStatus.PENDING:
                row.status = status
                await db.commit()


class NotifierUserChannel:
    def __init__(self, notifier: Any | None = None) -> None:
        if notifier is None:
            from backend.notify.apns import get_notifier

            notifier = get_notifier()
        self._notifier = notifier

    async def ask(self, user_id: str, request: UserRequest) -> None:
        await self._notifier.ask_user(user_id, request)

    async def alert(self, user_id: str, title: str, body: str) -> None:
        await self._notifier.alert(user_id, title, body)

    def publish(self, msg: LiveMessage) -> None:
        from backend.notify.live import hub

        hub.publish(msg)


# --------------------------------------------------------------------------- #
# In-memory twins (USE_MOCKS=true and unit tests)
# --------------------------------------------------------------------------- #


class FakeCallControl:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.digits: list[str] = []
        self.transitions: list[tuple[CallState, str]] = []
        self.stopped = 0
        self.hung_up = False

    async def speak(self, session_id: str, text: str) -> None:
        self.spoken.append(text)

    async def stop_speaking(self, session_id: str) -> None:
        self.stopped += 1

    async def press_digits(self, session_id: str, digits: str) -> None:
        self.digits.append(digits)

    async def hang_up(self, session_id: str) -> None:
        self.hung_up = True

    async def transition(
        self, session: CallSession, to_state: CallState, reason: str, confidence: float = 1.0
    ) -> None:
        session.state = to_state
        if to_state == CallState.ENDED:
            session.ended_at = utcnow()
        self.transitions.append((to_state, reason))


class MemoryRequestStore:
    def __init__(self) -> None:
        self.requests: dict[str, UserRequest] = {}

    async def create(self, request: UserRequest) -> None:
        self.requests[request.id] = request.model_copy()

    async def set_status(self, request_id: str, status: UserRequestStatus) -> None:
        req = self.requests.get(request_id)
        if req is not None:
            req.status = status


class MemoryAuditSink:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        actor: str,
        action: str,
        subject: str | None = None,
        allowed: bool = True,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        # Same redaction the real audit log applies.
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "subject": subject,
                "allowed": allowed,
                "reason": audit.redact(reason),
                "evidence": audit.redact(evidence or {}),
            }
        )


class MemoryUserChannel:
    def __init__(self) -> None:
        self.asked: list[UserRequest] = []
        self.alerts: list[tuple[str, str]] = []
        self.live: list[LiveMessage] = []

    async def ask(self, user_id: str, request: UserRequest) -> None:
        self.asked.append(request)

    async def alert(self, user_id: str, title: str, body: str) -> None:
        self.alerts.append((title, body))

    def publish(self, msg: LiveMessage) -> None:
        self.live.append(msg.model_copy(update={"data": audit.redact(msg.data)}))


class MemoryAnswerSource:
    def __init__(self) -> None:
        self.waiting: dict[str, asyncio.Future[str]] = {}

    def expect(self, request_id: str) -> asyncio.Future[str]:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.waiting[request_id] = fut
        return fut

    def release(self, request_id: str) -> None:
        fut = self.waiting.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def deliver(self, request_id: str, answer: str) -> bool:
        fut = self.waiting.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True


def memory_ports() -> AgentPorts:
    return AgentPorts(
        control=FakeCallControl(),
        requests=MemoryRequestStore(),
        audit=MemoryAuditSink(),
        user=MemoryUserChannel(),
        answers=MemoryAnswerSource(),
    )
