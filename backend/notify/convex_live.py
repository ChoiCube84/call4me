"""Ordered, bounded, redacted delivery to Convex's reactive replay cache."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.audit.log import redact
from backend.contracts import LiveMessage, new_id
from backend.db.convex_client import get_client

log = logging.getLogger(__name__)


class ConvexLiveWriter:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def publish(self, message: LiveMessage) -> None:
        payload = {"id": new_id(), **message.model_dump(mode="json")}
        payload["data"] = redact(payload["data"])
        if self.queue.full():
            log.error("Convex replay queue full; dropping oldest replay message")
            self.queue.get_nowait()
            self.queue.task_done()
        self.queue.put_nowait(payload)

    async def _run(self) -> None:
        while True:
            batch = [await self.queue.get()]
            while len(batch) < 50 and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                # Stable IDs make retry after an unknown HTTP outcome idempotent.
                for attempt in range(4):
                    try:
                        await get_client().call("mutation", "live:publish", messages=redact(batch))
                        break
                    except Exception:
                        if attempt == 3:
                            log.error(
                                "Convex replay delivery failed; local WebSocket replay retained"
                            )
                        else:
                            await asyncio.sleep(0.5 * 2**attempt)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def close(self) -> None:
        if self.task is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=10)
        except TimeoutError:
            log.error("Convex replay queue did not drain before shutdown")
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None
