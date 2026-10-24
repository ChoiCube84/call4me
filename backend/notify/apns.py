"""APNs push and Live Activity updates.

Alerts are always visual (banner) plus haptic on the Watch; we never rely on
sound (CLAUDE.md rule 7). The real APNs sender is filled in at build step 13;
until then `MockNotifier` records what would have been sent.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.audit.log import redact
from backend.config import get_settings
from backend.contracts import CallState, Notifier, UserRequest

log = logging.getLogger(__name__)

ASK_USER_CATEGORY = "ASK_USER"  # iOS category with a text-input action


class MockNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def alert(
        self,
        user_id: str,
        title: str,
        body: str,
        *,
        category: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        msg = {"user_id": user_id, "title": title, "body": body, "category": category, "data": data}
        self.sent.append(msg)
        log.info("mock push %s", redact(msg))

    async def ask_user(self, user_id: str, request: UserRequest) -> None:
        await self.alert(
            user_id,
            "Needs your input",
            request.question,
            category=ASK_USER_CATEGORY,
            data={"request_id": request.id, "session_id": request.session_id},
        )

    async def update_live_activity(
        self, user_id: str, state: CallState, data: dict[str, Any] | None = None
    ) -> None:
        self.sent.append({"user_id": user_id, "live_activity": state, "data": data})


class APNsNotifier(Notifier):
    """Token-based APNs over HTTP/2. Implemented at build step 13."""

    async def alert(self, user_id: str, title: str, body: str, *, category=None, data=None):
        raise NotImplementedError

    async def ask_user(self, user_id: str, request: UserRequest) -> None:
        raise NotImplementedError

    async def update_live_activity(self, user_id: str, state: CallState, data=None) -> None:
        raise NotImplementedError


def get_notifier() -> Notifier:
    return MockNotifier() if get_settings().use_mocks else APNsNotifier()
