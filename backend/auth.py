"""Access token for the public API.

On Azure the API is on the open internet and `POST /calls` dials real phone numbers,
so every API route needs `API_TOKEN` (as `Authorization: Bearer`, or `?token=` on
WebSockets, which browsers can't give headers). Unset in dev: everything is open.

Protected: every API prefix below (tests/test_auth.py fails if a new API route is
not covered). Everything else is open: the web app's static files at `/` (they hold
no data; the app asks for the token itself), `/health`, Cartesia's callbacks (they
carry their own secret), and the phone-mode rep socket.
"""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import parse_qs

from backend.config import get_settings

ASGIApp = Any
PROTECTED_PREFIXES = (
    "/calls",
    "/requests",
    "/devices",
    "/companies",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES)


def _given_token(scope: dict[str, Any]) -> str:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip()
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return (query.get("token") or [""])[0].strip()


def token_ok(scope: dict[str, Any], expected: str) -> bool:
    given = _given_token(scope)
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        expected = get_settings().api_token
        if (
            scope["type"] not in ("http", "websocket")
            or not expected
            or not is_protected(scope["path"])
            or (scope["type"] == "http" and scope["method"] == "OPTIONS")
            or token_ok(scope, expected)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Close before accepting: the client sees a failed handshake (HTTP 403).
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"missing or wrong access token"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
