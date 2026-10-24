"""Telephony (owned by `telephony`): Twilio calls, `/media` stream, DTMF, takeover.

Public surface for other areas::

    from backend.telephony import get_telephony
    tel = get_telephony()            # TwilioTelephonyClient, or MockTelephonyClient if USE_MOCKS
    await tel.place_call(session)
    await tel.wait_connected(session.id)
    async for frame in tel.audio_in(session.id): ...   # 160-byte mu-law frames, 20 ms
    await tel.send_audio(session.id, frame)            # TTS frames from audio-speech
    await tel.mark(session.id, "greeting")             # returns when it has played
    await tel.press_digits(session.id, "2")
    await tel.start_takeover(session.id) / end_takeover / hang_up / clear_audio

See `base.CallControl` for the extras beyond `contracts.TelephonyClient`.
Platform wiring: `media_ws.handle` (WS /media), `twilio_client.handle_status_form`
(POST /twilio/status), `routes.router` (POST /twilio/voice), `fakeline.app.app`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.telephony.base import CallControl

_client: CallControl | None = None


def get_telephony() -> CallControl:
    """Process-wide telephony client, chosen by USE_MOCKS."""
    global _client
    if _client is None:
        # Imported lazily: the mock pulls in fakeline, which imports this package.
        from backend.config import get_settings

        if get_settings().use_mocks:
            from backend.telephony.mock_client import MockTelephonyClient

            _client = MockTelephonyClient()
        else:
            from backend.telephony.twilio_client import TwilioTelephonyClient

            _client = TwilioTelephonyClient()
    return _client


def set_telephony(client: CallControl | None) -> None:
    """Override the process-wide client (tests, evals)."""
    global _client
    _client = client
