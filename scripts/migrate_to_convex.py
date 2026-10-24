"""Copy SQL rows to Convex, preserving external IDs. Dry-run unless --apply.

Stop the call worker before migrating. Set SOURCE_DATABASE_URL and the target
CONVEX_URL/CONVEX_BACKEND_TOKEN in the environment or .env. The source is only
read; exact target rows are skipped, conflicting target rows abort the copy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.db.convex_client import ConvexClient
from backend.db.models import (
    AuditLogRow,
    CallEventRow,
    CallSessionRow,
    DeviceRow,
    MenuPathRow,
    UserRequestRow,
)
from backend.db.store import serialize

MODELS = (CallSessionRow, UserRequestRow, CallEventRow, AuditLogRow, MenuPathRow, DeviceRow)


async def migrate(*, apply: bool) -> dict[str, int]:
    load_dotenv()
    source_url = os.environ.get("SOURCE_DATABASE_URL", "")
    if not source_url:
        raise ValueError("Set SOURCE_DATABASE_URL; never infer the migration source")
    engine = create_async_engine(source_url)
    target = (
        ConvexClient(os.environ.get("CONVEX_URL", ""), os.environ.get("CONVEX_BACKEND_TOKEN", ""))
        if apply
        else None
    )
    counts: dict[str, int] = {}
    try:
        async with async_sessionmaker(engine)() as source, source.begin():
            if engine.dialect.name == "postgresql":
                await source.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            elif engine.dialect.name == "sqlite":
                await source.execute(text("PRAGMA query_only = ON"))
            else:
                raise ValueError("Supported migration sources: PostgreSQL and SQLite")
            for model in MODELS:
                rows = await source.stream_scalars(select(model).execution_options(yield_per=50))
                counts[model.__tablename__] = 0
                async for batch in rows.partitions(50):
                    if target:
                        await target.call(
                            "mutation",
                            "storage:commit",
                            writes=[
                                {
                                    "table": model.__tablename__,
                                    "value": serialize(row),
                                    "expected": None,
                                }
                                for row in batch
                            ],
                        )
                    counts[model.__tablename__] += len(batch)
    finally:
        await engine.dispose()
        if target:
            await target.close()
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    counts = asyncio.run(migrate(apply=args.apply))
    print(json.dumps({"applied": args.apply, "rows": counts}))
