"""Telephony webhooks that platform mounts with ``app.include_router(router)``.

- ``POST /twilio/voice``: set as the agent number's "A call comes in" webhook.
  Answers calls to our agent number (company callbacks) by opening a Media
  Stream for the session that asked for a callback (`expect_callback`), or a
  fresh ``inbound-<CallSid>`` session. Each answered call is announced on
  `streams.registry.incoming` for call-engine.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.config import get_settings
from backend.telephony.base import IncomingCall
from backend.telephony.signature import verify_twilio_request
from backend.telephony.streams import registry
from backend.telephony.twilio_client import media_url
from backend.telephony.twiml import Response as TwiML

router = APIRouter()

TwilioForm = Annotated[dict[str, str], Depends(verify_twilio_request)]


@router.post("/twilio/voice")
async def incoming_call(form: TwilioForm) -> Response:
    call_sid = form.get("CallSid", "")
    session_id = registry.claim_callback()
    matched = session_id is not None
    session_id = session_id or f"inbound-{call_sid}"
    registry.reopen(session_id)
    registry.bind_call(session_id, call_sid)
    registry.incoming.put_nowait(
        IncomingCall(
            session_id=session_id,
            call_sid=call_sid,
            from_number=form.get("From", ""),
            matched_callback=matched,
        )
    )
    base = get_settings().public_base_url
    t = TwiML().connect_stream(media_url(base), {"session_id": session_id, "inbound": "1"})
    return Response(content=t.to_xml(), media_type="application/xml")
