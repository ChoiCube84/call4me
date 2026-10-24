"""FastAPI app and routes (CLAUDE.md section 10).

Sessions, requests, and the audit trail live in the DB. Call control endpoints
record the user's intent, then drive the live call (`backend.calls`) if one is
running in this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import calls
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
from backend.db.models import (
    AuditLogRow,
    CallEventRow,
    CallSessionRow,
    DeviceRow,
    UserRequestRow,
)
from backend.db.session import get_db, init_db
from backend.notify.live import answers, hub
from backend.telephony import media_ws
from backend.telephony.routes import router as twilio_router
from backend.telephony.signature import verify_twilio_request
from backend.telephony.twilio_client import handle_status_form
from fakeline.app import app as fakeline_app

log = logging.getLogger("call4me")

Db = Annotated[AsyncSession, Depends(get_db)]
TwilioForm = Annotated[dict[str, str], Depends(verify_twilio_request)]


REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
EVAL_RESULTS_DIR = REPO_ROOT / "evals" / "results"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    callbacks = asyncio.create_task(calls.watch_incoming_callbacks())
    yield
    callbacks.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await callbacks


app = FastAPI(title="call4me", lifespan=lifespan)
# Tests turn this off to exercise the API without running a call pipeline.
app.state.start_calls = True
app.include_router(twilio_router)
# The fake support line, webhook <PUBLIC_BASE_URL>/fakeline/voice (CLAUDE.md section 13).
app.mount("/fakeline", fakeline_app)


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


class DeviceIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    apns_token: str | None = Field(default=None, max_length=200)
    live_activity_token: str | None = Field(default=None, max_length=200)


class HoldIn(BaseModel):
    keep_waiting: bool


class AuditOut(BaseModel):
    at: datetime
    actor: str
    action: str
    subject: str | None
    allowed: bool
    reason: str
    diff: dict[str, Any]
    evidence: dict[str, Any]


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
    if app.state.start_calls:
        # Dialing takes seconds; the app follows progress on WS /calls/{id}/live.
        _background(calls.start_call(session))
    return session


@app.get("/calls", response_model=list[CallSession])
async def list_calls(db: Db, user_id: str | None = None, limit: int = 20) -> list[CallSession]:
    q = select(CallSessionRow).order_by(CallSessionRow.started_at.desc()).limit(min(limit, 100))
    if user_id:
        q = q.where(CallSessionRow.user_id == user_id)
    return [_to_session(r) for r in (await db.scalars(q)).all()]


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


@app.get("/calls/{call_id}/audit", response_model=list[AuditOut])
async def call_audit(call_id: str, db: Db) -> list[AuditOut]:
    """The append-only audit trail for a call (already redacted when written)."""
    await _get_session_row(db, call_id)
    request_ids = select(UserRequestRow.id).where(UserRequestRow.session_id == call_id)
    rows = (
        await db.scalars(
            select(AuditLogRow)
            .where((AuditLogRow.subject == call_id) | AuditLogRow.subject.in_(request_ids))
            .order_by(AuditLogRow.at)
        )
    ).all()
    return [
        AuditOut(
            at=r.at,
            actor=r.actor,
            action=r.action,
            subject=r.subject,
            allowed=r.allowed,
            reason=r.reason,
            diff=r.diff_json or {},
            evidence=r.evidence_json or {},
        )
        for r in rows
    ]


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
    await _record_user_action(db, row, "takeover")
    if (live := calls.get_call(call_id)) is not None:
        if not await live.takeover():
            return Accepted(ok=False, detail=f"cannot take over in {live.session.state}")
        return Accepted(detail="you have the call")
    return Accepted(detail="takeover requested")


@app.post("/calls/{call_id}/handback", response_model=Accepted, status_code=202)
async def handback(call_id: str, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    await _record_user_action(db, row, "handback")
    if (live := calls.get_call(call_id)) is not None:
        if not await live.handback():
            return Accepted(ok=False, detail=f"cannot hand back in {live.session.state}")
        return Accepted(detail="the assistant has the call")
    return Accepted(detail="handback requested")


@app.post("/calls/{call_id}/cancel", response_model=Accepted, status_code=202)
async def cancel(call_id: str, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    await _record_user_action(db, row, "cancel")
    if (live := calls.get_call(call_id)) is not None:
        await live.cancel()
    return Accepted(detail="cancel requested")


@app.post("/calls/{call_id}/hold", response_model=Accepted, status_code=202)
async def continue_hold(call_id: str, body: HoldIn, db: Db) -> Accepted:
    """The user's answer to "Still on hold. Keep waiting?"."""
    row = await _get_session_row(db, call_id)
    await audit.append(
        db,
        actor="user",
        action="hold.continue" if body.keep_waiting else "hold.stop",
        subject=row.id,
        reason="user answered the max-hold alert",
    )
    await db.commit()
    if (live := calls.get_call(call_id)) is not None:
        await live.continue_hold(body.keep_waiting)
    return Accepted(detail="keep waiting" if body.keep_waiting else "stopping")


@app.post("/calls/{call_id}/type", response_model=Accepted, status_code=202)
async def type_to_talk(call_id: str, body: TypeIn, db: Db) -> Accepted:
    row = await _get_session_row(db, call_id)
    live = calls.get_call(call_id)
    if live is not None and live.session.state != CallState.TAKEOVER:
        raise HTTPException(409, "take over the call before typing")
    # The user typed this themselves; it may hold private info, so log only that it happened.
    await audit.append(
        db, actor="user", action="type_to_talk", subject=row.id, evidence={"chars": len(body.text)}
    )
    await db.commit()
    if live is not None:
        _background(live.type_text(body.text))
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
async def twilio_status(form: TwilioForm, db: Db) -> Accepted:
    # Signature checked by verify_twilio_request; the update reaches the call's engine.
    update = handle_status_form(form)
    await audit.append(
        db,
        actor="twilio",
        action="twilio.status",
        subject=update.session_id if update else str(form.get("CallSid", "")),
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


# --------------------------------------------------------------------------- #
# Devices (APNs tokens) and metrics
# --------------------------------------------------------------------------- #


@app.post("/devices", response_model=Accepted)
async def register_device(body: DeviceIn, db: Db) -> Accepted:
    row = await db.get(DeviceRow, body.user_id)
    if row is None:
        row = DeviceRow(user_id=body.user_id)
        db.add(row)
    # Send only what changed: the Live Activity token arrives later than the device token.
    if body.apns_token is not None:
        row.apns_token = body.apns_token
    if body.live_activity_token is not None:
        row.live_activity_token = body.live_activity_token
    await audit.append(
        db, actor="user", action="device.register", subject=body.user_id, reason="push tokens"
    )
    await db.commit()
    return Accepted()


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """The latest eval report (evals/run.py) for the dashboard."""
    reports = sorted(EVAL_RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        raise HTTPException(404, "no eval results yet: run `uv run python -m evals.run`")
    return json.loads(reports[-1].read_text())


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard/")


if DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


_bg: set[asyncio.Task[Any]] = set()


def _background(coro: Any) -> None:
    """Run a coroutine past the request; keep a reference so it isn't collected."""
    task = asyncio.ensure_future(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)
