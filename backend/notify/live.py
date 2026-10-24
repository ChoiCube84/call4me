"""In-process channels between the backend and the user.

- `LiveHub`: fan-out of `LiveMessage`s to `WS /calls/{id}/live` subscribers.
- `AnswerBroker`: hands an `ask_user` answer from `POST /requests/{id}/answer`
  to whoever is awaiting it (the conversation agent). The answer is passed in
  memory only and never stored.

Single-replica for the hackathon; swap for Redis pub/sub if we scale out.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from backend.audit.log import redact
from backend.contracts import LiveMessage


class LiveHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[LiveMessage]]] = defaultdict(set)

    def subscribe(self, session_id: str) -> asyncio.Queue[LiveMessage]:
        q: asyncio.Queue[LiveMessage] = asyncio.Queue(maxsize=500)
        self._subs[session_id].add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[LiveMessage]) -> None:
        self._subs[session_id].discard(q)
        if not self._subs[session_id]:
            del self._subs[session_id]

    def publish(self, msg: LiveMessage) -> None:
        msg = msg.model_copy(update={"data": redact(msg.data)})
        for q in list(self._subs.get(msg.session_id, ())):
            if q.full():  # slow client: drop its oldest message rather than block the call
                q.get_nowait()
            q.put_nowait(msg)


class AnswerBroker:
    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Future[str]] = {}

    def expect(self, request_id: str) -> asyncio.Future[str]:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = fut
        return fut

    async def wait(self, request_id: str, timeout: float) -> str | None:  # noqa: ASYNC109
        fut = self._waiting.get(request_id) or self.expect(request_id)
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiting.pop(request_id, None)

    def deliver(self, request_id: str, answer: str) -> bool:
        fut = self._waiting.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True


hub = LiveHub()
answers = AnswerBroker()
