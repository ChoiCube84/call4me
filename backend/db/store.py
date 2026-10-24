"""Storage boundary shared by the API, call worker, and notification adapters.

SQL remains available for existing installations and regression tests. Convex uses
the same row DTOs without opening a SQL connection. Each commit is one atomic
mutation; only changed fields are patched, with optimistic conflict checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from sqlalchemy import DateTime, select

from backend.config import get_settings
from backend.contracts import utcnow
from backend.db import session as sql
from backend.db.convex_client import get_client
from backend.db.models import (
    AuditLogRow,
    Base,
    CallEventRow,
    CallSessionRow,
    MenuPathRow,
    UserRequestRow,
)

Row = TypeVar("Row", bound=Base)


class Store(Protocol):
    async def get(self, model: type[Row], key: str) -> Row | None: ...
    def add(self, row: Base) -> None: ...
    async def flush(self) -> None: ...
    async def commit(self) -> None: ...
    async def calls(self, user_id: str | None, limit: int) -> list[CallSessionRow]: ...
    async def events(self, call_id: str) -> list[CallEventRow]: ...
    async def audit(self, call_id: str) -> list[AuditLogRow]: ...
    async def menu(self, company: str) -> list[str] | None: ...
    async def save_menu(self, company: str, steps: list[str]) -> None: ...


class SqlStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def get(self, model: type[Row], key: str) -> Row | None:
        return await self.db.get(model, key)

    def add(self, row: Base) -> None:
        self.db.add(row)

    async def flush(self) -> None:
        await self.db.flush()

    async def commit(self) -> None:
        await self.db.commit()

    async def calls(self, user_id: str | None, limit: int) -> list[CallSessionRow]:
        q = select(CallSessionRow).order_by(CallSessionRow.started_at.desc()).limit(limit)
        if user_id:
            q = q.where(CallSessionRow.user_id == user_id)
        return list((await self.db.scalars(q)).all())

    async def events(self, call_id: str) -> list[CallEventRow]:
        q = select(CallEventRow).where(CallEventRow.session_id == call_id).order_by(CallEventRow.at)
        return list((await self.db.scalars(q)).all())

    async def audit(self, call_id: str) -> list[AuditLogRow]:
        requests = select(UserRequestRow.id).where(UserRequestRow.session_id == call_id)
        q = (
            select(AuditLogRow)
            .where((AuditLogRow.subject == call_id) | AuditLogRow.subject.in_(requests))
            .order_by(AuditLogRow.at)
        )
        return list((await self.db.scalars(q)).all())

    async def menu(self, company: str) -> list[str] | None:
        row = await self.db.scalar(
            select(MenuPathRow)
            .where(MenuPathRow.company == company)
            .order_by(MenuPathRow.success_count.desc(), MenuPathRow.last_used_at.desc())
            .limit(1)
        )
        return list(row.steps_json) if row else None

    async def save_menu(self, company: str, steps: list[str]) -> None:
        rows = (
            await self.db.scalars(select(MenuPathRow).where(MenuPathRow.company == company))
        ).all()
        row = next((r for r in rows if list(r.steps_json) == steps), None)
        if row is None:
            row = MenuPathRow(company=company, steps_json=list(steps), success_count=0)
            self.db.add(row)
        row.success_count = (row.success_count or 0) + 1
        row.last_used_at = utcnow()
        await self.db.commit()


def serialize(row: Base) -> dict[str, Any]:
    values = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, datetime):
            # SQL's SQLite fallback returns naive UTC datetimes.
            value = value.replace(tzinfo=value.tzinfo or UTC).isoformat()
        values[col.name] = deepcopy(value)
    return values


def deserialize(model: type[Row], data: dict[str, Any]) -> Row:
    values = {col.name: data[col.name] for col in model.__table__.columns}
    for col in model.__table__.columns:
        if isinstance(col.type, DateTime) and values[col.name] is not None:
            values[col.name] = datetime.fromisoformat(values[col.name])
    return model(**values)


class ConvexStore:
    def __init__(self, client: Any = None) -> None:
        self.client = client or get_client()
        self.loaded: dict[tuple[str, str], tuple[Base, dict[str, Any] | None]] = {}

    def _key(self, row: Base) -> tuple[str, str]:
        primary = next(iter(row.__table__.primary_key.columns)).name
        return row.__tablename__, getattr(row, primary)

    def add(self, row: Base) -> None:
        # SQLAlchemy normally applies these defaults at flush; Convex has no SQL flush.
        for col in row.__table__.columns:
            if getattr(row, col.name) is None and col.default is not None:
                default = col.default
                value = default.arg(None) if default.is_callable else default.arg
                setattr(row, col.name, value)
        self.loaded[self._key(row)] = (row, None)

    async def get(self, model: type[Row], key: str) -> Row | None:
        identity = (model.__tablename__, key)
        if identity in self.loaded:
            return self.loaded[identity][0]  # type: ignore[return-value]
        data = await self.client.call("query", "storage:get", table=identity[0], key=key)
        if data is None:
            return None
        row = deserialize(model, data)
        self.loaded[identity] = (row, serialize(row))
        return row

    async def flush(self) -> None:
        # Adds already have IDs/defaults. Persist only on commit, atomically.
        pass

    async def commit(self) -> None:
        writes = [
            {"table": table, "value": serialize(row), "expected": before}
            for (table, _), (row, before) in self.loaded.items()
            if before is None or serialize(row) != before
        ]
        if writes:
            await self.client.call("mutation", "storage:commit", writes=writes)
            self.loaded = {key: (row, serialize(row)) for key, (row, _) in self.loaded.items()}

    async def calls(self, user_id: str | None, limit: int) -> list[CallSessionRow]:
        rows = await self.client.call("query", "storage:listCalls", user_id=user_id, limit=limit)
        return [deserialize(CallSessionRow, row) for row in rows]

    async def events(self, call_id: str) -> list[CallEventRow]:
        rows = await self.client.call("query", "storage:events", call_id=call_id)
        return [deserialize(CallEventRow, row) for row in rows]

    async def audit(self, call_id: str) -> list[AuditLogRow]:
        rows = await self.client.call("query", "storage:audit", call_id=call_id)
        return [deserialize(AuditLogRow, row) for row in rows]

    async def menu(self, company: str) -> list[str] | None:
        return await self.client.call("query", "storage:menu", company=company)

    async def save_menu(self, company: str, steps: list[str]) -> None:
        await self.client.call("mutation", "storage:saveMenu", company=company, steps=steps)


@asynccontextmanager
async def open_store() -> AsyncIterator[Store]:
    if get_settings().storage_backend == "convex":
        yield ConvexStore()
    else:
        async with sql.sessionmaker()() as db:
            yield SqlStore(db)


async def get_store() -> AsyncIterator[Store]:
    async with open_store() as db:
        yield db


async def init_store() -> None:
    if get_settings().storage_backend == "convex":
        settings = get_settings()
        if not settings.api_token or settings.api_token == settings.convex_backend_token:
            raise ValueError("Convex requires a separate, nonempty API_TOKEN for viewers")
        await get_client().call("query", "storage:ready")
    else:
        await sql.init_db()
