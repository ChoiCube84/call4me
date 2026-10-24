"""X-Twilio-Signature validation for webhooks.

Algorithm (https://www.twilio.com/docs/usage/security#validating-requests):
base64(HMAC-SHA1(auth_token, full_url + concat(sorted POST key+value pairs))).

Behind ngrok / Azure ingress the URL Twilio signed is the public one, so we
rebuild it from PUBLIC_BASE_URL plus the request path and query.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping

from fastapi import HTTPException, Request

from backend.config import get_settings


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature or "")


def public_url(request: Request) -> str:
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return str(request.url)
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


async def verify_twilio_request(request: Request) -> dict[str, str]:
    """FastAPI dependency: returns the form params, or 403 on a bad signature.

    Skipped when USE_MOCKS=true or no auth token is configured (local dev).
    """
    form = {k: str(v) for k, v in (await request.form()).items()}
    settings = get_settings()
    if settings.use_mocks or not settings.twilio_auth_token:
        return form
    sig = request.headers.get("X-Twilio-Signature", "")
    if not is_valid(settings.twilio_auth_token, public_url(request), form, sig):
        raise HTTPException(403, "invalid Twilio signature")
    return form
