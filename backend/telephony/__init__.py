"""Telephony for our own pipeline (the backup to Cartesia calls): the browser fake
phone and the scripted mock line, both playing the fake support line.

Public surface for other areas::

    from backend.telephony import get_telephony
    tel = get_telephony()            # PhoneLineClient (TELEPHONY=phone) or MockTelephonyClient
    await tel.place_call(session)
    await tel.wait_connected(session.id)
    async for frame in tel.audio_in(session.id): ...   # 160-byte mu-law frames, 20 ms
    await tel.send_audio(session.id, frame)            # TTS frames from audio-speech
    await tel.mark(session.id, "greeting")             # returns when it has played
    await tel.press_digits(session.id, "2")
    await tel.start_takeover(session.id) / end_takeover / hang_up / clear_audio

See `base.CallControl` for the extras beyond `contracts.TelephonyClient`.
Real phone calls go through Cartesia instead (`backend/cartesia`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.telephony.base import CallControl

_client: CallControl | None = None


def get_telephony() -> CallControl:
    """Process-wide line for our own pipeline: the browser fake phone (TELEPHONY=phone)
    or the scripted mock line. Cartesia calls (the default) don't use this."""
    global _client
    if _client is None:
        # Imported lazily: the mock pulls in fakeline, which imports this package.
        from backend.config import get_settings

        if get_settings().telephony_mode == "phone":
            from backend.telephony.phone_client import PhoneLineClient

            _client = PhoneLineClient()
        else:
            from backend.telephony.mock_client import MockTelephonyClient

            _client = MockTelephonyClient()
    return _client


def set_telephony(client: CallControl | None) -> None:
    """Override the process-wide client (tests, evals)."""
    global _client
    _client = client
