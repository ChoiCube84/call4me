"""SQLAlchemy models (CLAUDE.md section 9).

`user_request` deliberately has no answer column: answers are never stored.
`audit_log` is append-only; updates and deletes are rejected in the ORM and,
on PostgreSQL, by a trigger installed in `backend.db.session.init_db`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.contracts import new_id, utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class CallSessionRow(Base):
    __tablename__ = "call_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    company: Mapped[str] = mapped_column(String(200))
    phone_number: Mapped[str] = mapped_column(String(32))
    goal: Mapped[str] = mapped_column(Text)
    limits_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    disclosure_text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)
    reference_number: Mapped[str | None] = mapped_column(String(100))
    minutes_saved: Mapped[float | None] = mapped_column(Float)


class CallEventRow(Base):
    __tablename__ = "call_event"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("call_session.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    detail_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class MenuPathRow(Base):
    __tablename__ = "menu_path"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    company: Mapped[str] = mapped_column(String(200), index=True)
    steps_json: Mapped[list[Any]] = mapped_column(default=list)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRequestRow(Base):
    __tablename__ = "user_request"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("call_session.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    sensitivity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str | None] = mapped_column(String(128), index=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    diff_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class DeviceRow(Base):
    __tablename__ = "device"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    apns_token: Mapped[str | None] = mapped_column(String(200))
    live_activity_token: Mapped[str | None] = mapped_column(String(200))


class AppendOnlyError(RuntimeError):
    pass


@event.listens_for(AuditLogRow, "before_update")
def _block_audit_update(*_: Any) -> None:
    raise AppendOnlyError("audit_log is append-only; write a new event instead")


@event.listens_for(AuditLogRow, "before_delete")
def _block_audit_delete(*_: Any) -> None:
    raise AppendOnlyError("audit_log is append-only; write a new event instead")
