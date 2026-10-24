"""FastAPI app and routes (CLAUDE.md section 10).

Phase 0: sessions, requests, and the audit trail are real; call control
endpoints record intent and publish to the live channel, and are wired to
telephony / engine / agent as those areas land.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.audit import log as audit
from backend.config import get_settings
from backend.contracts import (
    CallEvent,
    CallLimits,
    CallSession,
    CallState,
    LiveMessage,
    UserRequestStatus,
    utcnow,
)
from backend.db import companies
from backend.db.models import CallEventRow, CallSessionRow, UserRequestRow
from backend.db.session import get_db, init_db
from backend.notify.live import answers, hub
from backend.telephony import media_ws

log = logging.getLogger("call4me")

Db = Annotated[AsyncSession, Depends(get_db)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


app = FastAPI(title="call4me", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class StartCallIn(BaseModel):
    user_id: str = "demo-user"
    company: str
    phone_number: str
    goal: str = Field(min_length=1)
    limits: CallLimits = Field(default_factory=CallLimits)
    disclosure_text: str = Field(min_length=1)


class EventOut(BaseModel):
    at: datetime
    kind: str
    from_state: str | None
    to_state: str | None
    confidence: float | None
    detail: dict[str, Any]


class CallOut(BaseModel):
    session: CallSession
    timeline: list[EventOut] = []


class TypeIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=500)


class Accepted(BaseModel):
    ok: bool = True
    detail: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _to_session(row: CallSessionRow) -> CallSession:
    return CallSession(
        id=row.id,
        user_id=row.user_id,
        company=row.company,
        phone_number=row.phone_number,
        goal=row.goal,
        limits=CallLimits.model_validate(row.limits_json),
        disclosure_text=row.disclosure_text,
        state=CallState(row.state),
        started_at=row.started_at,
        ended_at=row.ended_at,
        outcome=row.outcome,
        reference_number=row.reference_number,
        minutes_saved=row.minutes_saved,
    )


async def _get_session_row(db: AsyncSession, call_id: str) -> CallSessionRow:
    row = await db.get(CallSessionRow, call_id)
    if row is None:
        raise HTTPException(404, "call not found")
    return row


async def _record_user_action(db: AsyncSession, row: CallSessionRow, action: str) -> None:
    await audit.append(db, actor="user", action=action, subject=row.id, reason=f"user {action}")
    await audit.record_event(db, CallEvent(session_id=row.id, kind=action))
    await db.commit()
    hub.publish(LiveMessage(type="event", session_id=row.id, data={"kind": action}))


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/calls", response_model=CallSession, status_code=201)
async def start_call(body: StartCallIn, db: Db) -> CallSession:
    session = CallSession(**body.model_dump())
    db.add(
        CallSessionRow(
            id=session.id,
            user_id=session.user_id,
            company=session.company,
            phone_number=session.phone_number,
            goal=session.goal,
            limits_json=session.limits.model_dump(),
            disclosure_text=session.disclosure_text,
            state=session.state,
            started_at=session.started_at,
        )
    )
    await db.flush()
    await audit.append(
        db,
        actor="user",
        action="call.start",
        subject=session.id,
        reason="user started a call",
        diff={"company": session.company, "goal": session.goal},
    )
    await audit.record_event(
        db, CallEvent(session_id=session.id, kind="transition", to_state=CallState.DIALING)
    )
    await db.commit()
    # TODO(telephony): place the call via TelephonyClient.place_call(session).
    return session


@app.get("/calls/{call_id}", response_model=CallOut)
async def get_call(call_id: str, db: Db) -> CallOut:
    row = await _get_session_row(db, call_id)
    events = (
        await db.scalars(
            select(CallEventRow).where(CallEventRow.session_id == call_id).order_by(CallEventRow.at)
        )
    ).all()
    return CallOut(
        session=_to_session(row),
        timeline=[
            EventOut(
                at=e.at,
                kind=e.kind,
                from_state=e.from_state,
                to_state=e.to_state,
                confidence=e.confidence,
                detail=e.detail_json,
            )
            for e in events
        ],
    )


@app.websocket("/calls/{call_id}/live")
async def live(websocket: WebSocket, call_id: str) -> None:
    await websocket.accept()
    q = hub.subscribe(call_id)
    try:
        while True:
            msg = await q.get()
            await websocket.send_text(msg.model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(call_id, q)


@app.post("/calls/{call_id}/takeover", response_model=Accepted, status_code=202)
async def takeover(call_id: str, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    # TODO(engine/agent/telephony): transition to TAKEOVER, stop agent speech, bridge user.
    await _record_user_action(db, row, "takeover")
    return Accepted(detail="takeover requested")


@app.post("/calls/{call_id}/handback", response_model=Accepted, status_code=202)
async def handback(call_id: str, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    await _record_user_action(db, row, "handback")
    return Accepted(detail="handback requested")


@app.post("/calls/{call_id}/cancel", response_model=Accepted, status_code=202)
async def cancel(call_id: str, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    # TODO(telephony): hang up; engine transitions to ENDED.
    await _record_user_action(db, row, "cancel")
    return Accepted(detail="cancel requested")


@app.post("/calls/{call_id}/type", response_model=Accepted, status_code=202)
async def type_to_talk(call_id: str, body: TypeIn, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    # The user typed this themselves; it may hold private info, so log only that it happened.
    await audit.append(
        db, actor="user", action="type_to_talk", subject=row.id, evidence={"chars": len(body.text)}
    )
    await db.commit()
    # TODO(audio-speech/telephony): speak body.text verbatim via TTS.
    return Accepted(detail="queued")


# --------------------------------------------------------------------------- #
# ask_user answers — the answer is never stored
# --------------------------------------------------------------------------- #


@app.post("/requests/{request_id}/answer", response_model=Accepted, status_code=202)
async def answer_request(request_id: str, body: AnswerIn, db: Db) -> Accepted:
    req = await db.get(UserRequestRow, request_id)
    if req is None:
        raise HTTPException(404, "request not found")
    if req.status != UserRequestStatus.PENDING:
        raise HTTPException(409, f"request is {req.status}")

    # Register first so nothing downstream can log the raw value.
    audit.register_secret(req.session_id, body.answer)
    delivered = answers.deliver(request_id, body.answer)

    req.status = UserRequestStatus.ANSWERED
    req.answered_at = utcnow()
    await audit.append(
        db,
        actor="user",
        action="user_request.answered",
        subject=request_id,
        reason="user answered" + ("" if delivered else " (no agent waiting)"),
    )
    await db.commit()
    return Accepted(detail="delivered" if delivered else "recorded")


# --------------------------------------------------------------------------- #
# Twilio
# --------------------------------------------------------------------------- #


@app.post("/twilio/status", response_model=Accepted)
async def twilio_status(request: Request, db: Db) -> Accepted:
    # TODO(telephony): validate X-Twilio-Signature and map CallSid -> session.
    form = dict(await request.form())
    await audit.append(
        db,
        actor="twilio",
        action="twilio.status",
        subject=str(form.get("CallSid", "")),
        evidence={k: str(form[k]) for k in ("CallStatus", "CallDuration") if k in form},
    )
    await db.commit()
    return Accepted()


@app.websocket("/media")
async def media(websocket: WebSocket) -> None:
    await media_ws.handle(websocket)


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #


@app.get("/companies", response_model=list[companies.Company])
async def list_companies(q: str = "") -> list[companies.Company]:
    return companies.search(q, get_settings().twilio_fakeline_number)
