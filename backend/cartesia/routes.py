"""Endpoints Cartesia calls during a call: the event webhook and our webhook tools.

Both check the shared secret (`setup.shared_secret`): the webhook sends it in
`X-Webhook-Secret`, tools as a bearer token.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from backend.cartesia.call import CartesiaCall, by_call_id
from backend.cartesia.setup import shared_secret
from backend.config import get_settings

log = logging.getLogger("call4me.cartesia.routes")

router = APIRouter(prefix="/cartesia", include_in_schema=False)


def _check(given: str | None) -> None:
    expected = shared_secret(get_settings())
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "bad secret")


@router.post("/webhook")
async def webhook(
    request: Request, x_webhook_secret: str | None = Header(default=None)
) -> dict[str, bool]:
    _check(x_webhook_secret)
    event: dict[str, Any] = await request.json()
    call = by_call_id.get(str(event.get("call_id") or ""))
    if call is None:
        # Not ours (or already wrapped up). 2xx so Cartesia doesn't retry.
        return {"ok": False}
    await call.on_event(event)
    return {"ok": True}


@router.post("/tools/{name}", response_class=PlainTextResponse)
async def tool(
    name: str, request: Request, authorization: str | None = Header(default=None)
) -> str:
    _check((authorization or "").removeprefix("Bearer ").strip())
    args: dict[str, Any] = await request.json()
    from backend import calls

    call = calls.registry.get(str(args.get("session_id") or ""))
    if not isinstance(call, CartesiaCall):
        return "This call is no longer active. Wrap up politely."
    return await call.tool(name, args)
