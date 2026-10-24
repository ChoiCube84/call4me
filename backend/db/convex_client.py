"""Async Convex function client. Never expose the worker credential to browsers."""

from __future__ import annotations

from typing import Any

import httpx

from backend.config import get_settings


class StorageConflict(RuntimeError):
    """The document changed since it was read; the transaction was not applied."""


class ConvexClient:
    def __init__(self, url: str, token: str, *, transport: Any = None) -> None:
        if not url or not token:
            raise ValueError("Convex requires CONVEX_URL and CONVEX_BACKEND_TOKEN")
        self.token = token
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=15, transport=transport)

    async def call(self, kind: str, path: str, **args: Any) -> Any:
        response = await self.http.post(
            f"/api/{kind}",
            json={"path": path, "args": {"token": self.token, **args}, "format": "json"},
        )
        # Do not include response bodies, which may contain submitted arguments.
        if response.status_code != 200:
            raise RuntimeError(f"Convex {kind} failed (HTTP {response.status_code})")
        result = response.json()
        if result.get("status") != "success":
            if result.get("errorData") == "CONFLICT":
                raise StorageConflict("Record changed; retry the operation")
            raise RuntimeError(f"Convex {kind} {path} failed")
        return result.get("value")

    async def close(self) -> None:
        await self.http.aclose()


_client: ConvexClient | None = None


def get_client() -> ConvexClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = ConvexClient(settings.convex_url, settings.convex_backend_token)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
