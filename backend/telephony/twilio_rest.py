"""Minimal async Twilio REST client (Calls resource only), on httpx.

We avoid the Twilio Python SDK because it is synchronous; these are the only
three calls we need. API: https://www.twilio.com/docs/voice/api/call-resource
"""

from __future__ import annotations

import logging

import httpx

from backend.telephony.base import TelephonyError

log = logging.getLogger("call4me.telephony")

API_BASE = "https://api.twilio.com/2010-04-01"
STATUS_EVENTS = ("initiated", "ringing", "answered", "completed")


class TwilioRest:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not account_sid or not auth_token:
            raise TelephonyError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
        self._calls_url = f"{base_url}/Accounts/{account_sid}/Calls"
        self._http = httpx.AsyncClient(
            auth=(account_sid, auth_token), timeout=10.0, transport=transport
        )

    async def _post(self, url: str, data: list[tuple[str, str]]) -> dict:
        form: dict[str, list[str]] = {}
        for k, v in data:  # repeated keys (StatusCallbackEvent) become lists
            form.setdefault(k, []).append(v)
        try:
            r = await self._http.post(url, data=form)
        except httpx.HTTPError as e:
            raise TelephonyError(f"Twilio request failed: {e}") from e
        if r.status_code >= 400:
            # Twilio error bodies carry code/message, never secrets.
            try:
                body = r.json()
                detail = f"{body.get('code')}: {body.get('message')}"
            except ValueError:
                detail = r.text[:200]
            raise TelephonyError(f"Twilio {r.status_code} {detail}")
        return r.json()

    async def create_call(
        self, *, to: str, from_: str, twiml: str, status_callback: str | None = None
    ) -> str:
        data = [("To", to), ("From", from_), ("Twiml", twiml)]
        if status_callback:
            data += [("StatusCallback", status_callback), ("StatusCallbackMethod", "POST")]
            data += [("StatusCallbackEvent", e) for e in STATUS_EVENTS]
        body = await self._post(f"{self._calls_url}.json", data)
        return body["sid"]

    async def update_call(
        self, call_sid: str, *, twiml: str | None = None, status: str | None = None
    ) -> None:
        data: list[tuple[str, str]] = []
        if twiml is not None:
            data.append(("Twiml", twiml))
        if status is not None:
            data.append(("Status", status))  # "completed" hangs up
        await self._post(f"{self._calls_url}/{call_sid}.json", data)

    async def aclose(self) -> None:
        await self._http.aclose()
