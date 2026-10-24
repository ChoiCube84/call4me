"""Call state machine (CLAUDE.md section 6).

Every state change in the backend goes through `transition()`. It checks the
move against `VALID_TRANSITIONS`, updates the session, and records the change
(from, to, reason, confidence) to `call_event` and `audit_log` via a
`TransitionRecorder`. Illegal moves raise `IllegalTransition` and are logged
as denied audit entries, so a buggy caller is visible in the audit trail.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.contracts import TERMINAL_STATES, CallEvent, CallSession, CallState, LiveMessage

log = logging.getLogger(__name__)

S = CallState

# States the user can take over from or the call can end/fail from: anything live.
_LIVE_STATES = frozenset(CallState) - TERMINAL_STATES

_BASE: dict[CallState, frozenset[CallState]] = {
    # Some lines answer straight into a recording or a person, skipping the menu.
    S.DIALING: frozenset({S.MENU, S.HOLD, S.RECORDED_MESSAGE, S.HUMAN_SUSPECTED, S.VOICEMAIL}),
    S.MENU: frozenset(
        {S.HOLD, S.RECORDED_MESSAGE, S.CALLBACK_OFFERED, S.HUMAN_SUSPECTED, S.VOICEMAIL}
    ),
    # Nested menus can follow a hold or a recording.
    S.HOLD: frozenset(
        {S.MENU, S.RECORDED_MESSAGE, S.CALLBACK_OFFERED, S.HUMAN_SUSPECTED, S.VOICEMAIL}
    ),
    S.RECORDED_MESSAGE: frozenset(
        {S.MENU, S.HOLD, S.CALLBACK_OFFERED, S.HUMAN_SUSPECTED, S.VOICEMAIL}
    ),
    # Accepting a callback usually leads to a menu ("press 1 to confirm"); declining
    # returns to hold.
    S.CALLBACK_OFFERED: frozenset({S.MENU, S.HOLD, S.RECORDED_MESSAGE, S.HUMAN_SUSPECTED}),
    # A false positive (a recording that sounds human) falls back to where we were.
    S.HUMAN_SUSPECTED: frozenset({S.HUMAN_CONFIRMED, S.HOLD, S.RECORDED_MESSAGE, S.MENU}),
    S.HUMAN_CONFIRMED: frozenset({S.IN_CONVERSATION, S.HOLD}),
    # Transfers ("please hold while I transfer you") return to HOLD.
    S.IN_CONVERSATION: frozenset({S.WAITING_ON_USER, S.HOLD}),
    S.WAITING_ON_USER: frozenset({S.IN_CONVERSATION, S.HOLD}),
    # Handback returns to wherever the call now is.
    S.TAKEOVER: frozenset({S.IN_CONVERSATION, S.HOLD, S.MENU, S.WAITING_ON_USER}),
    S.VOICEMAIL: frozenset(),
    S.FAILED: frozenset(),
    S.ENDED: frozenset(),
}


def _build_table() -> dict[CallState, frozenset[CallState]]:
    table: dict[CallState, frozenset[CallState]] = {}
    for state, targets in _BASE.items():
        if state in TERMINAL_STATES:
            table[state] = frozenset()
            continue
        extra = {S.ENDED, S.FAILED}
        if state is not S.TAKEOVER:
            extra.add(S.TAKEOVER)  # rule 6: the user can take over at any time
        table[state] = targets | extra
    return table


VALID_TRANSITIONS: dict[CallState, frozenset[CallState]] = _build_table()


def can_transition(from_state: CallState, to_state: CallState) -> bool:
    return to_state in VALID_TRANSITIONS[from_state]


class IllegalTransition(RuntimeError):
    def __init__(self, from_state: CallState, to_state: CallState, reason: str) -> None:
        super().__init__(f"illegal transition {from_state} -> {to_state} ({reason})")
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason


@dataclass(frozen=True)
class TransitionRecord:
    session_id: str
    from_state: CallState
    to_state: CallState
    reason: str
    confidence: float | None
    actor: str
    evidence: dict[str, Any] = field(default_factory=dict)
    allowed: bool = True


class TransitionRecorder(Protocol):
    """Where transitions and engine events are written."""

    async def transition(self, record: TransitionRecord) -> None: ...

    async def event(self, event: CallEvent, *, audit_action: str | None = None) -> None: ...


class MemoryRecorder:
    """In-memory recorder for tests and the fake line."""

    def __init__(self) -> None:
        self.transitions: list[TransitionRecord] = []
        self.events: list[CallEvent] = []
        self.audit_actions: list[str] = []

    async def transition(self, record: TransitionRecord) -> None:
        self.transitions.append(record)

    async def event(self, event: CallEvent, *, audit_action: str | None = None) -> None:
        self.events.append(event)
        if audit_action:
            self.audit_actions.append(audit_action)


class DbRecorder:
    """Writes to `call_event`, `audit_log`, and `call_session.state`, then publishes live.

    Each write opens its own short DB session so the engine never holds a
    transaction across a slow network call.
    """

    async def transition(self, record: TransitionRecord) -> None:
        from backend.audit import log as audit
        from backend.db.models import CallSessionRow
        from backend.db.session import sessionmaker
        from backend.notify.live import hub

        async with sessionmaker()() as db:
            await audit.append(
                db,
                actor=record.actor,
                action="state.transition",
                subject=record.session_id,
                allowed=record.allowed,
                reason=record.reason,
                diff={"from": record.from_state, "to": record.to_state},
                evidence={"confidence": record.confidence, **record.evidence},
            )
            if record.allowed:
                await audit.record_event(
                    db,
                    CallEvent(
                        session_id=record.session_id,
                        kind="transition",
                        from_state=record.from_state,
                        to_state=record.to_state,
                        confidence=record.confidence,
                        detail={"reason": record.reason, **record.evidence},
                    ),
                )
                row = await db.get(CallSessionRow, record.session_id)
                if row is not None:
                    row.state = record.to_state
            await db.commit()
        if record.allowed:
            hub.publish(
                LiveMessage(
                    type="state",
                    session_id=record.session_id,
                    data={
                        "from": record.from_state,
                        "to": record.to_state,
                        "reason": record.reason,
                        "confidence": record.confidence,
                    },
                )
            )

    async def event(self, event: CallEvent, *, audit_action: str | None = None) -> None:
        from backend.audit import log as audit
        from backend.db.session import sessionmaker
        from backend.notify.live import hub

        async with sessionmaker()() as db:
            await audit.record_event(db, event)
            if audit_action:
                await audit.append(
                    db,
                    actor="engine",
                    action=audit_action,
                    subject=event.session_id,
                    reason=str(event.detail.get("reason", "")),
                    evidence={"confidence": event.confidence, **event.detail},
                )
            await db.commit()
        hub.publish(
            LiveMessage(
                type="event", session_id=event.session_id, data={"kind": event.kind, **event.detail}
            )
        )


_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def forget_session(session_id: str) -> None:
    _locks.pop(session_id, None)


async def transition(
    session: CallSession,
    to_state: CallState,
    *,
    reason: str,
    recorder: TransitionRecorder,
    confidence: float | None = None,
    actor: str = "engine",
    evidence: dict[str, Any] | None = None,
) -> bool:
    """Move `session` to `to_state`. The only place call state changes.

    Returns True if the state changed, False for a no-op (already in
    `to_state`). Raises `IllegalTransition` for moves not in the table.
    """
    async with _lock_for(session.id):
        from_state = session.state
        if from_state == to_state:
            return False
        record = TransitionRecord(
            session_id=session.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            confidence=confidence,
            actor=actor,
            evidence=evidence or {},
            allowed=can_transition(from_state, to_state),
        )
        if not record.allowed:
            log.warning("denied %s -> %s for %s: %s", from_state, to_state, session.id, reason)
            await recorder.transition(record)
            raise IllegalTransition(from_state, to_state, reason)
        session.state = to_state
        log.info("%s: %s -> %s (%s, p=%s)", session.id, from_state, to_state, reason, confidence)
        try:
            await recorder.transition(record)
        except Exception:
            # A DB hiccup must not wedge a live call; the process log still has it.
            log.exception("failed to record transition %s -> %s", from_state, to_state)
        return True
