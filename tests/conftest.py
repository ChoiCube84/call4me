from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from backend.audit import log as audit
from backend.db import session as db_session


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[None]:
    db_session.configure(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db_session.init_db()
    yield
    await db_session.get_engine().dispose()


@pytest.fixture
async def client(db) -> AsyncIterator[AsyncClient]:
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_secrets() -> None:
    audit._secrets.clear()
